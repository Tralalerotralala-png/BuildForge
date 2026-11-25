# BuildForge 0.4 — targeted compilation beta

Local, resumable C/C++ object compilation and optional strict Android ELF shared-library linking. Source edition, not a certified production release.

## Start on Windows

Extract to a **new folder**, then run `OPEN_BUILDFORGE.cmd` using Python 3.9+ with Tkinter. An existing Android NDK is required. Optional psutil enables richer hardware/worker telemetry and best-effort Windows process priority controls. No cloud service or telemetry upload. Use saved profiles or import a previous job JSON from the local BuildForgeARM64/jobs directory. Do not start a second engine against any active output directory. Existing jobs in previous app versions cannot be controlled by the new version.

## Target & diagnose

1. Finish or stop the old build. If it is unresponsive, use Inspect / recover only after checking the exact output path and process identities. Recovery is Windows-only and has mock tests, not native Windows verification. Do not delete cached objects.
2. Load your previous profile. Keep source, output, NDK, include order, API, base optimization, definitions and architecture unchanged. If the previous job is still running, do not start this build.
3. Open **Target & diagnose**. Enter one source filename per line followed by ` | 0` for O0, ` | 1` for O1, or ` | 2` for O2. Use ` | same | timing` to leave optimization unchanged and request Clang's `-ftime-report`; ` | 0 | timing` combines both. Names must match existing source filenames exactly. Invalid/duplicate targets are rejected before touching the output. A target is prioritized at next launch, not automatically killed or restarted while running.
4. Start / resume. The whole build plan is checked, but only cache misses or units with changed flags are compiled. Keep target overrides saved with your profile to reuse them on future runs. Clearing overrides returns those units to the base flags and will recompile them if needed.
5. Read the per-file `.log` and `unit_timings.json`. `object_manifest.json` records source/object digests, per-unit contexts and flags. A compiler timing report is emitted by Clang at exit, not a live optimizer trace. Active row selection can be copied as O0 target lines for the next retry. No automatic fallback or automatic termination is performed.

Mixed O0/O1 generally preserves ordinary non-LTO ABI compatibility when other flags remain identical, but runtime correctness, performance and project-specific assumptions require testing. Some legacy link validators intentionally reject multiple contexts. Do not disable their validation or pretend the overridden objects have the original context. The optional generic linker understands BuildForge's per-unit fingerprints; a specialized project pipeline may require a separately reviewed adapter.

## Resource control and diagnostics

The slider is a scheduler budget, not a CPU quota: 0% admits no new jobs and drains active ones; 25/50/75/100% admits up to ceil(worker ceiling × percentage / 100). Changing budget does not alter object fingerprints. A lower budget can take time to become effective while existing jobs finish. RAM reserve and per-worker headroom limit new admission; they are not hard memory allocation limits. Optional Windows process priority classes are best-effort and never realtime/high. Existing compiler threads cannot be split by moving the slider. Physical core count, optional process-tree CPU seconds/RSS and temperatures depend on platform support; missing values are shown as unavailable. RSS is a sum of live process working sets, not unique physical memory; CPU time is a live process-tree sample, not a permanent cumulative counter. Slow-unit warnings are configurable and do not establish a hang. The app shows actual completed/cached/failed counts, active unit PIDs, elapsed time and available output disk space. No fake per-file percentages or exact completion forecast.

## Optional strict linking

The Link tab can link all validated objects to an Android `libNAME.so` using the configured NDK Clang driver. Enter explicit archive/shared-library paths and NDK system library names if needed. Normal Android linking expects a genuine NDK sysroot and runtime libraries. Freestanding mode (`-nostdlib`) is an advanced option for self-contained synthetic code only. Link uses `--no-undefined`, checks each input against its current stamp and manifest, and checks ELF type and architecture before atomically publishing the output. A failed link preserves any previous library but writes a failed `link_status.json`; never mistake the presence of an old `.so` for a fresh success. Object readiness and link success are independent. Shared-library link success does not establish device execution, JNI/Unity integration, dependency availability, APK generation or runtime ABI correctness. Do not turn on the generic linker as a substitute for a specialized project pipeline.

## Supported scope and limitations

Android AArch64 and x86-64 ELF relocatable objects, C17/C++17, explicit include folders/defines, recursive discovery, separate private output, exact source/object digest validation and legacy fixed-count profile cache compatibility. Cache fingerprints include compiler/version, base or per-unit flags and configured header roots. The legacy `gos` JSON key is retained solely for compatibility. Cache discovery is conservative, not a general dependency graph; external/generated dependencies outside the roots need explicit management. Do not edit sources or headers while a build is active. Outputs are refused within Git checkouts to prevent inadvertent publication. Profiles remain local. Native COFF/MSVC, arbitrary build scripts, CMake/Ninja project import, closed-loop temperature policy, process-tree cancellation/automatic fallback during an active job, polished themes, clean-machine installer validation and Windows-specific process API validation remain open. No proprietary project source, objects or personal profiles are included.

## Windows packaging

`BUILD_WINDOWS_EXE.cmd` is a PyInstaller recipe for an already installed Python/PyInstaller environment. It creates `dist/BuildForge/BuildForge.exe` plus dependencies on Windows. The source distribution includes no executable; the recipe has not been executed on Windows here. Install optional psutil in that same environment for richer Windows telemetry. `BuildForge.iss` is an optional Inno Setup installer recipe requiring that built directory and Inno Setup 6. Neither the executable nor installer is verified on a clean Windows system. Review branding and Windows packaging before distributing binaries.

## Tests

`python -m unittest discover -v`

Synthetic Linux Clang/LLD cross-compilation verified AArch64 and x86-64 object generation, C/C++ compilation, legacy cache fingerprints, interrupted/drained scheduling, slider changes, per-file O0 override with eight unrelated objects unchanged in hash and mtime, subsequent nine-object cache hit, reverting only the selected unit to base settings, actual freestanding ARM64 shared library link, and strict rejection of a missing symbol while preserving the previous binary. Windows recovery is tested with mocks. Tk startup, profile/override round-trip and slider callback were smoke-tested under Xvfb; no native Windows or Android runtime test has been performed. Tests use no game code.

## Design references

[Ninja job pools](https://ninja-build.org/manual.html), [CMake job pools](https://cmake.org/cmake/help/latest/prop_gbl/JOB_POOLS.html), [ccache](https://ccache.dev/), [sccache](https://github.com/mozilla/sccache), [psutil](https://psutil.readthedocs.io/). These are design references, not bundled dependencies or copied implementation. MIT license for the original BuildForge source; third-party components retain their licenses.

## 0.4 hardening

Analyze session log in Target & diagnose produces reviewable O0/timing suggestions for named slow active units; the existing job is not stopped. The CLI also supports `python diagnostics.py session.log --threshold 900 --targets`. For older logs without active worker names, select the exact original source directory or supply `--source-dir PATH` (and `--recursive` if needed). Suggestions require a fully accounted terminal count, an exhausted queue, and a known source set. Elapsed time in inferred rows is a lower bound on the observed unchanged tail, not individual start time. Source edits detected before object publication fail the unit; header or compiler changes detected at final validation block publication of the ready manifest. This does not guarantee a consistent snapshot when files change and change back during compilation; keep inputs immutable. Toolchain discovery calls have bounded timeouts. No automatic cancellation or Windows certification.
