"""Regression checks for metadata isolation and additional dependency coverage."""
import tempfile
import unittest
from pathlib import Path

import cache_compat
import engine


class ArtifactIsolationTests(unittest.TestCase):
    def test_metadata_stems_rejected_before_output_created(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            src = root / 'src'
            src.mkdir()
            ndk = root / 'ndk'
            ndk.mkdir()
            out = root / 'out'
            c = dict(source=str(src), ndk=str(ndk), output=str(out), includes=[])
            for name in ('safe_status.cpp', 'OBJECT_MANIFEST.c', 'unit_timings.cc',
                         'link_status.cxx', 'Link.cpp'):
                p = src / name
                p.touch()
                with self.subTest(name=name), self.assertRaisesRegex(ValueError, 'reserved'):
                    engine.plan(c)
                p.unlink()
            self.assertFalse(out.exists())

    def test_generic_header_extensions_do_not_change_legacy_hash(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / 'existing.hpp').write_text('old')
            legacy = cache_compat.header_digest([root])
            generic = cache_compat.header_digest([root], generic=True)
            for name in ('template.tpp', 'inline.ipp', 'extensionless'):
                p = root / name
                p.write_text('new')
                self.assertEqual(cache_compat.header_digest([root]), legacy)
                self.assertNotEqual(cache_compat.header_digest([root], generic=True), generic)
                generic = cache_compat.header_digest([root], generic=True)
                p.write_text('changed')
                self.assertNotEqual(cache_compat.header_digest([root], generic=True), generic)
                generic = cache_compat.header_digest([root], generic=True)
            (root / 'normal.cpp').write_text('int main(){}')
            self.assertEqual(cache_compat.header_digest([root], generic=True), generic)
