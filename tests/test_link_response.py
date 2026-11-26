"""Regression checks for linker response-file integrity."""
import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import engine
import link_stage
from cache_compat import GIB, digest


class LinkResponseTests(unittest.TestCase):
    def test_response_text_checks_names(self):
        with tempfile.TemporaryDirectory(prefix='forge space ') as d:
            out = Path(d).resolve()
            self.assertEqual(link_stage.response_text(out, ['one file.o']),
                             '"' + (out / 'one file.o').as_posix() + '"\n')
            for name in ('../bad.o', 'bad.cpp', 'bad"quote.o', 'bad\nline.o', 'bad\\slash.o'):
                with self.subTest(name=name), self.assertRaises(ValueError):
                    link_stage.response_text(out, [name])

    @unittest.skipUnless(os.name != 'nt' and shutil.which('clang++') and shutil.which('ld.lld'),
                         'Local Linux Clang/LLD required')
    def test_changed_public_response_fails_without_overwriting_library(self):
        with tempfile.TemporaryDirectory(prefix='forge response ') as d:
            root = Path(d)
            src = root / 'src'
            src.mkdir()
            (src / 'one file.cpp').write_text('int one(){return 1;}')
            out = root / 'out'
            ndk = root / 'ndk'
            tool = ndk / 'toolchains/llvm/prebuilt/linux-x86_64'
            (tool / 'bin').mkdir(parents=True)
            (tool / 'sysroot/usr/include').mkdir(parents=True)
            (tool / 'bin/clang++').symlink_to(shutil.which('clang++'))
            c = dict(source=str(src), output=str(out), ndk=str(ndk), includes=[],
                     workers=1, api=21, opt='1', reserve=2, headroom=.5,
                     stopfile=str(root / 'stop'), arch='aarch64', link=True,
                     freestanding=True, link_name='libdemo.so', link_page_size=16384,
                     linker_path=shutil.which('ld.lld'))
            with patch('engine.available_ram', return_value=64 * GIB), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(engine.run(c, str(out)), 0,
                                 (out / 'link.log').read_text(errors='replace')
                                 if (out / 'link.log').exists() else 'No linker log')
                status = json.loads((out / 'link_status.json').read_text())
                self.assertEqual(status['required_page_size'], 16384)
                self.assertEqual(status['elf_compatible_page_sizes'], [4096, 16384])
                previous = digest(out / 'libdemo.so')
                # Even a successful linker exit cannot publish a page-incompatible output.
                with patch('link_stage.compatible_page_sizes', return_value=[4096]):
                    self.assertEqual(engine.run(c, str(out)), 4)
                self.assertEqual(digest(out / 'libdemo.so'), previous)
                status = json.loads((out / 'link_status.json').read_text())
                self.assertFalse(status['success'])
                self.assertIn('16384-byte', status['error'])
                real_link = link_stage.link

                def changed(c, out, *args, **kwargs):
                    (out / 'objects.rsp').write_text('unexpected.o\n', encoding='utf-8')
                    return real_link(c, out, *args, **kwargs)

                with patch('link_stage.link', side_effect=changed):
                    self.assertEqual(engine.run(c, str(out)), 4)
                self.assertEqual(digest(out / 'libdemo.so'), previous)
                status = json.loads((out / 'link_status.json').read_text())
                self.assertFalse(status['success'])
                self.assertIn('differs from the validated manifest', status['error'])
                self.assertEqual(engine.run(c, str(out)), 0,
                                 (out / 'link.log').read_text(errors='replace')
                                 if (out / 'link.log').exists() else 'No linker log')
                self.assertEqual(list(out.glob('*.tmp.rsp')), [])


if __name__ == '__main__':
    unittest.main()
