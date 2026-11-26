import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest

from tools.package_candidate import bundle
from tools.verify_candidate import verify_candidate


class VerifyCandidateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name).resolve()
        portable = root / "portable"
        installer = root / "installer"
        portable.mkdir()
        installer.mkdir()
        self.commit = "a" * 40
        (portable / "BuildForge.exe").write_bytes(b"synthetic executable")
        (portable / "build-metadata.json").write_text(json.dumps({"commit": self.commit}), encoding="utf-8")
        (installer / "BuildForge-0.7.0-rc.1-Setup.exe").write_bytes(b"synthetic installer")
        self.out = root / "candidate"
        bundle(portable, installer, self.out)
        self.metadata = self.out / "build-metadata.json"
        self.expected = hashlib.sha256(self.metadata.read_bytes()).hexdigest()

    def verify(self, **kwargs):
        options = {"expected_manifest_sha256": self.expected, "expected_commit": self.commit}
        options.update(kwargs)
        return verify_candidate(self.out, **options)

    def rewrite(self, obj):
        self.metadata.write_text(json.dumps(obj), encoding="utf-8")
        self.expected = hashlib.sha256(self.metadata.read_bytes()).hexdigest()

    def test_current_bundle(self):
        result = self.verify()
        self.assertEqual(result["version"], "0.7.0-rc.1")
        self.assertEqual(result["commit"], self.commit)
        self.assertEqual(len(result["artifacts"]), 2)
        self.assertIn("not authorized", result["status"])

    def test_wrong_trusted_digest(self):
        with self.assertRaises(ValueError):
            self.verify(expected_manifest_sha256="0" * 64)

    def test_wrong_commit(self):
        with self.assertRaises(ValueError):
            self.verify(expected_commit="b" * 40)

    def test_changed_artifact(self):
        next(self.out.glob("*.exe")).write_bytes(b"changed")
        with self.assertRaises(ValueError):
            self.verify()

    def test_changed_manifest_even_with_locally_updated_checksums(self):
        self.metadata.write_bytes(self.metadata.read_bytes() + b" ")
        (self.out / "SHA256SUMS.txt").write_text("attacker-controlled")
        with self.assertRaises(ValueError):
            self.verify()

    def test_traversal_even_with_trusted_manifest(self):
        data = json.loads(self.metadata.read_text(encoding="utf-8"))
        name, digest = data["artifacts"].popitem()
        data["artifacts"]["../" + name] = digest
        self.rewrite(data)
        with self.assertRaises(ValueError):
            self.verify()

    def test_duplicate_keys(self):
        self.metadata.write_bytes(b'{"commit":"a","commit":"b"}')
        self.expected = hashlib.sha256(self.metadata.read_bytes()).hexdigest()
        with self.assertRaises(ValueError):
            self.verify()

    def test_mismatched_versions(self):
        data = json.loads(self.metadata.read_text(encoding="utf-8"))
        name = next(n for n in data["artifacts"] if n.endswith(".exe"))
        data["artifacts"][name.replace("0.7.0", "0.8.0")] = data["artifacts"].pop(name)
        self.rewrite(data)
        with self.assertRaises(ValueError):
            self.verify()

    def test_missing_artifact(self):
        next(self.out.glob("*.exe")).unlink()
        with self.assertRaises(OSError):
            self.verify()

    def test_cancelled(self):
        cancel = threading.Event()
        cancel.set()
        with self.assertRaisesRegex(ValueError, "cancelled"):
            self.verify(cancelled=cancel)

    def test_symlink_artifact(self):
        artifact = next(self.out.glob("*.exe"))
        other = self.out.parent / "other.exe"
        artifact.rename(other)
        try:
            artifact.symlink_to(other)
        except (OSError, NotImplementedError):
            self.skipTest("Symbolic links unavailable")
        with self.assertRaises(ValueError):
            self.verify()
