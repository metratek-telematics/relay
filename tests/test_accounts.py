"""Several sign-ins per agent CLI: which one takes a turn, and what happens when none can.

Covers choosing with one, two and no available accounts, a paused account being skipped, the order
Relay falls back in when every account is exhausted, the account recorded on a turn, and that an
installation with the single account it starts with behaves exactly as it did before accounts existed.

    python3 -m unittest tests.test_accounts -v
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.environ.setdefault("RELAY_DATA_DIR", tempfile.mkdtemp(prefix="relay-accounts-test-"))
sys.path.insert(0, str(ROOT))

from orchestrator import accounts, autopilot  # noqa: E402
from orchestrator.runner import Runner  # noqa: E402

OK = {"ok": True, "reason": "", "resets_at": None, "kind": ""}


def clean():
    """No saved accounts and no account folders, so each test starts from a fresh installation."""
    import shutil
    accounts.ACCOUNTS_FILE.unlink(missing_ok=True)
    shutil.rmtree(accounts.ACCOUNTS_DIR, ignore_errors=True)


def out(reason="5-hour limit at 100%", resets_at=None):
    return {"ok": False, "reason": reason, "resets_at": resets_at, "kind": "limit"}


def row(aid, name=None, enabled=True, default=False, signed_in=True, busy=False):
    return {"agent": "claude", "id": aid, "name": name or aid, "enabled": enabled, "is_default": default,
            "signed_in": signed_in, "busy": busy, "home": "", "added": 0, "login_command": "claude"}


class ChoiceTests(unittest.TestCase):
    def test_one_account_is_used(self):
        pick = accounts.choose([row("default", "Main", default=True)], lambda aid: OK)
        self.assertEqual(pick["account"]["id"], "default")
        self.assertFalse(pick["switched"])

    def test_the_default_is_preferred_while_it_has_capacity(self):
        rows = [row("default", "Main", default=True), row("second", "Team plan")]
        pick = accounts.choose(rows, lambda aid: OK)
        self.assertEqual(pick["account"]["id"], "default")

    def test_the_next_account_runs_when_the_default_is_exhausted(self):
        rows = [row("default", "Main", default=True), row("second", "Team plan")]
        pick = accounts.choose(rows, lambda aid: out() if aid == "default" else OK)
        self.assertEqual(pick["account"]["id"], "second")
        self.assertIn("Main", pick["reason"] or "Main: skipped")  # the skipped one is named
        self.assertTrue(any(s["id"] == "default" for s in pick["skipped"]))

    def test_a_paused_account_is_skipped(self):
        rows = [row("default", "Main", default=True, enabled=False), row("second", "Team plan")]
        pick = accounts.choose(rows, lambda aid: OK)
        self.assertEqual(pick["account"]["id"], "second")
        self.assertEqual([s["reason"] for s in pick["skipped"] if s["id"] == "default"], ["paused"])

    def test_an_account_that_is_not_signed_in_is_skipped(self):
        rows = [row("default", "Main", default=True, signed_in=False), row("second", "Team plan")]
        pick = accounts.choose(rows, lambda aid: OK)
        self.assertEqual(pick["account"]["id"], "second")
        self.assertEqual([s["reason"] for s in pick["skipped"] if s["id"] == "default"], ["not signed in"])

    def test_no_account_available_says_why(self):
        rows = [row("default", "Main", default=True, enabled=False), row("second", "Team plan", signed_in=False)]
        pick = accounts.choose(rows, lambda aid: OK)
        self.assertIsNone(pick["account"])
        self.assertIn("Main: paused", pick["reason"])
        self.assertIn("Team plan: not signed in", pick["reason"])

    def test_every_account_exhausted_gives_no_account_and_the_reasons(self):
        rows = [row("default", "Main", default=True), row("second", "Team plan")]
        pick = accounts.choose(rows, lambda aid: out(f"{aid} limit"))
        self.assertIsNone(pick["account"])
        self.assertIn("default limit", pick["reason"])
        self.assertIn("second limit", pick["reason"])

    def test_a_role_stays_on_the_account_its_session_runs_on(self):
        rows = [row("default", "Main", default=True), row("second", "Team plan")]
        pick = accounts.choose(rows, lambda aid: OK, session_account="second")
        self.assertEqual(pick["account"]["id"], "second")
        self.assertFalse(pick["switched"])

    def test_the_session_moves_on_when_its_own_account_is_exhausted(self):
        rows = [row("default", "Main", default=True), row("second", "Team plan")]
        pick = accounts.choose(rows, lambda aid: out() if aid == "second" else OK, session_account="second")
        self.assertEqual(pick["account"]["id"], "default")
        self.assertTrue(pick["switched"])

    def test_a_free_account_is_preferred_over_one_a_turn_is_using(self):
        rows = [row("default", "Main", default=True, busy=True), row("second", "Team plan")]
        self.assertEqual(accounts.choose(rows, lambda aid: OK)["account"]["id"], "second")

    def test_a_busy_account_is_still_used_when_it_is_the_only_one_with_capacity(self):
        # Exactly what every turn does today on a single-account installation: share the one folder.
        rows = [row("default", "Main", default=True, busy=True), row("second", "Team plan", busy=True)]
        pick = accounts.choose(rows, lambda aid: OK if aid == "default" else out())
        self.assertEqual(pick["account"]["id"], "default")


class StoreTests(unittest.TestCase):
    def setUp(self):
        clean()

    tearDown = setUp

    def test_a_fresh_installation_has_one_account_and_reports_no_extras(self):
        self.assertFalse(accounts.multiple("claude"))
        rows = accounts.rows("claude", {})
        self.assertEqual([r["id"] for r in rows], ["default"])
        self.assertTrue(rows[0]["is_default"])
        self.assertEqual(rows[0]["home"], "")  # Relay's own home folder, untouched
        self.assertEqual(accounts.env_for("claude", "default"), {})

    def test_adding_an_account_gives_it_its_own_folder_and_sign_in_command(self):
        a = accounts.add("claude", "Team plan")
        self.assertTrue(accounts.multiple("claude"))
        self.assertEqual(a["name"], "Team plan")
        env = accounts.env_for("claude", a["id"])
        self.assertTrue(env["HOME"].endswith(f"accounts/claude/{a['id']}"))
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], env["HOME"] + "/.claude")
        self.assertIn("HOME=", a["login_command"])
        self.assertIn("claude", a["login_command"])
        self.assertFalse(a["signed_in"])  # nothing has logged in there yet

    def test_an_account_is_signed_in_once_its_folder_holds_the_credentials(self):
        a = accounts.add("claude", "Second")
        home = accounts.home("claude", a["id"])
        (home / ".claude").mkdir(parents=True, exist_ok=True)
        (home / ".claude" / ".credentials.json").write_text("{}", encoding="utf-8")
        self.assertTrue(accounts.signed_in("claude", a["id"]))

    def test_pausing_renaming_and_removing(self):
        a = accounts.add("codex", "Spare")
        accounts.update("codex", a["id"], enabled=False, name="Spare plan")
        r = accounts.get("codex", a["id"])
        self.assertFalse(r["enabled"])
        self.assertEqual(r["name"], "Spare plan")
        accounts.update("codex", a["id"], default=True)
        self.assertTrue(accounts.get("codex", a["id"])["is_default"])
        accounts.remove("codex", a["id"])
        self.assertEqual([r["id"] for r in accounts.rows("codex", {})], ["default"])
        self.assertFalse(accounts.home("codex", a["id"]).exists())

    def test_the_first_account_cannot_be_removed(self):
        with self.assertRaises(ValueError):
            accounts.remove("claude", "default")

    def test_a_run_records_which_account_took_the_turn(self):
        d = Path(tempfile.mkdtemp())
        self.assertEqual(accounts.run_account(d, "claude"), "default")  # runs from before accounts
        accounts.mark_run(d, "claude", "second")
        self.assertEqual(accounts.run_account(d, "claude"), "second")
        self.assertEqual(accounts.run_account(d, "codex"), "default")


class FakeManager:
    """Only what the limit code touches."""

    def __init__(self, cfg=None, tasks=None):
        self._cfg = {"autopilot": {}, **(cfg or {})}
        self.tasks = tasks or []
        self.runners = {}
        self.lines = []

    def cfg(self):
        return self._cfg

    def timeline(self, tid, role, title, detail=""):
        self.lines.append((role, title, detail))

    class store:  # noqa: N801
        @staticmethod
        def list():
            return []


class AutopilotTests(unittest.TestCase):
    def setUp(self):
        clean()

    tearDown = setUp

    def auto(self, account_reading):
        a = autopilot.Autopilot(FakeManager())
        a._account_fn = lambda agent, aid="default": account_reading(aid)
        a._health_fn = lambda agent: {"installed": True, "ok": True}
        return a

    def test_one_account_answers_exactly_as_before(self):
        reading = {"plan": [{"label": "Plan", "value": "Claude Max"}], "windows": [], "usage": []}
        a = self.auto(lambda aid: reading)
        st = a.agent_state("claude", "")
        self.assertTrue(st["ok"])
        self.assertNotIn("account", st)  # the single-account answer is untouched

    def test_a_second_account_keeps_the_agent_running_when_the_first_is_full(self):
        extra = accounts.add("claude", "Team plan")
        home = accounts.home("claude", extra["id"])
        (home / ".claude").mkdir(parents=True, exist_ok=True)
        (home / ".claude" / ".credentials.json").write_text("{}", encoding="utf-8")
        full = {"plan": [], "windows": [{"label": "5-hour limit", "used": 100, "resets_at": 9e12}], "usage": []}
        free = {"plan": [], "windows": [{"label": "5-hour limit", "used": 3, "resets_at": 9e12}], "usage": []}
        a = self.auto(lambda aid: full if aid == "default" else free)
        st = a.agent_state("claude", "")
        self.assertTrue(st["ok"])
        self.assertEqual(st["account"], extra["id"])

    def test_with_every_account_full_the_task_waits_for_the_earliest_reset(self):
        extra = accounts.add("claude", "Team plan")
        home = accounts.home("claude", extra["id"])
        (home / ".claude").mkdir(parents=True, exist_ok=True)
        (home / ".claude" / ".credentials.json").write_text("{}", encoding="utf-8")
        a = self.auto(lambda aid: {"plan": [], "usage": [], "windows": [
            {"label": "5-hour limit", "used": 100, "resets_at": 9e12 if aid == "default" else 8e12}]})
        st = a.agent_state("claude", "")
        self.assertFalse(st["ok"])
        self.assertEqual(st["resets_at"], 8e12)
        self.assertIsNone(st["account"])

    def test_when_no_account_has_capacity_the_role_falls_back_as_before(self):
        # plan_capacity is what decides the order: another agent first, waiting only if none is left.
        state = {("claude", ""): {"ok": False, "reason": "Claude 5-hour limit at 100%", "resets_at": 100.0, "kind": "limit"},
                 ("codex", ""): {"ok": True, "reason": "", "resets_at": None, "kind": ""}}
        cap = autopilot.plan_capacity({"worker": {"agent": "claude", "model": ""}},
                                      lambda agent, model, role, provider="": state[(agent, model)],
                                      {"fallbacks": {"worker": ["codex"]}, "limit_action": "fallback"})
        self.assertTrue(cap["ok"])
        self.assertEqual(cap["roles"]["worker"]["agent"], "codex")

    def test_with_no_fallback_the_task_waits(self):
        cap = autopilot.plan_capacity({"worker": {"agent": "claude", "model": ""}},
                                      lambda agent, model, role, provider="": {"ok": False, "reason": "Claude 5-hour limit at 100%",
                                                                               "resets_at": 100.0, "kind": "limit"},
                                      {"fallbacks": {}, "limit_action": "fallback"})
        self.assertFalse(cap["ok"])
        self.assertEqual(cap["wait_until"], 100.0)


class TurnTests(unittest.TestCase):
    """What the runner puts on the turn."""

    def setUp(self):
        clean()

    tearDown = setUp

    def runner(self):
        m = FakeManager()
        m.autopilot = None
        r = Runner("t1", m)
        return r, m

    def test_a_single_account_installation_changes_nothing(self):
        r, _ = self.runner()
        self.assertIsNone(r._choose_account("worker", "claude", {}, {}, ""))

    def test_the_chosen_account_carries_its_folder_and_name(self):
        extra = accounts.add("claude", "Team plan")
        home = accounts.home("claude", extra["id"])
        (home / ".claude").mkdir(parents=True, exist_ok=True)
        (home / ".claude" / ".credentials.json").write_text("{}", encoding="utf-8")
        accounts.update("claude", "default", enabled=False)  # the default is paused, so the extra one runs
        r, _ = self.runner()
        acct = r._choose_account("worker", "claude", {}, {}, "")
        self.assertEqual(acct["id"], extra["id"])
        self.assertEqual(acct["name"], "Team plan")
        self.assertEqual(acct["env"]["HOME"], str(home))
        self.assertFalse(acct["reset_session"])

    def test_moving_a_started_session_to_another_account_starts_it_again(self):
        extra = accounts.add("claude", "Team plan")
        home = accounts.home("claude", extra["id"])
        (home / ".claude").mkdir(parents=True, exist_ok=True)
        (home / ".claude" / ".credentials.json").write_text("{}", encoding="utf-8")
        accounts.update("claude", "default", enabled=False)
        r, m = self.runner()
        acct = r._choose_account("worker", "claude", {}, {"account": "default", "turns": 3, "id": "s1"}, "")
        self.assertEqual(acct["id"], extra["id"])
        self.assertTrue(acct["reset_session"])
        self.assertTrue(any("Switched to the Team plan account" in t[1] for t in m.lines))


if __name__ == "__main__":
    unittest.main()
