"""Live, per-session scheduler policy. Resource target is not a CPU quota."""
from __future__ import annotations

import ctypes
import json
import math
import os
import platform
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from cache_compat import GIB, total_ram

_WIN_PRIORITY_CLASSES = {
    'idle': 0x00000040,
    'below-normal': 0x00004000,
    'normal': 0x00000020,
}


@dataclass(frozen=True)
class Budget:
    percent: int = 100

    def __post_init__(self):
        if type(self.percent) is not int or not 0 <= self.percent <= 100:
            raise ValueError('Budget must be an integer from 0 to 100')

    def workers(self, ceiling):
        return math.ceil(max(1, ceiling) * self.percent / 100) if self.percent else 0

    def reserve(self, base, total):
        # More headroom in low-impact modes. No hard allocation or cap.
        return (
            max(base, min(total / GIB * 0.5, base + (100 - self.percent) / 100 * total / GIB * 0.25))
            if total
            else base
        )

    @property
    def priority(self):
        return 'idle' if self.percent <= 25 else 'below-normal' if self.percent < 75 else 'normal'


class LiveControl:
    def __init__(self, path=None, initial=100):
        self.path = Path(path) if path else None
        self.budget = Budget(initial)

    def read(self):
        if self.path:
            try:
                data = json.loads(self.path.read_text(encoding='utf-8'))
                self.budget = Budget(data['percent'])
            except (OSError, ValueError, TypeError, KeyError):
                pass  # atomic writer; preserve last valid setting
        return self.budget


