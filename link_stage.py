"""Optional strict Android shared library link. Caller holds the output lock."""
import json
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path

from cache_compat import _replace_with_retry, atomic_json, digest, is_elf_object


def response_text(out, names):
    """Encode only absolute object paths, rejecting ambiguous response syntax."""
    paths = []
    for name in names:
        if not isinstance(name, str) or Path(name).name != name or Path(name).suffix != '.o':
            raise ValueError('Invalid object filename in response list.')
        p = (out / name).as_posix()
        if not (out / name).is_absolute() or any(ch in p for ch in ('"', chr(92))) or any(ord(ch) < 32 or ord(ch) == 127 for ch in p):
            raise ValueError('Object path cannot be represented safely in a linker response file.')
        paths.append('"' + p + '"\n')
    return ''.join(paths)


def validate_config(c):
    if not c.get('link'):
        return
    name = c.get('link_name', 'liboutput.so')
    if not re.fullmatch(r'lib[A-Za-z0-9_.-]+\.so', name):
        raise ValueError('Link name must be a simple libNAME.so filename.')
    if type(c.get('freestanding', False)) is not bool:
        raise ValueError('Invalid freestanding setting.')
    selected_linker(c)
    for s in c.get('link_system_libs', []):
        if not re.fullmatch(r'[A-Za-z0-9_+.-]+', s) or s.startswith('-'):
            raise ValueError('Invalid system library name.')
    for s in c.get('link_libraries', []):
        p = Path(s)
        if not p.is_file() or p.suffix not in ('.a', '.so'):
            raise ValueError('Missing or invalid link library: ' + s)


def selected_linker(c):
    raw = c.get('linker_path', '')
    if not isinstance(raw, str):
        raise ValueError('Linker path must be text.')
    if not raw.strip():
        return None
    p = Path(raw)
    if not p.is_absolute() or not p.is_file() or p.name.lower() not in ('ld.lld', 'ld.lld.exe'):
        raise ValueError('Select an existing absolute path to ld.lld or ld.lld.exe.')
    return p


def linker_options(c, creationflags=0):
    p = selected_linker(c)
    if p is None:
        return [], 'NDK driver default'
    version = subprocess.check_output(
        [str(p), '--version'], stderr=subprocess.STDOUT,
        timeout=15, creationflags=creationflags,
    ).decode(errors='replace').strip()
    if not re.search(r'\bLLD\b', version, re.IGNORECASE):
        raise ValueError('Selected linker did not identify itself as LLVM LLD.')
    return ['-fuse-ld=' + str(p)], version[:500]


def link(c, out, compiler, flags, entries, registry=None, notify=None):
    validate_config(c)
    target = out / c.get('link_name', 'liboutput.so')
    machine = 62 if any(f.startswith('--target=x86_64-') for f in flags) else 183
    expected_rsp = response_text(out, [e['object'] for e in entries])
    if (out / 'objects.rsp').read_text(encoding='utf-8') != expected_rsp:
        raise ValueError('Object response file differs from the validated manifest.')
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
    creationflags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
    cancel = Path(c['cancelfile']) if c.get('cancelfile') else None
    linker_flags, linker_version = linker_options(c, creationflags)
    fd, name = tempfile.mkstemp(dir=out, suffix='.tmp.so')
    os.close(fd)
    tmp = Path(name)
    response = tmp.with_suffix('.rsp')
    response_created = False
    args = [
        str(compiler),
        *[f for f in flags if f.startswith(('--target=', '--sysroot=', '-stdlib='))],
        *linker_flags,
        '-shared',
        '-Wl,--no-undefined',
        '-Wl,-soname,' + target.name,
    ]
    if c.get('freestanding'):
        args.append('-nostdlib')
    args += [
        '@' + str(response),
        *[str(Path(s).resolve()) for s in c.get('link_libraries', [])],
        *['-l' + s for s in c.get('link_system_libs', [])],
        '-o',
        str(tmp),
    ]
    start = time.monotonic()
    cancelled = bool(cancel and cancel.exists())
    try:
        if cancelled:
            atomic_json(out / 'link_status.json', {'success': False, 'cancelled': True, 'linker_version': linker_version, 'device_execution_verified': False})
            return False
        # Never give the driver the public response file after validation.
        # Use an exclusive, session-local copy generated from manifest entries.
        with response.open('xb') as f:
            response_created = True
            f.write(expected_rsp.encode('utf-8'))
        if notify:
            notify('Linker: ' + linker_version)
        with (out / 'link.log').open('wb') as log:
            with subprocess.Popen(
                args, stdout=log, stderr=subprocess.STDOUT, creationflags=creationflags
            ) as p:
                if registry:
                    registry.add(p, 'Shared library linker')
                try:
                    last = start
                    while True:
                        try:
                            code = p.wait(timeout=0.5)
                            break
                        except subprocess.TimeoutExpired:
                            if cancel and cancel.exists() and not cancelled:
                                cancelled = True
                                if notify:
                                    notify('Link cancellation requested; waiting for owned process to exit.')
                                if registry:
                                    registry.terminate_all()
                                else:
                                    p.terminate()
                            now = time.monotonic()
                            if notify and now - last >= 10:
                                notify(f'Linking active: {now - start:.1f}s elapsed (no percentage available).')
                                last = now
                    cancelled = cancelled or bool(cancel and cancel.exists())
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
        ok = code == 0 and valid and not cancelled
        if ok:
            _replace_with_retry(tmp, target)
        atomic_json(
            out / 'link_status.json',
            {
                'success': ok,
                'exit_code': code,
                'cancelled': cancelled,
                'elapsed_seconds': round(time.monotonic() - start, 3),
                'linker_version': linker_version,
                'artifact': target.name if ok else None,
                'sha256': digest(target) if ok else None,
                'device_execution_verified': False,
            },
        )
        return ok
    finally:
        try:
            if response_created:
                response.unlink(missing_ok=True)
        finally:
            tmp.unlink(missing_ok=True)
