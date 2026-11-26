import unittest
from pathlib import Path


class ReleaseMetadataTests(unittest.TestCase):
    def test_candidate_version_consistent(self):
        root = Path(__file__).resolve().parents[1]
        readme = (root / 'README.md').read_text(encoding='utf-8')
        version = readme.splitlines()[0].split()[2]
        self.assertEqual(version, '0.7.0-rc.1')
        app = (root / 'app.py').read_text(encoding='utf-8')
        installer = (root / 'BuildForge.iss').read_text(encoding='utf-8')
        self.assertIn('workbench · ' + version, app)
        self.assertIn('BuildForge  ' + version, app)
        self.assertIn('AppVersion=' + version + '\n', installer)
        self.assertIn('OutputBaseFilename=BuildForge-' + version + '-Setup', installer)
        self.assertIn('AppId=BuildForge.Local.Workbench.Beta', installer)
        self.assertIn('not a certified production release', readme)
