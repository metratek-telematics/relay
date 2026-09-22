"""relay-perf and the performance gate: task detection, perf specs, compare/assert, trace analysis, verification.

No browser is needed: the measurement itself is replaced by synthetic perf.json files; compare/assert and the trace
analysis run through node (skipped when node is missing).

    python -m unittest tests.test_perf -v
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.environ.setdefault("RELAY_DATA_DIR", tempfile.mkdtemp(prefix="relay-perf-test-"))
sys.path.insert(0, str(ROOT))

from orchestrator import perfcheck  # noqa: E402

NODE = shutil.which("node")
PERF = str(ROOT / "tools" / "perf.cjs")


def perf_json(fps_p5=20.0, tbt=4000.0, long_max=300.0, scripting=9000.0, heap=1.0, pan_fps_p5=18.0, scenario="s1"):
    s = {"fps_avg": 40.0, "fps_p5": fps_p5, "jank_pct": 8.0, "long_tasks": 30, "long_task_max_ms": long_max, "tbt_ms": tbt,
         "main_busy_pct": 70.0, "scripting_ms": scripting, "heap_growth_mb": heap, "inp_ms": 200.0, "requests": 500,
         "phases": {"pan": {"fps_avg": 38.0, "fps_p5": pan_fps_p5, "long_tasks": 10, "long_task_max_ms": long_max, "tbt_ms": tbt / 2, "main_busy_pct": 60.0}}}
    return {"tool": "relay-perf", "url": "http://x/", "time": "t", "label": "", "scenario_hash": scenario,
            "summary": {"1x": dict(s), "4x": s}, "profiles": [{"steps_ok": True, "navigation_error": None, "detail": {"scenario": {"hot_functions": []}}}],
            "findings": []}


class Detection(unittest.TestCase):
    def test_perf_words(self):
        yes = ["The map is laggy when panning", "Make the vessel map smooth", "Page freezes on load", "FPS drops at zoom 11",
               "Optimise the dashboard", "reduce CPU usage of the map", "fix the memory leak in the chart", "Improve Lighthouse score",
               "Performance: slow first load"]
        no = ["Add a feature flag for exports", "Rename the Save button", "Fix the login redirect", "Update the flagship page copy"]
        for t in yes:
            self.assertTrue(perfcheck.is_perf_task({"name": t}), t)
        for t in no:
            self.assertFalse(perfcheck.is_perf_task({"name": t}), t)
        self.assertFalse(perfcheck.is_perf_task({"name": "slow map", "workflow": {"perf": False}}))
        self.assertTrue(perfcheck.is_perf_task({"name": "anything", "workflow": {"perf": True}}))

    def test_web_repo_only(self):
        d = Path(tempfile.mkdtemp())
        self.assertFalse(perfcheck.is_web_repo(d))
        (d / "package.json").write_text("{}")
        self.assertTrue(perfcheck.is_web_repo(d))

    def test_spec(self):
        self.assertIsNone(perfcheck.normalize_spec({"steps": []}))
        s = perfcheck.normalize_spec({"url": "/map?x=1", "throttle": ["1x", "6x"], "steps": [{"wait": 100}], "targets": ["4x.fps_p5>=45", ""]})
        self.assertEqual(s["throttle"], "1,6")
        self.assertEqual(s["targets"], ["4x.fps_p5>=45"])
        self.assertEqual(s["steps"], [{"wait": 100}])
        self.assertEqual(perfcheck.normalize_spec({"url": "/", "throttle": "rm -rf /"})["throttle"], "1,4")


@unittest.skipUnless(NODE, "node is not installed")
class CompareAssert(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())

    def write(self, name, data):
        p = self.d / name
        p.write_text(json.dumps(data))
        return str(p)

    def run_tool(self, *args):
        r = subprocess.run([NODE, PERF, *args], capture_output=True, text=True, timeout=60)
        return r.returncode, r.stdout

    def test_improved(self):
        b = self.write("b.json", perf_json())
        a = self.write("a.json", perf_json(fps_p5=50, tbt=800, long_max=90, scripting=3000, pan_fps_p5=48))
        rc, out = self.run_tool("compare", b, a, "--json")
        c = json.loads(out)
        self.assertEqual(rc, 0)
        self.assertEqual(c["verdict"], "improved")
        self.assertIn("4x.fps_p5", c["improved"])
        self.assertIn("4x.pan.fps_p5", c["improved"])
        rc, out = self.run_tool("assert", a, "4x.pan.fps_p5>=45", "4x.long_task_max_ms<=100", "4x.tbt_ms<=-50%", "--baseline", b)
        r = json.loads(out)
        self.assertTrue(r["ok"], r)
        self.assertEqual(rc, 0)

    def test_regressed_and_noise(self):
        b = self.write("b.json", perf_json())
        a = self.write("a.json", perf_json(fps_p5=19.5, tbt=4100, heap=30))  # small noise, one real regression (heap)
        c = json.loads(self.run_tool("compare", b, a, "--json")[1])
        self.assertEqual(c["verdict"], "regressed")
        self.assertEqual(c["regressed"], ["1x.heap_growth_mb", "4x.heap_growth_mb"])
        self.assertNotIn("4x.fps_p5", c["improved"] + c["regressed"])

    def test_assert_misses_and_errors(self):
        a = self.write("a.json", perf_json())
        rc, out = self.run_tool("assert", a, "4x.fps_p5>=45", "4x.nope>1", "4x.tbt_ms<=-50%")
        r = json.loads(out)
        self.assertEqual(rc, 1)
        self.assertFalse(r["results"][0]["ok"])
        self.assertIn("no value", r["results"][1]["error"])
        self.assertIn("--baseline", r["results"][2]["error"])

    def test_different_scenarios_warn(self):
        b = self.write("b.json", perf_json(scenario="one"))
        a = self.write("a.json", perf_json(scenario="two"))
        c = json.loads(self.run_tool("compare", b, a, "--json")[1])
        self.assertFalse(c["comparable"])


@unittest.skipUnless(NODE, "node is not installed")
class TraceAnalysis(unittest.TestCase):
    def test_long_tasks_breakdown_and_fps(self):
        script = r"""
        const A = require(process.argv[1]);
        const M = (tid, name) => ({ ph: "M", name: "thread_name", pid: 1, tid, args: { name } });
        const X = (tid, name, ts, dur, args) => ({ ph: "X", name, pid: 1, tid, ts, dur, tdur: dur, args: args || {} });
        const ev = [M(1, "CrRendererMain"), M(2, "DedicatedWorker thread"),
          { ph: "I", name: "TracingStartedInBrowser", pid: 9, tid: 9, ts: 1, args: { data: { frames: [{ frame: "F", url: "http://x/", processId: 1 }] } } },
          X(1, "RunTask", 1000, 120000), X(1, "FunctionCall", 1000, 110000, { data: { frame: "F", url: "http://x/a.js", lineNumber: 9 } }),
          X(1, "Layout", 50000, 10000), X(1, "MinorGC", 70000, 5000),
          X(1, "RunTask", 200000, 30000), X(1, "Paint", 200000, 20000),
          X(2, "RunTask", 0, 250000)];
        const m = A.buildModel(ev);
        const { res } = A.analyze(m, { windows: { scenario: [0, 300000], phases: {} } });
        const fps = A.fpsStats([0, 16.7, 33.4, 50, 250, 266.7], 0, 266.7);
        console.log(JSON.stringify({ res, fps }));
        """
        r = subprocess.run([NODE, "-e", script, str(ROOT / "tools" / "perf_analyze.cjs")], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        sc = out["res"]["scenario"]
        self.assertEqual(sc["long_tasks"], 1)
        self.assertEqual(sc["long_task_max_ms"], 120)
        self.assertEqual(sc["tbt_ms"], 70)
        self.assertEqual(sc["forced_layouts"]["count"], 1)       # Layout inside a FunctionCall
        self.assertEqual(sc["gc"]["count"], 1)
        self.assertGreater(sc["breakdown_ms"]["scripting"], 90)
        self.assertEqual(sc["breakdown_ms"]["painting"], 20)
        self.assertEqual(out["res"]["threads"][0]["thread"], "DedicatedWorker thread")  # the busiest thread first
        self.assertLess(out["fps"]["fps_p5"], 10)                # the 200 ms gap dominates the worst 5% of time
        self.assertGreater(out["fps"]["janky_time_pct"], 50)


class FakeRunner:
    def __init__(self):
        self.shell = []

    def timeline(self, *a, **k):
        pass

    def run_shell(self, cmd, cwd, role="verify", timeout=None, title=None):
        self.shell.append(cmd)
        return {"ok": True, "rc": 0, "output": ""}


class FakeManager:
    def __init__(self):
        self.meta = {}

    def set_meta(self, tid, **kw):
        self.meta.update(kw)


class FakePipeline:
    def __init__(self, wt, run_dir):
        self.task = {"name": "Map is laggy when panning", "requirements": "", "repo": str(wt)}
        self.wt, self.run_dir, self.base, self.tid = Path(wt), Path(run_dir), "HEAD", "t1"
        self.cfg, self.state, self.verify_mode = {}, {}, "each_report"
        self.r, self.m = FakeRunner(), FakeManager()
        self.asked = []

    def save(self):
        pass

    def supervisor_turn(self, prompt, label, turn=None):
        self.asked.append(prompt)
        return {}, {"type": "plan"}


@unittest.skipUnless(NODE, "node is not installed")
class Gate(unittest.TestCase):
    def setUp(self):
        self.wt = Path(tempfile.mkdtemp())
        (self.wt / "package.json").write_text("{}")
        git = ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false"]
        subprocess.run(["git", "init", "-q"], cwd=self.wt, check=True)
        subprocess.run([*git, "add", "-A"], cwd=self.wt, check=True)
        subprocess.run([*git, "commit", "-qm", "init"], cwd=self.wt, check=True)
        self.p = FakePipeline(self.wt, tempfile.mkdtemp())
        self.results = {}
        orig = perfcheck._measure

        def fake_measure(p, wt, label, spec):
            out = Path(p.run_dir) / "perf" / label
            out.mkdir(parents=True, exist_ok=True)
            data = self.results["baseline" if label == "baseline" else "after"]
            (out / "perf.json").write_text(json.dumps(data))
            return {"ok": True, "label": label, "dir": str(out), "summary": data["summary"], "time": "now", "top": []}

        perfcheck._measure = fake_measure
        self.addCleanup(lambda: setattr(perfcheck, "_measure", orig))

    def test_no_spec_fails_verification(self):
        perfcheck.after_plan(self.p, {"type": "plan"})
        self.assertEqual(len(self.p.asked), 1)                  # asked once for the spec
        items, parts = perfcheck.pipeline_verify(self.p)
        self.assertFalse(items[0]["ok"])
        self.assertIn("without a measurement", parts[0])
        self.assertFalse(self.p.m.meta["perf"]["ok"])

    def test_targets_met_passes_and_regression_blocks(self):
        self.results["baseline"] = perf_json()
        perfcheck.after_plan(self.p, {"type": "plan", "perf": {"url": "/", "targets": ["4x.pan.fps_p5>=45", "4x.tbt_ms<=-50%"]}})
        self.assertTrue(self.p.m.meta["perf"]["baseline"]["ok"])
        self.results["after"] = perf_json(fps_p5=50, tbt=800, long_max=90, scripting=3000, pan_fps_p5=48)
        items, parts = perfcheck.pipeline_verify(self.p)
        self.assertTrue(items[0]["ok"], parts)
        self.assertEqual(self.p.m.meta["perf"]["verdict"], "improved")
        # A new change that regresses: the worktree fingerprint changes, the measurement is re-taken and blocks.
        (self.wt / "x.js").write_text("changed")
        self.results["after"] = perf_json(fps_p5=50, tbt=800, long_max=90, scripting=3000, pan_fps_p5=48, heap=40)
        items, parts = perfcheck.pipeline_verify(self.p)
        self.assertFalse(items[0]["ok"])
        self.assertIn("regressed", parts[0])

    def test_not_a_web_repo_or_not_perf(self):
        (self.wt / "package.json").unlink()
        self.assertEqual(perfcheck.pipeline_verify(self.p), ([], []))
        (self.wt / "package.json").write_text("{}")
        self.p.task["name"] = "Rename the Save button"
        self.assertEqual(perfcheck.pipeline_verify(self.p), ([], []))


if __name__ == "__main__":
    unittest.main()
