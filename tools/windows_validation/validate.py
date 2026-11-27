"""Run the pinned BuildForge validation gates and write an allowlisted report."""
import contextlib
import ctypes
import datetime
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
import zipfile

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
# The launcher uses isolated Python mode. Resolve imports from this checkout.
sys.path.insert(0, str(REPO_ROOT))

from pipeline import detect_ndk_paths
from tests import windows_ndk_smoke
from tools.verify_candidate import (
    _MAX_EXPANDED, _MAX_FILES, _portable_members, _unique, verify_candidate,
)
from tools.windows_validation.windows_job import child_job

RUN = "45820654560"
ARTIFACT = 15117572427
COMMIT = "08e769cf514415c1cd90e5d728f6b5dea192aa82"
ARCHIVE_HASH = "175e833fc11797f56edd549573b8d34739eca99e5a1793a5cabd828fb624435f"
MANIFEST_HASH = "8c44c38bb6a286c81da4685826ccf66adb3fc301dcf6e10367b9d9aa9ebd0d52"
VERSION = "0.7.0-rc.1"
SETUP = f"BuildForge-{VERSION}-Setup.exe"
PORTABLE = f"BuildForge-{VERSION}-Windows-portable.zip"
BUNDLE = "BuildForge-Windows-release-candidate.zip"
NATIVE_MARKERS = (
    "aarch64 compiled OK", "aarch64 cached OK",
    "aarch64 dependency invalidation / failed link / cancel / resume OK",
    "x86_64 compiled OK", "x86_64 cached OK",
    "x86_64 dependency invalidation / failed link / cancel / resume OK",
)
GATE_TIMEOUTS = {
    "integrity": 90, "verifier_fixtures": 120, "signed_staging_fixtures": 120,
    "cancellation_regressions": 120, "recovery_regressions": 120,
    "frozen_gui": 75, "native_ndk": 3300,
}
MANUAL = {
    "clean_machine": "Confirm a clean Windows baseline and a true standard-user account.",
    "installer_install_launch_uninstall": "Run installed-app acceptance and uninstall separately.",
    "existing_profile_preservation": "Verify a real pre-existing profile survives install and uninstall.",
    "authorized_project": "Build and link a representative authorized project.",
    "frozen_mid_compile_cancellation": "Cancel an in-flight compile from the frozen UI on Windows.",
    "display_scaling_keyboard": "Exercise display scaling and keyboard navigation.",
    "android_runtime": "Validate runtime loading and behavior on AArch64 and x86-64 Android.",
    "performance_and_scope": "Review race boundaries, telemetry, performance and deferred integrations.",
    "licenses_and_branding": "Audit frozen runtime notices and branding rights.",
    "signing_and_publication": "Maintainer reviews evidence, verifies signatures and approves publication.",
}


def result(status, **evidence):
    return {"status": status, **evidence}


