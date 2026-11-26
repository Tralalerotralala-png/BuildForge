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


def _read_verified(path, expected, *, limit=None, cancelled=None):
    before = _plain(path)
    if limit is not None and before.st_size > limit:
        raise ValueError("Candidate metadata exceeds its size limit.")
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


def verify_candidate(directory, *, expected_manifest_sha256, expected_commit, cancelled=None):
    """Verify the two current candidate artifacts against trusted metadata.

    Results describe only bytes checked in this invocation, not an authorization
    to execute them later. The updater must preserve exclusive staging ownership
    and authenticate its release source; do not use this on shared mutable paths.
    """
    if not isinstance(expected_manifest_sha256, str) or not _DIGEST.fullmatch(expected_manifest_sha256):
        raise ValueError("An independently trusted manifest SHA-256 is required.")
    if not isinstance(expected_commit, str) or not _COMMIT.fullmatch(expected_commit):
        raise ValueError("An exact trusted commit SHA is required.")
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
        sizes[name] = _read_verified(directory / name, artifacts[name], cancelled=cancelled)
        if sizes[name] <= 0:
            raise ValueError("Empty candidate artifact.")
    if _identity(_plain(directory, directory=True)) != root_before:
        raise ValueError("Candidate directory changed during verification.")
    return {"commit": expected_commit, "manifest_sha256": expected_manifest_sha256,
            "version": next(iter(versions)), "artifacts": dict(artifacts), "bytes": sizes,
            "status": "integrity verified; installation not authorized"}


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
