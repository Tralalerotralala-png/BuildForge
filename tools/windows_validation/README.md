# BuildForge Windows candidate validation

Download the **BuildForge-Windows-release-candidate** artifact from the linked GitHub Actions run. Save the downloaded ZIP, without extracting it, as `BuildForge-Windows-release-candidate.zip` alongside this README in the repository checkout. Double-click **Validate-BuildForge.cmd** from a non-elevated, standard-user desktop session.
Use an existing Python 3.9+ installation and a Windows x64 Android NDK.
The GitHub-hosted reference used NDK 27.3.13750724 and Android API 24.
The test uses API 24 and records the detected NDK and LLD versions.

The harness looks for the NDK in the usual Android SDK and Unity Hub locations
and honors `ANDROID_NDK_HOME`. For an NDK in another location, set that
variable to the NDK root before launching. The optional signature fixtures
require `cryptography` in the selected Python installation. A missing backend
is recorded as a skip for those fixtures. The pinned candidate integrity check
uses Python's standard library.

The downloaded candidate ZIP can be used offline and is pinned to BuildForge
0.7.0-rc.1, workflow 45820654560, artifact 15117572427 and commit
`08e769cf514415c1cd90e5d728f6b5dea192aa82`.

- [GitHub Actions run and artifact downloads](https://github.com/Tralalerotralala-png/BuildForge/actions/runs/45820654560)
- [Release checklist at the candidate commit](https://github.com/Tralalerotralala-png/BuildForge/blob/08e769cf514415c1cd90e5d728f6b5dea192aa82/RELEASE_CHECKLIST.md)

## Evidence collected

| Check | Scope |
| --- | --- |
| CI artifact integrity | Pins the GitHub ZIP digest, verifies the manifest, commit, workflow run, checksum list, installer hash and portable hash, and checks embedded metadata before any Windows executable runs. The candidate is unsigned. |
| Frozen startup | Runs the upstream hidden-window GUI smoke test with redirected profile directories and checks a synthetic profile sentinel. |
| Frozen NDK build | Runs the upstream two-ABI C/C++ compile and strict shared-library link smoke test. It verifies cache reuse, isolated header invalidation, previous output preservation on failed link, pre-admission cancellation, lock release and successful resume. Source, job, cache and stage paths are under a temporary directory containing spaces and a non-ASCII character. |
| Active cancellation and pipeline recovery | Runs upstream source regressions that cancel owned synthetic worker processes and check interrupted-pipeline recovery. |
| Signed staging | Runs upstream signed portable tests with ephemeral fixture keys when the backend is available. They exercise verification, staging and rejection paths. |

The harness uses a fresh temporary workspace and Windows Job Objects for its
test processes. It runs only the portable candidate's smoke entry points and
the repository tests. The installer is hashed as data. Temporary files belong to
the current run. The harness needs no network access after download.

The window prints the name of the `BuildForge-validation-*.json` report and
pauses at completion. Look next to the launcher, or in the user's temporary
folder if the launcher directory is read-only. The report includes fixed
check names, candidate identifiers and hashes, version numbers, counters and
status flags. Raw compiler output, user paths, project names and profile
contents stay out of the report. **INCOMPLETE** means one or more required
automated gates could not run. The report tracks hosted CI separately.

## Evidence still needed for release

Run the harness on a clean Windows machine under a true standard-user account.
Run installed-app acceptance, installer removal and preservation of an existing
profile as separate manual checks. Representative authorized builds, frozen UI
mid-compile cancellation, scaling and keyboard navigation, Android runtime
checks, real-hardware performance review, license and branding review, signing
and publication also remain release gates.

## Harness verification

The harness uses the repository verifier and regression tests. The launcher and orchestrator add isolation, CI pinning and sanitized reporting.

Before delivery on Linux, the exact CI archive passed integrity and staging,
and all 35 selected upstream regressions passed, including the real Ed25519
signed-staging fixtures. Four harness tests passed using a tampered archive,
a simulated engine, synthetic GUI profile state and an isolated worker
timeout. These results cover harness behavior and archive bytes. The Windows
launcher, Windows Job Objects, frozen Tk startup and real NDK compilation
require a Windows run.

To rerun the four harness tests on Linux, run
`python3 -B -m unittest -v tests.test_validation_harness` from the repository root. The artifact integrity test requires the downloaded ZIP.
