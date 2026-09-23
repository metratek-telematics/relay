"""Redeploy after merge: eligibility, execution, settings separation and the API contract.

No network and no real deployment: the pull request lookup is stubbed and every command
is a harmless local python one-liner.

    RELAY_DATA_DIR is set to a temporary folder before Relay is imported, so nothing touches real settings.
    python -m unittest tests.test_redeploy -v
"""
from __future__ import annotations

import os
import shlex
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
TMP = tempfile.mkdtemp(prefix="relay-redeploy-test-")
os.environ["RELAY_DATA_DIR"] = TMP
# The test client calls the admin API from loopback; in the check image that is otherwise refused.
os.environ["RELAY_ALLOW_LOOPBACK_ADMIN"] = "1"
sys.path.insert(0, str(ROOT))

from orchestrator import config as C  # noqa: E402
from orchestrator import redeploy  # noqa: E402
from orchestrator.manager import Manager  # noqa: E402

MERGED = {"state": "MERGED", "mergedAt": "2026-09-23T00:00:00Z", "mergeCommit": {"oid": "abc123"}}
OPEN = {"state": "OPEN", "mergedAt": None, "mergeCommit": None}


def cmd(code: str) -> str:
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}"


class RedeployTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.manager = Manager(lambda typ, payload: self.events.append((typ, payload)))
        self.repo = Path(TMP) / "repo"
        self.repo.mkdir(parents=True, exist_ok=True)
        self.task = {
            "id": "task-redeploy", "name": "Redeploy test", "status": "done",
            "repo": str(self.repo), "worktree": str(self.repo),
            "github_repo": "owner/repo", "pr_number": 42,
            "workflow": {"redeploy_on_merge": True}, "events": [],
        }
        self.manager.store.add(dict(self.task))
        self.cfg = dict(C.DEFAULTS)
        self.cfg.update({"redeploy_enabled": True, "redeploy_command": cmd("print('redeploy-ok')"),
                         "redeploy_poll_seconds": 15})
        self.manager._cfg = self.cfg
        self.manager._cfg_at = 10 ** 20  # the manager never re-reads the real settings file

    def tearDown(self):
        self.manager.stop_redeploy_watcher()

    # ---------------------------------------------------------------- eligibility
    def test_disabled_or_not_opted_in_does_not_run(self):
        disabled = dict(self.cfg, redeploy_enabled=False)
        self.assertFalse(redeploy.should_run(self.task, disabled))
        self.assertIsNone(self.manager._redeploy_candidate(self.task, disabled))
        no_command = dict(self.cfg, redeploy_command="   ")
        self.assertFalse(redeploy.should_run(self.task, no_command))
        un_opted = dict(self.task, workflow={"redeploy_on_merge": False})
        self.assertFalse(redeploy.should_run(un_opted, self.cfg))
        self.assertIsNone(self.manager._redeploy_candidate(un_opted, self.cfg))
        self.assertIsNone(self.manager.store.get(self.task["id"]).get("redeploy"))

    def test_unfinished_task_is_never_a_candidate(self):
        running = dict(self.task, status="running")
        self.assertFalse(redeploy.should_run(running, self.cfg, MERGED))

    @patch("orchestrator.redeploy.github.pr_state", return_value=OPEN)
    def test_open_pr_does_not_run(self, pr_state):
        self.assertIsNone(self.manager._redeploy_candidate(self.task, self.cfg))
        self.assertIsNone(self.manager.store.get(self.task["id"]).get("redeploy"))
        pr_state.assert_called_once()

    @patch("orchestrator.redeploy.github.pr_state", side_effect=AssertionError("PR lookup"))
    def test_delivered_trigger_does_not_consult_github(self, pr_state):
        cfg = dict(self.cfg, redeploy_trigger="delivered")
        trigger, state = self.manager._redeploy_candidate(self.task, cfg)
        self.assertEqual(trigger, "delivered")
        self.assertIsNone(state)

    # ---------------------------------------------------------------- execution
    @patch("orchestrator.redeploy.github.pr_state", return_value=MERGED)
    def test_merged_task_runs_once_and_records(self, pr_state):
        marker = Path(TMP) / "runs.txt"
        self.cfg["redeploy_command"] = cmd(f"open({str(marker)!r}, 'a').write('run\\n')")
        candidate = self.manager._redeploy_candidate(self.task, self.cfg)
        self.assertEqual(candidate[0], "pr_merged")
        record = self.manager._run_redeploy(self.task["id"], *candidate)
        self.assertEqual(record["status"], "succeeded")
        self.assertEqual(record["exit_code"], 0)
        self.assertEqual(record["pr_state"]["state"], "MERGED")
        self.assertTrue(record["started_at"] and record["finished_at"])
        self.assertEqual(record["cwd"], str(self.repo))
        self.assertTrue(Path(record["log_path"]).exists())
        self.assertEqual(marker.read_text(), "run\n")
        # A task that already has a record is never picked up again.
        self.assertIsNone(self.manager._redeploy_candidate(self.manager.store.get(self.task["id"]), self.cfg))
        self.assertTrue(any(e[0] == "event" and "Redeploy succeeded" in str(e[1].get("title")) for e in self.events))
        self.assertTrue(any(e[0] == "notify" for e in self.events))
        pr_state.assert_called_once()

    def test_failure_is_recorded_and_manually_retryable(self):
        self.cfg["redeploy_command"] = cmd("print('failed-output'); raise SystemExit(7)")
        first = self.manager.redeploy_now(self.task["id"])
        self.assertEqual(first["status"], "failed")
        self.assertEqual(first["exit_code"], 7)
        self.assertIn("failed-output", first["output"])
        self.assertEqual(self.manager.store.get(self.task["id"])["status"], "done")
        second = self.manager.redeploy_now(self.task["id"])
        self.assertEqual(second["exit_code"], 7)

    @patch("orchestrator.redeploy.github.pr_state", return_value=MERGED)
    def test_failed_automatic_run_is_not_repeated_but_manual_retry_runs(self, pr_state):
        counter = Path(TMP) / "retry-count.txt"
        code = (f"from pathlib import Path; import sys; p=Path({str(counter)!r}); "
                "n=(int(p.read_text()) + 1) if p.exists() else 1; p.write_text(str(n)); "
                "sys.exit(7 if n == 1 else 0)")
        self.cfg["redeploy_command"] = cmd(code)
        self.manager._run_redeploy(self.task["id"], *self.manager._redeploy_candidate(self.task, self.cfg))
        self.assertEqual(self.manager.store.get(self.task["id"])["redeploy"]["status"], "failed")
        self.assertIsNone(self.manager._redeploy_candidate(self.manager.store.get(self.task["id"]), self.cfg))
        self.assertEqual(self.manager.redeploy_now(self.task["id"])["status"], "succeeded")
        self.assertEqual(counter.read_text(), "2")
        self.assertEqual(pr_state.call_count, 1)

    def test_timeout_kills_the_whole_process_group(self):
        marker = Path(TMP) / "orphan.txt"
        # /bin/sh backgrounds a child that would outlive a kill of the shell alone.
        self.cfg["redeploy_command"] = (
            f"{cmd(f'import time; time.sleep(3); open({str(marker)!r}, chr(119)).write(chr(120))')} & wait")
        self.cfg["redeploy_timeout_minutes"] = 1 / 60  # one second
        record = self.manager.redeploy_now(self.task["id"])
        self.assertEqual(record["status"], "failed")
        self.assertEqual(record["exit_code"], -1)
        self.assertIn("timed out", record["output"])
        # Killing only /bin/sh would leave the backgrounded child to write the marker.
        time.sleep(4)
        self.assertFalse(marker.exists())

    # ---------------------------------------------------------------- settings
    def test_per_task_default_is_its_own_setting(self):
        """The default for new tasks must not be the master switch (PR #50 follow-up)."""
        self.cfg["redeploy_enabled"] = True
        self.cfg["redeploy_default_on"] = False
        self.assertFalse(self.manager.build_workflow({})["redeploy_on_merge"])
        self.cfg["redeploy_default_on"] = True
        self.assertTrue(self.manager.build_workflow({})["redeploy_on_merge"])
        # and turning the master switch off leaves the default alone
        self.cfg["redeploy_enabled"] = False
        self.assertTrue(self.manager.build_workflow({})["redeploy_on_merge"])
        # an explicit per-task value always wins
        self.assertFalse(self.manager.build_workflow({"workflow": {"redeploy_on_merge": False}})["redeploy_on_merge"])

    def test_default_on_alone_never_runs_anything(self):
        cfg = dict(self.cfg, redeploy_enabled=False, redeploy_default_on=True)
        self.assertFalse(redeploy.should_run(self.task, cfg, MERGED))
        self.assertIsNone(self.manager._redeploy_candidate(self.task, cfg))

    def test_defaults_ship_off(self):
        self.assertFalse(C.DEFAULTS["redeploy_enabled"])
        self.assertFalse(C.DEFAULTS["redeploy_default_on"])
        self.assertEqual(C.DEFAULTS["redeploy_command"], "")

    # ---------------------------------------------------------------- API
    def test_api_ignores_merge_state_and_rejects_a_missing_command(self):
        import web_app

        original = web_app.manager
        web_app.manager = self.manager
        try:
            with patch("orchestrator.redeploy.github.pr_state", side_effect=AssertionError("PR lookup")):
                r = web_app.app.test_client().post(f"/api/tasks/{self.task['id']}/redeploy", json={})
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.get_json()["status"], "succeeded")
            self.assertEqual(r.get_json()["trigger"], "manual")
            self.assertEqual(r.get_json()["command"], self.cfg["redeploy_command"])
            # The command is owner configuration: a request body cannot supply one.
            self.cfg["redeploy_command"] = ""
            r = web_app.app.test_client().post(f"/api/tasks/{self.task['id']}/redeploy",
                                               json={"command": cmd("print('from the body')")})
            self.assertEqual(r.status_code, 400)
            self.assertEqual(self.manager.store.get(self.task["id"])["redeploy"]["status"], "succeeded")
        finally:
            web_app.manager = original


if __name__ == "__main__":
    unittest.main()
