"""Live, per-session scheduler policy. Resource target is not a CPU quota."""
from __future__ import annotations
import json, math, os, threading, time
from dataclasses import dataclass
from pathlib import Path
from cache_compat import GIB

@dataclass(frozen=True)
class Budget:
    percent: int = 100
    def __post_init__(self):
        if type(self.percent) is not int or not 0 <= self.percent <= 100:
            raise ValueError('Budget must be an integer from 0 to 100')
    def workers(self, ceiling):
        return math.ceil(max(1, ceiling)*self.percent/100) if self.percent else 0
    def reserve(self, base, total):
        # More headroom in low-impact modes. No hard allocation or cap.
        return max(base, min(total/GIB*.5, base + (100-self.percent)/100*total/GIB*.25)) if total else base
    @property
    def priority(self):
        return 'idle' if self.percent <= 25 else 'below-normal' if self.percent < 75 else 'normal'

class LiveControl:
    def __init__(self, path=None, initial=100):
        self.path=Path(path) if path else None
        self.budget=Budget(initial)
    def read(self):
        if self.path:
            try:
                data=json.loads(self.path.read_text(encoding='utf-8'))
                self.budget=Budget(data['percent'])
            except (OSError, ValueError, TypeError, KeyError):
                pass # atomic writer; preserve last valid setting
        return self.budget

class ProcessRegistry:
    """Track only children launched by this engine. Never manipulate arbitrary PIDs."""
    def __init__(self):
        self.lock=threading.Lock(); self.children={}; self.priority='normal'
    def add(self, p, label):
        with self.lock:self.children[p]=(label,time.monotonic())
    def remove(self,p):
        with self.lock:self.children.pop(p,None)
    def snapshot(self):
        with self.lock:
            rows=[]
            for p,(name,t) in self.children.items():
                if p.poll() is not None:continue
                row={'name':name,'pid':p.pid,'seconds':round(time.monotonic()-t),'cpu_seconds':None,'rss_mib':None}
                try:
                    import psutil
                    proc=psutil.Process(p.pid); tree=[proc,*proc.children(recursive=True)]; cpu=0.; rss=0; measured=0
                    for child in tree:
                        try:
                            times=child.cpu_times();cpu+=times.user+times.system;rss+=child.memory_info().rss;measured+=1
                        except (psutil.Error,OSError):pass
                    if measured and p.poll() is None:row.update(cpu_seconds=round(cpu,1),rss_mib=round(rss/1024**2))
                except (ImportError,OSError,RuntimeError):pass
                except Exception:pass # optional telemetry never stops a build
                rows.append(row)
            return rows
    def apply(self, priority):
        # Windows supports reversible priority classes. On other systems, or when
        # psutil isn't installed, scheduling remains functional without priority changes.
        if os.name!='nt':return 'scheduler only'
        try:import psutil
        except ImportError:return 'scheduler only (optional psutil unavailable)'
        levels={'idle':psutil.IDLE_PRIORITY_CLASS,'below-normal':psutil.BELOW_NORMAL_PRIORITY_CLASS,'normal':psutil.NORMAL_PRIORITY_CLASS}
        failures=0
        with self.lock:
            for p in self.children:
                if p.poll() is not None:continue
                try:
                    proc=psutil.Process(p.pid)
                    # The original process is still owned and running; check before changing.
                    if p.poll() is not None:continue
                    for child in [proc,*proc.children(recursive=True)]:
                        try:
                            if child.nice()!=levels[priority]:child.nice(levels[priority])
                        except (psutil.Error,OSError):failures+=1
                except (psutil.Error,OSError):failures+=1
        return priority+(' (partial)' if failures else '')

def hardware():
    import platform, shutil
    d={'cpu':platform.processor() or platform.machine(),'architecture':platform.machine(),'logical':os.cpu_count() or 1,'physical':None,'total_ram':None,'temperature':None,'disk_free':None}
    try:
        import psutil
        d['logical']=len(psutil.Process().cpu_affinity()) if hasattr(psutil.Process(),'cpu_affinity') else d['logical']
        d['physical']=psutil.cpu_count(logical=False);d['total_ram']=psutil.virtual_memory().total
        if hasattr(psutil,'sensors_temperatures'):
            temps=psutil.sensors_temperatures(); readings=[x.current for values in temps.values() for x in values if x.current is not None]
            if readings:d['temperature']=max(readings)
    except (ImportError,OSError,RuntimeError,AttributeError):pass
    return d
