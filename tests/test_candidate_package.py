import json
from collections import Counter
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
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

    def test_artifacts_hashed_once_and_worker_modes_match(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            portable = root / 'portable'
            portable.mkdir()
            (portable / 'BuildForge.exe').write_bytes(b'portable' * 65536)
            (portable / 'build-metadata.json').write_text('{"status":"candidate"}')
            installer = root / 'installer'
            installer.mkdir()
            (installer / 'BuildForge-test-Setup.exe').write_bytes(b'installer' * 65536)
            expected = Counter({'BuildForge-test-Windows-portable.zip': 1,
                                'BuildForge-test-Setup.exe': 1,
                                'build-metadata.json': 1})
            outputs = []
            for workers in (1, 2):
                out = root / f'candidate-{workers}'
                with patch('tools.package_candidate.sha256', wraps=sha256) as observed:
                    bundle(portable, installer, out, hash_workers=workers)
                self.assertEqual(Counter(c.args[0].name for c in observed.call_args_list), expected)
                for line in (out / 'SHA256SUMS.txt').read_text().splitlines():
                    digest, name = line.split('  ', 1)
                    self.assertEqual(digest, sha256(out / name))
                outputs.append((out / 'SHA256SUMS.txt').read_bytes())
            self.assertEqual(outputs[0], outputs[1])

    def test_invalid_worker_count_rejected_before_output(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            out = root / 'candidate'
            for workers in (0, 3, -1, True, 1.0):
                with self.subTest(workers=workers), self.assertRaisesRegex(ValueError, 'Hash workers'):
                    bundle(root / 'portable', root / 'installer', out, hash_workers=workers)
                self.assertFalse(out.exists())
