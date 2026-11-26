import json
from pathlib import Path
import tempfile
import unittest
import zipfile

from tools.package_candidate import bundle, sha256, zip_portable


class CandidatePackageTests(unittest.TestCase):
    def test_archive_and_checksums(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            portable = root / 'portable'
            portable.mkdir()
            (portable / 'BuildForge.exe').write_bytes(b'fake exe')
            (portable / 'build-metadata.json').write_text('{"status":"candidate"}')
            (portable / 'nested').mkdir()
            (portable / 'nested' / 'test.txt').write_bytes(b'payload')
            installer = root / 'installer'
            installer.mkdir()
            (installer / 'BuildForge-0.7.0-rc.1-Setup.exe').write_bytes(b'fake installer')
            first, second = root / 'first.zip', root / 'second.zip'
            zip_portable(portable, first)
            zip_portable(portable, second)
            self.assertEqual(sha256(first), sha256(second))
            with zipfile.ZipFile(first) as archive:
                self.assertEqual(archive.namelist(), sorted(archive.namelist()))
                self.assertEqual(archive.read('BuildForge/nested/test.txt'), b'payload')
            out = root / 'candidate'
            bundle(portable, installer, out)
            manifest = json.loads((out / 'build-metadata.json').read_text())
            for name, value in manifest['artifacts'].items():
                self.assertEqual(value, sha256(out / name))
            for line in (out / 'SHA256SUMS.txt').read_text().splitlines():
                value, name = line.split('  ', 1)
                self.assertEqual(value, sha256(out / name))
            with self.assertRaises(FileExistsError):
                bundle(portable, installer, out)
