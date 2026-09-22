"""Speed work: triage, proportional phases, the solo fast path, verification shortcuts and the time breakdown.

The pipeline tests drive the real Manager and Pipeline against a throwaway git repository with a stubbed agent
runner (canned envelopes), so no agent CLI, network or real settings are touched.

    python -m unittest tests.test_speed -v
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = tempfile.mkdtemp(prefix="relay-speed-test-")
os.environ["RELAY_DATA_DIR"] = TMP
sys.path.insert(0, str(ROOT))

from orchestrator import judge, solo, timing, triage, verifyfast  # noqa: E402


def task(req, template="feature", **kw):
    return {"id": "t1", "name": req[:40], "requirements": req, "template": template, **kw}


class TriageTest(unittest.TestCase):
    def test_small_ui_change_is_solo(self):
        t = task("in fenders page make fender compression in berthing metrics to have an average line /metrics")
        h = triage.heuristic(t)
        d = triage.decide(t, {}, h)
        self.assertEqual(h["level"], "simple")
        self.assertTrue(h["confident"])
        self.assertEqual(d["mode"], "solo")

    def test_redesign_goes_to_team_with_exploration(self):
        t = task("I need a complete new idea dont like the polygons or sprites")
        d = triage.decide(t, {}, triage.heuristic(t))
        self.assertEqual(d["mode"], "team")
        self.assertTrue(d["ui_redesign"])
        self.assertTrue(triage.exploration_wanted({**d}, False, "auto")[0])

    def test_performance_request_that_keeps_the_look_never_explores(self):
        t = task("the map feels slow when panning; find the bottleneck and make it smooth, and visually it must look identically the same")
        h = triage.heuristic(t)
        self.assertTrue(h["not_visual"])
        d = triage.decide(t, {}, h)
        self.assertFalse(d["ui_redesign"])
        want, why = triage.exploration_wanted({**d, "mode": "team"}, False, "auto")
        self.assertFalse(want)
        self.assertIn("look unchanged", why)
        self.assertFalse(triage.research_wanted(d)[0])

    def test_small_ui_tweak_is_not_a_redesign(self):
        t = task("slightly tweak the toggle label colour on the playback bar so it is clearer")
        d = triage.decide(t, {}, triage.heuristic(t))
        self.assertFalse(d["ui_redesign"])
        self.assertEqual(d["mode"], "solo")

    def test_risk_and_several_repositories_mean_team(self):
        t = task("add password reset to the login flow")
        self.assertEqual(triage.decide(t, {}, triage.heuristic(t))["mode"], "team")
        t2 = task("add a column", repos=["/r/b"])
        d2 = triage.decide(t2, {}, triage.heuristic(t2))
        self.assertEqual((d2["mode"], d2["level"]), ("team", "complex"))

    def test_explicit_mode_wins(self):
        t = task("add password reset to the login flow", workflow={"team_mode": "solo"})
        self.assertEqual(triage.decide(t, {}, triage.heuristic(t))["mode"], "solo")
        t = task("fix a typo", workflow={"team_mode": "team"})
        self.assertEqual(triage.decide(t, {}, triage.heuristic(t))["mode"], "team")

    def test_rating_settles_borderline(self):
        t = task("scan the optimizer service, find holes and bugs and improve speed and reliability")
        h = triage.heuristic(t)
        self.assertFalse(h["confident"])
        d = triage.decide(t, {}, h, {"level": "complex", "reason": "open-ended", "needs_team": True, "risk": [], "ui_redesign": False})
        self.assertEqual(d["mode"], "team")
        self.assertIn("rating", d["why"])

    def test_parse_rating(self):
        self.assertEqual(triage.parse_rating('sure: {"level":"moderate","reason":"x","needs_team":false,"risk":[],"ui_redesign":false}')["level"], "moderate")
        self.assertIsNone(triage.parse_rating("no json here"))
        self.assertIsNone(triage.parse_rating('{"level":"huge"}'))

    def test_design_is_proportional(self):
        self.assertFalse(triage.design_wanted({"level": "moderate", "contracts": False}, "auto", {"level": "moderate"}, False)[0])
        self.assertTrue(triage.design_wanted({"level": "moderate", "contracts": True}, "auto", {"level": "moderate"}, False)[0])
        self.assertTrue(triage.design_wanted({"level": "simple"}, "auto", {"level": "complex"}, False)[0])
        self.assertTrue(triage.design_wanted({"level": "simple"}, "always", {}, False)[0])
        self.assertFalse(triage.design_wanted({"level": "complex"}, "never", {}, True)[0])

    def test_check_wanted(self):
        self.assertFalse(triage.check_wanted({"risk": []}, {"files": 3, "insertions": 40, "deletions": 2}, [{"path": "src/a.vue"}])[0])
        self.assertTrue(triage.check_wanted({"risk": []}, {"files": 3, "insertions": 40}, [{"path": "db/migrations/001.sql"}])[0])
        self.assertTrue(triage.check_wanted({"risk": ["auth"]}, {}, [])[0])
        self.assertTrue(triage.check_wanted({"risk": []}, {"files": 30, "insertions": 2000}, [])[0])


class HumanTextTest(unittest.TestCase):
    def test_waivers_and_delivery(self):
        self.assertEqual(solo.waiver_ids("Waive D1 and deliver the performance PR", ["A1", "D1"]), ["D1"])
        self.assertEqual(solo.waiver_ids("D1 looks fine", ["D1"]), [])
        self.assertEqual(solo.waiver_ids("waive Z9", ["D1"]), [])
        self.assertTrue(solo.wants_delivery("just make a pr"))
        self.assertTrue(solo.wants_delivery("Waive D1 and deliver the performance PR"))
        self.assertFalse(solo.wants_delivery("do not open a PR yet"))
        self.assertFalse(solo.wants_delivery("the PR description should say X"))


class VerifyFastTest(unittest.TestCase):
    def test_build_classification(self):
        self.assertTrue(verifyfast.is_build("npm run build"))
        self.assertFalse(verifyfast.is_build("npm run lint"))
        self.assertFalse(verifyfast.is_build("npm run test"))
        self.assertFalse(verifyfast.is_build("npx tsc --noEmit"))

    def test_build_needed(self):
        self.assertFalse(verifyfast.build_needed(["tests/a.spec.js", "docs/x.md", "README.md"])[0])
        self.assertTrue(verifyfast.build_needed(["tests/a.spec.js", "src/a.vue"])[0])
        self.assertTrue(verifyfast.build_needed([])[0])

    def test_groups(self):
        g = verifyfast.plan_groups(["npm run lint", "npm run test", "npm run build"], True)
        self.assertEqual(g, [["npm run lint", "npm run test"], ["npm run build"]])
        self.assertEqual(len(verifyfast.plan_groups(["a", "b"], False)), 2)
        out = verifyfast.run_group(["x", "y", "z"], lambda c: c.upper())
        self.assertEqual(out, {"x": "X", "y": "Y", "z": "Z"})

    def test_baseline_cache(self):
        self.assertIsNone(verifyfast.baseline_get("/r", "abc", "npm test"))
        verifyfast.baseline_put("/r", "abc", "npm test", {"ok": False, "rc": 1})
        self.assertEqual(verifyfast.baseline_get("/r", "abc", "npm test")["rc"], 1)

    def test_deps_cache_roundtrip(self):
        repo = Path(tempfile.mkdtemp(dir=TMP))
        wt1, wt2 = repo / "wt1", repo / "wt2"
        for wt in (wt1, wt2):
            wt.mkdir()
            (wt / "package.json").write_text('{"name":"x"}')
            (wt / "package-lock.json").write_text('{"lockfileVersion":3}')
        (wt1 / "node_modules" / "left-pad").mkdir(parents=True)
        (wt1 / "node_modules" / "left-pad" / "index.js").write_text("module.exports=1")
        self.assertIsNone(verifyfast.deps_restore(str(repo), wt2))
        self.assertTrue(verifyfast.deps_save(str(repo), wt1))
        got = verifyfast.deps_restore(str(repo), wt2)
        self.assertTrue(got)
        self.assertEqual((wt2 / "node_modules" / "left-pad" / "index.js").read_text(), "module.exports=1")
        self.assertFalse((wt2 / "node_modules" / ".relay-complete").exists())
        # A different lockfile is a different key.
        wt3 = repo / "wt3"
        wt3.mkdir()
        (wt3 / "package.json").write_text('{"name":"x"}')
        (wt3 / "package-lock.json").write_text('{"lockfileVersion":3,"x":1}')
        self.assertIsNone(verifyfast.deps_restore(str(repo), wt3))


class TimingTest(unittest.TestCase):
    def test_breakdown_from_events_and_turns(self):
        base = 1_800_000_000.0

        def iso(s):
            from datetime import datetime
            return datetime.fromtimestamp(base + s).isoformat(timespec="seconds")
        t = {"created_at": iso(-600), "started_at": iso(0), "finished_at": iso(400),
             "events": [{"time": iso(0), "title": "Task started"}, {"time": iso(1), "title": "Preparing environment"},
                        {"time": iso(30), "title": "Environment ready"}, {"time": iso(60), "title": "Plan agreed"},
                        {"time": iso(300), "title": "Supervisor declared the task complete"},
                        {"time": iso(390), "title": "Verification passed"}, {"time": iso(391), "title": "Committed"}],
             "metrics": {"log": [{"start": base + 30, "end": base + 60, "role": "supervisor"},
                                 {"start": base + 60, "end": base + 280, "role": "worker"},
                                 {"start": base + 280, "end": base + 300, "role": "supervisor"}]}}
        msgs = [{"ts": base + 1, "kind": "command", "role": "setup", "duration": 28.0},
                {"ts": base + 300, "kind": "command", "role": "verify", "duration": 85.0}]
        b = timing.breakdown(t, msgs)
        self.assertEqual(b["queue_seconds"], 600)
        self.assertEqual(b["active_seconds"], 400)
        phases = {p["key"]: p for p in b["phases"]}
        self.assertEqual(round(phases["build"]["seconds"]), 240)
        self.assertEqual(round(phases["build"]["kinds"]["worker"]), 220)
        self.assertEqual(round(phases["verify"]["kinds"]["checks"]), 85)
        self.assertAlmostEqual(sum(p["seconds"] for p in b["phases"]), 1000, delta=1)
        kinds = {k["key"]: k["seconds"] for k in b["kinds"]}
        self.assertEqual(round(kinds["worker"]), 220)

    def test_attribute_priority(self):
        acc = timing._attribute(0, 100, [(0, 50, "worker"), (10, 20, "checks"), (60, 70, "human")])
        self.assertEqual(round(acc["worker"]), 50)
        self.assertEqual(round(acc["human"]), 10)
        self.assertEqual(round(acc["overhead"]), 40)


# ----------------------------------------------------------------------------- the solo pipeline, end to end with a stub agent
def git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


class StubRunnerMixin:
    """Canned agent replies: a solo report that writes a file and proves its criterion."""

    replies: list = []

    def run_agent(self, role, agent_name, prompt, cwd, cfg, model, session, run_dir, label="", turn=None, effort=""):
        self.calls.append((role, label, prompt[:200]))
        make = self.replies.pop(0)
        text = make(Path(cwd), prompt)
        sess = {**(session or {}), "agent": agent_name, "id": session.get("id") or f"s-{role}", "turns": int(session.get("turns") or 0) + 1}
        return {"ok": True, "session": sess, "text": text, "last_message": text}


class SoloPipelineTest(unittest.TestCase):
    def setUp(self):
        from orchestrator import config as C
        self.repo = Path(tempfile.mkdtemp(dir=TMP)) / "app"
        self.repo.mkdir()
        (self.repo / "app.py").write_text("def add(a, b):\n    return a + b\n")
        git("init", "-q", "-b", "main", cwd=self.repo)
        git("-c", "user.email=t@t", "-c", "user.name=t", "add", "-A", cwd=self.repo)
        git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init", cwd=self.repo)
        C.update({"github_auto_push_on_pass": False, "github_auto_create_pr": False, "retro_enabled": False, "env_prepare": False,
                  "auto_detect_verification": False, "design_gate": False, "triage_rating": False, "lessons_inject": False})

    def run_task(self, req, replies, **payload):
        from orchestrator import manager as M
        from orchestrator.pipeline import orchestrate
        from orchestrator.runner import Runner
        m = M.Manager(lambda *a, **k: None)
        t = m.create_task({"repo": str(self.repo), "requirements": req, "queue": False,
                           "workflow": {"roles": {"supervisor": {"agent": "claude"}, "worker": {"agent": "codex"}, "reviewer": {"agent": ""}},
                                        **payload.get("workflow", {})}})

        class R(StubRunnerMixin, Runner):
            pass
        r = R(t["id"], m)
        r.calls = []
        R.replies = list(replies)
        m.store.update(t["id"], status="running", started_at=time.strftime("%Y-%m-%dT%H:%M:%S"))
        orchestrate(m.store.get(t["id"]), r, m)
        return m, m.store.get(t["id"]), r

    def test_solo_run_is_one_agent_turn(self):
        def work(cwd, prompt):
            self.assertIn("working alone", prompt)
            (cwd / "app.py").write_text("def add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n    return a - b\n")
            return "done\n```json\n" + json.dumps({
                "type": "report", "status": "complete", "summary": "added sub", "plan": "1. add sub()",
                "acceptance": [{"id": "A1", "criterion": "sub(a,b) returns a-b", "how_to_verify": "inspection: app.py:5",
                                "required": True, "status": "met", "evidence": "app.py:5 returns a - b"}],
                "complexity": {"level": "simple", "reason": "one function"}, "report": "- added sub", "files": ["app.py"]}) + "\n```"
        m, t, r = self.run_task("add a sub function next to add in app.py", [work])
        self.assertEqual(t["status"], "done", t.get("error"))
        self.assertEqual([c[0] for c in r.calls], ["worker"])
        self.assertEqual(t["triage"]["mode"], "solo")
        self.assertEqual(t["acceptance"][0]["status"], "met")
        self.assertFalse(t["ceremony"]["review"]["run"])
        log = subprocess.run(["git", "log", "--oneline", t["branch"]], cwd=self.repo, capture_output=True, text=True).stdout
        self.assertIn("agent:", log)
        keys = [p["key"] for p in timing.breakdown(t, m.store.messages(t["id"]))["phases"]]
        self.assertIn("build", keys)
        self.assertIn("deliver", keys)

    def test_unproven_criterion_gets_one_nudge_then_delivers(self):
        def work(cwd, prompt):
            (cwd / "app.py").write_text("def add(a, b):\n    return a + b  # checked\n")
            return "```json\n" + json.dumps({"type": "report", "status": "complete", "summary": "x",
                                             "acceptance": [{"id": "A1", "criterion": "c", "how_to_verify": "test: x", "required": True,
                                                             "status": "met", "evidence": "ok"}]}) + "\n```"

        def prove(cwd, prompt):
            self.assertIn("ACCEPTANCE NOT PROVEN", prompt)
            return "```json\n" + json.dumps({"type": "report", "status": "complete", "summary": "x",
                                             "acceptance": [{"id": "A1", "criterion": "c", "how_to_verify": "test: x", "required": True,
                                                             "status": "met", "evidence": "`python -c 'import app'` → exit 0"}]}) + "\n```"
        m, t, r = self.run_task("tweak the add comment", [work, prove])
        self.assertEqual(t["status"], "done", t.get("error"))
        self.assertEqual(len(r.calls), 2)
        self.assertEqual(t["acceptance"][0]["status"], "met")

    def test_needs_team_escalates_to_supervisor(self):
        def work(cwd, prompt):
            (cwd / "notes.txt").write_text("partial")
            return "```json\n" + json.dumps({"type": "report", "status": "partial", "summary": "bigger than it looked", "needs_team": True}) + "\n```"

        def plan(cwd, prompt):
            self.assertIn("SOLO ATTEMPT BEFORE YOU", prompt)
            return "```json\n" + json.dumps({"type": "plan", "summary": "finish", "plan": "1. finish", "instruction": "finish it",
                                             "acceptance": [{"id": "A1", "criterion": "notes exist", "how_to_verify": "inspection: notes.txt",
                                                             "required": True}], "complexity": {"level": "moderate", "reason": "r"}}) + "\n```"

        def finish(cwd, prompt):
            (cwd / "notes.txt").write_text("complete")
            return "```json\n" + json.dumps({"type": "report", "status": "complete", "summary": "finished"}) + "\n```"

        def done(cwd, prompt):
            return "```json\n" + json.dumps({"type": "decision", "decision": "done", "summary": "ok",
                                             "criteria": [{"id": "A1", "status": "met", "evidence": "notes.txt:1 says complete"}]}) + "\n```"
        m, t, r = self.run_task("write the notes file", [work, plan, finish, done])
        self.assertEqual(t["status"], "done", t.get("error"))
        self.assertEqual([c[0] for c in r.calls], ["worker", "supervisor", "worker", "supervisor"])
        self.assertEqual(t["triage"]["mode"], "team")


if __name__ == "__main__":
    unittest.main()