def stage_candidate(root, bundle=HERE / BUNDLE):
    """Verify the pinned unsigned CI bytes before extracting executable content."""
    incoming = root / "candidate"
    incoming.mkdir()
    with bundle.open("rb") as stream:
        digest = hashlib.sha256()
        length = 0
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            length += len(chunk)
            if length > 64 * 1024 * 1024:
                raise ValueError("Candidate ZIP exceeds budget")
            digest.update(chunk)
        if digest.hexdigest() != ARCHIVE_HASH:
            raise ValueError("Candidate ZIP digest mismatch")
        stream.seek(0)
        with zipfile.ZipFile(stream) as outer:
            expected = {"build-metadata.json", "SHA256SUMS.txt", PORTABLE, SETUP}
            if len(outer.infolist()) != len(expected) or set(outer.namelist()) != expected:
                raise ValueError("Unexpected CI ZIP entries")
            for name in expected:
                with outer.open(name) as source, (incoming / name).open("xb") as output:
                    shutil.copyfileobj(source, output)
    verified = verify_candidate(incoming, expected_manifest_sha256=MANIFEST_HASH,
                                expected_commit=COMMIT, max_artifact_bytes=64 * 1024 * 1024)
    metadata = json.loads((incoming / "build-metadata.json").read_bytes(), object_pairs_hook=_unique)
    if metadata.get("workflow_run") != RUN or verified["version"] != VERSION:
        raise ValueError("Unexpected candidate run or version")
    expected_sums = {f"{value}  {name}" for name, value in verified["artifacts"].items()}
    expected_sums.add(f"{MANIFEST_HASH}  build-metadata.json")
    sums = (incoming / "SHA256SUMS.txt").read_text(encoding="ascii").splitlines()
    if len(sums) != len(expected_sums) or set(sums) != expected_sums:
        raise ValueError("CI checksum list mismatch")

    # Reuse the upstream Windows ZIP path and size policy. The files here are
    # the exact digest-pinned CI output in a private workspace.
    stage = root / "stage"
    stage.mkdir()
    with zipfile.ZipFile(incoming / PORTABLE) as archive:
        members, expanded = _portable_members(archive, _MAX_FILES, _MAX_EXPANDED)
        for member, parts in members:
            destination = stage.joinpath(*parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member) as source, destination.open("xb") as output:
                shutil.copyfileobj(source, output)
            if destination.stat().st_size != member.file_size:
                raise ValueError("Portable entry size mismatch")
    embedded = json.loads((stage / "BuildForge" / "build-metadata.json").read_bytes(),
                          object_pairs_hook=_unique)
    del metadata["artifacts"]
    if embedded != metadata:
        raise ValueError("Embedded candidate metadata mismatch")
    (incoming / SETUP).unlink()  # Only the temporary copy used for hash verification.
    return result("PASS", trust="pinned GitHub Actions ZIP and manifest", signed=False,
                  archive_files=len(members), expanded_bytes=expanded,
                  installer_hash=verified["artifacts"][SETUP],
                  portable_hash=verified["artifacts"][PORTABLE])


def regression(*names):
    suite = unittest.defaultTestLoader.loadTestsFromNames(names)
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        outcome = unittest.TextTestRunner(stream=io.StringIO()).run(suite)
    skipped = len(outcome.skipped)
    failed = len(outcome.failures) + len(outcome.errors) + len(outcome.unexpectedSuccesses)
    executed = outcome.testsRun - skipped
    return result("FAIL" if failed else "PASS" if executed else "SKIP",
                  passed=executed - failed, failed=failed, skipped=skipped)


def ndk_release(ndk):
    properties = (ndk / "source.properties").read_text(encoding="utf-8")
    match = re.search(r"^Pkg\.Revision\s*=\s*([0-9]+(?:\.[0-9]+){1,3})\s*$",
                      properties, re.MULTILINE)
    return match[1] if match else None


def native_smoke(executable, ndk):
    revision = ndk_release(ndk)
    lld = ndk / "toolchains/llvm/prebuilt/windows-x86_64/bin/ld.lld.exe"
    version = subprocess.check_output([str(lld), "--version"], timeout=20,
                                      stderr=subprocess.STDOUT).decode("utf-8", "replace")
    match = re.search(r"\bLLD ([0-9]+(?:\.[0-9]+){1,3})\b", version)
    lld_version = match[1] if match else None
    completed = subprocess.run([sys.executable, "-I", "-B",
                                str(REPO_ROOT / "tests/windows_ndk_smoke.py"), str(executable)],
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               env={**os.environ, "ANDROID_NDK_HOME": str(ndk)})
    # The subprocess can print paths and compiler diagnostics. Persist only
    # exact, prescribed success markers from the upstream smoke test.
    lines = set(completed.stdout.decode("utf-8", "replace").splitlines())
    milestones = [marker for marker in NATIVE_MARKERS if marker in lines]
    return result("PASS" if completed.returncode == 0 and milestones == list(NATIVE_MARKERS) else "FAIL",
                  completed=milestones, ndk=revision, lld=lld_version, android_api=24,
                  path_exercise="temporary path contains spaces and non-ASCII characters",
                  cancel_scope="frozen engine pre-admission cancellation and resume")


def choose_ndk():
    try:
        return windows_ndk_smoke.ndk_path()
    except RuntimeError:
        for candidate in detect_ndk_paths():
            bin_directory = candidate / "toolchains/llvm/prebuilt/windows-x86_64/bin"
            if (bin_directory / "clang++.exe").is_file():
                return candidate.resolve()
    return None


