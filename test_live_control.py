import contextlib, io, json, os, shutil, subprocess, tempfile, threading, time, unittest
from pathlib import Path
from unittest.mock import patch
from resource_control import Budget, LiveControl
import engine
from cache_compat import atomic_json, GIB, is_elf_object

class LiveTests(unittest.TestCase):
    def test_worker_targets(self):
        self.assertEqual([Budget(x).workers(16) for x in (0,1,25,50,75,100)],[0,1,4,8,12,16])
        self.assertGreater(Budget(25).reserve(4,64*GIB),Budget(100).reserve(4,64*GIB))
        for x in (-1,101,True,1.5):
            with self.assertRaises(ValueError):Budget(x)
    def test_last_valid(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'control.json';c=LiveControl(p,75);self.assertEqual(c.read().percent,75)
            atomic_json(p,{'percent':0});self.assertEqual(c.read().percent,0)
            p.write_text('bad');self.assertEqual(c.read().percent,0)
            atomic_json(p,{'percent':100});self.assertEqual(c.read().percent,100)
    @unittest.skipUnless(os.name!='nt' and shutil.which('clang++'),'requires local Linux Clang')
    def test_live_scheduler_real_compiler(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);src=root/'src';src.mkdir();ndk=root/'ndk';tool=ndk/'toolchains/llvm/prebuilt/linux-x86_64';(tool/'bin').mkdir(parents=True);(tool/'sysroot/usr/include').mkdir(parents=True);(tool/'bin/clang++').symlink_to(shutil.which('clang++'));out=root/'out'
            for i in range(8):(src/f'u{i}.cpp').write_text(f'int f{i}(){{return {i};}}')
            (src/'plain.c').write_text('int c_function(void){return 42;}')
            control=root/'budget.json';atomic_json(control,{'percent':0});stop=root/'stop'
            c=dict(source=str(src),output=str(out),ndk=str(ndk),includes=[],workers=4,api=21,opt='1',reserve=2,headroom=.5,stopfile=str(stop),controlfile=str(control),budget=0,arch='x86_64')
            results=[];events=[];started=[];mutex=threading.Lock();first=threading.Event();release=threading.Event();ready=threading.Event();real=engine.compile_one
            def track(*args):
                with mutex:started.append(args[0].name);first.set()
                if not release.wait(12):raise RuntimeError('test timeout')
                return real(*args)
            def event(kind,**kw):
                with mutex:events.append((kind,kw))
                if kind=='progress' and kw.get('target')==0:ready.set()
            def job():
                try:results.append(engine.run(c,str(out)))
                except BaseException as e:results.append(e)
            with patch('engine.available_ram',return_value=64*GIB),patch('engine.compile_one',side_effect=track),patch('engine.event',side_effect=event):
                t=threading.Thread(target=job);t.start()
                try:
                    self.assertTrue(ready.wait(8));self.assertEqual(started,[])
                    atomic_json(control,{'percent':25});self.assertTrue(first.wait(8));time.sleep(.8);self.assertEqual(len(started),1)
                    atomic_json(control,{'percent':0});time.sleep(.8);release.set();time.sleep(1.2);self.assertEqual(len(started),1)
                    atomic_json(control,{'percent':100});t.join(15);self.assertFalse(t.is_alive());self.assertEqual(results,[0])
                    self.assertTrue(all(is_elf_object(p,62) for p in out.glob('*.o')));self.assertEqual(len(list(out.glob('*.o'))),9)
                    self.assertTrue(any(k=='progress' and v.get('target')==4 for k,v in events))
                finally:
                    release.set();stop.touch();t.join(15)
if __name__=='__main__':unittest.main()
