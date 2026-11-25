# Stable-release evidence checklist

BuildForge 0.7.0-rc.1 is an integration candidate, not a stable release.

## Verified baseline

- Combined Python 3.9/3.11 Linux and Windows tests, frozen app startup, hosted Windows installer install/uninstall, native NDK compilation/link/cache on both architectures: [run 45648144801](https://github.com/Tralalerotralala-png/BuildForge/actions/runs/45648144801).
- Windows smoke used NDK 27.3.13750724 (r27d), Android API 24. This does not certify other NDK/LLD versions or device execution.
- Main after integration: [run 45650050149](https://github.com/Tralalerotralala-png/BuildForge/actions/runs/45650050149).
- Source and built candidate artifacts must not contain private projects or profiles.

## Candidate package

Download the CI candidate artifact only from a successful workflow for the intended commit. Extract into a separate directory; verify the installer and portable ZIP against SHA256SUMS.txt. Check build-metadata.json against the intended commit/run. Hashes detect corruption, but are not digital signatures. Frozen packaging versions are pinned in requirements-build.txt; OS image, Python patch version and installer versions can still vary. Reproducibility and runtime license coverage require separate review.

## Manual gates (before v1.0)

- On a clean, standard-user Windows machine: install, launch, compile/link synthetic project and representative authorized project, resume cache, exercise cancellation and failed linking; verify uninstall and profile preservation. Test spaces and non-ASCII paths and display scaling/keyboard navigation.
- Test actual AArch64/x86-64 Android runtime loading and behavior for the intended app/ABI. Record NDK/API/LLD versions and limits.
- Review dependency/cancellation race boundaries, telemetry and performance on real hardware. Decide explicitly which broader backends and integrations are deferred.
- Audit frozen-runtime notices (Python, Tcl/Tk, PyInstaller runtime and dependencies); confirm branding rights. Have the authorized maintainer sign and verify binaries through their own signing infrastructure. Do not store private signing keys in Git or share them in chat.
- Review release notes, final successful CI, artifact hashes and signature verification. Publish only after the maintainer approves the evidence.

Track open decisions in [issue #9](https://github.com/Tralalerotralala-png/BuildForge/issues/9).
