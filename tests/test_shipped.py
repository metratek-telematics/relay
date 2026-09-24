"""Honest success (#65): what counts as shipped, what the backfill can and cannot tell,
and the guards that run before delivery.

Nothing here talks to GitHub or to a real repository except the temporary git repositories the
stale-branch tests create for themselves.

    RELAY_DATA_DIR is set to a temporary folder before Relay is imported, so nothing touches real data.
    python -m unittest tests.test_shipped -v
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = tempfile.mkdtemp(prefix="relay-shipped-test-")
os.environ.setdefault("RELAY_DATA_DIR", TMP)
sys.path.insert(0, str(ROOT))

from orchestrator import autopilot, predelivery, shipped  # noqa: E402


def card(**over) -> dict:
    """A scorecard of a delivered run, merged and untouched unless the test says otherwise."""
    pr = {"url": "https://github.com/acme/app/pull/7", "number": 7, "state": "merged", "merged_at": "2026-09-01T10:00:00Z",
          "merge_sha": "abc1234", "human_commits": 0, "base": "main"}
    pr.update(over.pop("pr", None) or {})
    c = {"task_id": "t1", "name": "A task", "finished_at": "2026-09-01T09:00:00", "outcome": "delivered_pr",
         "repo": "acme/app", "repo_label": "acme/app", "pairing": "claude→codex", "pairing_label": "Claude → Codex",
         "review_rounds": 1, "score": 90, "team": {"worker": {"agent": "claude"}, "reviewer": {"agent": "codex"}}, "pr": pr}
    c.update(over)
    return c


def task(**over) -> dict:
    t = {"id": "t1", "name": "A task", "github_repo": "acme/app", "status": "done",
         "delivery_check": {"base_behind": 0, "conflicted": False, "ref": "origin/main"},
         "deploy": {"status": "succeeded"}, "changed_files": [{"path": "src/app.py"}]}
    t.update(over)
    return t


CLEAN = {"checked": True, "commits": 0, "files": [], "authors": []}
TOUCHED = {"checked": True, "commits": 2, "files": ["src/app.py"], "authors": ["ergys"]}


class SuccessDefinition(unittest.TestCase):
    """merged AND deployed AND untouched by a person."""

    def test_merged_deployed_untouched_is_shipped(self):
        r = shipped.record(task(), card(), deploy_targets=1, post_merge=CLEAN)
        self.assertEqual(r["verdict"], "shipped")
        self.assertEqual(r["reasons"], [])
        self.assertEqual(r["missing"], [])

    def test_no_deployment_target_still_ships(self):
        """A repository with nothing to deploy ships when it merges; the record says why."""
        r = shipped.record(task(deploy={}), card(), deploy_targets=0, post_merge=CLEAN)
        self.assertEqual(r["verdict"], "shipped")
        self.assertEqual(r["facts"]["deploy_state"], "none_configured")

    def test_merged_but_hand_edited_is_not_a_success(self):
        r = shipped.record(task(), card(), deploy_targets=1, post_merge=TOUCHED)
        self.assertEqual(r["verdict"], "rescued")
        self.assertEqual([x["code"] for x in r["reasons"]], ["human_touched"])

    def test_commits_pushed_to_the_branch_count_as_a_person(self):
        r = shipped.record(task(), card(pr={"human_commits": 1}), deploy_targets=1, post_merge=CLEAN)
        self.assertEqual(r["verdict"], "rescued")
        self.assertIn("human_touched", [x["code"] for x in r["reasons"]])

    def test_delivered_but_unmergeable_is_not_a_success(self):
        """The failure that started this: a pull request opened against a stale base that never merged."""
        r = shipped.record(task(delivery_check={"base_behind": 38, "conflicted": True, "ref": "origin/main"}),
                           card(pr={"state": "open", "merged_at": None}), deploy_targets=1)
        self.assertEqual(r["verdict"], "rescued")
        codes = [x["code"] for x in r["reasons"]]
        self.assertEqual(codes, ["pr_open", "merge_conflicted", "stale_base"])
        self.assertIn("38 commits behind origin/main", r["reasons"][2]["text"])

    def test_closed_without_merging_is_not_a_success(self):
        r = shipped.record(task(), card(pr={"state": "closed", "merged_at": None}), deploy_targets=1)
        self.assertEqual(r["verdict"], "rescued")
        self.assertIn("pr_closed", [x["code"] for x in r["reasons"]])

    def test_failed_deployment_is_not_a_success(self):
        r = shipped.record(task(deploy={"status": "failed"}), card(), deploy_targets=1, post_merge=CLEAN)
        self.assertEqual(r["verdict"], "rescued")
        self.assertIn("deploy_failed", [x["code"] for x in r["reasons"]])

    def test_reverted_within_a_day(self):
        r = shipped.record(task(), card(pr={"reverted": True, "revert_at": "2026-09-01T18:00:00Z"}),
                           deploy_targets=1, post_merge=CLEAN)
        self.assertEqual(r["verdict"], "rescued")
        self.assertIn("reverted_fast", [x["code"] for x in r["reasons"]])
        self.assertTrue(r["facts"]["reverted_within_day"])

    def test_a_failed_run_never_shipped(self):
        c = card(outcome="failed")
        c["pr"] = None
        r = shipped.record(task(status="failed"), c, deploy_targets=1)
        self.assertEqual(r["verdict"], "rescued")
        self.assertEqual(r["reasons"][0]["code"], "not_delivered")


class Backfill(unittest.TestCase):
    """What cannot be reconstructed is marked unknown, not guessed."""

    def test_unknown_when_nobody_checked_the_later_commits(self):
        rows = shipped.backfill([task(deploy={})], {"t1": card(pr={"human_commits": None})},
                                gh_json=None, targets_for_repo=lambda r: [])
        self.assertEqual(rows[0]["verdict"], "unknown")
        self.assertIn("human_touched", rows[0]["missing"])
        self.assertEqual(rows[0]["facts"]["human_touched_scope"], "nothing was checked")

    def test_a_no_says_how_far_it_was_checked(self):
        """The branch was checked and was clean; the record does not pretend the base branch was too."""
        rows = shipped.backfill([task(deploy={})], {"t1": card()}, gh_json=None, targets_for_repo=lambda r: [])
        self.assertEqual(rows[0]["verdict"], "shipped")
        self.assertEqual(rows[0]["facts"]["human_touched_scope"], "branch only")

    def test_unknown_deployment_is_not_read_as_success(self):
        rows = shipped.backfill([task(deploy={})], {"t1": card()}, gh_json=None, targets_for_repo=lambda r: [{"id": "web"}])
        self.assertEqual(rows[0]["verdict"], "unknown")
        self.assertIn("deployed", rows[0]["missing"])

    def test_a_known_failure_beats_a_missing_fact(self):
        """An unknown never hides a failure that is already known."""
        rows = shipped.backfill([task(deploy={})], {"t1": card(pr={"state": "open", "merged_at": None, "human_commits": None})},
                                gh_json=None, targets_for_repo=lambda r: [])
        self.assertEqual(rows[0]["verdict"], "rescued")

    def test_unknowns_are_left_out_of_the_rate(self):
        recs = [shipped.record(task(), card(task_id=f"s{i}"), deploy_targets=1, post_merge=CLEAN) for i in range(3)]
        recs += [shipped.record(task(), card(task_id=f"r{i}"), deploy_targets=1, post_merge=TOUCHED) for i in range(1)]
        recs += [shipped.record(task(deploy={}), card(task_id="u1"), deploy_targets=None)]
        for r, tid in zip(recs, ["s0", "s1", "s2", "r0", "u1"]):
            r["task_id"] = tid
        s = shipped.summarize(recs, today=__import__("datetime").date(2026, 9, 3))
        self.assertEqual((s["shipped"], s["rescued"], s["unknown"]), (3, 1, 1))
        self.assertEqual(s["known"], 4)
        self.assertAlmostEqual(s["rate"], 0.75)
        self.assertTrue(s["too_few"], "five runs cannot mean anything")
        self.assertEqual(s["min_sample"], 10)
        self.assertEqual([c["code"] for c in s["causes"]], ["human_touched"])

    def test_post_merge_edits_ignore_relay_and_merge_commits(self):
        calls = []

        def gh(args, timeout=0):
            calls.append(args)
            if args[1].startswith("repos/acme/app/commits?"):
                return [{"sha": "abc1234", "commit": {"message": "Merge pull request #7"}},
                        {"sha": "d1", "commit": {"message": "agent: another task"}, "author": {"login": "bot"}},
                        {"sha": "d2", "commit": {"message": "fix the thing relay got wrong"}, "author": {"login": "ergys"}}]
            return {"files": [{"filename": "src/app.py"}]}

        out = shipped.post_merge_edits("acme/app", "abc1234", "2026-09-01T10:00:00Z", ["src/app.py"], gh, prefix="agent:")
        self.assertTrue(out["checked"])
        self.assertEqual(out["commits"], 1)
        self.assertEqual(out["authors"], ["ergys"])

    def test_a_github_failure_leaves_the_fact_unknown(self):
        def boom(args, timeout=0):
            raise RuntimeError("gh: not signed in")

        out = shipped.post_merge_edits("acme/app", "abc", "2026-09-01T10:00:00Z", ["a.py"], boom)
        self.assertFalse(out["checked"])
        self.assertIn("not signed in", out["why"])


# ============================================================================ guards
def git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=False)


def make_repo() -> Path:
    repo = Path(tempfile.mkdtemp(prefix="relay-guard-", dir=TMP))
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "t@t")
    git(repo, "config", "user.name", "t")
    (repo / "a.txt").write_text("one\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "base")
    return repo


class Recorder:
    def __init__(self):
        self.lines = []

    def timeline(self, role, title, detail=""):
        self.lines.append((role, title, detail))


class StaleBranchGuard(unittest.TestCase):
    def setUp(self):
        self.repo = make_repo()
        git(self.repo, "branch", "origin/main")   # a local stand-in for the remote branch
        git(self.repo, "checkout", "-q", "-b", "task")

    def test_a_branch_on_current_code_passes_untouched(self):
        (self.repo / "b.txt").write_text("work\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "agent: work")
        rec = predelivery.base_guard(Recorder(), self.repo, {"github_pr_base": "main"}, {}, fetch=False)
        self.assertEqual(rec["base_behind"], 0)
        self.assertFalse(rec["conflicted"])
        self.assertFalse(rec["rebased"])
        self.assertFalse(rec["needs_human"])

    def test_a_conflicting_base_is_rebased_and_reverified(self):
        (self.repo / "a.txt").write_text("mine\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "agent: work")
        git(self.repo, "checkout", "-q", "origin/main")
        (self.repo / "a.txt").write_text("theirs\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "someone else")
        git(self.repo, "branch", "-f", "origin/main", "HEAD")
        git(self.repo, "checkout", "-q", "task")
        rebased = []

        def fake_rebase(runner, wt, ref):
            rebased.append(ref)
            return {"ok": True, "conflicts": [], "error": ""}

        rec = predelivery.base_guard(Recorder(), self.repo, {"github_pr_base": "main"}, {}, fetch=False, rebaser=fake_rebase)
        self.assertEqual(rebased, ["origin/main"])
        self.assertTrue(rec["rebased"])
        self.assertTrue(rec["reverify"], "verification must run again on the rebased branch")
        self.assertFalse(rec["conflicted"])
        self.assertFalse(rec["needs_human"])

    def test_a_rebase_that_conflicts_asks_instead_of_delivering(self):
        (self.repo / "a.txt").write_text("mine\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "agent: work")
        git(self.repo, "checkout", "-q", "origin/main")
        (self.repo / "a.txt").write_text("theirs\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "someone else")
        git(self.repo, "branch", "-f", "origin/main", "HEAD")
        git(self.repo, "checkout", "-q", "task")
        rec = predelivery.base_guard(Recorder(), self.repo, {"github_pr_base": "main"}, {}, fetch=False)
        self.assertTrue(rec["needs_human"])
        self.assertIn("a.txt", rec["conflicts"])
        self.assertIn("did not deliver", rec["note"])
        # the worktree is left as it was, never half-rebased
        self.assertEqual(git(self.repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip(), "task")

    def test_real_rebase_puts_the_work_on_top_of_the_base(self):
        (self.repo / "b.txt").write_text("work\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "agent: work")
        git(self.repo, "checkout", "-q", "origin/main")
        (self.repo / "c.txt").write_text("theirs\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "someone else")
        git(self.repo, "branch", "-f", "origin/main", "HEAD")
        git(self.repo, "checkout", "-q", "task")
        out = predelivery.rebase(Recorder(), self.repo, "origin/main")
        self.assertTrue(out["ok"], out)
        self.assertEqual(git(self.repo, "merge-base", "--is-ancestor", "origin/main", "HEAD").returncode, 0)

    def test_drift_counts_what_landed_during_the_run(self):
        (self.repo / "b.txt").write_text("work\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "agent: work")
        git(self.repo, "checkout", "-q", "origin/main")
        for i in range(3):
            (self.repo / f"x{i}.txt").write_text("t\n")
            git(self.repo, "add", "-A")
            git(self.repo, "commit", "-q", "-m", f"theirs {i}")
        git(self.repo, "branch", "-f", "origin/main", "HEAD")
        git(self.repo, "checkout", "-q", "task")
        d = predelivery.drift(self.repo, "origin/main", fetch=False)
        self.assertEqual((d["behind"], d["ahead"], d["ok"]), (3, 1, False))


class EvidenceGate(unittest.TestCase):
    """The judge's rules, read once more at the gate; empty proof is refused with a named reason."""

    def crit(self, **over):
        c = {"id": "A1", "criterion": "the unit tests pass", "how_to_verify": "run the tests", "kind": "test",
             "required": True, "status": "met", "evidence": "ran `python -m unittest` and it passed"}
        c.update(over)
        return [c]

    def test_a_tests_pass_claim_with_no_captured_output_is_refused(self):
        res = predelivery.evidence_gate(self.crit(evidence="the test suite is green, see tests/"), None,
                                        {"commands": [], "screenshots": []})
        self.assertFalse(res["ok"])
        self.assertEqual(res["refusals"][0]["id"], "A1")
        self.assertIn("no command output captured", res["refusals"][0]["reason"])

    def test_captured_output_passes(self):
        proof = {"commands": [{"command": "python -m unittest", "ok": True, "captured": True}], "screenshots": []}
        res = predelivery.evidence_gate(self.crit(), {"items": [{"command": "python -m unittest", "ok": True, "rc": 0}]}, proof)
        self.assertTrue(res["ok"], res["refusals"])

    def test_a_ui_change_with_no_browser_evidence_is_refused(self):
        crit = self.crit(id="A2", criterion="the button renders in the sidebar", how_to_verify="screenshot the page",
                         kind="screenshot", evidence="checked the page in the browser, it looks right")
        res = predelivery.evidence_gate(crit, None, {"commands": [], "screenshots": []})
        self.assertFalse(res["ok"])
        self.assertIn("no browser evidence", res["refusals"][0]["reason"])

    def test_a_screenshot_in_the_run_passes(self):
        crit = self.crit(id="A2", criterion="the button renders in the sidebar", how_to_verify="screenshot the page",
                         kind="screenshot", evidence="see runs/t1/sidebar.png")
        res = predelivery.evidence_gate(crit, None, {"commands": [], "screenshots": ["/runs/t1/sidebar.png"]})
        self.assertTrue(res["ok"], res["refusals"])

    def test_the_judges_own_rules_still_apply(self):
        """A criterion nobody marked met is refused by the judge, and the gate says the same thing."""
        res = predelivery.evidence_gate(self.crit(status="unmet", evidence=""), None, {"commands": [], "screenshots": []})
        self.assertFalse(res["ok"])
        self.assertEqual(res["refusals"][0]["reason"], "not reported as met")

    def test_a_failing_relay_check_overrides_the_claim(self):
        verification = {"items": [{"command": "python -m unittest", "ok": False, "rc": 1}]}
        res = predelivery.evidence_gate(self.crit(), verification,
                                        {"commands": [{"command": "python -m unittest", "ok": False, "captured": True}], "screenshots": []})
        self.assertFalse(res["ok"])
        self.assertIn("verification failed", res["refusals"][0]["reason"])

    def test_proof_index_reads_what_relay_captured(self):
        idx = predelivery.proof_index({"items": [{"command": "pytest -q", "ok": True, "rc": 0}]}, artifacts=["out/page.png", "notes.md"])
        self.assertEqual(idx["commands"][0]["captured"], True)
        self.assertEqual(idx["screenshots"], ["out/page.png"])


