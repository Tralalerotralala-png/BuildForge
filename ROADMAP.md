# BuildForge release gates

## Implemented in 0.4 development beta
- Live budget and RAM admission, drain/resume, optional Windows process priority and local telemetry.
- Legacy and independently keyed per-file flags; target-first queue; explicit O0/1/2 and timing report; slow warnings and per-unit timing manifest.
- Optional strict shared-library link with per-object hash validation and separate status; atomic output on success.
- Source package, Windows executable/installer recipes, tests and synthetic example.
- 20 automated tests and Linux Xvfb GUI smoke test in 0.4; run `python -m unittest discover -v` to reproduce.

## Remaining release blockers
- Test genuine Windows NDK, GUI, lock recovery and process API behavior; clean-machine executable and installer tests.
- Safe owned-process-tree cancellation and in-session retry, dependency-file cache design, compiler-specific diagnostic options, cross-platform backend support, CMake/Ninja/ccache integration.
- More robust job telemetry, throughput estimation, temperature feedback where supported, accessible themes, keyboard/scaling checks, benchmarks on real hardware.
- Security review of generic linker input paths and response files, external dependency fingerprints and race resistance; verify project-specific mixed context compatibility.
- Licensing/branding review, signed release workflow, documentation and release review. Do not publish private files or claim production readiness.

## 0.4 source release gates

- Log-derived retry planning with explicit confirmation and exact-set inference for legacy logs, immutable input validation and bounded compiler discovery.
- Native Windows CI and portable packaging recipe. Windows process control and clean-machine installer validation still require actual testing.
- Next: owned-process cancellation with a race-free Windows Job Object launch, dependency graph and compiler backends; no automatic kill/retry until validated.
