# BuildForge release gates

## 0.6 integration candidate — combined CI required
- Bounded dashboard event handling and remaining-unit count.
- Optional external LLD and link heartbeat.
- Session and selected-unit cancellation admission guards.
- Read-only full cache audit on a background UI worker.
- Do not distribute as a release until combined Windows/Linux CI and packaging checks pass.

## Implemented in 0.5 development beta
- Owned-process-tree cancellation (`cancelfile` + `ProcessRegistry.terminate_all()`) with safe lock release and preserved cache hits.
- Slow-then-failed unit retention in `diagnostics.py` across both named `0.4+` logs and legacy exhausted-tail logs.
- Missing / failed unit finder (`engine.find_missing_units`) wired into Target & diagnose, plus inline failure log tails and color-coded build console.
- Windows hardening: `CREATE_NO_WINDOW` on compiler/linker subprocesses, transient `PermissionError` retry on atomic replacement, stdlib Windows RAM/priority fallbacks, Unity Hub NDK discovery, null-safe `CreationDate` and `lld`/`ld.lld` support in `recovery.py`.
- Last-used profile persistence and profile deletion.

## Implemented in 0.4 development beta
- Live budget and RAM admission, drain/resume, optional Windows process priority and local telemetry.
- Legacy and independently keyed per-file flags; target-first queue; explicit O0/1/2 and timing report; slow warnings and per-unit timing manifest.
- Optional strict shared-library link with per-object hash validation and separate status; atomic output on success.
- Source package, Windows executable/installer recipes, tests and synthetic example.

## Remaining release blockers
- Clean-machine Windows executable and Inno Setup installer validation on an unprivileged host.
- Dependency-file (`.d`) cache design, compiler-specific diagnostic options, cross-platform backend support, CMake/Ninja/ccache integration.
- More robust job telemetry, throughput estimation, temperature feedback where supported, accessible themes, keyboard/scaling checks, benchmarks on real hardware.
- Security review of generic linker input paths and response files, external dependency fingerprints and race resistance; verify project-specific mixed context compatibility.
- Licensing/branding review, signed release workflow, documentation and release review. Do not publish private files or claim production readiness.
