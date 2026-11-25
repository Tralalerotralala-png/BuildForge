"""Optional strict Android shared library link. Caller holds the output lock."""
import json
import os
import re
import subprocess
import tempfile
import time
import sys
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


def probe_toolchain(c):
    """Test driver/linker compatibility using only disposable synthetic C code."""
    arch = c.get('arch', 'aarch64')
    api = int(c.get('api', 21))
    if arch not in ('aarch64', 'x86_64') or not 21 <= api <= 35:
        raise ValueError('Unsupported architecture or Android API.')
    raw = c.get('ndk', '')
    if not raw:
        raise ValueError('Select an NDK first.')
    host = ('windows-x86_64' if os.name == 'nt' else
            'linux-x86_64' if sys.platform.startswith('linux') else 'darwin-x86_64')
    tool = Path(raw).resolve() / 'toolchains' / 'llvm' / 'prebuilt' / host
    compiler = tool / 'bin' / ('clang++.exe' if os.name == 'nt' else 'clang++')
    sysroot = tool / 'sysroot'
    if not compiler.is_file() or not sysroot.is_dir():
        raise ValueError('NDK compiler/sysroot not found.')
    creationflags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
    linker_flags, version = linker_options(c, creationflags)
    common = [str(compiler), f'--target={arch}-linux-android{api}', f'--sysroot={sysroot}']
    start = time.monotonic()
    with tempfile.TemporaryDirectory(prefix='buildforge-link-probe-') as d:
        root = Path(d)
        source, obj, shared = root / 'probe.c', root / 'probe.o', root / 'libprobe.so'
        source.write_text('int buildforge_probe(void) { return 42; }\n', encoding='utf-8')
        machine = 62 if arch == 'x86_64' else 183
        for stage, args in [
            ('compile', common + ['-x', 'c', '-fPIC', '-c', str(source), '-o', str(obj)]),
            ('link', common + linker_flags + ['-shared', '-nostdlib', '-Wl,--no-undefined',
                                              str(obj), '-o', str(shared)]),
        ]:
            try:
                result = subprocess.run(args, capture_output=True, timeout=30, creationflags=creationflags)
            except subprocess.TimeoutExpired as e:
                raise ValueError(f'Synthetic {stage} probe timed out after 30 seconds.') from e
            if result.returncode:
                diagnostic = (result.stdout + result.stderr).decode(errors='replace')[-2000:]
                raise ValueError(f'Synthetic {stage} probe failed (exit {result.returncode}): {diagnostic}')
            if stage == 'compile' and not is_elf_object(obj, machine):
                raise ValueError('Synthetic compile produced an invalid target object.')
        if not is_shared_elf(shared, machine):
            raise ValueError('Synthetic linker output is not the expected Android ELF shared library.')
    return {'success': True, 'arch': arch, 'api': api, 'linker_version': version,
            'elapsed_seconds': round(time.monotonic() - start, 3),
            'scope': 'synthetic freestanding compile/link only'}


def is_shared_elf(path, machine):
    try:
        with path.open('rb') as f:
            b = f.read(64)
        return (len(b) == 64 and b[:6] == b'\x7fELF\x02\x01'
                and int.from_bytes(b[16:18], 'little') == 3
                and int.from_bytes(b[18:20], 'little') == machine)
    except OSError:
        return False


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
    libraries = [Path(s).resolve(strict=True) for s in c.get('link_libraries', [])]
    if any(p == target.resolve() for p in libraries):
        raise ValueError('Link output cannot also be an input library.')
    library_hashes = {p: digest(p) for p in libraries}
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
        *[str(p) for p in libraries],
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
        valid = is_shared_elf(tmp, machine)
        ok = code == 0 and valid and not cancelled
        if ok:
            # Do not publish a successful link if an explicit input changed.
            for e in entries:
                p = out / e['object']
                if digest(p) != e['object_sha256']:
                    raise ValueError('Object changed during link: ' + p.name)
            for p, sha in library_hashes.items():
                if digest(p) != sha:
                    raise ValueError('External library changed during link: ' + p.name)
            _replace_with_retry(tmp, target)
        atomic_json(
            out / 'link_status.json',
            {
                'success': ok,
                'exit_code': code,
                'cancelled': cancelled,
                'elapsed_seconds': round(time.monotonic() - start, 3),
                'linker_version': linker_version,
                'explicit_library_sha256': {str(p): sha for p, sha in library_hashes.items()},
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