def _set_win_priority_stdlib(pid: int, priority_class: int) -> bool:
    """Set Windows priority class for a single owned PID via kernel32."""
    PROCESS_SET_INFORMATION = 0x0200
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(PROCESS_SET_INFORMATION | PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return False
    try:
        return bool(kernel32.SetPriorityClass(handle, int(priority_class)))
    finally:
        kernel32.CloseHandle(handle)


class ProcessRegistry:
    """Track only children launched by this engine. Never manipulate arbitrary PIDs."""

    def __init__(self):
        self.lock = threading.Lock()
        self.children = {}
        self.priority = 'normal'
        self.cancelled = False
        self.cancelled_labels = set()

    def launch(self, label, args, **kwargs):
        """Serialize child creation with cancellation; never spawn after cancel."""
        with self.lock:
            if self.cancelled or label in self.cancelled_labels:
                return None
            p = subprocess.Popen(args, **kwargs)
            self.children[p] = (label, time.monotonic())
            return p

    def add(self, p, label):
        with self.lock:
            self.children[p] = (label, time.monotonic())

    def remove(self, p):
        with self.lock:
            self.children.pop(p, None)

    def snapshot(self):
        with self.lock:
            items = list(self.children.items())
        rows = []
        for p, (name, t) in items:
            if p.poll() is not None:
                continue
            row = {
                'name': name,
                'pid': p.pid,
                'seconds': round(time.monotonic() - t),
                'cpu_seconds': None,
                'rss_mib': None,
            }
            try:
                import psutil

                proc = psutil.Process(p.pid)
                tree = [proc, *proc.children(recursive=True)]
                cpu = 0.0
                rss = 0
                measured = 0
                for child in tree:
                    try:
                        times = child.cpu_times()
                        cpu += times.user + times.system
                        rss += child.memory_info().rss
                        measured += 1
                    except (psutil.Error, OSError):
                        pass
                if measured and p.poll() is None:
                    row.update(cpu_seconds=round(cpu, 1), rss_mib=round(rss / 1024 ** 2))
            except (ImportError, OSError, RuntimeError):
                pass
            except Exception:
                pass  # optional telemetry never stops a build
            rows.append(row)
        return rows

    def apply(self, priority):
        # Windows supports reversible priority classes. On other systems, scheduling
        # remains functional without priority changes.
        if os.name != 'nt':
            return 'scheduler only'
        with self.lock:
            procs = [p for p in self.children if p.poll() is None]
        try:
            import psutil
        except ImportError:
            target_cls = _WIN_PRIORITY_CLASSES[priority]
            failures = 0
            for p in procs:
                if p.poll() is not None:
                    continue
                if not _set_win_priority_stdlib(p.pid, target_cls):
                    failures += 1
            return priority + (' (root only, partial)' if failures else ' (root only)')

        levels = {
            'idle': psutil.IDLE_PRIORITY_CLASS,
            'below-normal': psutil.BELOW_NORMAL_PRIORITY_CLASS,
            'normal': psutil.NORMAL_PRIORITY_CLASS,
        }
        failures = 0
        for p in procs:
            if p.poll() is not None:
                continue
            try:
                proc = psutil.Process(p.pid)
                if p.poll() is not None:
                    continue
                for child in [proc, *proc.children(recursive=True)]:
                    try:
                        if child.nice() != levels[priority]:
                            child.nice(levels[priority])
                    except (psutil.Error, OSError):
                        failures += 1
            except (psutil.Error, OSError):
                failures += 1
        return priority + (' (partial)' if failures else '')

    def terminate_all(self) -> int:
        """Safely terminate only active child process trees owned by this registry."""
        with self.lock:
            self.cancelled = True
            procs = [p for p in self.children if p.poll() is None]
        return self._terminate(procs)

    def cancel_labels(self, labels):
        """Latch only the named units; never cancel another registered worker."""
        labels = set(labels)
        with self.lock:
            self.cancelled_labels.update(labels)
            procs = [p for p, (label, _) in self.children.items()
                     if label in labels and p.poll() is None]
        return self._terminate(procs)

    def _terminate(self, procs):
        terminated = 0
        for p in procs:
            if p.poll() is not None:
                continue
            done = False
            try:
                import psutil

                proc = psutil.Process(p.pid)
                children = proc.children(recursive=True)
                for child in children:
                    try:
                        child.terminate()
                    except (psutil.Error, OSError):
                        pass
                try:
                    proc.terminate()
                except (psutil.Error, OSError):
                    pass
                _, alive = psutil.wait_procs([proc, *children], timeout=1.5)
                for rem in alive:
                    try:
                        rem.kill()
                    except (psutil.Error, OSError):
                        pass
                done = True
            except (ImportError, OSError, RuntimeError):
                pass
            except Exception:
                pass
            if not done and p.poll() is None:
                if os.name == 'nt':
                    try:
                        subprocess.run(
                            ['taskkill', '/T', '/F', '/PID', str(p.pid)],
                            capture_output=True,
                            timeout=10,
                            creationflags=subprocess.CREATE_NO_WINDOW,
                        )
                    except (OSError, subprocess.SubprocessError):
                        try:
                            p.kill()
                        except OSError:
                            pass
                else:
                    try:
                        p.terminate()
                        p.wait(timeout=1.5)
                    except subprocess.TimeoutExpired:
                        try:
                            p.kill()
                        except OSError:
                            pass
                    except OSError:
                        pass
            terminated += 1
        return terminated


def hardware():
    d = {
        'cpu': platform.processor() or platform.machine(),
        'architecture': platform.machine(),
        'logical': os.cpu_count() or 1,
        'physical': None,
        'total_ram': total_ram(),
        'temperature': None,
        'disk_free': None,
    }
    try:
        import psutil

        d['logical'] = (
            len(psutil.Process().cpu_affinity())
            if hasattr(psutil.Process(), 'cpu_affinity')
            else d['logical']
        )
        d['physical'] = psutil.cpu_count(logical=False)
        d['total_ram'] = psutil.virtual_memory().total
        if hasattr(psutil, 'sensors_temperatures'):
            temps = psutil.sensors_temperatures()
            readings = [x.current for values in temps.values() for x in values if x.current is not None]
            if readings:
                d['temperature'] = max(readings)
    except (ImportError, OSError, RuntimeError, AttributeError):
        pass
    return d
