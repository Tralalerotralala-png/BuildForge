#!/usr/bin/env python3
"""Local compiler cache compatibility and memory guard.

Retains the exact legacy fingerprint and object-stamp format. Python 3.9+.
"""
from __future__ import annotations

import ctypes
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

EXPECTED_UNITS = 513
GIB = 1024 ** 3
_IS_WINDOWS = os.name == 'nt'


def _replace_with_retry(src: Path, dst: Path, retries: int = 6, delay: float = 0.05) -> None:
    """Replace dst with src, retrying briefly on transient Windows file locks."""
    for attempt in range(retries):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == retries - 1 or not _IS_WINDOWS:
                raise
            time.sleep(delay * (attempt + 1))


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def private(path: Path) -> Path:
    p = path.resolve()
    if any((parent / '.git').exists() for parent in (p, *p.parents)):
        raise ValueError('Refusing object output within a Git checkout')
    return p


def units(src: Path) -> list[Path]:
    found = {p.name: p for p in src.glob('recomp*.cpp') if p.is_file()}
    expected = {f'recomp_{i:03d}.cpp' for i in range(EXPECTED_UNITS - 1)} | {'recomp_table.cpp'}
    if set(found) != expected:
        raise ValueError(
            f'Expected {EXPECTED_UNITS} translated units; missing {len(expected - set(found))}, '
            f'unexpected {len(set(found) - expected)}'
        )
    return [found[n] for n in sorted(expected)]


def header_digest(roots: list[Path], *, generic: bool = False) -> str:
    h = hashlib.sha256()
    for i, root in enumerate(roots):
        if not root.is_dir():
            continue
        # Include extensionless libc++ headers as well as .h/.hpp/.inc files.
        for p in sorted(root.rglob('*')):
            if p.is_file():
                suffixes = ('.h', '.hpp', '.hh', '.inc', '.inl')
                if generic:
                    suffixes += ('', '.hxx', '.ipp', '.tpp', '.txx', '.def', '.s', '.asm', '.ixx', '.cuh')
                if root.name not in ('include', 'include-fixed') and p.suffix.lower() not in suffixes:
                    continue
                h.update(f'{i}:{p.relative_to(root).as_posix()}\0{digest(p)}\n'.encode())
    return h.hexdigest()


def is_elf_object(path: Path, machine: int = 183) -> bool:
    try:
        with path.open('rb') as f:
            b = f.read(64)
        return (
            len(b) == 64
            and b[:6] == b'\x7fELF\x02\x01'
            and int.from_bytes(b[16:18], 'little') == 1
            and int.from_bytes(b[18:20], 'little') == machine
        )
    except OSError:
        return False


def is_aarch64_object(path: Path) -> bool:
    return is_elf_object(path, 183)


