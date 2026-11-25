"""Hosted Windows smoke test for the frozen engine and a real installed Android NDK."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def ndk_path():
    candidates = [Path(os.environ[k]) for k in (
        'ANDROID_NDK_HOME', 'ANDROID_NDK_ROOT', 'ANDROID_NDK_LATEST_HOME', 'NDK_HOME'
    ) if os.environ.get(k)]
    for key in ('ANDROID_HOME', 'ANDROID_SDK_ROOT'):
        if os.environ.get(key):
            sdk = Path(os.environ[key])
            if (sdk / 'ndk').is_dir():
                candidates.extend(sorted((sdk / 'ndk').iterdir(), reverse=True))
    for path in candidates:
        if (path / 'toolchains/llvm/prebuilt/windows-x86_64/bin/clang++.exe').is_file():
            return path.resolve()
    raise RuntimeError('A real Android NDK is required for the Windows release smoke test.')


def main():
    executable = Path(sys.argv[1]).resolve()
    ndk = ndk_path()
    print('NDK:', (ndk / 'source.properties').read_text(encoding='utf-8'))
    with tempfile.TemporaryDirectory(prefix='BuildForge NDK smoke ') as d:
        root = Path(d)
        src = root / 'source'
        src.mkdir()
        external = root / 'external dependency'
        external.mkdir()
        header = external / 'value.h'
        header.write_text('#define ANSWER 42\n', encoding='utf-8')
        answer = src / 'answer.cpp'
        original = '#include "../external dependency/value.h"\nextern "C" int answer(){return ANSWER;}\n'
        answer.write_text(original, encoding='utf-8')
        (src / 'helper.c').write_text('int helper(void){return 7;}\n', encoding='utf-8')
        for arch in ('aarch64', 'x86_64'):
            out = root / arch
            job = root / (arch + '.json')
            config = dict(source=str(src), output=str(out), ndk=str(ndk), includes=[],
                          workers=1, api=24, opt='1', reserve=2, headroom=.5,
                          stopfile=str(root / 'stop'), arch=arch, link=True,
                          freestanding=False, link_name='libsmoke.so', track_dependencies=True,
                          cancelfile=str(root / 'cancel'))
            job.write_text(json.dumps(config), encoding='utf-8')
            for expected in ('compiled', 'cached'):
                subprocess.run([str(executable), '--engine', '--job', str(job), '--out', str(out)],
                               check=True, timeout=240)
                status = json.loads((out / 'safe_status.json').read_text(encoding='utf-8'))
                link = json.loads((out / 'link_status.json').read_text(encoding='utf-8'))
                if status.get(expected) != 2 or not link.get('success') or not (out / 'libsmoke.so').is_file():
                    raise RuntimeError('Unexpected native NDK results: ' + str(status))
                print(arch, expected, 'OK')
            command = [str(executable), '--engine', '--job', str(job), '--out', str(out)]
            library = out / 'libsmoke.so'
            header.write_text('#define ANSWER 43\n', encoding='utf-8')
            subprocess.run(command, check=True, timeout=240)
            state = json.loads((out / 'safe_status.json').read_text(encoding='utf-8'))
            if (state.get('compiled'), state.get('cached')) != (1, 1):
                raise RuntimeError('External dependency change was not isolated: ' + str(state))
            prior = hashlib.sha256(library.read_bytes()).hexdigest()

            # Force a link failure without breaking compilation; previous binary must survive.
            answer.write_text('extern int missing(); extern "C" int answer(){return missing();}\n', encoding='utf-8')
            result = subprocess.run(command, timeout=240)
            link = json.loads((out / 'link_status.json').read_text(encoding='utf-8'))
            if result.returncode != 4 or link.get('success') or hashlib.sha256(library.read_bytes()).hexdigest() != prior:
                raise RuntimeError('Failed link did not preserve the previous library.')

            # Pre-existing cancellation token must prevent work admission and release the lock.
            answer.write_text(original, encoding='utf-8')
            cancel = root / 'cancel'
            cancel.touch()
            try:
                result = subprocess.run(command, timeout=240)
                if result.returncode != 3 or (out / '.compile.lock').exists() or hashlib.sha256(library.read_bytes()).hexdigest() != prior:
                    raise RuntimeError('Cancelled build did not preserve the previous library or clear lock.')
            finally:
                cancel.unlink()
            subprocess.run(command, check=True, timeout=240)
            header.write_text('#define ANSWER 42\n', encoding='utf-8')
            print(arch, 'dependency invalidation / failed link / cancel / resume OK')


if __name__ == '__main__':
    main()