def frozen_gui(executable, root):
    # Never touch the actual profile. The public GUI smoke path itself uses a
    # disposable HOME. This additional ambient-profile sentinel checks isolation.
    profile = root / "local" / "BuildForgeARM64"
    profile.mkdir()
    sentinel = profile / "profiles.json"
    fixture = b'{"synthetic-profile":{"validation":true}}\n'
    sentinel.write_bytes(fixture)
    completed = subprocess.run([str(executable), "--gui-smoke-test"],
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    preserved = list(profile.iterdir()) == [sentinel] and sentinel.read_bytes() == fixture
    started = completed.returncode == 0 and b"BuildForge GUI smoke test passed." in completed.stdout
    return result("PASS" if preserved and started else "FAIL",
                  gui_initialized=started, synthetic_ambient_profile_preserved=preserved)


def run_worker(gate, root, ndk):
    executable = root / "stage" / "BuildForge" / "BuildForge.exe"
    if gate == "integrity":
        return stage_candidate(root)
    if gate == "native_ndk":
        return native_smoke(executable, ndk)
    if gate == "frozen_gui":
        return frozen_gui(executable, root)
    if gate == "verifier_fixtures":
        return regression("tests.test_verify_candidate.VerifyCandidateTests",
                          "tests.test_verify_candidate.SignatureBackendPolicyTests")
    if gate == "signed_staging_fixtures":
        answer = regression("tests.test_verify_candidate.SignedCandidateTests")
        answer["scope"] = "ephemeral fixture keys and staged portable bytes only"
        if answer["status"] == "SKIP":
            answer["reason"] = "Optional cryptography backend unavailable"
        return answer
    if gate == "cancellation_regressions":
        group = "tests.test_live_control.LiveTests."
        return regression(*(group + test for test in (
            "test_cancel_latches_before_worker_spawn", "test_cancel_serializes_with_process_creation",
            "test_registry_terminate_all_owned_process", "test_selected_cancel_blocks_delayed_unit_only",
            "test_selected_cancel_leaves_other_worker_running")))
    if gate == "recovery_regressions":
        group = "tests.test_pipeline.PipelineTests."
        return regression(*(group + test for test in (
            "test_resumable_pipeline_reuses_cache_and_recovers_interrupted_stage",
            "test_pipeline_stop_event_and_git_checkout_guard",
            "test_throttled_state_flush_persists_all_items_on_completion_and_failure")))
    raise ValueError("Unknown gate")


def worker_environment(root):
    directories = {"TMP": "temp", "TEMP": "temp", "TMPDIR": "temp",
                   "LOCALAPPDATA": "local", "APPDATA": "roaming",
                   "HOME": "home", "USERPROFILE": "home", "XDG_CACHE_HOME": "cache"}
    environment = {key: value for key, value in os.environ.items()
                   if not key.upper().startswith("PYTHON")}
    environment.update(PYTHONNOUSERSITE="1", PYTHONDONTWRITEBYTECODE="1")
    for key, name in directories.items():
        path = root / name
        path.mkdir(exist_ok=True)
        environment[key] = str(path)
    return environment


def isolated_gate(gate, root, environment, ndk=None, timeout=None):
    command = [sys.executable, "-I", "-B", str(Path(__file__).resolve()),
               "--worker", gate, str(root)]
    if ndk is not None:
        command.append(str(ndk))
    if timeout is None:
        timeout = GATE_TIMEOUTS[gate]
    process = None
    try:
        with child_job() as attach:
            process = subprocess.Popen(command, cwd=root, env=environment,
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE)
            # Worker waits for this byte before starting descendant processes.
            attach(process)
            stdout, _ = process.communicate(b"1", timeout=timeout)
        if process.returncode == 0:
            return json.loads(stdout)
        return result("FAIL", reason="Validation worker exited unexpectedly")
    except subprocess.TimeoutExpired:
        return result("FAIL", reason="Validation worker exceeded its time limit")
    except (OSError, ValueError):
        return result("FAIL", reason="Could not start or read the isolated validation worker")
    finally:
        if process is not None:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)
            for pipe in (process.stdin, process.stdout, process.stderr):
                pipe.close()


