import contextlib,io,json,os,shutil,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import engine,link_stage
from cache_compat import GIB,digest
from targeting import parse_overrides,format_overrides,flags_for,overrides_for

class TargetTests(unittest.TestCase):
    def test_parse_roundtrip(self):
        s='hard.cpp | 0 | timing\nnormal.cpp | same'
        self.assertEqual(parse_overrides(format_overrides(parse_overrides(s))),parse_overrides(s))
        for bad in ['x.cpp | 3','x.cpp | 0\nx.cpp | 1','x.cpp | 0 | other']:
            with self.assertRaises(ValueError):parse_overrides(bad)
        with self.assertRaises(ValueError):overrides_for([Path('one.cpp')],{'two.cpp':{'opt':'0'}})
    def test_override_only_changes_opt(self):
        self.assertEqual(flags_for(['-O1','-DNDEBUG','-fPIC'],{'opt':'0','timing':True}),['-O0','-DNDEBUG','-fPIC','-ftime-report'])
    def test_link_validation(self):
        for name in ['../bad.so','a.dll','lib/evil.so']:
            with self.assertRaises(ValueError):link_stage.validate_config({'link':True,'link_name':name})
    def test_external_linker_path_validation(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'ld.lld.exe'
            p.touch()
            self.assertEqual(link_stage.selected_linker({'linker_path': str(p)}), p)
            self.assertIsNone(link_stage.selected_linker({}))
            for raw in ['ld.lld', str(Path(d) / 'missing' / 'ld.lld'), str(Path(d) / 'wrong.exe')]:
                with self.assertRaises(ValueError):
                    link_stage.selected_linker({'linker_path': raw})
            with patch('link_stage.subprocess.check_output', return_value=b'LLD 17.0.0'):
                self.assertEqual(link_stage.linker_options({'linker_path': str(p)})[0], ['-fuse-ld=' + str(p)])
            with patch('link_stage.subprocess.check_output', return_value=b'not a linker'):
                with self.assertRaises(ValueError):
                    link_stage.linker_options({'linker_path': str(p)})
    def test_probe_rejects_invalid_target_and_missing_ndk(self):
        for c in [{'arch':'armv7'}, {'api':19}, {'ndk':''}]:
            with self.assertRaises(ValueError):
                link_stage.probe_toolchain(c)

    @unittest.skipUnless(os.name!='nt' and shutil.which('clang++') and shutil.which('ld.lld'),'Local Linux Clang/LLD required')
    def test_synthetic_linker_probe_both_architectures(self):
        with tempfile.TemporaryDirectory(prefix='forge probe space ') as d:
            root=Path(d); ndk=root/'ndk'; tool=ndk/'toolchains/llvm/prebuilt/linux-x86_64'
            (tool/'bin').mkdir(parents=True); (tool/'sysroot/usr/include').mkdir(parents=True)
            (tool/'bin/clang++').symlink_to(shutil.which('clang++'))
            private=root/'untouched'; private.mkdir()
            sentinel=private/'sentinel'; sentinel.write_text('unchanged')
            before=sorted(str(p.relative_to(root)) for p in root.rglob('*'))
            for arch in ('aarch64','x86_64'):
                r=link_stage.probe_toolchain(dict(ndk=str(ndk),arch=arch,api=21,linker_path=shutil.which('ld.lld')))
                self.assertTrue(r['success'])
                self.assertEqual(r['arch'],arch)
                self.assertIn('LLD',r['linker_version'])
            self.assertEqual(sentinel.read_text(),'unchanged')
            self.assertEqual(sorted(str(p.relative_to(root)) for p in root.rglob('*')),before)

    @unittest.skipUnless(os.name!='nt' and shutil.which('clang++') and shutil.which('ld.lld'),'Local Linux Clang/LLD required')
    def test_actual_mixed_cache_and_strict_link(self):
        with tempfile.TemporaryDirectory(prefix='forge space ') as d:
            root=Path(d);src=root/'src';src.mkdir();out=root/'out';ndk=root/'ndk';tool=ndk/'toolchains/llvm/prebuilt/linux-x86_64';(tool/'bin').mkdir(parents=True);(tool/'sysroot/usr/include').mkdir(parents=True);(tool/'bin/clang++').symlink_to(shutil.which('clang++'))
            for i in range(8):(src/f'f{i}.cpp').write_text(f'extern "C" int f{i}(){{return {i};}}')
            (src/'stubborn.cpp').write_text('extern "C" int hard(int n){int x=0; for(int i=0;i<n;i++)x+=i;return x;}')
            c=dict(source=str(src),output=str(out),ndk=str(ndk),includes=[],workers=4,api=21,opt='1',reserve=2,headroom=.5,stopfile=str(root/'stop'),arch='aarch64',link=True,freestanding=True,link_name='libdemo.so')
            def run():
                with patch('engine.available_ram',return_value=64*GIB),contextlib.redirect_stdout(io.StringIO()):return engine.run(c,str(out))
            c['linker_path']=shutil.which('ld.lld')
            self.assertEqual(run(),0); self.assertIn('LLD',json.loads((out/'link_status.json').read_text())['linker_version']); old={p.name:(digest(p),p.stat().st_mtime_ns) for p in out.glob('*.o')}; oldstamp=json.loads((out/'stubborn.json').read_text())
            c['overrides']={'stubborn.cpp':{'opt':'0','timing':True}};self.assertEqual(run(),0)
            status=json.loads((out/'safe_status.json').read_text());self.assertEqual((status['cached'],status['compiled']),(8,1));self.assertTrue(status['mixed_flags']);self.assertTrue(status['android_link_verified'])
            for p in out.glob('f*.o'):self.assertEqual((digest(p),p.stat().st_mtime_ns),old[p.name])
            self.assertNotEqual(json.loads((out/'stubborn.json').read_text())['key'],oldstamp['key']);self.assertIn('-O0',next(x for x in json.loads((out/'object_manifest.json').read_text())['objects'] if x['source']=='stubborn.cpp')['flags']);self.assertTrue((out/'stubborn.log').stat().st_size>0)
            self.assertEqual(run(),0);self.assertEqual(json.loads((out/'safe_status.json').read_text())['cached'],9)
            # A missing external must fail linking, preserving the previous binary, not cache.
            prev=digest(out/'libdemo.so');(src/'stubborn.cpp').write_text('extern int missing(); extern "C" int hard(int n){return missing()+n;}')
            self.assertEqual(run(),4);self.assertEqual(digest(out/'libdemo.so'),prev);self.assertFalse(json.loads((out/'link_status.json').read_text())['success']);self.assertTrue(json.loads((out/'safe_status.json').read_text())['all_translated_objects_ready'])
            # Removing the override returns only that file to its base fingerprint.
            c['overrides']={};c['link']=False;self.assertEqual(run(),0);self.assertEqual(json.loads((out/'safe_status.json').read_text())['cached'],8)
            self.assertEqual(json.loads((out/'stubborn.json').read_text())['key']['context_sha256'],oldstamp['key']['context_sha256'])

if __name__=='__main__':unittest.main()
