import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from tools.package_candidate import bundle
from updater import (
    SecureUpdater,
    UpdateError,
    check_version_upgrade,
    compare_versions,
    discover_release,
    parse_version,
    validate_release_descriptor,
)


def _make_candidate(root: Path, version: str, commit: str, exe_bytes: bytes):
    portable = root / f"portable-{version}"
    installer = root / f"installer-{version}"
    out = root / f"candidate-{version}"
    portable.mkdir(parents=True, exist_ok=True)
    installer.mkdir(parents=True, exist_ok=True)
    (portable / "BuildForge.exe").write_bytes(exe_bytes)
    (portable / "README.txt").write_text(f"version={version}", encoding="utf-8")
    (portable / "build-metadata.json").write_text(json.dumps({"commit": commit}), encoding="utf-8")
    (installer / f"BuildForge-{version}-Setup.exe").write_bytes(b"setup-" + exe_bytes)
    bundle(portable, installer, out, hash_workers=1)
    manifest_sha = hashlib.sha256((out / "build-metadata.json").read_bytes()).hexdigest()
    return out, manifest_sha


class UpdaterTests(unittest.TestCase):
    def test_semver_and_downgrade_protection(self):
        self.assertEqual(compare_versions("0.7.0", "0.7.0-rc.1"), 1)
        self.assertEqual(compare_versions("0.7.0-rc.2", "0.7.0-rc.1"), 1)
        self.assertEqual(compare_versions("0.7.1", "0.7.0"), 1)
        self.assertEqual(compare_versions("0.6.9", "0.7.0-rc.1"), -1)
        check_version_upgrade("0.7.0", "0.7.0-rc.1")
        with self.assertRaises(UpdateError):
            check_version_upgrade("0.7.0-rc.1", "0.7.0-rc.1")
        with self.assertRaises(UpdateError):
            check_version_upgrade("0.6.9", "0.7.0-rc.1")
        with self.assertRaises(UpdateError):
            parse_version("not-a-version")

    def test_release_discovery_rejects_untrusted_origins_and_downgrades(self):
        desc = {
            "version": "0.7.0",
            "commit": "a" * 40,
            "manifest_sha256": "b" * 64,
            "download_url": "https://api.github.com/repos/Tralalerotralala-png/BuildForge/releases/assets/1",
        }
        validated = validate_release_descriptor(desc, current_version="0.7.0-rc.1")
        self.assertEqual(validated["version"], "0.7.0")

        bad_origin = dict(desc, download_url="https://evil.example.com/BuildForge.zip")
        with self.assertRaises(UpdateError):
            validate_release_descriptor(bad_origin, current_version="0.7.0-rc.1")

        bad_scheme = dict(desc, download_url="http://api.github.com/repos/Tralalerotralala-png/BuildForge/x")
        with self.assertRaises(UpdateError):
            validate_release_descriptor(bad_scheme, current_version="0.7.0-rc.1")

        def fake_opener(req, timeout=15):
            return io.BytesIO(json.dumps(desc).encode("utf-8"))

        found = discover_release(
            "https://api.github.com/repos/Tralalerotralala-png/BuildForge/releases/latest",
            current_version="0.7.0-rc.1",
            opener=fake_opener,
        )
        self.assertEqual(found["commit"], "a" * 40)

        with self.assertRaises(UpdateError):
            discover_release(
                "https://evil.example.com/latest.json",
                current_version="0.7.0-rc.1",
                opener=fake_opener,
            )

    def test_stage_activate_rollback_and_interruption_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            install_dir = root / "install"
            state_dir = root / "state"
            build_dir = root / "active-build"
            install_dir.mkdir()
            build_dir.mkdir()
            (install_dir / "BuildForge.exe").write_bytes(b"v0.7.0-rc.1-bin")
            (install_dir / "README.txt").write_text("version=0.7.0-rc.1", encoding="utf-8")

            commit = "c" * 40
            cand, manifest_sha = _make_candidate(root, "0.7.0", commit, b"v0.7.0-bin")
            updater = SecureUpdater(install_dir, state_dir)

            # Reject staging downgrade.
            with self.assertRaises(UpdateError):
                updater.stage_candidate(
                    cand,
                    current_version="0.8.0",
                    expected_manifest_sha256=manifest_sha,
                    expected_commit=commit,
                )

            staged = updater.stage_candidate(
                cand,
                current_version="0.7.0-rc.1",
                expected_manifest_sha256=manifest_sha,
                expected_commit=commit,
            )

            # Refuse activation while an active build lock exists.
            (build_dir / ".compile.lock").write_text("busy", encoding="utf-8")
            with self.assertRaises(UpdateError):
                updater.activate(staged, active_build_dirs=[build_dir])
            (build_dir / ".compile.lock").unlink()

            # Defer cleanly if binary is currently running.
            deferred = updater.activate(staged, target_running=True)
            self.assertEqual(deferred["status"], "deferred_target_running")
            self.assertEqual((install_dir / "BuildForge.exe").read_bytes(), b"v0.7.0-rc.1-bin")

            # Crash mid-activation -> automatic rollback restores original installation intact.
            with self.assertRaises(RuntimeError):
                updater.activate(staged, inject_failure_phase="activating")
            self.assertEqual((install_dir / "BuildForge.exe").read_bytes(), b"v0.7.0-rc.1-bin")
            self.assertEqual((install_dir / "README.txt").read_text(encoding="utf-8"), "version=0.7.0-rc.1")

            # Simulate an unrecovered interrupted state journal on disk -> recover_interrupted_update restores it.
            (install_dir / "BuildForge.exe").write_bytes(b"corrupted-partial")
            journal = json.loads(updater.journal_path.read_text(encoding="utf-8"))
            journal["phase"] = "activating"
            updater.journal_path.write_text(json.dumps(journal), encoding="utf-8")
            rec = updater.recover_interrupted_update()
            self.assertTrue(rec["recovered"])
            self.assertEqual(rec["phase"], "rolled_back")
            self.assertEqual((install_dir / "BuildForge.exe").read_bytes(), b"v0.7.0-rc.1-bin")

            # Clean activation succeeds and explicit rollback works too.
            committed = updater.activate(staged)
            self.assertEqual(committed["phase"], "committed")
            self.assertEqual((install_dir / "BuildForge.exe").read_bytes(), b"v0.7.0-bin")

            rolled = updater.rollback_last()
            self.assertEqual(rolled["phase"], "rolled_back")
            self.assertEqual((install_dir / "BuildForge.exe").read_bytes(), b"v0.7.0-rc.1-bin")


if __name__ == "__main__":
    unittest.main()
