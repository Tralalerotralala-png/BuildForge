"""Secure BuildForge updater: release discovery, trust, staging, activation, and rollback."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import shutil
import stat
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable

from cache_compat import atomic_json, digest
from tools.verify_candidate import (
    _plain,
    stage_signed_portable,
    verify_candidate,
    verify_signed_candidate,
)


_SEMVER_RE = re.compile(
    r"^v?(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)"
    r"(?:-(?P<pre>[0-9A-Za-z.-]+))?$"
)
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_DEFAULT_ALLOWED_ORIGIN = "https://api.github.com/repos/Tralalerotralala-png/BuildForge/"


class UpdateError(ValueError):
    """Raised when release discovery, trust, staging, or activation fails safely."""


def parse_version(raw: str) -> tuple[int, int, int, tuple[tuple[int, int | str], ...]]:
    """Parse strict SemVer (major.minor.patch[-prerelease]) into a comparable tuple."""
    if not isinstance(raw, str):
        raise UpdateError("Version must be a string.")
    m = _SEMVER_RE.fullmatch(raw.strip())
    if not m:
        raise UpdateError(f"Invalid semantic version: {raw!r}")
    major, minor, patch = int(m.group("major")), int(m.group("minor")), int(m.group("patch"))
    pre_raw = m.group("pre")
    if not pre_raw:
        # Release sorts higher than any prerelease of the same major.minor.patch.
        return (major, minor, patch, ((2, 0),))
    pre_parts: list[tuple[int, int | str]] = []
    for part in pre_raw.split("."):
        if not part:
            raise UpdateError(f"Invalid prerelease identifier in version: {raw!r}")
        if part.isdigit():
            if len(part) > 1 and part.startswith("0"):
                raise UpdateError(f"Leading zero in numeric prerelease part: {raw!r}")
            pre_parts.append((0, int(part)))
        else:
            pre_parts.append((1, part))
    return (major, minor, patch, ((1, 0), *pre_parts))


def compare_versions(candidate: str, current: str) -> int:
    """Return 1 if candidate > current, 0 if equal, -1 if candidate < current."""
    a, b = parse_version(candidate), parse_version(current)
    return 1 if a > b else (-1 if a < b else 0)


def check_version_upgrade(candidate: str, current: str, *, allow_downgrade: bool = False) -> None:
    """Enforce anti-rollback version policy."""
    cmp_val = compare_versions(candidate, current)
    if cmp_val == 0:
        raise UpdateError(f"Release {candidate} is already installed ({current}).")
    if cmp_val < 0 and not allow_downgrade:
        raise UpdateError(f"Blocked version downgrade from {current} to {candidate}.")


def validate_release_descriptor(
    payload: dict[str, Any],
    *,
    current_version: str,
    allowed_origin: str = _DEFAULT_ALLOWED_ORIGIN,
    allow_downgrade: bool = False,
) -> dict[str, Any]:
    """Validate discovered release metadata before any download or staging."""
    if not isinstance(payload, dict):
        raise UpdateError("Release descriptor must be a JSON object.")
    version = payload.get("version")
    commit = payload.get("commit")
    manifest_sha256 = payload.get("manifest_sha256")
    download_url = payload.get("download_url")
    if not isinstance(version, str):
        raise UpdateError("Missing release version.")
    check_version_upgrade(version, current_version, allow_downgrade=allow_downgrade)
    if not isinstance(commit, str) or not _COMMIT_RE.fullmatch(commit):
        raise UpdateError("Release descriptor lacks a valid 40-char commit SHA.")
    if not isinstance(manifest_sha256, str) or not _HEX64_RE.fullmatch(manifest_sha256):
        raise UpdateError("Release descriptor lacks a valid manifest SHA-256.")
    if not isinstance(download_url, str):
        raise UpdateError("Release descriptor lacks a download URL.")
    parsed = urllib.parse.urlsplit(download_url)
    allowed = urllib.parse.urlsplit(allowed_origin)
    if parsed.scheme != "https" or parsed.netloc.lower() != allowed.netloc.lower():
        raise UpdateError("Blocked untrusted release download origin.")
    if not parsed.path.startswith(allowed.path):
        raise UpdateError("Blocked release download path outside allowed repository.")
    return {
        "version": version.lstrip("v"),
        "commit": commit,
        "manifest_sha256": manifest_sha256,
        "download_url": download_url,
    }


def discover_release(
    endpoint_url: str,
    *,
    current_version: str,
    allowed_origin: str = _DEFAULT_ALLOWED_ORIGIN,
    opener: Callable[..., Any] | None = None,
    timeout: int = 15,
) -> dict[str, Any]:
    """Query an authenticated HTTPS release endpoint and enforce version/origin policy."""
    parsed = urllib.parse.urlsplit(endpoint_url)
    allowed = urllib.parse.urlsplit(allowed_origin)
    if parsed.scheme != "https" or parsed.netloc.lower() != allowed.netloc.lower():
        raise UpdateError("Blocked untrusted release discovery endpoint.")
    req = urllib.request.Request(
        endpoint_url,
        headers={"Accept": "application/json", "User-Agent": "BuildForge-Updater/1.0"},
    )
    fetch = opener or urllib.request.urlopen
    try:
        with fetch(req, timeout=timeout) as resp:
            raw = resp.read(512 * 1024)
    except (OSError, urllib.error.URLError) as exc:
        raise UpdateError(f"Release discovery failed: {exc}") from exc
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise UpdateError("Invalid release discovery JSON.") from exc
    return validate_release_descriptor(
        data, current_version=current_version, allowed_origin=allowed_origin
    )


def _ensure_safe_dir_tree(path: Path) -> None:
    resolved = Path(os.path.abspath(path))
    for parent in (resolved, *resolved.parents):
        if parent.exists():
            _plain(parent, directory=True)


def _check_no_active_build_locks(watch_dirs: list[Path] | None) -> None:
    for d in watch_dirs or []:
        dp = Path(d)
        for lock_name in (".compile.lock", ".pipeline.lock"):
            if (dp / lock_name).exists():
                raise UpdateError(f"Active build lock present in {dp}; refusing update activation.")


def _inventory_directory(root: Path) -> dict[str, str]:
    """Return {posix_relpath: sha256} for regular files inside root; reject symlinks."""
    _plain(root, directory=True)
    files: dict[str, str] = {}
    for item in sorted(root.rglob("*")):
        st = item.lstat()
        if getattr(st, "st_file_attributes", 0) & 0x400 or stat.S_ISLNK(st.st_mode):
            raise UpdateError(f"Unexpected symlink/reparse point in update tree: {item}")
        if stat.S_ISDIR(st.st_mode):
            continue
        if not stat.S_ISREG(st.st_mode):
            raise UpdateError(f"Non-regular file in update tree: {item}")
        rel = item.relative_to(root).as_posix()
        if ".." in Path(rel).parts:
            raise UpdateError(f"Unsafe relative path: {rel}")
        files[rel] = digest(item)
    return files


def _copy_verified_tree(src: Path, dst: Path, expected_hashes: dict[str, str]) -> None:
    """Copy exact verified files from src into a fresh or cleared dst and verify hashes."""
    dst.mkdir(parents=True, exist_ok=True)
    for rel, expected_sha in expected_hashes.items():
        s = src / rel
        d = dst / rel
        d.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(s, d)
        if not hmac.compare_digest(digest(d), expected_sha):
            raise UpdateError(f"Hash mismatch while copying {rel}")


def _clear_directory_contents(root: Path) -> None:
    if not root.exists():
        return
    for entry in sorted(root.iterdir(), key=lambda p: len(p.parts), reverse=True):
        st = entry.lstat()
        if getattr(st, "st_file_attributes", 0) & 0x400 or stat.S_ISLNK(st.st_mode):
            raise UpdateError(f"Refusing to clear symlink/reparse point: {entry}")
        if stat.S_ISDIR(st.st_mode):
            shutil.rmtree(entry)
        else:
            entry.unlink()


class SecureUpdater:
    """Transactional updater with trusted staging, active-build guard, and rollback."""

    def __init__(self, install_dir: Path, state_root: Path) -> None:
        self.install_dir = Path(os.path.abspath(install_dir))
        self.state_root = Path(os.path.abspath(state_root))
        _ensure_safe_dir_tree(self.install_dir)
        _ensure_safe_dir_tree(self.state_root)
        self.state_root.mkdir(parents=True, exist_ok=True)
        self.journal_path = self.state_root / "update_state.json"
        self.deferred_path = self.state_root / "pending_deferred_activation.json"
        self.rollback_root = self.state_root / "rollback"
        self.stage_root = self.state_root / "staging"
        self.lock_path = self.state_root / ".updater.lock"

    def _acquire_lock(self) -> bytes:
        token = json.dumps({"pid": os.getpid(), "time": time.time()}).encode("utf-8")
        fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(fd, "wb") as f:
            f.write(token)
            f.flush()
            os.fsync(f.fileno())
        return token

    def _release_lock(self, token: bytes) -> None:
        try:
            if self.lock_path.read_bytes() == token:
                self.lock_path.unlink()
        except FileNotFoundError:
            pass

    def _read_journal(self) -> dict[str, Any]:
        if not self.journal_path.is_file():
            return {"schema": 1, "phase": "idle"}
        return json.loads(self.journal_path.read_text(encoding="utf-8"))

    def stage_candidate(
        self,
        candidate_dir: Path,
        *,
        current_version: str,
        expected_manifest_sha256: str,
        expected_commit: str,
        signed_manifest: bytes | None = None,
        signature: bytes | None = None,
        trusted_public_key: bytes | None = None,
        trusted_key_sha256: str | None = None,
        expected_version: str | None = None,
        allow_downgrade: bool = False,
    ) -> dict[str, Any]:
        """Verify candidate integrity (and optional Ed25519 signature) into isolated staging."""
        self.stage_root.mkdir(parents=True, exist_ok=True)
        if signed_manifest is not None:
            if not (signature and trusted_public_key and trusted_key_sha256 and expected_version):
                raise UpdateError("Signed staging requires signature, public key, pin, and expected version.")
            check_version_upgrade(expected_version, current_version, allow_downgrade=allow_downgrade)
            staged = stage_signed_portable(
                candidate_dir,
                self.stage_root,
                signed_manifest=signed_manifest,
                signature=signature,
                trusted_public_key=trusted_public_key,
                trusted_key_sha256=trusted_key_sha256,
                expected_commit=expected_commit,
                expected_version=expected_version,
            )
            payload_dir = Path(staged["staged_directory"])
            version = staged["version"]
        else:
            verified = verify_candidate(
                candidate_dir,
                expected_manifest_sha256=expected_manifest_sha256,
                expected_commit=expected_commit,
            )
            version = verified["version"]
            check_version_upgrade(version, current_version, allow_downgrade=allow_downgrade)
            import zipfile

            zip_name = next(n for n in verified["artifacts"] if n.endswith("-Windows-portable.zip"))
            zip_path = Path(candidate_dir) / zip_name
            stage = Path(tempfile.mkdtemp(prefix="bf-stage-", dir=self.stage_root))
            with zipfile.ZipFile(zip_path, "r") as zf:
                for member in zf.infolist():
                    parts = tuple(member.filename.split("/"))
                    if len(parts) < 2 or parts[0] != "BuildForge" or any(p in ("", ".", "..") for p in parts):
                        shutil.rmtree(stage, ignore_errors=True)
                        raise UpdateError("Unsafe archive member in portable candidate.")
                    dest = stage.joinpath(*parts)
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(member, "r") as src, dest.open("wb") as out:
                        shutil.copyfileobj(src, out, 1024 * 1024)
            payload_dir = stage / "BuildForge"

        staged_files = _inventory_directory(payload_dir)
        if "BuildForge.exe" not in staged_files:
            raise UpdateError("Staged payload missing BuildForge.exe.")
        return {
            "version": version,
            "commit": expected_commit,
            "payload_dir": str(payload_dir),
            "files": staged_files,
        }

    def activate(
        self,
        staged: dict[str, Any],
        *,
        active_build_dirs: list[Path] | None = None,
        target_running: bool = False,
        inject_failure_phase: str | None = None,
    ) -> dict[str, Any]:
        """Atomically back up current install, activate verified stage, verify, or roll back."""
        _check_no_active_build_locks(active_build_dirs)
        payload_dir = Path(staged["payload_dir"])
        staged_files = dict(staged["files"])
        # Re-verify staged files before touching the live installation.
        if _inventory_directory(payload_dir) != staged_files:
            raise UpdateError("Staged payload changed before activation.")

        if target_running:
            handoff = {
                "schema": 1,
                "status": "deferred_target_running",
                "target_version": staged["version"],
                "commit": staged["commit"],
                "payload_dir": str(payload_dir),
                "files": staged_files,
            }
            atomic_json(self.deferred_path, handoff)
            return handoff

        token = self._acquire_lock()
        backup_dir: Path | None = None
        backup_files: dict[str, str] = {}
        try:
            self.rollback_root.mkdir(parents=True, exist_ok=True)
            backup_dir = Path(tempfile.mkdtemp(prefix="backup-", dir=self.rollback_root))
            if self.install_dir.exists():
                backup_files = _inventory_directory(self.install_dir)
            else:
                self.install_dir.mkdir(parents=True, exist_ok=True)

            atomic_json(
                self.journal_path,
                {
                    "schema": 1,
                    "phase": "backing_up",
                    "target_version": staged["version"],
                    "commit": staged["commit"],
                    "payload_dir": str(payload_dir),
                    "staged_files": staged_files,
                    "backup_dir": str(backup_dir),
                    "backup_files": backup_files,
                },
            )
            if backup_files:
                _copy_verified_tree(self.install_dir, backup_dir, backup_files)
            if inject_failure_phase == "backing_up":
                raise RuntimeError("Simulated interruption during backup.")

            atomic_json(
                self.journal_path,
                {
                    "schema": 1,
                    "phase": "activating",
                    "target_version": staged["version"],
                    "commit": staged["commit"],
                    "payload_dir": str(payload_dir),
                    "staged_files": staged_files,
                    "backup_dir": str(backup_dir),
                    "backup_files": backup_files,
                },
            )
            _clear_directory_contents(self.install_dir)
            if inject_failure_phase == "activating":
                raise RuntimeError("Simulated crash mid-activation.")
            _copy_verified_tree(payload_dir, self.install_dir, staged_files)

            atomic_json(
                self.journal_path,
                {
                    "schema": 1,
                    "phase": "verifying",
                    "target_version": staged["version"],
                    "commit": staged["commit"],
                    "payload_dir": str(payload_dir),
                    "staged_files": staged_files,
                    "backup_dir": str(backup_dir),
                    "backup_files": backup_files,
                },
            )
            if inject_failure_phase == "verifying":
                raise UpdateError("Simulated post-activation verification failure.")
            if _inventory_directory(self.install_dir) != staged_files:
                raise UpdateError("Activated installation integrity mismatch.")

            result = {
                "schema": 1,
                "phase": "committed",
                "target_version": staged["version"],
                "commit": staged["commit"],
                "backup_dir": str(backup_dir),
                "backup_files": backup_files,
                "installed_files": staged_files,
            }
            atomic_json(self.journal_path, result)
            self.deferred_path.unlink(missing_ok=True)
            return result
        except Exception as exc:
            # Roll back immediately from the verified backup on any activation failure.
            if backup_dir is not None and backup_dir.is_dir():
                self._restore_from_backup(backup_dir, backup_files, reason=str(exc))
            raise
        finally:
            self._release_lock(token)

    def _restore_from_backup(
        self, backup_dir: Path, backup_files: dict[str, str], *, reason: str
    ) -> dict[str, Any]:
        if _inventory_directory(backup_dir) != backup_files:
            raise UpdateError("Rollback backup failed integrity check; manual intervention required.")
        self.install_dir.mkdir(parents=True, exist_ok=True)
        _clear_directory_contents(self.install_dir)
        if backup_files:
            _copy_verified_tree(backup_dir, self.install_dir, backup_files)
        if _inventory_directory(self.install_dir) != backup_files:
            raise UpdateError("Restored installation failed rollback verification.")
        state = {
            "schema": 1,
            "phase": "rolled_back",
            "reason": reason,
            "backup_dir": str(backup_dir),
            "backup_files": backup_files,
            "restored_files": backup_files,
        }
        atomic_json(self.journal_path, state)
        return state

    def recover_interrupted_update(self) -> dict[str, Any]:
        """Detect an incomplete update journal on startup and roll back safely."""
        journal = self._read_journal()
        phase = journal.get("phase", "idle")
        if phase in ("idle", "committed", "rolled_back"):
            return {"recovered": False, "phase": phase}
        backup_dir = Path(journal["backup_dir"])
        backup_files = dict(journal.get("backup_files", {}))
        restored = self._restore_from_backup(
            backup_dir, backup_files, reason=f"Recovered interrupted update from phase {phase}"
        )
        return {"recovered": True, **restored}

    def rollback_last(self) -> dict[str, Any]:
        """Explicitly roll back the most recent committed update to its verified backup."""
        journal = self._read_journal()
        if journal.get("phase") != "committed" or not journal.get("backup_dir"):
            raise UpdateError("No committed update backup available for rollback.")
        return self._restore_from_backup(
            Path(journal["backup_dir"]),
            dict(journal.get("backup_files", {})),
            reason="Explicit operator rollback",
        )
