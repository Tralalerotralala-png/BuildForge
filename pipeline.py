"""Generic resumable multi-stage build pipeline, toolchain detection, and bottleneck analytics."""
from __future__ import annotations

import concurrent.futures as cf
import dataclasses
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from cache_compat import GIB, atomic_json, available_ram, digest
from elf_compat import compatible_page_sizes
from resource_control import Budget, hardware


_DIAG_RE = re.compile(
    r"^(?P<file>[^:\r\n]+):(?P<line>\d+)(?::\d+)?:\s*(?P<severity>fatal error|error|warning|note):\s*(?P<message>.+)$"
)
_LD_UNDEF_RE = re.compile(r"undefined symbol:\s*(.+)$")


def detect_ndk_paths() -> list[Path]:
    """Return discovered Android NDK root paths in priority order."""
    candidates: list[Path] = []
    for key in ("ANDROID_NDK_HOME", "ANDROID_NDK_ROOT", "NDK_HOME"):
        val = os.environ.get(key)
        if val:
            candidates.append(Path(val))
    sdk = Path(
        os.environ.get("ANDROID_SDK_ROOT")
        or os.environ.get("ANDROID_HOME")
        or Path.home() / "AppData" / "Local" / "Android" / "Sdk"
    )
    if (sdk / "ndk").is_dir():
        candidates.extend(sorted((sdk / "ndk").iterdir(), reverse=True))
    candidates.append(sdk / "ndk-bundle")
    if os.name == "nt":
        for pf in {os.environ.get("ProgramFiles", r"C:\Program Files"), os.environ.get("ProgramFiles(x86)", "")}:
            if not pf:
                continue
            hub = Path(pf) / "Unity" / "Hub" / "Editor"
            if hub.is_dir():
                for ed in sorted(hub.iterdir(), reverse=True):
                    candidates.append(ed / "Editor" / "Data" / "PlaybackEngines" / "AndroidPlayer" / "NDK")
    seen: set[str] = set()
    valid: list[Path] = []
    for p in candidates:
        try:
            rp = str(p.resolve())
        except OSError:
            continue
        if rp in seen:
            continue
        if (p / "toolchains" / "llvm" / "prebuilt").is_dir():
            seen.add(rp)
            valid.append(p)
    return valid


def _find_exe(name: str, extra_dirs: list[Path] | None = None) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    for d in extra_dirs or []:
        if not d.is_dir():
            continue
        for cand in (d / name, d / f"{name}.exe", d / f"{name}.bat", d / f"{name}.cmd"):
            if cand.is_file():
                return str(cand)
    return None


def _probe_version(exe: str | None, args: tuple[str, ...] = ("--version",)) -> str | None:
    if not exe:
        return None
    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    try:
        out = subprocess.check_output(
            [exe, *args], stderr=subprocess.STDOUT, timeout=10, creationflags=creationflags
        )
        lines = [ln.strip() for ln in out.decode(errors="replace").splitlines() if ln.strip()]
        return lines[0][:200] if lines else None
    except (OSError, subprocess.SubprocessError):
        return None