class NotIdling(unittest.TestCase):
    def crits(self):
        return [{"id": "A1", "criterion": "the deploy key is stored in the vault", "required": True, "status": "unmet"},
                {"id": "A2", "criterion": "the table sorts by date", "required": True, "status": "unmet"},
                {"id": "A3", "criterion": "already done", "required": True, "status": "met"}]

    def test_work_that_the_question_does_not_block_carries_on(self):
        plan = predelivery.keep_working(self.crits(), "Which vault should the deploy key go in?", 0)
        self.assertTrue(plan["go"])
        self.assertEqual([c["id"] for c in plan["items"]], ["A2"])

    def test_a_question_naming_the_only_open_criterion_blocks(self):
        crits = [{"id": "A1", "criterion": "the deploy key is stored in the vault", "required": True, "status": "unmet"}]
        plan = predelivery.keep_working(crits, "A1: which vault should the deploy key go in?", 0)
        self.assertFalse(plan["go"])
        self.assertIn("depends on the answer", plan["reason"])

    def test_it_stops_carrying_on_after_the_agreed_number_of_packages(self):
        plan = predelivery.keep_working(self.crits(), "Which vault?", predelivery.MAX_DEFERRALS)
        self.assertFalse(plan["go"])
        self.assertIn("already waited", plan["reason"])


