# BuildForge 0.7.0-rc.1 — C/C++ build workbench

Local, resumable C/C++ compilation and strict optional Android ELF shared-library linking. Integration candidate, not a certified production release. No telemetry upload or bundled proprietary source.

## Quick start

On Windows, extract to a fresh folder and run `OPEN_BUILDFORGE.cmd` with Python 3.9+ and Tkinter. Select an installed Android NDK (Unity Hub NDK discovery is supported), source folder and a separate private output folder. Optional `psutil` enables richer resource telemetry and process-tree priority control.

- Start or resume a build; cached objects are validated by source, object and toolchain fingerprints.
- Target & diagnose: enter exact filenames with ` | 0`, ` | 1`, ` | 2`, or ` | same | timing` to override optimization or request a compiler timing report. Cache auditing is read-only; targeted slow failures can optionally retry once at O0.
- Use the resource slider to drain or admit workers, not as a precise CPU quota. Session and selected-unit cancellation affect only owned compiler processes.
- Link optionally checks object manifests, ELF type and architecture, uses strict undefined-symbol validation, and publishes output atomically. An old library is not proof of a fresh successful link. An optional external LLD must be explicitly selected; test compatibility using the disposable toolchain probe.

Try `examples/synthetic` with ARM64, O1 and a separate empty output folder. Resume with `example.cpp | 0 | timing` to exercise targeted caching. Freestanding linking is for self-contained synthetic examples only.

## Build and test

Run `python -m unittest discover -v` (Linux integration tests require Clang and LLD). `BUILD_WINDOWS_EXE.cmd` builds a portable executable using PyInstaller; `BuildForge.iss` optionally packages it using Inno Setup 6. CI checks source, hosted Windows NDK builds and packaging.

## Limitations and development

Supports Android AArch64/x86-64 ELF, C17/C++17, explicit include roots and defines. Keep inputs stable during a build; never run two engines against one output directory. Mixed optimization requires ABI/runtime validation. Native COFF/MSVC, general dependency graphs, CMake/Ninja import, polished accessibility and clean-machine installer/device validation remain open. See [release gate #9](https://github.com/Tralalerotralala-png/BuildForge/issues/9). Do not bypass output locks or fingerprint checks. Never commit private source, build products, personal profiles or logs.

MIT license; third-party components retain their licenses. Design references: [Ninja](https://ninja-build.org/manual.html), [CMake](https://cmake.org/cmake/help/latest/prop_gbl/JOB_POOLS.html), [ccache](https://ccache.dev/), [sccache](https://github.com/mozilla/sccache), [psutil](https://psutil.readthedocs.io/).
