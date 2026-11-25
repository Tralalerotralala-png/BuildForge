#!/usr/bin/env python3
"""Local compiler cache compatibility and memory guard.

Retains the exact legacy fingerprint and object-stamp format. Python 3.9+.
"""
from __future__ import annotations
import argparse
from collections import deque
import ctypes
import time
import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

EXPECTED_UNITS = 513

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
        raise ValueError(f'Expected {EXPECTED_UNITS} translated units; missing {len(expected-set(found))}, unexpected {len(set(found)-expected)}')
    return [found[n] for n in sorted(expected)]

def header_digest(roots: list[Path]) -> str:
    h = hashlib.sha256()
    for i, root in enumerate(roots):
        if not root.is_dir():
            continue
        # Include extensionless libc++ headers as well as .h/.hpp/.inc files.
        for p in sorted(root.rglob('*')):
            if p.is_file():
                if root.name not in ('include', 'include-fixed') and p.suffix.lower() not in ('.h','.hpp','.hh','.inc','.inl'):
                    continue
                h.update(f'{i}:{p.relative_to(root).as_posix()}\0{digest(p)}\n'.encode())
    return h.hexdigest()

def is_elf_object(path: Path, machine: int = 183) -> bool:
    try:
        with path.open('rb') as f: b=f.read(64)
        return len(b)==64 and b[:6]==b'\x7fELF\x02\x01' and int.from_bytes(b[16:18],'little')==1 and int.from_bytes(b[18:20],'little')==machine
    except OSError: return False

def is_aarch64_object(path: Path) -> bool:
    return is_elf_object(path,183)

def atomic_json(path: Path, value: dict) -> None:
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent, delete=False) as f:
        tmp=Path(f.name); json.dump(value,f,sort_keys=True); f.write('\n')
    try: os.replace(tmp,path)
    finally: tmp.unlink(missing_ok=True)

def compile_one(src: Path, out_dir: Path, compiler: Path, flags: list[str], context: str, reuse: bool, registry=None) -> tuple[str,str]:
    machine=62 if any(f.startswith('--target=x86_64-') for f in flags) else 183
    if src.suffix.lower()=='.c':
        flags=[f for f in flags if not f.startswith('-std=') and not f.startswith('-stdlib=')]+['-x','c','-std=c17']
        context=hashlib.sha256((context+'|C17').encode()).hexdigest()
    obj=out_dir/(src.stem+'.o'); stamp=out_dir/(src.stem+'.json'); log=out_dir/(src.stem+'.log')
    key={'source_sha256':digest(src),'context_sha256':context}
    if reuse and obj.exists() and stamp.exists():
        try:
            s=json.loads(stamp.read_text(encoding='utf-8'))
            if s.get('key')==key and is_elf_object(obj,machine) and s.get('object_sha256')==digest(obj):
                return src.name,'cached'
        except (OSError, ValueError): pass
    # Keep prior object/stamp until replacement succeeds. The run removes the
    # ready manifest first, so a stale object cannot make the new run ready.
    with tempfile.NamedTemporaryFile(dir=out_dir,suffix='.tmp.o',delete=False) as f: tmp=Path(f.name)
    try:
        with log.open('wb') as f:
            with subprocess.Popen([str(compiler),*flags,'-c',str(src),'-o',str(tmp)],stdout=f,stderr=subprocess.STDOUT) as r:
                if registry:registry.add(r,src.name)
                try:code=r.wait()
                finally:
                    if registry:registry.remove(r)
        if code or not is_elf_object(tmp,machine) or digest(src)!=key['source_sha256']:
            return src.name,'failed'
        os.replace(tmp,obj)
        atomic_json(stamp,{'key':key,'object_sha256':digest(obj)})
        return src.name,'compiled'
    finally: tmp.unlink(missing_ok=True)

GIB = 1024 ** 3


def available_ram():
    """Physical memory available to Windows, not total installed RAM."""
    if os.name == 'nt':
        class MemoryStatus(ctypes.Structure):
            _fields_ = [('dwLength', ctypes.c_ulong), ('dwMemoryLoad', ctypes.c_ulong)] + [
                (name, ctypes.c_ulonglong) for name in ('ullTotalPhys', 'ullAvailPhys',
                'ullTotalPageFile', 'ullAvailPageFile', 'ullTotalVirtual',
                'ullAvailVirtual', 'ullAvailExtendedVirtual')]
        m = MemoryStatus()
        m.dwLength = ctypes.sizeof(m)
        f = ctypes.windll.kernel32.GlobalMemoryStatusEx
        f.argtypes = [ctypes.POINTER(MemoryStatus)]
        f.restype = ctypes.c_int
        if not f(ctypes.byref(m)):
            raise OSError('Cannot measure available physical memory')
        return int(m.ullAvailPhys)
    if sys.platform.startswith('linux'):
        for line in Path('/proc/meminfo').read_text().splitlines():
            if line.startswith('MemAvailable:'):
                return int(line.split()[1]) * 1024
    raise OSError('Automatic physical RAM measurement unavailable')


def can_launch(free_bytes, inflight, reserve_gib, headroom_gib):
    # Conservative: existing jobs can still grow. This is a guard, not a hard
    # per-process memory limit or a guarantee that paging cannot occur.
    return free_bytes >= (reserve_gib + headroom_gib * (inflight + 1)) * GIB