class RetryOnce(unittest.TestCase):
    def test_a_failed_run_retries_once_with_what_it_learned(self):
        t = {"status": "failed", "error": "verification still fails: `pytest` exit 1", "autopsy": {"cause": "the fixture was never created"}}
        plan = predelivery.retry_plan(t, {})
        self.assertTrue(plan["retry"])
        self.assertEqual(plan["attempt"], 1)
        self.assertIn("pytest", plan["note"])
        self.assertIn("the fixture was never created", plan["note"])

    def test_and_only_once(self):
        t = {"status": "failed", "error": "boom", "honest_retries": 1}
        plan = predelivery.retry_plan(t, {})
        self.assertFalse(plan["retry"])
        self.assertIn("already had its one retry", plan["note"])

    def test_a_run_that_did_not_fail_is_left_alone(self):
        self.assertFalse(predelivery.retry_plan({"status": "done"}, {})["retry"])


class BlockedInOnePlace(unittest.TestCase):
    """Anything genuinely blocked appears in the digest that already exists, with what it needs."""

    def rows(self):
        return [
            {"id": "a", "number": 1, "name": "Vault work", "status": "needs_input", "updated_at": "2026-09-24T07:00:00",
             "pending": {"id": "q1", "question": "Which vault?", "options": ["A", "B"], "time": "2026-09-24T07:00:00"}},
            {"id": "b", "number": 2, "name": "Tile cache", "status": "failed", "honest_retries": 1,
             "error": "verification still fails", "finished_at": "2026-09-24T06:00:00", "updated_at": "2026-09-24T06:00:00"},
            {"id": "c", "number": 3, "name": "Carrying on", "status": "running", "updated_at": "2026-09-24T08:00:00",
             "pending": {"id": "q2", "question": "Which endpoint?", "time": "2026-09-24T08:00:00"},
             "checkpoint": {"deferred_questions": {"q2": 1}}},
            {"id": "d", "number": 4, "name": "Fine", "status": "done", "updated_at": "2026-09-24T05:00:00"},
        ]

    def test_it_lists_what_each_one_needs(self):
        out = autopilot.blocked_items(self.rows())
        self.assertEqual([x["task_id"] for x in out], ["b", "a", "c"])
        self.assertIn("Which vault?", out[1]["needs"])
        self.assertIn("failed again after its one retry", out[0]["needs"])

    def test_a_run_working_around_its_question_says_so(self):
        out = autopilot.blocked_items(self.rows())
        carrying = next(x for x in out if x["task_id"] == "c")
        self.assertTrue(carrying["working_around"])

    def test_the_digest_carries_it(self):
        d = autopilot.build_digest(self.rows(), {}, 0, 9e9, queue_order=[], running=[], parallel=1,
                                   lessons_pending=[], settings={})
        self.assertEqual(d["headline"]["blocked"], 3)
        self.assertEqual(len(d["blocked"]), 3)


if __name__ == "__main__":
    unittest.main()