def atomic_json(path: Path, value: dict) -> None:
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent, delete=False) as f:
        tmp = Path(f.name)
        json.dump(value, f, sort_keys=True)
        f.write('\n')
    try:
        _replace_with_retry(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def parse_depfile(text: str) -> list[Path]:
    """Parse a single Clang Make dependency rule with a fixed target."""
    text = text.replace('\\\r\n', ' ').replace('\\\n', ' ').strip()
    prefix = 'buildforge_dep:'
    if not text.startswith(prefix) or '\n' in text or '\r' in text:
        raise ValueError('Invalid or unsupported compiler dependency file.')
    body = text[len(prefix):]
    tokens = []
    token = []
    i = 0
    while i < len(body):
        ch = body[i]
        if ch == '\\':
            if i + 1 == len(body):
                raise ValueError('Incomplete dependency escape.')
            nxt = body[i + 1]
            if nxt.isspace() or nxt in ('#', '\\'):
                token.append(nxt)
                i += 2
                continue
            token.append(ch)
        elif ch == '$':
            if i + 1 >= len(body) or body[i + 1] != '$':
                raise ValueError('Unsupported dependency variable.')
            token.append('$')
            i += 2
            continue
        elif ch == '#':
            raise ValueError('Unescaped dependency comment.')
        elif ch.isspace():
            if token:
                tokens.append(''.join(token))
                token = []
        else:
            token.append(ch)
        i += 1
    if token:
        tokens.append(''.join(token))
    if not tokens:
        raise ValueError('Compiler did not report dependencies.')
    return [Path(t).absolute() for t in tokens]


def dependency_snapshot(depfile: Path, src: Path) -> dict:
    paths = set(parse_depfile(depfile.read_text(encoding='utf-8')))
    if src.absolute() not in paths:
        raise ValueError('Compiler dependency list does not contain the source.')
    if any(not p.is_file() for p in paths):
        raise ValueError('Compiler dependency is missing or is not a regular file.')
    return {str(p): digest(p) for p in sorted(paths)}


def dependencies_valid(data, src: Path) -> bool:
    if not isinstance(data, dict) or not data:
        return False
    try:
        paths = {Path(p) for p in data}
        if src.absolute() not in paths:
            return False
        return all(Path(p).is_absolute() and Path(p).is_file()
                   and isinstance(h, str) and digest(Path(p)) == h
                   for p, h in data.items())
    except (OSError, ValueError, TypeError):
        return False


def run_compiler(src, args, log, registry, creationflags):
    kwargs = dict(stdout=log, stderr=subprocess.STDOUT, creationflags=creationflags)
    proc = registry.launch(src.name, args, **kwargs) if registry else subprocess.Popen(args, **kwargs)
    if proc is None:
        return None
    with proc:
        try:
            return proc.wait()
        finally:
            if registry:
                registry.remove(proc)


def scan_dependencies(src, out_dir, compiler, flags, registry, creationflags, log):
    with tempfile.TemporaryDirectory(dir=out_dir, prefix='.bf-deps-') as d:
        depfile = Path(d) / 'scan.d'
        with tempfile.TemporaryFile(mode='w+b') as diagnostics:
            code = run_compiler(src, [str(compiler), *flags, '-M', '-MF', str(depfile),
                                     '-MT', 'buildforge_dep', str(src)], diagnostics,
                                registry, creationflags)
            if code != 0:
                diagnostics.seek(0)
                log.write_bytes(diagnostics.read())
                raise ValueError('Dependency preprocessing failed or was cancelled.')
        return dependency_snapshot(depfile, src)


def compile_one(
    src: Path,
    out_dir: Path,
    compiler: Path,
    flags: list[str],
    context: str,
    reuse: bool,
    registry=None,
    *, track_dependencies=False,
) -> tuple[str, str]:
    machine = 62 if any(f.startswith('--target=x86_64-') for f in flags) else 183
    if src.suffix.lower() == '.c':
        flags = [f for f in flags if not f.startswith('-std=') and not f.startswith('-stdlib=')] + [
            '-x', 'c', '-std=c17'
        ]
        context = hashlib.sha256((context + '|C17').encode()).hexdigest()
    obj = out_dir / (src.stem + '.o')
    stamp = out_dir / (src.stem + '.json')
    log = out_dir / (src.stem + '.log')
    key = {'source_sha256': digest(src), 'context_sha256': context}
    creationflags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
    before = None
    if track_dependencies:
        try:
            before = scan_dependencies(src, out_dir, compiler, flags, registry, creationflags, log)
        except (OSError, ValueError) as e:
            with log.open('ab') as f:
                f.write(('\nDependency scan: ' + str(e)).encode('utf-8', errors='replace'))
            return src.name, 'failed'
    if reuse and obj.exists() and stamp.exists():
        try:
            s = json.loads(stamp.read_text(encoding='utf-8'))
            if isinstance(s, dict) and s.get('key') == key and is_elf_object(obj, machine) and s.get('object_sha256') == digest(obj):
                if not track_dependencies or s.get('dependencies') == before:
                    return src.name, 'cached'
        except (OSError, ValueError):
            pass
    # Keep prior object/stamp until replacement succeeds. The run removes the
    # ready manifest first, so a stale object cannot make the new run ready.
    with tempfile.NamedTemporaryFile(dir=out_dir, suffix='.tmp.o', delete=False) as f:
        tmp = Path(f.name)
    depfile = tmp.with_suffix('.d')
    try:
        with log.open('wb') as f:
            args = [str(compiler), *flags, '-c', str(src), '-o', str(tmp)]
            if track_dependencies:
                args += ['-MD', '-MF', str(depfile), '-MT', 'buildforge_dep']
            code = run_compiler(src, args, f, registry, creationflags)
        if code != 0 or not is_elf_object(tmp, machine) or digest(src) != key['source_sha256']:
            return src.name, 'failed'
        if track_dependencies:
            try:
                if dependency_snapshot(depfile, src) != before:
                    raise ValueError('Dependencies changed during compilation.')
            except (OSError, ValueError) as e:
                with log.open('ab') as f:
                    f.write(('\nDependency validation: ' + str(e)).encode('utf-8', errors='replace'))
                return src.name, 'failed'
        _replace_with_retry(tmp, obj)
        data = {'key': key, 'object_sha256': digest(obj)}
        if track_dependencies:
            data['dependencies'] = before
        atomic_json(stamp, data)
        return src.name, 'compiled'
    finally:
        tmp.unlink(missing_ok=True)
        if track_dependencies:
            depfile.unlink(missing_ok=True)


def _win_memory_status():
    class MemoryStatus(ctypes.Structure):
        _fields_ = [
            ('dwLength', ctypes.c_ulong),
            ('dwMemoryLoad', ctypes.c_ulong),
        ] + [
            (name, ctypes.c_ulonglong)
            for name in (
                'ullTotalPhys',
                'ullAvailPhys',
                'ullTotalPageFile',
                'ullAvailPageFile',
                'ullTotalVirtual',
                'ullAvailVirtual',
                'ullAvailExtendedVirtual',
            )
        ]

    m = MemoryStatus()
    m.dwLength = ctypes.sizeof(m)
    f = ctypes.windll.kernel32.GlobalMemoryStatusEx
    f.argtypes = [ctypes.POINTER(MemoryStatus)]
    f.restype = ctypes.c_int
    if not f(ctypes.byref(m)):
        raise OSError('Cannot measure physical memory')
    return m


def available_ram() -> int:
    """Physical memory available to OS, not total installed RAM."""
    if os.name == 'nt':
        return int(_win_memory_status().ullAvailPhys)
    if sys.platform.startswith('linux'):
        for line in Path('/proc/meminfo').read_text(encoding='utf-8').splitlines():
            if line.startswith('MemAvailable:'):
                return int(line.split()[1]) * 1024
    raise OSError('Automatic physical RAM measurement unavailable')


def total_ram() -> int | None:
    """Total installed physical memory in bytes, or None if unavailable."""
    try:
        if os.name == 'nt':
            return int(_win_memory_status().ullTotalPhys)
        if sys.platform.startswith('linux'):
            for line in Path('/proc/meminfo').read_text(encoding='utf-8').splitlines():
                if line.startswith('MemTotal:'):
                    return int(line.split()[1]) * 1024
    except OSError:
        return None
    return None


def can_launch(free_bytes, inflight, reserve_gib, headroom_gib):
    # Conservative: existing jobs can still grow. This is a guard, not a hard
    # per-process memory limit or a guarantee that paging cannot occur.
    return free_bytes >= (reserve_gib + headroom_gib * (inflight + 1)) * GIB
