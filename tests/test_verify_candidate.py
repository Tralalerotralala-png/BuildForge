import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import stat
import sys
import threading
import unittest
from unittest.mock import patch
import zipfile

from tools.package_candidate import bundle
from tools.verify_candidate import (verify_candidate, stage_signed_portable,
                                    verify_signed_candidate, _SIGNED_DOMAIN)


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


# Keys are ephemeral test fixtures; no production trust material is distributed.
_HAS_CRYPTO = importlib.util.find_spec("cryptography") is not None


class SignedCandidateTests(unittest.TestCase):
    def setUp(self):
        if not _HAS_CRYPTO:
            self.skipTest("Optional signature verification backend unavailable")
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        self.key = Ed25519PrivateKey.generate()
        self.public = self.key.public_key().public_bytes(serialization.Encoding.Raw,
                                                        serialization.PublicFormat.Raw)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.portable = self.root / "portable"
        installer = self.root / "installer"
        self.stage_parent = self.root / "stage"
        for path in (self.portable, installer, self.stage_parent):
            path.mkdir()
        self.commit = "a" * 40
        self.version = "0.7.0-rc.1"
        self.metadata = {"commit": self.commit, "status": "synthetic candidate"}
        (self.portable / "BuildForge.exe").write_bytes(b"synthetic executable")
        (self.portable / "build-metadata.json").write_text(json.dumps(self.metadata), encoding="utf-8")
        (self.portable / "_internal").mkdir()
        (self.portable / "_internal" / "fixture.dll").write_bytes(b"synthetic dll")
        (installer / f"BuildForge-{self.version}-Setup.exe").write_bytes(b"synthetic installer")
        self.out = self.root / "candidate"
        bundle(self.portable, installer, self.out)
        self.manifest = self.out / "build-metadata.json"
        self.archive = next(self.out.glob("*.zip"))
        self.installation = self.root / "installation"
        self.installation.mkdir()
        (self.installation / "BuildForge.exe").write_bytes(b"working installation")

    def options(self, **kwargs):
        raw = self.manifest.read_bytes()
        values = dict(signed_manifest=raw, signature=self.key.sign(_SIGNED_DOMAIN + raw),
                      trusted_public_key=self.public, trusted_key_sha256=hashlib.sha256(self.public).hexdigest(),
                      expected_commit=self.commit, expected_version=self.version)
        values.update(kwargs)
        return values

    def stage(self, **kwargs):
        return stage_signed_portable(self.out, self.stage_parent, **self.options(**kwargs))

    def verify(self, **kwargs):
        return verify_signed_candidate(self.out, **self.options(**kwargs))

    def assert_clean(self):
        self.assertEqual(list(self.stage_parent.iterdir()), [])
        self.assertEqual((self.installation / "BuildForge.exe").read_bytes(), b"working installation")

    def resign_archive(self, entries):
        # Construct malformed *signed* candidates to verify defense in depth.
        with zipfile.ZipFile(self.archive, "w") as archive:
            for name, data, mode in entries:
                info = zipfile.ZipInfo(name)
                # ZipInfo normalizes backslashes on Windows even when constructing a malformed fixture.
                info.filename = name
                info.create_system = 3
                info.external_attr = mode << 16
                archive.writestr(info, data)
        obj = json.loads(self.manifest.read_bytes())
        obj["artifacts"][self.archive.name] = hashlib.sha256(self.archive.read_bytes()).hexdigest()
        self.manifest.write_text(json.dumps(obj), encoding="utf-8")

    def entries(self):
        return [("BuildForge/BuildForge.exe", b"synthetic executable", stat.S_IFREG | 0o644),
                ("BuildForge/build-metadata.json", json.dumps(self.metadata).encode(), stat.S_IFREG | 0o644)]

    def test_real_signature_and_stage(self):
        self.assertIn("signature and integrity verified", self.verify()["status"])
        result = self.stage()
        stage = result["stage"]
        self.assertEqual(stage.parent, self.stage_parent)
        self.assertEqual(result["files"], 3)
        self.assertEqual((stage / "BuildForge" / "BuildForge.exe").read_bytes(), b"synthetic executable")
        self.assertEqual((stage / "BuildForge" / "_internal" / "fixture.dll").read_bytes(), b"synthetic dll")
        self.assertGreaterEqual(result["verify_seconds"], 0)
        self.assertGreaterEqual(result["stage_seconds"], 0)
        self.assertIn("activation not authorized", result["status"])
        self.assertEqual((self.installation / "BuildForge.exe").read_bytes(), b"working installation")
        self.assertNotEqual(self.stage()["stage"], stage)

    def test_invalid_signature_and_wrong_domain(self):
        for sig in (b"x" * 64, self.key.sign(self.manifest.read_bytes()), b""):
            with self.subTest(size=len(sig)), self.assertRaises(ValueError):
                self.stage(signature=sig)
            self.assert_clean()

    def test_key_pin_and_release_selection(self):
        for options in ({"trusted_key_sha256": "0" * 64}, {"expected_commit": "b" * 40},
                        {"expected_version": "0.6.0"}, {"trusted_public_key": b"x" * 32}):
            with self.subTest(options=tuple(options)), self.assertRaises(ValueError):
                self.stage(**options)
            self.assert_clean()

    def test_valid_signature_does_not_authorize_changed_disk_metadata(self):
        options = self.options()
        self.manifest.write_bytes(self.manifest.read_bytes() + b" ")
        with self.assertRaises(ValueError):
            stage_signed_portable(self.out, self.stage_parent, **options)
        self.assert_clean()

    def test_signature_backend_missing_fails_closed(self):
        with patch.dict(sys.modules, {"cryptography.hazmat.primitives.asymmetric.ed25519": None}):
            with self.assertRaisesRegex(RuntimeError, "backend unavailable"):
                self.stage()
        self.assert_clean()

    def test_windows_path_hazards(self):
        paths = ("../escape", "BuildForge/../escape", "BuildForge/a\\b", "BuildForge/a:stream",
                 "BuildForge/NUL.txt", "BuildForge/COM1 .txt", "BuildForge/dir./file",
                 "BuildForge/a ", "BuildForge/FOO~1.txt", "BuildForge/nonascii-\u00e9")
        for path in paths:
            with self.subTest(path=path):
                self.resign_archive(self.entries() + [(path, b"bad", stat.S_IFREG | 0o644)])
                with self.assertRaises(ValueError):
                    self.stage()
                self.assert_clean()

    def test_case_alias_and_file_directory_collision(self):
        for names in (("BuildForge/Folder/a", "BuildForge/folder/b"),
                      ("BuildForge/a", "BuildForge/a/b"), ("BuildForge/buildforge.exe",)):
            with self.subTest(names=names):
                self.resign_archive(self.entries() + [(n, b"x", stat.S_IFREG | 0o644) for n in names])
                with self.assertRaises(ValueError):
                    self.stage()
                self.assert_clean()

    def test_special_entries_and_missing_executable(self):
        self.resign_archive(self.entries() + [("BuildForge/link", b"outside", stat.S_IFLNK | 0o777)])
        with self.assertRaises(ValueError):
            self.stage()
        self.assert_clean()
        self.resign_archive(self.entries()[1:])
        with self.assertRaises(ValueError):
            self.stage()
        self.assert_clean()

    def test_budget_failures_and_cancellation(self):
        for options in ({"max_files": 1}, {"max_bytes": 1}, {"max_bytes": True}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.stage(**options)
            self.assert_clean()
        with patch("tools.verify_candidate._MAX_ARCHIVE", 1):
            with self.assertRaises(ValueError):
                self.stage()
        self.assert_clean()
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(ValueError):
            self.stage(cancelled=cancel)
        self.assert_clean()

    def test_embedded_metadata_mismatch_cleans_partial_stage(self):
        entries = self.entries()
        entries[1] = (entries[1][0], b'{"commit":"wrong"}', entries[1][2])
        self.resign_archive(entries)
        with self.assertRaisesRegex(ValueError, "Embedded build metadata"):
            self.stage()
        self.assert_clean()

    def test_archive_change_between_verification_and_extraction(self):
        from tools import verify_candidate as module
        original = module.verify_signed_candidate

        def swapped(*args, **kwargs):
            result = original(*args, **kwargs)
            raw = self.archive.read_bytes()
            self.archive.write_bytes(bytes([raw[0] ^ 1]) + raw[1:])
            return result

        with patch.object(module, "verify_signed_candidate", side_effect=swapped):
            with self.assertRaisesRegex(ValueError, "checksum mismatch"):
                self.stage()
        self.assert_clean()

    def test_cancellation_during_extraction_cleans_partial_stage(self):
        cancel = threading.Event()
        original = zipfile.ZipFile.open

        def cancelled(*args, **kwargs):
            result = original(*args, **kwargs)
            cancel.set()
            return result

        with patch.object(zipfile.ZipFile, "open", cancelled):
            with self.assertRaisesRegex(ValueError, "cancelled"):
                self.stage(cancelled=cancel)
        self.assert_clean()

    def test_signature_from_another_key_even_with_matching_pin(self):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        other = Ed25519PrivateKey.generate().public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        with self.assertRaisesRegex(ValueError, "signature verification failed"):
            self.stage(trusted_public_key=other, trusted_key_sha256=hashlib.sha256(other).hexdigest())
        self.assert_clean()

    def test_symlink_stage_parent_rejected(self):
        alias = self.root / "alias"
        try:
            alias.symlink_to(self.stage_parent, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("Symbolic links unavailable")
        with self.assertRaises(ValueError):
            stage_signed_portable(self.out, alias, **self.options())
        self.assert_clean()

    def test_crc_failure_cleans_partial_stage(self):
        # The synthetic signed ZIP is intentionally internally inconsistent.
        self.resign_archive(self.entries())
        raw = self.archive.read_bytes()
        offset = raw.index(b"synthetic executable")
        self.archive.write_bytes(raw[:offset] + b"X" + raw[offset + 1:])
        obj = json.loads(self.manifest.read_bytes())
        obj["artifacts"][self.archive.name] = hashlib.sha256(self.archive.read_bytes()).hexdigest()
        self.manifest.write_text(json.dumps(obj), encoding="utf-8")
        with self.assertRaises(zipfile.BadZipFile):
            self.stage()
        self.assert_clean()


class SignatureBackendPolicyTests(unittest.TestCase):
    def test_backend_missing_never_falls_back_to_unsigned(self):
        key = b"x" * 32
        with patch.dict(sys.modules, {"cryptography.hazmat.primitives.asymmetric.ed25519": None,
                                     "cryptography.exceptions": None}):
            with self.assertRaisesRegex(RuntimeError, "backend unavailable"):
                verify_signed_candidate("not accessed", signed_manifest=b"{}", signature=b"x" * 64,
                                        trusted_public_key=key, trusted_key_sha256=hashlib.sha256(key).hexdigest(),
                                        expected_commit="a" * 40, expected_version="0.7.0-rc.1")
