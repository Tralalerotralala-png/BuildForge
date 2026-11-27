import json
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from pipeline import (
    PipelineRunner,
    PipelineStage,
    WorkItem,
    detect_ndk_paths,
    detect_toolchains,
    extract_compiler_diagnostics,
    verify_android_apk_elfs,
)


class PipelineTests(unittest.TestCase):
    def test_detect_toolchains_and_diagnostics(self):
        info = detect_toolchains()
        self.assertIn("hardware", info)
        self.assertIn("tools", info)
        self.assertIsInstance(detect_ndk_paths(), list)
        log = (
            "src/foo.cpp:12:5: error: 'bar' was not declared in this scope\n"
            "src/foo.cpp:2:10: fatal error: 'missing.h' file not found\n"
            "ld.lld: error: undefined symbol: MissingNativeFn\n"
            "warning: PT_LOAD segment not aligned for 16384\n"
        )
        diag = extract_compiler_diagnostics(log)
        self.assertEqual(diag["error_count"], 2)
        self.assertEqual(diag["undefined_symbols"], ["MissingNativeFn"])
        self.assertGreaterEqual(len(diag["hints"]), 3)

    def test_resumable_pipeline_reuses_cache_and_recovers_interrupted_stage(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            src_dir = root / "src"
            out_dir = root / "out"
            src_dir.mkdir()
            f1 = src_dir / "a.txt"
            f2 = src_dir / "b.txt"
            f1.write_text("alpha", encoding="utf-8")
            f2.write_text("beta", encoding="utf-8")
            calls = []

            def transform(item: WorkItem):
                calls.append(item.item_id)
                item.outputs[0].parent.mkdir(parents=True, exist_ok=True)
                data = item.inputs[0].read_text(encoding="utf-8").upper()
                item.outputs[0].write_text(data + "|" + item.params.get("tag", "v1"), encoding="utf-8")

            def fail_once(item: WorkItem):
                calls.append(item.item_id)
                if not item.params.get("fixed"):
                    raise RuntimeError("Simulated mid-stage interruption")
                item.outputs[0].write_text("packed", encoding="utf-8")

            o1, o2 = out_dir / "a.out", out_dir / "b.out"
            pkg_out = out_dir / "pkg.bin"
            runner = PipelineRunner(out_dir, pipeline_name="test-app")

            # Run 1: stage 1 completes, stage 2 fails.
            stages_run1 = [
                PipelineStage(
                    "transform",
                    [
                        WorkItem("a", [f1], [o1], transform, {"tag": "v1"}),
                        WorkItem("b", [f2], [o2], transform, {"tag": "v1"}),
                    ],
                ),
                PipelineStage(
                    "package",
                    [WorkItem("pkg", [o1, o2], [pkg_out], fail_once, {"fixed": False})],
                    parallel=False,
                ),
            ]
            r1 = runner.run(stages_run1)
            self.assertFalse(r1["success"])
            self.assertEqual(set(calls), {"a", "b", "pkg"})

            # Run 2 (resume after fixing stage 2): stage 1 must hit cache (0 executions).
            calls.clear()
            stages_run2 = [
                stages_run1[0],
                PipelineStage(
                    "package",
                    [WorkItem("pkg", [o1, o2], [pkg_out], fail_once, {"fixed": True})],
                    parallel=False,
                ),
            ]
            r2 = runner.run(stages_run2)
            self.assertTrue(r2["success"])
            self.assertEqual(calls, ["pkg"])
            self.assertEqual(r2["stages"][0]["cached"], 2)
            self.assertEqual(r2["stages"][0]["executed"], 0)
            self.assertIn("bottleneck_stage", r2)

            # Run 3: modify f1 -> only item 'a' and downstream 'pkg' re-execute; 'b' stays cached.
            calls.clear()
            f1.write_text("alpha-2", encoding="utf-8")
            r3 = runner.run(stages_run2)
            self.assertTrue(r3["success"])
            self.assertEqual(calls, ["a", "pkg"])
            self.assertEqual(r3["stages"][0]["cached"], 1)
            self.assertEqual(r3["stages"][0]["executed"], 1)

    def test_pipeline_stop_event_and_git_checkout_guard(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            git_dir = root / "repo"
            (git_dir / ".git").mkdir(parents=True)
            with self.assertRaises(ValueError):
                PipelineRunner(git_dir / "out")

            stop = threading.Event()
            stop.set()
            src = root / "in.txt"
            src.write_text("x", encoding="utf-8")
            runner = PipelineRunner(root / "out")
            res = runner.run(
                [PipelineStage("s1", [WorkItem("x", [src], [root / "out/x"], lambda _: None)])],
                stop_event=stop,
            )
            self.assertFalse(res["success"])
            self.assertTrue(res["interrupted"])

    def test_hard_exit_preserves_checkpoint_and_refuses_unverified_locks(self):
        import hashlib
        import sys
        import textwrap
        import time

        with tempfile.TemporaryDirectory(prefix="bf-hard-exit-") as tmp:
            root = Path(tmp).resolve()
            if any((p / ".git").exists() for p in (root, *root.parents)):
                self.skipTest("No eligible scratch outside Git checkout")
            out = root / "out"
            child_code = textwrap.dedent('''
                import sys
                import time
                from pathlib import Path
                from pipeline import PipelineRunner, PipelineStage, WorkItem

                out = Path(sys.argv[1])
                a, b = out / "a.bin", out / "b.bin"

                def stage_a(item):
                    a.write_bytes(b"verified-a")

                def stage_b(item):
                    b.write_bytes(b"partial-b")
                    (out / "entered-b").touch()
                    while True:
                        time.sleep(0.1)

                PipelineRunner(out, state_flush_interval_s=3600).run([
                    PipelineStage("A", [WorkItem("a", [], [a], stage_a)]),
                    PipelineStage("B", [WorkItem("b", [a], [b], stage_b)]),
                ])
            ''')
            child = subprocess.Popen(
                [sys.executable, "-c", child_code, str(out)],
                cwd=str(Path(__file__).resolve().parents[1]),
            )
            try:
                deadline = time.monotonic() + 20
                while not (out / "entered-b").is_file():
                    if child.poll() is not None or time.monotonic() >= deadline:
                        self.fail("Synthetic child did not enter stage B")
                    time.sleep(0.02)
                lock = out / ".pipeline.lock"
                original_lock = lock.read_bytes()
                self.assertEqual(json.loads(original_lock)["pid"], child.pid)
                state_path = out / "pipeline_state.json"
                original_state = state_path.read_bytes()
                state = json.loads(original_state)
                a = out / "a.bin"
                self.assertEqual(a.read_bytes(), b"verified-a")
                record = state["stages"]["A"]["items"]["a"]
                self.assertEqual(record["status"], "completed")
                self.assertEqual(record["outputs"][str(a.resolve())], hashlib.sha256(b"verified-a").hexdigest())
                self.assertNotIn("b", state["stages"].get("B", {}).get("items", {}))
                self.assertEqual((out / "b.bin").read_bytes(), b"partial-b")
                def must_not_run(_item):
                    self.fail("A competing writer entered the pipeline")

                restart_stages = [
                    PipelineStage("A", [WorkItem("a", [], [a], must_not_run)]),
                    PipelineStage("B", [WorkItem("b", [a], [out / "b.bin"], must_not_run)]),
                ]
                self.assertIsNone(child.poll())
                with self.assertRaises(FileExistsError):
                    PipelineRunner(out).run(restart_stages)
                self.assertEqual(lock.read_bytes(), original_lock)
                child.kill()
                self.assertNotEqual(child.wait(timeout=10), 0)
                # A real abrupt exit bypasses the child's finally block. Until
                # an explicit safe recovery exists, do not silently steal its lock.
                with self.assertRaises(FileExistsError):
                    PipelineRunner(out).run(restart_stages)
                self.assertEqual(lock.read_bytes(), original_lock)
                self.assertEqual(state_path.read_bytes(), original_state)
                self.assertEqual(a.read_bytes(), b"verified-a")
                self.assertEqual((out / "b.bin").read_bytes(), b"partial-b")

                ambiguous = root / "ambiguous"
                ambiguous.mkdir()
                ambiguous_lock = ambiguous / ".pipeline.lock"
                ambiguous_lock.write_bytes(b"unverified fixture")
                with self.assertRaises(FileExistsError):
                    PipelineRunner(ambiguous).run([])
                self.assertEqual(ambiguous_lock.read_bytes(), b"unverified fixture")
            finally:
                if child.poll() is None:
                    child.kill()
                child.wait(timeout=10)

    @unittest.skipUnless(shutil.which("clang") and shutil.which("ld.lld"), "Clang/LLD required")
    def test_verify_android_apk_elfs_real_binary(self):
        import zipfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            src, lib4, lib16 = root / "x.c", root / "lib4.so", root / "lib16.so"
            src.write_text("int f(void) { return 7; }\n", encoding="utf-8")
            for page, out in ((4096, lib4), (16384, lib16)):
                subprocess.run(
                    [
                        "clang", "--target=aarch64-linux-android21", "-fuse-ld=lld",
                        "-shared", "-nostdlib", "-fPIC", f"-Wl,-z,max-page-size={page}",
                        str(src), "-o", str(out),
                    ],
                    check=True,
                    capture_output=True,
                    timeout=30,
                )
            apk = root / "test.apk"
            with zipfile.ZipFile(apk, "w") as zf:
                zf.write(lib4, "lib/arm64-v8a/libfour.so")
                zf.write(lib16, "lib/arm64-v8a/libsixteen.so")
            audit4 = verify_android_apk_elfs(apk, required_page_size=4096)
            self.assertTrue(audit4["compatible"])
            audit16 = verify_android_apk_elfs(apk, required_page_size=16384)
            self.assertFalse(audit16["compatible"])
            self.assertEqual(audit16["offending"], ["libfour.so"])

    def test_throttled_state_flush_persists_all_items_on_completion_and_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            src_dir = root / "src"
            out_dir = root / "out"
            src_dir.mkdir()
            items = []
            for i in range(12):
                inp = src_dir / f"in_{i}.txt"
                out = out_dir / f"out_{i}.txt"
                inp.write_text(str(i), encoding="utf-8")

                def _fn(w: WorkItem, idx: int = i):
                    w.outputs[0].parent.mkdir(parents=True, exist_ok=True)
                    w.outputs[0].write_text(f"ok-{idx}", encoding="utf-8")

                items.append(WorkItem(f"item-{i}", [inp], [out], _fn))

            runner = PipelineRunner(out_dir, pipeline_name="throttle-test", state_flush_interval_s=60.0)
            import pipeline as pipeline_mod

            orig_atomic = pipeline_mod.atomic_json
            state_writes = []

            def spy_atomic(path: Path, payload: dict):
                if Path(path).name == "pipeline_state.json":
                    state_writes.append(len(payload["stages"].get("s1", {}).get("items", {})))
                return orig_atomic(path, payload)

            with patch.object(pipeline_mod, "atomic_json", side_effect=spy_atomic):
                rep = runner.run([PipelineStage("s1", items, parallel=False)])

            self.assertTrue(rep["success"])
            # One consolidated state write at stage completion instead of 12 per-item writes.
            self.assertEqual(state_writes, [12])
            saved = json.loads((out_dir / "pipeline_state.json").read_text(encoding="utf-8"))
            self.assertEqual(len(saved["stages"]["s1"]["items"]), 12)

            # A failed item must force an immediate state flush despite a large flush interval.
            def _fail(_w: WorkItem):
                raise RuntimeError("boom")

            fail_in = src_dir / "fail.txt"
            fail_in.write_text("x", encoding="utf-8")
            state_writes.clear()
            with patch.object(pipeline_mod, "atomic_json", side_effect=spy_atomic):
                rep2 = runner.run([
                    PipelineStage("s2", [WorkItem("fail-item", [fail_in], [out_dir / "fail.out"], _fail)], parallel=False)
                ])
            self.assertFalse(rep2["success"])
            self.assertEqual(len(state_writes), 1)
            saved2 = json.loads((out_dir / "pipeline_state.json").read_text(encoding="utf-8"))
            self.assertEqual(saved2["stages"]["s2"]["items"]["fail-item"]["status"], "failed")


if __name__ == "__main__":
    unittest.main()
