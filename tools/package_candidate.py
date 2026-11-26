"""Build candidate evidence from CI's explicit distribution and installer outputs."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import importlib.metadata as metadata
import json
import os
from pathlib import Path
import platform
import shutil
import sys
import zipfile


PACKAGES = ('altgraph', 'packaging', 'pefile', 'psutil', 'pyinstaller',
            'pyinstaller-hooks-contrib', 'pywin32-ctypes')


def sha256(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def build_metadata():
    return {
        'commit': os.environ.get('GITHUB_SHA', 'local-unverified'),
        'workflow_run': os.environ.get('GITHUB_RUN_ID'),
        'python': platform.python_version(),
        'platform': platform.platform(),
        'build_packages': {name: metadata.version(name) for name in PACKAGES},
        'status': 'unsigned release candidate; not a production certification',
    }


def prepare(portable):
    if not (portable / 'BuildForge.exe').is_file():
        raise ValueError('Portable executable is missing.')
    notices = portable / 'third-party-build-notices'
    notices.mkdir(exist_ok=True)
    inventory = {}
    for name in PACKAGES:
        dist = metadata.distribution(name)
        target = notices / name
        target.mkdir(exist_ok=True)
        copied = []
        for item in dist.files or ():
            if not any(part.endswith('.dist-info') for part in item.parts):
                continue
            if not item.name.upper().startswith(('LICENSE', 'COPYING', 'NOTICE')):
                continue
            source = Path(dist.locate_file(item))
            if source.is_file():
                # Preserve nested license paths without escaping the notice directory.
                relative = Path(*item.parts[item.parts.index(next(p for p in item.parts if p.endswith('.dist-info'))) + 1:])
                if '..' in relative.parts or relative.is_absolute():
                    raise ValueError('Unsafe license path.')
                dest = target / relative
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, dest)
                copied.append(relative.as_posix())
        inventory[name] = {'version': dist.version, 'metadata_notices': sorted(copied)}
    python_license = Path(sys.base_prefix) / 'LICENSE.txt'
    if python_license.is_file():
        shutil.copyfile(python_license, notices / 'Python-LICENSE.txt')
    (notices / 'inventory.json').write_text(json.dumps(inventory, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    (notices / 'README.txt').write_text(
        'Build environment package notices, not a complete frozen-runtime license audit.\n'
        'Some listed build tools may not be bundled. Missing notices require manual review.\n',
        encoding='utf-8')
    (portable / 'build-metadata.json').write_text(json.dumps(build_metadata(), indent=2, sort_keys=True) + '\n', encoding='utf-8')


def zip_portable(portable, output):
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in sorted(portable.rglob('*'), key=lambda p: p.relative_to(portable).as_posix()):
            if path.is_symlink():
                raise ValueError('Unexpected symlink in portable distribution.')
            if not path.is_file():
                continue
            member = 'BuildForge/' + path.relative_to(portable).as_posix()
            info = zipfile.ZipInfo(member, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            with path.open('rb') as source, archive.open(info, 'w', force_zip64=True) as dest:
                shutil.copyfileobj(source, dest)


def bundle(portable, installer, output, *, hash_workers=2):
    if type(hash_workers) is not int or hash_workers not in (1, 2):
        raise ValueError('Hash workers must be 1 or 2.')
    setup = sorted(installer.glob('BuildForge-*-Setup.exe'))
    if len(setup) != 1 or not (portable / 'BuildForge.exe').is_file():
        raise ValueError('Expected one installer and a portable executable.')
    if not (portable / 'build-metadata.json').is_file():
        raise ValueError('Missing prepared build metadata.')
    output.mkdir(parents=True, exist_ok=False)
    archive = output / (setup[0].name.removesuffix('-Setup.exe') + '-Windows-portable.zip')
    zip_portable(portable, archive)
    target = output / setup[0].name
    shutil.copyfile(setup[0], target)
    manifest = json.loads((portable / 'build-metadata.json').read_text(encoding='utf-8'))
    # These files have just been written into a new, exclusively owned output
    # directory. Hash each once; never reuse digests across builds. Consumers
    # must still verify the downloaded bytes independently.
    artifacts = (archive, target)
    if hash_workers == 1:
        digests = [sha256(p) for p in artifacts]
    else:
        with ThreadPoolExecutor(max_workers=hash_workers) as pool:
            digests = list(pool.map(sha256, artifacts))
    manifest['artifacts'] = {p.name: digest for p, digest in zip(artifacts, digests)}
    metadata_path = output / 'build-metadata.json'
    metadata_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    checksums = dict(manifest['artifacts'])
    checksums[metadata_path.name] = sha256(metadata_path)
    (output / 'SHA256SUMS.txt').write_text(''.join(f'{checksums[name]}  {name}\n' for name in sorted(checksums)), encoding='utf-8')


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest='command', required=True)
    prep = sub.add_parser('prepare')
    prep.add_argument('portable', type=Path)
    package = sub.add_parser('bundle')
    package.add_argument('portable', type=Path)
    package.add_argument('installer', type=Path)
    package.add_argument('output', type=Path)
    package.add_argument('--hash-workers', type=int, choices=(1, 2), default=2,
                         help='Parallel artifact hash readers (use 1 on slow disks).')
    args = parser.parse_args()
    if args.command == 'prepare':
        prepare(args.portable)
    else:
        bundle(args.portable, args.installer, args.output, hash_workers=args.hash_workers)


if __name__ == '__main__':
    main()
