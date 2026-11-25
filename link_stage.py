"""Optional strict Android shared library link. Caller holds the output lock."""
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

from cache_compat import _replace_with_retry, atomic_json, digest, is_elf_object


def validate_config(c):
    if not c.get('link'):
        return
    name = c.get('link_name', 'liboutput.so')
    if not re.fullmatch(r'lib[A-Za-z0-9_.-]+\.so', name):
        raise ValueError('Link name must be a simple libNAME.so filename.')
    if type(c.get('freestanding', False)) is not bool:
        raise ValueError('Invalid freestanding setting.')
    for s in c.get('link_system_libs', []):
        if not re.fullmatch(r'[A-Za-z0-9_+.-]+', s) or s.startswith('-'):
            raise ValueError('Invalid system library name.')
    for s in c.get('link_libraries', []):
        p = Path(s)
        if not p.is_file() or p.suffix not in ('.a', '.so'):
            raise ValueError('Missing or invalid link library: ' + s)


def link(c, out, compiler, flags, entries, registry=None):
    validate_config(c)
    target = out / c.get('link_name', 'liboutput.so')
    machine = 62 if any(f.startswith('--target=x86_64-') for f in flags) else 183
    for e in entries:
        p = out / e['object']
        stamp = json.loads(p.with_suffix('.json').read_text(encoding='utf-8'))
        if (
            not is_elf_object(p, machine)
            or digest(p) != e['object_sha256']
            or stamp.get('key') != e['key']
            or stamp.get('object_sha256') != e['object_sha256']
        ):
            raise ValueError('Object changed before link: ' + p.name)
    fd, name = tempfile.mkstemp(dir=out, suffix='.tmp.so')
    os.close(fd)
    tmp = Path(name)
    args = [
        str(compiler),
        *[f for f in flags if f.startswith(('--target=', '--sysroot=', '-stdlib='))],
        '-shared',
        '-Wl,--no-undefined',
        '-Wl,-soname,' + target.name,
    ]
    if c.get('freestanding'):
        args.append('-nostdlib')
    args += [
        '@' + str(out / 'objects.rsp'),
        *[str(Path(s).resolve()) for s in c.get('link_libraries', [])],
        *['-l' + s for s in c.get('link_system_libs', [])],
        '-o',
        str(tmp),
    ]
    creationflags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
    try:
        with (out / 'link.log').open('wb') as log:
            with subprocess.Popen(
                args, stdout=log, stderr=subprocess.STDOUT, creationflags=creationflags
            ) as p:
                if registry:
                    registry.add(p, 'Shared library linker')
                try:
                    code = p.wait()
                finally:
                    if registry:
                        registry.remove(p)
        with tmp.open('rb') as f:
            b = f.read(64)
        valid = (
            len(b) == 64
            and b[:6] == b'\x7fELF\x02\x01'
            and int.from_bytes(b[16:18], 'little') == 3
            and int.from_bytes(b[18:20], 'little') == machine
        )
        ok = code == 0 and valid
        if ok:
            _replace_with_retry(tmp, target)
        atomic_json(
            out / 'link_status.json',
            {
                'success': ok,
                'exit_code': code,
                'artifact': target.name if ok else None,
                'sha256': digest(target) if ok else None,
                'device_execution_verified': False,
            },
        )
        return ok
    finally:
        tmp.unlink(missing_ok=True)
