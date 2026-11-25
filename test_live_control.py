import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import engine
from cache_compat import GIB, atomic_json, compile_one, is_elf_object, total_ram
from resource_control import Budget, LiveControl, ProcessRegistry


class LiveTests(unittest.TestCase):
    def test_worker_targets(self):
        self.assertEqual([Budget(x).workers(16) for x in (0, 1, 25, 50, 75, 100)], [0, 1, 4, 8, 12, 16])
        self.assertGreater(Budget(25).reserve(4, 64 * GIB), Budget(100).reserve(4, 64 * GIB))
        for x in (-1, 101, True, 1.5):
            with self.assertRaises(ValueError):
                Budget(x)

    def test_last_valid(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'control.json'
            c = LiveControl(p, 75)
            self.assertEqual(c.read().percent, 75)
            atomic_json(p, {'percent': 0})
            self.assertEqual(c.read().percent, 0)
            p.write_text('bad', encoding='utf-8')
            self.assertEqual(c.read().percent, 0)
            atomic_json(p, {'percent': 100})
            self.assertEqual(c.read().percent, 100)

    def test_total_ram_detected(self):
        ram = total_ram()
        self.assertIsInstance(ram, int)
        self.assertGreater(ram, 0)

    def test_registry_terminate_all_owned_process(self):
        reg = ProcessRegistry()
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
        proc = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], creationflags=creationflags)
        try:
            reg.add(proc, 'dummy.cpp')
            snap = reg.snapshot()
            self.assertEqual(len(snap), 1)
            self.assertEqual(snap[0]['pid'], proc.pid)
            self.assertEqual(reg.terminate_all(), 1)
            rc = proc.wait(timeout=5)
            self.assertIsNotNone(rc)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)

    def test_selected_cancel_leaves_other_worker_running(self):
        reg = ProcessRegistry()
        cmd = [sys.executable, '-c', 'import time; time.sleep(30)']
        a = reg.launch('a.cpp', cmd)
        b = reg.launch('b.cpp', cmd)
        try:
            self.assertEqual(reg.cancel_labels({'a.cpp'}), 1)
            self.assertIsNotNone(a.wait(timeout=5))
            self.assertIsNone(b.poll())
            self.assertIsNone(reg.launch('a.cpp', cmd))
            self.assertFalse(reg.cancelled)
            self.assertEqual(reg.cancel_labels({'unrelated.cpp'}), 0)
            self.assertIsNone(b.poll())
        finally:
            for p in (a, b):
                if p.poll() is None:
                    p.kill()
                    p.wait(timeout=5)

    def test_selected_cancel_blocks_delayed_unit_only(self):
        reg = ProcessRegistry()
        self.assertEqual(reg.cancel_labels({'late.cpp'}), 0)
        with patch('resource_control.subprocess.Popen') as spawn:
            self.assertIsNone(reg.launch('late.cpp', ['unused']))
            spawn.assert_not_called()
            self.assertIs(reg.launch('other.cpp', ['unused']), spawn.return_value)
            spawn.assert_called_once()

    def test_cancel_latches_before_worker_spawn(self):
        reg = ProcessRegistry()
        self.assertEqual(reg.terminate_all(), 0)
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            src = root / 'late.cpp'
            src.write_text('int late(){return 0;}', encoding='utf-8')
            with patch('resource_control.subprocess.Popen') as spawn:
                self.assertEqual(compile_one(src, root, Path('unused'), [], 'context', False, reg),
                                 ('late.cpp', 'failed'))
                spawn.assert_not_called()
            self.assertFalse((root / 'late.o').exists())
            self.assertFalse((root / 'late.json').exists())
            self.assertEqual(list(root.glob('*.tmp.o')), [])

    def test_cancel_serializes_with_process_creation(self):
        reg = ProcessRegistry()
        started = threading.Event()
        release = threading.Event()
        cancelled = threading.Event()
        created = []
        errors = []
        real_popen = subprocess.Popen
        def delayed_spawn(*args, **kwargs):
            started.set()
            if not release.wait(5):
                raise RuntimeError('test spawn timed out')
            return real_popen(*args, **kwargs)
        def launch():
            try:
                created.append(reg.launch('unit.cpp', [sys.executable, '-c', 'import time; time.sleep(30)']))
            except BaseException as e:
                errors.append(e)
        def stop():
            try:
                reg.terminate_all()
                cancelled.set()
            except BaseException as e:
                errors.append(e)
        with patch('resource_control.subprocess.Popen', side_effect=delayed_spawn):
            worker = threading.Thread(target=launch)
            stopper = threading.Thread(target=stop)
            worker.start()
            try:
                self.assertTrue(started.wait(5))
                stopper.start()
                self.assertFalse(cancelled.wait(0.1))
                release.set()
                worker.join(8)
                stopper.join(15)
                self.assertFalse(worker.is_alive())
                self.assertFalse(stopper.is_alive())
                self.assertEqual(errors, [])
                self.assertTrue(cancelled.is_set())
                self.assertEqual(len(created), 1)
                self.assertIsNotNone(created[0].wait(timeout=5))
            finally:
                release.set()
                worker.join(8)
                if stopper.ident is not None:
                    stopper.join(15)
                for proc in created:
                    if proc.poll() is None:
                        proc.kill()
                        proc.wait(timeout=5)

    @unittest.skipUnless(os.name != 'nt' and shutil.which('clang++'), 'requires local Linux Clang')
    def test_live_scheduler_real_compiler(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            src = root / 'src'
            src.mkdir()
            ndk = root / 'ndk'
            tool = ndk / 'toolchains/llvm/prebuilt/linux-x86_64'
            (tool / 'bin').mkdir(parents=True)
            (tool / 'sysroot/usr/include').mkdir(parents=True)
            (tool / 'bin/clang++').symlink_to(shutil.which('clang++'))
            out = root / 'out'
            for i in range(8):
                (src / f'u{i}.cpp').write_text(f'int f{i}(){{return {i};}}')
            (src / 'plain.c').write_text('int c_function(void){return 42;}')
            control = root / 'budget.json'
            atomic_json(control, {'percent': 0})
            stop = root / 'stop'
            c = dict(
                source=str(src),
                output=str(out),
                ndk=str(ndk),
                includes=[],
                workers=4,
                api=21,
                opt='1',
                reserve=2,
                headroom=0.5,
                stopfile=str(stop),
                controlfile=str(control),
                budget=0,
                arch='x86_64',
            )
            results = []
            events = []
            started = []
            mutex = threading.Lock()
            first = threading.Event()
            release = threading.Event()
            ready = threading.Event()
            real = engine.compile_one

            def track(*args):
                with mutex:
                    started.append(args[0].name)
                    first.set()
                if not release.wait(12):
                    raise RuntimeError('test timeout')
                return real(*args)

            def event(kind, **kw):
                with mutex:
                    events.append((kind, kw))
                if kind == 'progress' and kw.get('target') == 0:
                    ready.set()

            def job():
                try:
                    results.append(engine.run(c, str(out)))
                except BaseException as e:
                    results.append(e)

            with (
                patch('engine.available_ram', return_value=64 * GIB),
                patch('engine.compile_one', side_effect=track),
                patch('engine.event', side_effect=event),
            ):
                t = threading.Thread(target=job)
                t.start()
                try:
                    self.assertTrue(ready.wait(8))
                    self.assertEqual(started, [])
                    atomic_json(control, {'percent': 25})
                    self.assertTrue(first.wait(8))
                    time.sleep(0.8)
                    self.assertEqual(len(started), 1)
                    atomic_json(control, {'percent': 0})
                    time.sleep(0.8)
                    release.set()
                    time.sleep(1.2)
                    self.assertEqual(len(started), 1)
                    atomic_json(control, {'percent': 100})
                    t.join(15)
                    self.assertFalse(t.is_alive())
                    self.assertEqual(results, [0])
                    self.assertTrue(all(is_elf_object(p, 62) for p in out.glob('*.o')))
                    self.assertEqual(len(list(out.glob('*.o'))), 9)
                    self.assertTrue(any(k == 'progress' and v.get('target') == 4 for k, v in events))
                finally:
                    release.set()
                    stop.touch()
                    t.join(15)


if __name__ == '__main__':
    unittest.main()