def report_for(eligible):
    return {
        "schema": 1, "generated_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "candidate": {"version": VERSION, "workflow_run": RUN, "artifact_id": ARTIFACT,
                      "commit": COMMIT, "ci_archive_sha256": ARCHIVE_HASH},
        "hosted_ci": {
            "status": "PASS",
            "evidence": "GitHub-hosted jobs for the pinned run, recorded independently of this machine",
            "windows_x64": ["regressions", "portable packaging", "native NDK smoke",
                            "installer smoke", "candidate packaging"],
        },
        "environment": {"platform": "Windows" if os.name == "nt" else "non-Windows",
                        "windows_build": sys.getwindowsversion().build if os.name == "nt" else None,
                        "python": ".".join(map(str, sys.version_info[:3])),
                        "non_elevated_windows_token": eligible, "clean_machine_verified": False},
        "gates": {},
        "manual_release_gates": {key: result("MANUAL", requirement=value) for key, value in MANUAL.items()},
    }


def write_report(report, directory):
    name = "BuildForge-validation-" + uuid.uuid4().hex[:10] + ".json"
    path = directory / name
    with path.open("x", encoding="utf-8") as destination:
        json.dump(report, destination, ensure_ascii=True, indent=2)
        destination.write("\n")
    return path


def main():
    eligible = False
    if os.name == "nt":
        try:
            eligible = ctypes.windll.shell32.IsUserAnAdmin() == 0
        except OSError:
            pass
    report = report_for(eligible)
    gates = report["gates"]
    try:
        with tempfile.TemporaryDirectory(prefix="BF validation \u00e9 ") as workspace:
            root = Path(workspace).resolve()
            environment = worker_environment(root)
            for gate in GATE_TIMEOUTS:
                print("Running", gate, flush=True)
                if gate in {"frozen_gui", "native_ndk"}:
                    if gates["integrity"]["status"] != "PASS":
                        gates[gate] = result("BLOCKED", reason="Candidate integrity gate failed")
                        continue
                    if not eligible:
                        gates[gate] = result("BLOCKED", reason="Run from a non-elevated Windows session")
                        continue
                ndk = None
                if gate == "native_ndk":
                    try:
                        ndk = choose_ndk()
                    except OSError:
                        gates[gate] = result("BLOCKED", reason="Could not inspect Android NDK locations")
                        continue
                    if ndk is None:
                        gates[gate] = result("BLOCKED", reason="Installed Windows Android NDK not found")
                        continue
                gates[gate] = isolated_gate(gate, root, environment, ndk)
        report["temporary_workspace_cleanup"] = "PASS"
    except KeyboardInterrupt:
        report["run_interrupted"] = True
    except OSError:
        report["workspace_error"] = True
    for gate in GATE_TIMEOUTS:
        gates.setdefault(gate, result("BLOCKED", reason="Validation stopped before this gate"))
    required = set(GATE_TIMEOUTS) - {"signed_staging_fixtures"}
    if any(item["status"] == "FAIL" for item in gates.values()) or report.get("workspace_error"):
        report["automated_result"] = "FAIL"
        code = 1
    elif (report.get("run_interrupted") or any(gates[k]["status"] != "PASS" for k in required)):
        report["automated_result"] = "INCOMPLETE"
        code = 2
    else:
        report["automated_result"] = "PASS"
        code = 0
    try:
        path = write_report(report, HERE)
    except OSError:
        try:
            path = write_report(report, Path(tempfile.gettempdir()))
        except OSError:
            print("Could not save the sanitized report.")
            return 1
    print("Automated result:", report["automated_result"])
    print("Report:", path)
    return code


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "--worker":
        if sys.stdin.buffer.read(1) != b"1":
            raise SystemExit(1)
        try:
            answer = run_worker(sys.argv[2], Path(sys.argv[3]),
                                Path(sys.argv[4]) if len(sys.argv) == 5 else None)
        except (Exception, KeyboardInterrupt):
            answer = result("FAIL", reason="Gate did not complete its assertions")
        print(json.dumps(answer))
    else:
        raise SystemExit(main())
