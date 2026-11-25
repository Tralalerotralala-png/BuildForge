"""Offline synthetic checks; no game files or network access."""
import contextlib
import hashlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import cache_compat as old
import engine
import recovery


def _fake_elf_object(machine: int = 183) -> bytes:
    buf = bytearray(64)
    buf[0:6] = b'\x7fELF\x02\x01'
    buf[16:18] = (1).to_bytes(2, 'little')
    buf[18:20] = machine.to_bytes(2, 'little')
    return bytes(buf)


class Tests(unittest.TestCase):
    def test_exact_process_selection(self):
        out = r'C:\Private Folder\objects'
        self.assertEqual(
            recovery.classify('python.exe', ['python.exe', 'compile_m2_objects.py', '--out', out], out),
            'Build controller',
        )
        self.assertEqual(
            recovery.classify('python.exe', ['python.exe', 'engine.py', '--out', out.upper()], out),
            'Build controller',
        )
        self.assertEqual(
            recovery.classify('clang++.exe', ['clang++.exe', '-o', out + r'\x.tmp.o'], out),
            'Compiler for this output',
        )
        self.assertEqual(
            recovery.classify('ld.lld.exe', ['ld.lld.exe', '-o', out + r'\libdemo.tmp.so'], out),
            'Linker for this output',
        )
        self.assertIsNone(recovery.classify('ld.lld.exe', ['ld.lld.exe', '-o', out + r'2\libdemo.tmp.so'], out))
        self.assertIsNone(recovery.classify('clang++.exe', ['clang++.exe', '-o', out + r'2\x.tmp.o'], out))
        self.assertIsNone(recovery.classify('python.exe', ['python.exe', 'other.py', '--out', out + r'2'], out))
        self.assertEqual(
            recovery.classify('python.exe', ['python.exe', 'other.py', '--out', out], out),
            'Unrecognized process using this output',
        )

    def test_recover_stale(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d)
            (out / '.compile.lock').touch()
            (out / 'x.o').write_bytes(b'keep')
            (out / 'x.json').write_text('keep', encoding='utf-8')
            with patch('recovery.time.sleep'):
                recovery.recover(out, [], scan=lambda _: ([], []), stop=lambda p: self.fail('no process should be stopped'))
            self.assertFalse((out / '.compile.lock').exists())
            self.assertEqual((out / 'x.o').read_bytes(), b'keep')
            self.assertEqual((out / 'x.json').read_text(encoding='utf-8'), 'keep')
            self.assertEqual(len(list(out.glob('.compile.lock.recovered-*'))), 1)

    def test_stop_approved_only(self):
        p = {'pid': 42, 'born': 'one', 'cmd': 'cmd', 'name': 'python.exe', 'role': 'Build controller'}
        with tempfile.TemporaryDirectory() as d:
            out = Path(d)
            (out / '.compile.lock').touch()
            calls = []
            seq = iter([([p], []), ([], []), ([], [])])
            with patch('recovery.time.sleep'):
                recovery.recover(out, [p], scan=lambda _: next(seq), stop=lambda p: calls.append(p['pid']))
            self.assertEqual(calls, [42])
            self.assertFalse((out / '.compile.lock').exists())

    def test_recovery_refuses_changed_or_inaccessible_process(self):
        p = {'pid': 42, 'born': 'one', 'cmd': 'cmd', 'name': 'python.exe', 'role': 'Build controller'}
        with tempfile.TemporaryDirectory() as d:
            out = Path(d)
            (out / '.compile.lock').touch()
            for result in [
                ([dict(p, born='two')], []),
                ([], [55]),
                ([dict(p, role='Unrecognized process using this output')], []),
            ]:
                with self.assertRaises(recovery.RecoveryError):
                    recovery.recover(out, [p], scan=lambda _: result, stop=lambda p: self.fail('must not stop'))
                self.assertTrue((out / '.compile.lock').exists())

    def test_lock_change(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d)
            lock = out / '.compile.lock'
            lock.touch()

            def scan(_):
                lock.write_bytes(b'changed')
                return [], []

            with patch('recovery.time.sleep'), self.assertRaises(recovery.RecoveryError):
                recovery.recover(out, [], scan=scan)
            self.assertEqual(lock.read_bytes(), b'changed')

    def test_atomic_json_retry_on_permission_error(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / 'status.json'
            real_replace = os.replace
            calls = {'n': 0}

            def flaky_replace(src, dst):
                calls['n'] += 1
                if calls['n'] == 1:
                    raise PermissionError('transient lock')
                return real_replace(src, dst)

            with patch('cache_compat._IS_WINDOWS', True), patch(
                'cache_compat.os.replace', side_effect=flaky_replace
            ), patch('cache_compat.time.sleep'):
                old.atomic_json(target, {'ok': True})
            self.assertEqual(calls['n'], 2)
            self.assertEqual(json.loads(target.read_text(encoding='utf-8')), {'ok': True})

    def test_find_missing_units(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            src = root / 'src'
            src.mkdir()
            out = root / 'out'
            out.mkdir()
            ndk = root / 'ndk'
            host = (
                'windows-x86_64'
                if os.name == 'nt'
                else 'linux-x86_64'
                if os.sys.platform.startswith('linux')
                else 'darwin-x86_64'
            )
            tool = ndk / 'toolchains/llvm/prebuilt' / host
            (tool / 'bin').mkdir(parents=True)
            (tool / 'sysroot/usr/include').mkdir(parents=True)
            (tool / 'bin' / ('clang++.exe' if os.name == 'nt' else 'clang++')).write_bytes(b'stub')
            for name in ('a.cpp', 'b.cpp', 'c.cpp'):
                (src / name).write_text('int f(){return 1;}', encoding='utf-8')
            (out / 'a.o').write_bytes(_fake_elf_object(183))
            (out / 'a.json').write_text('{}', encoding='utf-8')
            (out / 'b.o').write_bytes(b'corrupt')
            (out / 'b.json').write_text('{}', encoding='utf-8')
            c = dict(
                source=str(src),
                output=str(out),
                ndk=str(ndk),
                includes=[],
                gos=False,
                recursive=False,
                defines=[],
                workers=2,
                api=21,
                opt='1',
                reserve=2,
                headroom=0.5,
                arch='aarch64',
            )
            self.assertEqual(engine.find_missing_units(c), ['b.cpp', 'c.cpp'])

    @unittest.skipUnless(os.name != 'nt' and shutil.which('clang++'), 'Synthetic Linux Clang test')
    def test_actual_arm64_cache_and_graceful_stop(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            src = root / 'src'
            src.mkdir()
            inc = root / 'runtime'
            inc.mkdir()
            out = root / 'out'
            out.mkdir()
            ndk = root / 'ndk'
            tool = ndk / 'toolchains/llvm/prebuilt/linux-x86_64'
            (tool / 'bin').mkdir(parents=True)
            (tool / 'sysroot/usr/include').mkdir(parents=True)
            (tool / 'bin/clang++').symlink_to(shutil.which('clang++'))
            (src / 'a.cpp').write_text('extern "C" int a(){return 42;}')
            (src / 'b.cpp').write_text('int b(){return 7;}')
            (inc / 'helper.h').write_text('#pragma once\n')
            stop = root / 'stop'
            c = dict(
                source=str(src),
                output=str(out),
                ndk=str(ndk),
                includes=[str(inc)],
                gos=False,
                recursive=False,
                defines=[],
                workers=2,
                api=21,
                opt='1',
                reserve=2,
                headroom=0.5,
                stopfile=str(stop),
            )
            sources, output, ndk, compiler, roots, flags, *_ = engine.plan(c)
            version = subprocess.check_output([str(compiler), '--version'])
            resource = (
                Path(subprocess.check_output([str(compiler), '-print-resource-dir'], text=True).strip()) / 'include'
            )
            # Recreate the old cache fingerprint, using old helper functions.
            context = hashlib.sha256(
                json.dumps(
                    {
                        'compiler_sha256': old.digest(compiler),
                        'version': version.decode(errors='replace'),
                        'flags': flags,
                        'headers': old.header_digest(
                            [
                                src,
                                inc,
                                tool / 'sysroot/usr/include',
                                resource,
                                ndk / 'sources/cxx-stl/llvm-libc++/include',
                            ]
                        ),
                    },
                    sort_keys=True,
                ).encode()
            ).hexdigest()
            self.assertEqual(old.compile_one(src / 'a.cpp', out, compiler, flags, context, True)[1], 'compiled')
            digest = old.digest(out / 'a.o')
            mtime = (out / 'a.o').stat().st_mtime_ns

            def run():
                with patch('engine.available_ram', return_value=64 * old.GIB), contextlib.redirect_stdout(io.StringIO()):
                    return engine.run(c, str(out))

            self.assertEqual(run(), 0)
            state = json.loads((out / 'safe_status.json').read_text())
            self.assertEqual((state['cached'], state['compiled']), (1, 1))
            self.assertEqual(old.digest(out / 'a.o'), digest)
            self.assertEqual((out / 'a.o').stat().st_mtime_ns, mtime)
            self.assertTrue(old.is_aarch64_object(out / 'b.o'))
            self.assertEqual(run(), 0)
            self.assertEqual(json.loads((out / 'safe_status.json').read_text())['cached'], 2)
            stop.touch()
            self.assertEqual(run(), 3)
            self.assertFalse((out / 'objects.rsp').exists())
            self.assertTrue((out / 'a.o').exists())
            self.assertFalse((out / '.compile.lock').exists())
            stop.unlink()
            self.assertEqual(run(), 0)
            self.assertEqual(json.loads((out / 'safe_status.json').read_text())['cached'], 2)
            (src / 'a.cpp').write_text('broken C++!')
            self.assertEqual(run(), 1)
            self.assertEqual(old.digest(out / 'a.o'), digest)
            self.assertFalse((out / 'objects.rsp').exists())
            (out / '.compile.lock').touch()
            with_lock = False
            try:
                run()
            except FileExistsError:
                with_lock = True
            self.assertTrue(with_lock)
            self.assertTrue((out / '.compile.lock').exists())

    def test_duplicate_basename_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            src = root / 'src'
            src.mkdir()
            (src / 'sub').mkdir()
            (src / 'x.cpp').touch()
            (src / 'sub/x.cpp').touch()
            ndk = root / 'ndk'
            ndk.mkdir()
            with self.assertRaisesRegex(ValueError, 'Duplicate source'):
                engine.plan(dict(source=str(src), output=str(root / 'out'), ndk=str(ndk), gos=False, recursive=True))


if __name__ == '__main__':
    unittest.main(verbosity=2)
