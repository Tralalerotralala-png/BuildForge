"""Strict dependency tracking: actual Clang dependencies outside configured roots."""
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

import cache_compat
import engine


class DependencyTests(unittest.TestCase):
    def test_parse_clang_make_escapes(self):
        text = 'buildforge_dep: one\\ file.h \\\n two\\#name.h cash$$.h\n'
        self.assertEqual([p.name for p in cache_compat.parse_depfile(text)],
                         ['one file.h', 'two#name.h', 'cash$.h'])
        for bad in ('wrong: a.h', 'buildforge_dep:', 'buildforge_dep: a$var',
                    'buildforge_dep: a.h\nother: b.h', 'buildforge_dep: #comment'):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                cache_compat.parse_depfile(bad)

    @unittest.skipUnless(os.name != 'nt' and shutil.which('clang++'), 'Local Clang required')
    def test_external_dependency_cache_and_audit(self):
        with tempfile.TemporaryDirectory(prefix='forge deps ') as d:
            root = Path(d)
            src = root / 'src'
            src.mkdir()
            ext = root / 'external files'
            ext.mkdir()
            header = ext / 'value #1.h'
            header.write_text('#define VALUE 1\n', encoding='utf-8')
            (src / 'dependent.cpp').write_text('#include "../external files/value #1.h"\nint value(){return VALUE;}\n')
            (src / 'independent.cpp').write_text('int independent(){return 7;}\n')
            out = root / 'out'
            ndk = root / 'ndk'
            tool = ndk / 'toolchains/llvm/prebuilt/linux-x86_64'
            (tool / 'bin').mkdir(parents=True)
            (tool / 'sysroot/usr/include').mkdir(parents=True)
            (tool / 'bin/clang++').symlink_to(shutil.which('clang++'))
            c = dict(source=str(src), output=str(out), ndk=str(ndk), includes=[],
                     workers=1, api=21, opt='1', reserve=2, headroom=.5,
                     stopfile=str(root / 'stop'), arch='aarch64', track_dependencies=True)
            def run():
                with patch('engine.available_ram', return_value=64 * cache_compat.GIB), contextlib.redirect_stdout(io.StringIO()):
                    return engine.run(c, str(out))
            self.assertEqual(run(), 0)
            stamp = json.loads((out / 'dependent.json').read_text())
            self.assertIn(cache_compat.digest(header), stamp['dependencies'].values())
            self.assertEqual(engine.find_missing_units(c), [])
            self.assertEqual(run(), 0)
            self.assertEqual(json.loads((out / 'safe_status.json').read_text())['cached'], 2)
            header.write_text('#define VALUE 2\n', encoding='utf-8')
            self.assertEqual(engine.find_missing_units(c), ['dependent.cpp'])
            self.assertEqual(run(), 0)
            state = json.loads((out / 'safe_status.json').read_text())
            self.assertEqual((state['compiled'], state['cached']), (1, 1))
            header.unlink()
            self.assertEqual(run(), 1)
            self.assertFalse((out / 'object_manifest.json').exists())
            self.assertFalse((out / '.compile.lock').exists())