def detect_toolchains() -> dict[str, Any]:
    """Automatically detect local C++/Android/Java/packaging toolchains and hardware."""
    llvm_dirs = [Path(r"C:\Program Files\LLVM\bin")] if os.name == "nt" else [Path("/usr/bin"), Path("/usr/local/bin")]
    unity_jdk_dirs: list[Path] = []
    unity_bt_dirs: list[Path] = []
    unity_pt_dirs: list[Path] = []
    if os.name == "nt":
        hub = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Unity" / "Hub" / "Editor"
        if hub.is_dir():
            for ed in sorted(hub.iterdir(), reverse=True):
                ap = ed / "Editor" / "Data" / "PlaybackEngines" / "AndroidPlayer"
                if (ap / "OpenJDK" / "bin").is_dir():
                    unity_jdk_dirs.append(ap / "OpenJDK" / "bin")
                if (ap / "SDK" / "build-tools").is_dir():
                    unity_bt_dirs.extend(sorted((ap / "SDK" / "build-tools").iterdir(), reverse=True))
                if (ap / "SDK" / "platform-tools").is_dir():
                    unity_pt_dirs.append(ap / "SDK" / "platform-tools")
    sdk = Path(
        os.environ.get("ANDROID_SDK_ROOT")
        or os.environ.get("ANDROID_HOME")
        or Path.home() / "AppData" / "Local" / "Android" / "Sdk"
    )
    sdk_bt_dirs = sorted((sdk / "build-tools").iterdir(), reverse=True) if (sdk / "build-tools").is_dir() else []
    bt_dirs = [*sdk_bt_dirs, *unity_bt_dirs]
    pt_dirs = [sdk / "platform-tools", Path.home() / "Downloads" / "platform-tools", *unity_pt_dirs]
    jdk_dirs = ([Path(os.environ["JAVA_HOME"]) / "bin"] if os.environ.get("JAVA_HOME") else []) + unity_jdk_dirs

    clangxx = _find_exe("clang++", llvm_dirs)
    lld = _find_exe("ld.lld", llvm_dirs)
    readelf = _find_exe("llvm-readelf", llvm_dirs) or _find_exe("readelf")
    java = _find_exe("java", jdk_dirs)
    javac = _find_exe("javac", jdk_dirs)
    zipalign = _find_exe("zipalign", bt_dirs)
    aapt2 = _find_exe("aapt2", bt_dirs)
    adb = _find_exe("adb", pt_dirs)

    iscc = None
    if os.name == "nt":
        for cand in (
            Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Inno Setup 6" / "ISCC.exe",
            Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Inno Setup 6" / "ISCC.exe",
        ):
            if cand.is_file():
                iscc = str(cand)
                break

    pyinstaller_ver = None
    try:
        import importlib.metadata as md
        pyinstaller_ver = md.version("pyinstaller")
    except Exception:
        pass

    ndks = [str(p) for p in detect_ndk_paths()]
    return {
        "hardware": hardware(),
        "available_ram_gib": round(available_ram() / GIB, 2),
        "python": sys.version.split()[0],
        "ndks": ndks,
        "tools": {
            "clang++": {"path": clangxx, "version": _probe_version(clangxx)},
            "ld.lld": {"path": lld, "version": _probe_version(lld)},
            "llvm-readelf": {"path": readelf, "version": _probe_version(readelf)},
            "java": {"path": java, "version": _probe_version(java, ("-version",))},
            "javac": {"path": javac, "version": _probe_version(javac, ("-version",))},
            "zipalign": {"path": zipalign},
            "aapt2": {"path": aapt2},
            "adb": {"path": adb},
            "iscc": {"path": iscc},
            "pyinstaller": {"version": pyinstaller_ver},
        },
    }


def extract_compiler_diagnostics(log_text: str, max_items: int = 12) -> dict[str, Any]:
    """Extract actionable errors, undefined symbols, and hints from compiler/linker logs."""
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    undefined_symbols: list[str] = []
    hints: list[str] = []
    seen_hints: set[str] = set()

    def add_hint(text: str) -> None:
        if text not in seen_hints:
            seen_hints.add(text)
            hints.append(text)

    for raw in log_text.splitlines():
        line = raw.strip()
        if not line:
            continue
        m = _DIAG_RE.match(line)
        if m:
            entry = {
                "file": m.group("file"),
                "line": int(m.group("line")),
                "severity": m.group("severity"),
                "message": m.group("message"),
            }
            if "error" in entry["severity"]:
                if len(errors) < max_items:
                    errors.append(entry)
                msg = entry["message"]
                if "file not found" in msg:
                    add_hint("Check include directories and header fingerprint roots.")
                elif "use of undeclared identifier" in msg:
                    add_hint("Verify missing forward declarations or namespace/macro definitions.")
            else:
                if len(warnings) < max_items:
                    warnings.append(entry)
            continue
        um = _LD_UNDEF_RE.search(line)
        if um:
            sym = um.group(1).strip()
            if sym not in undefined_symbols and len(undefined_symbols) < max_items:
                undefined_symbols.append(sym)
            add_hint("Resolve missing symbol in runtime glue, static archives, or system libraries.")
        elif "PT_LOAD" in line or "max-page-size" in line:
            add_hint("Relink shared libraries with -Wl,-z,max-page-size=16384 for 16 KiB ELF alignment.")

    return {
        "error_count": len(errors),
        "warning_count": len(warnings),
        "undefined_symbol_count": len(undefined_symbols),
        "errors": errors,
        "warnings": warnings,
        "undefined_symbols": undefined_symbols,
        "hints": hints,
    }


