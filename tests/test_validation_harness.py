"""Harness wiring tests. The simulated engine never compiles a real binary."""
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

from tools.windows_validation import validate

FAKE_ENGINE = """
import json
from pathlib import Path
import sys

job = json.loads(Path(sys.argv[sys.argv.index('--job') + 1]).read_text(encoding='utf-8'))
root = Path(job['source'])
assert 'é' in str(root) and ' ' in str(root)
assert job['api'] == 24 and job['track_dependencies']
out = Path(job['output'])
out.mkdir(exist_ok=True)
library = out / job['link_name']
private = root / 'secret-local-diagnostic'
print(private)
if Path(job['cancelfile']).exists():
    sys.exit(3)
source = (root / 'answer.cpp').read_text(encoding='utf-8')
if 'missing()' in source:
    (out / 'link_status.json').write_text('{"success": false}')
    sys.exit(4)
header = (root.parent / 'external dependency/value.h').read_text(encoding='utf-8')
if not library.exists():
    counts = dict(compiled=2, cached=0)
elif library.read_text() == header:
    counts = dict(compiled=0, cached=2)
else:
    counts = dict(compiled=1, cached=1)
(out / 'safe_status.json').write_text(json.dumps(counts))
(out / 'link_status.json').write_text('{"success": true}')
library.write_text(header)
"""


class HarnessTests(unittest.TestCase):
    @unittest.skipUnless((validate.HERE / validate.BUNDLE).is_file(), "Download the pinned CI artifact to run this test")
    def test_actual_ci_bytes_and_tamper_rejection(self):
        with tempfile.TemporaryDirectory(prefix="BF fixture é ") as workspace:
            root = Path(workspace).resolve()
            good, bad = root / "good", root / "bad"
            good.mkdir()
            bad.mkdir()
            evidence = validate.stage_candidate(good)
            self.assertEqual(evidence["status"], "PASS")
            self.assertFalse(evidence["signed"])
            self.assertTrue((good / "stage/BuildForge/BuildForge.exe").is_file())
            self.assertFalse((good / "candidate" / validate.SETUP).exists())
            candidate = root / "modified.zip"
            shutil.copyfile(validate.HERE / validate.BUNDLE, candidate)
            with candidate.open("ab") as stream:
                stream.write(b"extra bytes")
            with self.assertRaises(ValueError):
                validate.stage_candidate(bad, candidate)
            self.assertFalse((bad / "stage").exists())

    @unittest.skipIf(os.name == "nt", "POSIX script fixtures are Linux-only")
    def test_upstream_ndk_smoke_wiring_and_report_redaction(self):
        with tempfile.TemporaryDirectory(prefix="BF fixture é ") as workspace:
            root = Path(workspace)
            env = validate.worker_environment(root)
            ndk = root / "fixture NDK"
            tools = ndk / "toolchains/llvm/prebuilt/windows-x86_64/bin"
            tools.mkdir(parents=True)
            (ndk / "source.properties").write_text("Pkg.Revision = 27.3.13750724\nSecret=private\n")
            (tools / "clang++.exe").touch()
            lld = tools / "ld.lld.exe"
            lld.write_text("#!/bin/sh\necho 'LLD 18.0.2 private diagnostic'\n")
            lld.chmod(0o700)
            fake = root / "fake-engine"
            fake.write_text("#!/usr/bin/env python3\n" + FAKE_ENGINE, encoding="utf-8")
            fake.chmod(0o700)
            with patch.dict(os.environ, env, clear=True):
                evidence = validate.native_smoke(fake, ndk)
                self.assertEqual(evidence["status"], "PASS")
                self.assertEqual(evidence["completed"], list(validate.NATIVE_MARKERS))
                self.assertEqual(evidence["lld"], "18.0.2")
                self.assertEqual(evidence["ndk"], "27.3.13750724")
                encoded = json.dumps(evidence)
                self.assertNotIn("secret-local-diagnostic", encoded)
                self.assertNotIn(str(root), encoded)
                fake.write_text("#!/bin/sh\necho 'private diagnostic'\nexit 99\n")
                failed = validate.native_smoke(fake, ndk)
                self.assertEqual(failed["status"], "FAIL")
                self.assertEqual(failed["completed"], [])
                self.assertNotIn("private diagnostic", json.dumps(failed))

    @unittest.skipIf(os.name == "nt", "POSIX script fixtures are Linux-only")
    def test_profile_sentinel_pass_and_detects_mutation(self):
        with tempfile.TemporaryDirectory(prefix="BF fixture é ") as workspace:
            root = Path(workspace)
            fake = root / "fake-gui"
            for label, mutation, expected in (
                ("good", "", "PASS"),
                ("bad", 'echo changed > "$LOCALAPPDATA/BuildForgeARM64/profiles.json"\n', "FAIL"),
            ):
                current = root / label
                current.mkdir()
                fake.write_text("#!/bin/sh\n" + mutation + "echo 'BuildForge GUI smoke test passed.'\n")
                fake.chmod(0o700)
                with patch.dict(os.environ, validate.worker_environment(current), clear=True):
                    evidence = validate.frozen_gui(fake, current)
                self.assertEqual(evidence["status"], expected)

    def test_isolated_worker_timeout_is_sanitized(self):
        with tempfile.TemporaryDirectory(prefix="BF fixture é ") as workspace:
            root = Path(workspace)
            evidence = validate.isolated_gate("integrity", root,
                                              validate.worker_environment(root), timeout=0)
            self.assertEqual(evidence, {"status": "FAIL",
                                       "reason": "Validation worker exceeded its time limit"})
