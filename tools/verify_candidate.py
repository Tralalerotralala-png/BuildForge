"""Read-only candidate integrity gate for an exclusively owned staging directory.

The caller MUST obtain the expected manifest digest and commit independently
from a trusted release channel. Checksums shipped with the candidate alone are
not authentication. This module never downloads, extracts, installs or executes
anything. Authentic release selection, downgrade prevention, locking and atomic
replacement remain updater responsibilities.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import stat
import shutil
import tempfile
import time
import zipfile


_DIGEST = re.compile(r"[0-9a-f]{64}")
_COMMIT = re.compile(r"[0-9a-f]{40}")
_ARTIFACT = re.compile(r"BuildForge-([0-9][A-Za-z0-9.-]{0,79})-(Setup\.exe|Windows-portable\.zip)")
_MAX_MANIFEST = 1024 * 1024


def _identity(s):
    return s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns


def _plain(path, directory=False):
    s = path.lstat()
    good = stat.S_ISDIR(s.st_mode) if directory else stat.S_ISREG(s.st_mode)
    if not good or getattr(s, "st_file_attributes", 0) & 0x400:
        raise ValueError("Candidate paths must be regular files and directories.")
    return s


def _read_verified(path, expected, *, limit=None, max_size=None, cancelled=None):
    before = _plain(path)
    if limit is not None and before.st_size > limit:
        raise ValueError("Candidate metadata exceeds its size limit.")
    if max_size is not None and before.st_size > max_size:
        raise ValueError("Candidate artifact exceeds its size limit.")
    h = hashlib.sha256()
    data = bytearray() if limit is not None else None
    total = 0
    with path.open("rb") as stream:
        if _identity(os.fstat(stream.fileno())) != _identity(before):
            raise ValueError("Candidate changed before verification.")
        while True:
            if cancelled is not None and cancelled.is_set():
                raise ValueError("Candidate verification cancelled.")
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > before.st_size or (limit is not None and total > limit):
                raise ValueError("Candidate grew during verification.")
            h.update(chunk)
            if data is not None:
                data.extend(chunk)
        if _identity(os.fstat(stream.fileno())) != _identity(before):
            raise ValueError("Candidate changed during verification.")
    if total != before.st_size or _identity(_plain(path)) != _identity(before):
        raise ValueError("Candidate changed during verification.")
    if not hmac.compare_digest(h.hexdigest(), expected):
        raise ValueError("Candidate checksum mismatch.")
    return bytes(data) if data is not None else total


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate candidate metadata key.")
        result[key] = value
    return result


def verify_candidate(directory, *, expected_manifest_sha256, expected_commit, cancelled=None,
                     max_artifact_bytes=None):
    """Verify the two current candidate artifacts against trusted metadata.

    Results describe only bytes checked in this invocation, not an authorization
    to execute them later. The updater must preserve exclusive staging ownership
    and authenticate its release source; do not use this on shared mutable paths.
    """
    if not isinstance(expected_manifest_sha256, str) or not _DIGEST.fullmatch(expected_manifest_sha256):
        raise ValueError("An independently trusted manifest SHA-256 is required.")
    if not isinstance(expected_commit, str) or not _COMMIT.fullmatch(expected_commit):
        raise ValueError("An exact trusted commit SHA is required.")
    if max_artifact_bytes is not None and (type(max_artifact_bytes) is not int or max_artifact_bytes <= 0):
        raise ValueError("Invalid candidate artifact size budget.")
    directory = Path(os.path.abspath(directory))
    for parent in (directory, *directory.parents):
        _plain(parent, directory=True)
    root_before = _identity(_plain(directory, directory=True))
    raw = _read_verified(directory / "build-metadata.json", expected_manifest_sha256,
                         limit=_MAX_MANIFEST, cancelled=cancelled)
    metadata = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique)
    if not isinstance(metadata, dict) or metadata.get("commit") != expected_commit:
        raise ValueError("Candidate commit does not match the trusted release.")
    artifacts = metadata.get("artifacts")
    if not isinstance(artifacts, dict) or len(artifacts) != 2:
        raise ValueError("Expected exactly one portable ZIP and one installer.")
    versions, kinds, sizes = set(), set(), {}
    for name, digest in artifacts.items():
        match = _ARTIFACT.fullmatch(name)
        if not match or not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
            raise ValueError("Invalid candidate artifact identity.")
        versions.add(match[1])
        kinds.add(match[2])
    if len(versions) != 1 or kinds != {"Setup.exe", "Windows-portable.zip"}:
        raise ValueError("Candidate artifact versions or types do not match.")
    for name in sorted(artifacts):
        sizes[name] = _read_verified(directory / name, artifacts[name], max_size=max_artifact_bytes,
                                     cancelled=cancelled)
        if sizes[name] <= 0:
            raise ValueError("Empty candidate artifact.")
    if _identity(_plain(directory, directory=True)) != root_before:
        raise ValueError("Candidate directory changed during verification.")
    return {"commit": expected_commit, "manifest_sha256": expected_manifest_sha256,
            "version": next(iter(versions)), "artifacts": dict(artifacts), "bytes": sizes,
            "status": "integrity verified; installation not authorized"}


# Domain separation prevents accepting a signature made for another protocol.
_SIGNED_DOMAIN = b"BuildForge release metadata v1\x00"
_MAX_ARCHIVE = 256 * 1024 * 1024
_MAX_EXPANDED = 1024 * 1024 * 1024
_MAX_FILES = 10000
_RESERVED = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"} | {
    prefix + str(n) for prefix in ("COM", "LPT") for n in range(10)
}


def verify_signed_candidate(directory, *, signed_manifest, signature, trusted_public_key,
                            trusted_key_sha256, expected_commit, expected_version,
                            cancelled=None):
    """Verify detached Ed25519 signature and the exact local candidate bytes.

    The key and its SHA-256 pin MUST come from a separately provisioned trust
    store, never the candidate or its discovery response. Signatures cover
    _SIGNED_DOMAIN followed by the exact manifest bytes. The caller selects an
    approved commit/version and enforces anti-rollback policy. No unsigned
    fallback: an unavailable cryptography backend is a hard failure.
    """
    if not isinstance(signed_manifest, bytes) or not 0 < len(signed_manifest) <= _MAX_MANIFEST:
        raise ValueError("Invalid signed manifest size or type.")
    if not isinstance(signature, bytes) or len(signature) != 64:
        raise ValueError("An exact detached Ed25519 signature is required.")
    if not isinstance(trusted_public_key, bytes) or len(trusted_public_key) != 32:
        raise ValueError("A provisioned raw Ed25519 public key is required.")
    if not isinstance(trusted_key_sha256, str) or not _DIGEST.fullmatch(trusted_key_sha256):
        raise ValueError("An independently provisioned key pin is required.")
    if not hmac.compare_digest(hashlib.sha256(trusted_public_key).hexdigest(), trusted_key_sha256):
        raise ValueError("Trusted release key pin mismatch.")
    if not isinstance(expected_version, str) or not re.fullmatch(r"[0-9][A-Za-z0-9.-]{0,79}", expected_version):
        raise ValueError("An approved release version is required.")
    if cancelled is not None and cancelled.is_set():
        raise ValueError("Candidate verification cancelled.")
    try:
        from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError as exc:
        raise RuntimeError("Trusted signature verification backend unavailable.") from exc
    try:
        Ed25519PublicKey.from_public_bytes(trusted_public_key).verify(
            signature, _SIGNED_DOMAIN + signed_manifest)
    except (InvalidSignature, UnsupportedAlgorithm) as exc:
        raise ValueError("Release signature verification failed.") from exc
    result = verify_candidate(directory, expected_manifest_sha256=hashlib.sha256(signed_manifest).hexdigest(),
                              expected_commit=expected_commit, cancelled=cancelled,
                              max_artifact_bytes=_MAX_ARCHIVE)
    if result["version"] != expected_version:
        raise ValueError("Candidate version does not match the approved release.")
    result["status"] = "signature and integrity verified; installation not authorized"
    return result


def _portable_members(archive, max_files, max_bytes):
    members = archive.infolist()
    if not 0 < len(members) <= max_files:
        raise ValueError("Portable archive file count exceeds policy.")
    seen, total, files = {}, 0, []
    for member in members:
        parts = tuple(member.filename.split("/"))
        if (member.orig_filename != member.filename or len(parts) < 2 or parts[0] != "BuildForge"
                or len(member.filename) > 240):
            raise ValueError("Unexpected portable archive path.")
        for part in parts:
            if (not part or part in {".", ".."} or part.startswith(" ") or part.endswith((" ", "."))
                    or any(ord(c) < 32 or ord(c) > 126 or c in '<>:"\\|?*~' for c in part)
                    or part.split(".")[0].rstrip(" ").upper() in _RESERVED):
                raise ValueError("Unsafe Windows archive path.")
        # package_candidate emits regular files only, no directory entries.
        if (member.create_system != 3 or not stat.S_ISREG(member.external_attr >> 16)
                or member.external_attr & 0x410 or member.flag_bits & 1
                or member.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}):
            raise ValueError("Unexpected portable archive entry type.")
        for i in range(1, len(parts) + 1):
            prefix = parts[:i]
            folded = tuple(p.lower() for p in prefix)
            is_file = i == len(parts)
            if folded in seen:
                old, old_file = seen[folded]
                if old != prefix or old_file or is_file:
                    raise ValueError("Conflicting portable archive paths.")
            else:
                seen[folded] = (prefix, is_file)
        if parts == ("BuildForge", "build-metadata.json") and member.file_size > _MAX_MANIFEST:
            raise ValueError("Embedded metadata exceeds limit.")
        total += member.file_size
        if member.file_size < 0 or total > max_bytes:
            raise ValueError("Portable archive exceeds extraction budget.")
        files.append((member, parts))
    for required in ("BuildForge.exe", "build-metadata.json"):
        if not any(parts == ("BuildForge", required) and member.file_size > 0
                   for member, parts in files):
            raise ValueError("Required portable file is missing or empty.")
    return files, total


def stage_signed_portable(directory, stage_parent, *, signed_manifest, signature,
                          trusted_public_key, trusted_key_sha256, expected_commit,
                          expected_version, cancelled=None, max_files=_MAX_FILES,
                          max_bytes=_MAX_EXPANDED):
    """Stage a verified portable ZIP into a new, exclusively owned directory.

    Both input and stage parent must be private, stable directories controlled
    by the caller, separate from any installation. No activation, execution,
    replacement, process termination or writes to an existing installation.
    Success returns the new stage, which is not permission to install it later.
    """
    if type(max_files) is not int or not 0 < max_files <= _MAX_FILES:
        raise ValueError("Invalid portable file count budget.")
    if type(max_bytes) is not int or not 0 < max_bytes <= _MAX_EXPANDED:
        raise ValueError("Invalid portable extraction budget.")
    start = time.monotonic()
    verified = verify_signed_candidate(directory, signed_manifest=signed_manifest, signature=signature,
                                       trusted_public_key=trusted_public_key, trusted_key_sha256=trusted_key_sha256,
                                       expected_commit=expected_commit, expected_version=expected_version,
                                       cancelled=cancelled)
    verified_at = time.monotonic()
    directory = Path(os.path.abspath(directory))
    stage_parent = Path(os.path.abspath(stage_parent))
    if directory == stage_parent or directory in stage_parent.parents:
        raise ValueError("Stage parent must be separate from candidate input.")
    for parent in (stage_parent, *stage_parent.parents):
        _plain(parent, directory=True)
    name = next(n for n in verified["artifacts"] if n.endswith("-Windows-portable.zip"))
    path = directory / name
    before = _plain(path)
    if before.st_size > _MAX_ARCHIVE or before.st_size != verified["bytes"][name]:
        raise ValueError("Portable archive exceeds policy or changed.")
    stage = None
    try:
        # Rehash on the very same file handle used for extraction. The earlier
        # bundle verification alone is not evidence of these later-opened bytes.
        with path.open("rb") as stream:
            if _identity(os.fstat(stream.fileno())) != _identity(before):
                raise ValueError("Portable archive changed before staging.")
            digest = hashlib.sha256()
            count = 0
            while True:
                if cancelled is not None and cancelled.is_set():
                    raise ValueError("Portable staging cancelled.")
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                count += len(chunk)
                if count > before.st_size:
                    raise ValueError("Portable archive grew during staging.")
                digest.update(chunk)
            if count != before.st_size or not hmac.compare_digest(digest.hexdigest(), verified["artifacts"][name]):
                raise ValueError("Portable archive checksum mismatch.")
            if _identity(os.fstat(stream.fileno())) != _identity(before):
                raise ValueError("Portable archive changed during staging.")
            stream.seek(0)
            with zipfile.ZipFile(stream) as archive:
                files, total = _portable_members(archive, max_files, max_bytes)
                if cancelled is not None and cancelled.is_set():
                    raise ValueError("Portable staging cancelled.")
                stage = Path(tempfile.mkdtemp(prefix="buildforge-stage-", dir=stage_parent))
                expanded = 0
                for member, parts in files:
                    if cancelled is not None and cancelled.is_set():
                        raise ValueError("Portable staging cancelled.")
                    dest = stage.joinpath(*parts)
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    written = 0
                    with archive.open(member) as source, dest.open("xb") as output:
                        while True:
                            if cancelled is not None and cancelled.is_set():
                                raise ValueError("Portable staging cancelled.")
                            chunk = source.read(1024 * 1024)
                            if not chunk:
                                break
                            written += len(chunk)
                            expanded += len(chunk)
                            if written > member.file_size or expanded > max_bytes:
                                raise ValueError("Portable extraction exceeded budget.")
                            output.write(chunk)
                    if written != member.file_size:
                        raise ValueError("Portable archive size mismatch.")
                if expanded != total:
                    raise ValueError("Portable archive total size mismatch.")
            if _identity(os.fstat(stream.fileno())) != _identity(before) or _identity(_plain(path)) != _identity(before):
                raise ValueError("Portable archive changed during staging.")
        embedded = stage / "BuildForge" / "build-metadata.json"
        if _plain(embedded).st_size > _MAX_MANIFEST:
            raise ValueError("Embedded metadata exceeds limit.")
        local = json.loads(embedded.read_bytes().decode("utf-8"), object_pairs_hook=_unique)
        outer = json.loads(signed_manifest.decode("utf-8"), object_pairs_hook=_unique)
        outer.pop("artifacts")
        if local != outer:
            raise ValueError("Embedded build metadata does not match signed release.")
        if cancelled is not None and cancelled.is_set():
            raise ValueError("Portable staging cancelled.")
        return {"stage": stage, "commit": expected_commit, "version": expected_version,
                "manifest_sha256": verified["manifest_sha256"], "files": len(files), "bytes": total,
                "verify_seconds": verified_at - start, "stage_seconds": time.monotonic() - verified_at,
                "status": "staged; activation not authorized"}
    except BaseException:
        if stage is not None:
            shutil.rmtree(stage)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--manifest-sha256", required=True)
    parser.add_argument("--commit", required=True)
    args = parser.parse_args()
    try:
        result = verify_candidate(args.directory, expected_manifest_sha256=args.manifest_sha256,
                                  expected_commit=args.commit)
    except (OSError, ValueError, UnicodeError, RecursionError):
        # Do not print local paths, untrusted metadata or exception internals.
        parser.exit(1, "Candidate verification failed; nothing was installed.\n")
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