def _hash_item_inputs(paths: list[Path]) -> dict[str, str]:
    result: dict[str, str] = {}
    for p in sorted({Path(x).resolve() for x in paths}):
        if not p.is_file():
            raise FileNotFoundError(f"Pipeline input missing: {p}")
        result[str(p)] = digest(p)
    return result


@dataclasses.dataclass
class WorkItem:
    """Single fingerprintable unit of work within a pipeline stage."""

    item_id: str
    inputs: list[Path]
    outputs: list[Path]
    fn: Callable[[WorkItem], Any]
    params: dict[str, Any] = dataclasses.field(default_factory=dict)
    toolchain_fingerprint: str = ""


@dataclasses.dataclass
class PipelineStage:
    """Ordered stage within a BuildForge multi-stage pipeline."""

    name: str
    items: list[WorkItem]
    parallel: bool = True
    max_workers: int | None = None
    ram_per_worker_gib: float = 0.75
    description: str = ""


class PipelineRunner:
    """Resumable, fingerprinted, hardware-aware multi-stage pipeline runner."""

    def __init__(
        self,
        workspace_out: Path,
        *,
        pipeline_name: str = "generic",
        budget_percent: int = 100,
        reserve_gib: float = 2.0,
        state_flush_interval_s: float = 1.0,
    ) -> None:
        self.out = Path(workspace_out).resolve()
        if any((parent / ".git").exists() for parent in (self.out, *self.out.parents)):
            raise ValueError("Refusing pipeline state output inside a Git checkout.")
        self.pipeline_name = pipeline_name
        self.budget = Budget(budget_percent)
        self.reserve_gib = float(reserve_gib)
        self.state_flush_interval_s = max(0.0, float(state_flush_interval_s))
        self.state_path = self.out / "pipeline_state.json"
        self.report_path = self.out / "pipeline_report.json"
        self.lock_path = self.out / ".pipeline.lock"

    def _item_fingerprint(self, stage_name: str, item: WorkItem) -> str:
        inputs = _hash_item_inputs(item.inputs)
        payload = {
            "schema": 1,
            "pipeline": self.pipeline_name,
            "stage": stage_name,
            "item_id": item.item_id,
            "inputs": inputs,
            "params": item.params,
            "toolchain": item.toolchain_fingerprint,
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _outputs_valid(self, item: WorkItem, recorded_outputs: dict[str, str] | None) -> bool:
        if not item.outputs or not isinstance(recorded_outputs, dict):
            return False
        expected = {str(Path(p).resolve()) for p in item.outputs}
        if set(recorded_outputs) != expected:
            return False
        for p_str, expected_sha in recorded_outputs.items():
            p = Path(p_str)
            if not p.is_file() or digest(p) != expected_sha:
                return False
        return True

    def _load_state(self) -> dict[str, Any]:
        if not self.state_path.is_file():
            return {"schema": 1, "pipeline": self.pipeline_name, "stages": {}}
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("stages"), dict):
                return data
        except (OSError, ValueError):
            pass
        return {"schema": 1, "pipeline": self.pipeline_name, "stages": {}}

    def _recommended_workers(self, stage: PipelineStage) -> int:
        if not stage.parallel:
            return 1
        hw = hardware()
        logical = max(1, int(hw.get("logical") or 1))
        ceiling = stage.max_workers if stage.max_workers is not None else logical
        ceiling = max(1, min(ceiling, logical))
        target = self.budget.workers(ceiling)
        if target <= 1:
            return max(1, target)
        free_gib = max(0.0, available_ram() / GIB - self.reserve_gib)
        ram_cap = max(1, int(free_gib / max(0.25, stage.ram_per_worker_gib)))
        return max(1, min(target, ram_cap))

    def run(
        self,
        stages: list[PipelineStage],
        *,
        stop_event: threading.Event | None = None,
        progress_cb: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        """Execute stages in order, reusing verified cached items and profiling bottlenecks."""
        self.out.mkdir(parents=True, exist_ok=True)
        token = uuid.uuid4().hex
        lock_payload = json.dumps(
            {"engine": "BuildForgePipeline", "pid": os.getpid(), "token": token, "time": time.time()}
        ).encode("utf-8")
        fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        try:
            with os.fdopen(fd, "wb") as lf:
                lf.write(lock_payload)
                lf.flush()
                os.fsync(lf.fileno())

            state = self._load_state()
            state_lock = threading.Lock()
            state_dirty = False
            last_state_flush = time.monotonic()

            def flush_state(*, force: bool = False) -> None:
                nonlocal state_dirty, last_state_flush
                state_dirty = True
                now = time.monotonic()
                if force or (now - last_state_flush) >= self.state_flush_interval_s:
                    atomic_json(self.state_path, state)
                    last_state_flush = now
                    state_dirty = False

            pipeline_t0 = time.monotonic()
            stage_reports: list[dict[str, Any]] = []
            overall_ok = True
            interrupted = False

            for stage in stages:
                if stop_event is not None and stop_event.is_set():
                    interrupted = True
                    overall_ok = False
                    break
                st_t0 = time.monotonic()
                stage_state = state["stages"].setdefault(stage.name, {"items": {}})
                recorded_items = stage_state.setdefault("items", {})
                to_run: list[tuple[WorkItem, str]] = []
                cached_count = 0
                executed_count = 0
                failed_count = 0
                item_timings: list[dict[str, Any]] = []

                for item in stage.items:
                    fp = self._item_fingerprint(stage.name, item)
                    prev = recorded_items.get(item.item_id)
                    if (
                        isinstance(prev, dict)
                        and prev.get("fingerprint") == fp
                        and prev.get("status") == "completed"
                        and self._outputs_valid(item, prev.get("outputs"))
                    ):
                        cached_count += 1
                        item_timings.append(
                            {"item_id": item.item_id, "status": "cached", "seconds": 0.0}
                        )
                    else:
                        to_run.append((item, fp))

                workers = self._recommended_workers(stage) if to_run else 1

                def run_one(pair: tuple[WorkItem, str]) -> dict[str, Any]:
                    item, fp = pair
                    if stop_event is not None and stop_event.is_set():
                        return {"item_id": item.item_id, "status": "interrupted", "seconds": 0.0}
                    t0 = time.monotonic()
                    try:
                        item.fn(item)
                        out_hashes = {}
                        for op in item.outputs:
                            rp = Path(op).resolve()
                            if not rp.is_file():
                                raise FileNotFoundError(f"Expected output missing after stage item: {rp}")
                            out_hashes[str(rp)] = digest(rp)
                        elapsed = round(time.monotonic() - t0, 4)
                        with state_lock:
                            recorded_items[item.item_id] = {
                                "fingerprint": fp,
                                "status": "completed",
                                "outputs": out_hashes,
                                "seconds": elapsed,
                            }
                            flush_state(force=False)
                        return {"item_id": item.item_id, "status": "executed", "seconds": elapsed}
                    except Exception as exc:
                        elapsed = round(time.monotonic() - t0, 4)
                        with state_lock:
                            recorded_items[item.item_id] = {
                                "fingerprint": fp,
                                "status": "failed",
                                "error": str(exc),
                                "seconds": elapsed,
                            }
                            flush_state(force=True)
                        return {
                            "item_id": item.item_id,
                            "status": "failed",
                            "seconds": elapsed,
                            "error": str(exc),
                        }

                if to_run:
                    if workers == 1:
                        for pair in to_run:
                            res = run_one(pair)
                            item_timings.append(res)
                            if res["status"] == "executed":
                                executed_count += 1
                            elif res["status"] == "interrupted":
                                interrupted = True
                                overall_ok = False
                            else:
                                failed_count += 1
                                overall_ok = False
                    else:
                        with cf.ThreadPoolExecutor(max_workers=workers) as pool:
                            for res in pool.map(run_one, to_run):
                                item_timings.append(res)
                                if res["status"] == "executed":
                                    executed_count += 1
                                elif res["status"] == "interrupted":
                                    interrupted = True
                                    overall_ok = False
                                else:
                                    failed_count += 1
                                    overall_ok = False

                with state_lock:
                    if state_dirty:
                        flush_state(force=True)

                wall = round(time.monotonic() - st_t0, 4)
                slowest = max(item_timings, key=lambda x: x["seconds"]) if item_timings else None
                stage_row = {
                    "name": stage.name,
                    "description": stage.description,
                    "workers_used": workers,
                    "total_items": len(stage.items),
                    "cached": cached_count,
                    "executed": executed_count,
                    "failed": failed_count,
                    "wall_seconds": wall,
                    "slowest_item": slowest,
                    "status": "failed" if failed_count else ("interrupted" if interrupted else "ok"),
                }
                stage_reports.append(stage_row)
                if progress_cb is not None:
                    progress_cb(stage_row)
                if failed_count or interrupted:
                    overall_ok = False
                    break

            total_wall = round(time.monotonic() - pipeline_t0, 4)
            for row in stage_reports:
                row["share_pct"] = round(100.0 * row["wall_seconds"] / total_wall, 1) if total_wall > 0 else 0.0
            bottleneck = max(stage_reports, key=lambda r: r["wall_seconds"]) if stage_reports else None
            report = {
                "schema": 1,
                "pipeline": self.pipeline_name,
                "success": overall_ok,
                "interrupted": interrupted,
                "total_wall_seconds": total_wall,
                "bottleneck_stage": bottleneck["name"] if bottleneck else None,
                "stages": stage_reports,
            }
            atomic_json(self.report_path, report)
            return report
        finally:
            try:
                if self.lock_path.read_bytes() == lock_payload:
                    self.lock_path.unlink()
            except FileNotFoundError:
                pass


def verify_android_apk_elfs(apk_path: Path, *, required_page_size: int = 4096) -> dict[str, Any]:
    """Audit ELF PT_LOAD alignment of every arm64-v8a shared library inside an APK."""
    import zipfile

    results: dict[str, list[int]] = {}
    offending: list[str] = []
    with zipfile.ZipFile(apk_path, "r") as zf, tempfile.TemporaryDirectory(prefix="bf-apk-elf-") as tmp:
        tmp_dir = Path(tmp)
        for info in zf.infolist():
            fn = info.filename
            if not (fn.startswith("lib/arm64-v8a/") and fn.endswith(".so")):
                continue
            name = Path(fn).name
            dest = tmp_dir / name
            with zf.open(info, "r") as src, dest.open("wb") as out:
                shutil.copyfileobj(src, out, 1024 * 1024)
            pages = compatible_page_sizes(dest, 183)
            results[name] = pages
            if required_page_size not in pages:
                offending.append(name)
    return {
        "apk": str(apk_path),
        "required_page_size": required_page_size,
        "libraries": results,
        "offending": sorted(offending),
        "compatible": not offending and bool(results),
    }
