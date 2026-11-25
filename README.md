# BuildForge 0.5 — local C++ build & targeted retry workbench

Local, resumable C/C++ object compilation and optional strict Android ELF shared-library linking. Source edition, not a certified production release.

## Start on Windows

Extract to a **new folder**, then run `OPEN_BUILDFORGE.cmd` using Python 3.9+ with Tkinter. An existing Android NDK (including Unity Hub Editor AndroidPlayer NDK installations) is auto-discovered when available. Optional `psutil` enables richer hardware/worker telemetry and full process-tree Windows priority controls; stdlib fallbacks provide total RAM detection and root-process Windows priority control when `psutil` is absent. No cloud service or telemetry upload. Use saved profiles or import a previous job JSON from the local `BuildForgeARM64/jobs` directory. Do not start a second engine against any active output directory.

## What's new in 0.5 beta

- **Safe owned-process cancellation**: **Cancel active jobs…** in the UI triggers a session cancel token that terminates only compiler process trees spawned by that engine instance, cleanly releasing `.compile.lock` and preserving already-completed cached objects.
- **Slow-then-failed diagnostics**: `Analyze session log…` now retains slow unit candidates even when active workers later exit as `failed` or are interrupted, across both `0.4+` named logs and legacy exhausted-tail logs.
- **Missing / failed unit finder**: **Find missing / failed units** in **Target & diagnose** scans the selected output directory and offers to populate units without valid `.o` / `.json` pairs directly into the override editor (e.g., as `unit.cpp | 0 | timing`).
- **Windows hardening**: `CREATE_NO_WINDOW` on Windows compiler/linker subprocesses, transient `PermissionError` retry on atomic object/JSON replacement, null-safe `CreationDate` handling and `ld.lld` recognition in `recovery.py`, and Unity Hub NDK auto-discovery.
- **UI polish**: Last-used profile persistence (`state.json`), profile deletion, color-tagged live build console, unit failure log tails surfaced inline, scrollbars on active/recovery tables, and **Clear log**.

## Target & diagnose

1. Finish, stop, or cancel the old build. If it is unresponsive, use **Inspect / recover** only after checking the exact output path and process identities. Do not delete cached objects.
2. Load your previous profile. Keep source, output, NDK, include order, API, base optimization, definitions and architecture unchanged. If the previous job is still running, do not start this build.
3. Open **Target & diagnose**. Enter one source filename per line followed by ` | 0` for O0, ` | 1` for O1, or ` | 2` for O2. Use ` | same | timing` to leave optimization unchanged and request Clang's `-ftime-report`; ` | 0 | timing` combines both. You can also click **Analyze session log…** or **Find missing / failed units** to populate candidates. Names must match existing source filenames exactly. Invalid/duplicate targets are rejected before touching the output.
4. **Start / resume build**. The whole build plan is checked, but only cache misses or units with changed flags are compiled. Keep target overrides saved with your profile to reuse them on future runs. Clearing overrides returns those units to the base flags and will recompile them if needed.
5. Read the per-file `.log` and `unit_timings.json`. `object_manifest.json` records source/object digests, per-unit contexts and flags. A compiler timing report is emitted by Clang at exit, not a live optimizer trace.

Mixed O0/O1 generally preserves ordinary non-LTO ABI compatibility when other flags remain identical, but runtime correctness, performance and project-specific assumptions require testing. Some legacy link validators intentionally reject multiple contexts. Do not disable their validation or pretend the overridden objects have the original context. The optional generic linker understands BuildForge's per-unit fingerprints; a specialized project pipeline may require a separately reviewed adapter.

## Resource control and diagnostics

The slider is a scheduler budget, not a CPU quota: 0% admits no new jobs and drains active ones; 25/50/75/100% admits up to `ceil(worker ceiling × percentage / 100)`. Changing budget does not alter object fingerprints. A lower budget can take time to become effective while existing jobs finish. RAM reserve and per-worker headroom limit new admission; they are not hard memory allocation limits. Optional Windows process priority classes are best-effort and never realtime/high. Existing compiler threads cannot be split by moving the slider. Physical core count, optional process-tree CPU seconds/RSS and temperatures depend on platform support; missing values are shown as unavailable.

## Optional strict linking

The **Link** tab can link all validated objects to an Android `libNAME.so` using the configured NDK Clang driver. Enter explicit archive/shared-library paths and NDK system library names if needed. Normal Android linking expects a genuine NDK sysroot and runtime libraries. Freestanding mode (`-nostdlib`) is an advanced option for self-contained synthetic code only. Link uses `--no-undefined`, checks each input against its current stamp and manifest, and checks ELF type and architecture before atomically publishing the output. A failed link preserves any previous library but writes a failed `link_status.json`; never mistake the presence of an old `.so` for a fresh success. Object readiness and link success are independent. Shared-library link success does not establish device execution, JNI/Unity integration, dependency availability, APK generation or runtime ABI correctness.

## Supported scope and limitations

Android AArch64 and x86-64 ELF relocatable objects, C17/C++17, explicit include folders/defines, recursive discovery, separate private output, exact source/object digest validation and legacy fixed-count profile cache compatibility. Cache fingerprints include compiler/version, base or per-unit flags and configured header roots. The legacy `gos` JSON key is retained solely for compatibility. Cache discovery is conservative, not a general dependency graph; external/generated dependencies outside the roots need explicit management. Do not edit sources or headers while a build is active. Outputs are refused within Git checkouts to prevent inadvertent publication. Profiles remain local. Native COFF/MSVC, arbitrary build scripts, CMake/Ninja project import, closed-loop temperature policy, polished themes and clean-machine installer validation remain open. No proprietary project source, objects or personal profiles are included.

## Windows packaging

`BUILD_WINDOWS_EXE.cmd` is a PyInstaller recipe for an already installed Python/PyInstaller environment. It creates `dist/BuildForge/BuildForge.exe` plus dependencies on Windows. The source distribution includes no executable. Install optional `psutil` in that same environment for richer Windows telemetry. `BuildForge.iss` is an optional Inno Setup installer recipe requiring that built directory and Inno Setup 6.

## Tests

`python -m unittest discover -v`

Cross-platform unit and integration tests verify process classification/recovery, atomic replacement retry, RAM measurement, owned-process-tree cancellation, slow-then-failed log diagnostics (named and legacy logs), missing-unit detection, target override parsing, and (on Linux with Clang/LLD) AArch64/x86-64 object compilation, legacy cache compatibility, live budget throttling, mixed-flag manifest generation, and strict shared-library linking.

## Design references

[Ninja job pools](https://ninja-build.org/manual.html), [CMake job pools](https://cmake.org/cmake/help/latest/prop_gbl/JOB_POOLS.html), [ccache](https://ccache.dev/), [sccache](https://github.com/mozilla/sccache), [psutil](https://psutil.readthedocs.io/). These are design references, not bundled dependencies or copied implementation. MIT license for the original BuildForge source; third-party components retain their licenses.

### Selected compiler cancellation (development)

Select active compiler rows and use **Cancel selected compiler units…** to request cancellation of those exact unit names in the current session. Other units continue. The request is session-specific and cumulative; it never accepts arbitrary PIDs. A unit which finishes before the request takes effect may remain successfully compiled. A cancelled unit is incomplete and can be retried on a subsequent resume, optionally with an explicit O0 override. No automatic retry or guaranteed descendant termination; termination remains best-effort. Keep the existing cache and flags for other units. This feature builds on the session cancellation admission guard.
