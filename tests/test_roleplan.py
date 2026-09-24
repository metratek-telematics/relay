"""What the task page says a role runs on comes from the orchestrator, not from the browser's own guesswork.

These cover the resolution the runner really uses (task team → global role settings → agent defaults, with an
effort the CLI does not offer dropped) and the record a finished turn leaves behind.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.environ.setdefault("RELAY_DATA_DIR", tempfile.mkdtemp(prefix="relay-roleplan-test-"))
sys.path.insert(0, str(ROOT))

from orchestrator.pipeline import Pipeline  # noqa: E402


class FakeManager:
    def __init__(self):
        self.meta = {}

    def set_meta(self, tid, **kw):
        self.meta.update(kw)


def pipeline(roles, cfg=None):
    """A Pipeline with only what role resolution touches, so the real methods are exercised."""
    p = Pipeline.__new__(Pipeline)
    p.roles = roles
    p.cfg = {"roles": {}, "agent_defaults": {}, **(cfg or {})}
    p.m = FakeManager()
    p.tid = "t1"
    return p


class RolePlanTests(unittest.TestCase):
    def test_task_team_wins_over_the_global_settings(self):
        p = pipeline({"worker": {"agent": "codex", "model": "gpt-5.6-luna", "effort": "high"}},
                     {"roles": {"worker": {"agent": "codex", "model": "other", "effort": "low"}}})
        plan = p.publish_role_plan()
        self.assertEqual(plan["worker"]["model"], "gpt-5.6-luna")
        self.assertEqual(plan["worker"]["effort"], "high")

    def test_global_settings_only_apply_to_the_same_agent(self):
        # The global worker model belongs to Codex; a task whose worker is Claude must not inherit it.
        cfg = {"roles": {"worker": {"agent": "codex", "model": "gpt-5.6-luna", "effort": "high"}}}
        same = pipeline({"worker": {"agent": "codex"}}, cfg).publish_role_plan()
        other = pipeline({"worker": {"agent": "claude"}}, cfg).publish_role_plan()
        self.assertEqual(same["worker"]["model"], "gpt-5.6-luna")
        self.assertEqual(other["worker"]["model"], "")
        self.assertEqual(other["worker"]["effort"], "")

    def test_agent_defaults_are_the_last_word(self):
        p = pipeline({"worker": {"agent": "claude"}},
                     {"agent_defaults": {"claude": {"model": "claude-opus-5", "effort": "max"}}})
        plan = p.publish_role_plan()
        self.assertEqual(plan["worker"]["model"], "claude-opus-5")
        self.assertEqual(plan["worker"]["effort"], "max")

    def test_an_effort_the_cli_does_not_offer_is_dropped(self):
        p = pipeline({"worker": {"agent": "gemini", "effort": "high"}})
        plan = p.publish_role_plan()
        self.assertEqual(plan["worker"]["effort"], "")
        self.assertFalse(plan["worker"]["supports_effort"])

    def test_the_plan_is_published_for_every_staffed_role_only(self):
        p = pipeline({"supervisor": {"agent": "claude"}, "worker": {"agent": "codex"}, "reviewer": {"agent": ""}})
        plan = p.publish_role_plan()
        self.assertEqual(sorted(plan), ["supervisor", "worker"])
        self.assertEqual(p.m.meta["role_plan"], plan)

    def test_the_plan_names_which_layer_decided_each_value(self):
        """The control says whose value is in effect, so the plan has to carry the answer."""
        p = pipeline({"worker": {"agent": "claude", "effort": "high"}},
                     {"roles": {"worker": {"agent": "claude", "model": "claude-sonnet-4.5"}},
                      "agent_defaults": {"claude": {"model": "claude-opus-5"}}})
        plan = p.publish_role_plan()
        self.assertEqual(plan["worker"]["model"], "claude-sonnet-4.5")
        self.assertEqual(plan["worker"]["sources"], {"model": "role", "effort": "task", "account": "auto"})

    def test_a_value_nobody_set_is_the_clis_own(self):
        plan = pipeline({"worker": {"agent": "claude"}}).publish_role_plan()
        self.assertEqual(plan["worker"]["sources"]["model"], "cli")
        self.assertEqual(plan["worker"]["sources"]["effort"], "cli")

    def test_the_agent_default_is_named_as_the_source(self):
        p = pipeline({"worker": {"agent": "claude"}}, {"agent_defaults": {"claude": {"model": "claude-opus-5"}}})
        self.assertEqual(p.publish_role_plan()["worker"]["sources"]["model"], "agent")

    def test_a_role_can_be_pinned_to_one_sign_in(self):
        p = pipeline({"worker": {"agent": "claude", "account": "work"}})
        self.assertEqual(p.role_account("worker"), "work")
        self.assertEqual(p.publish_role_plan()["worker"]["account"], "work")

    def test_the_role_default_pins_the_account_for_the_same_agent_only(self):
        cfg = {"roles": {"worker": {"agent": "claude", "account": "work"}}}
        self.assertEqual(pipeline({"worker": {"agent": "claude"}}, cfg).role_account("worker"), "work")
        self.assertEqual(pipeline({"worker": {"agent": "codex"}}, cfg).role_account("worker"), "")

    def test_openrouter_role_reports_its_provider(self):
        p = pipeline({"worker": {"agent": "cline", "provider": "openrouter", "model": "qwen/qwen3-coder:free"}})
        plan = p.publish_role_plan()
        self.assertEqual(plan["worker"]["provider"], "openrouter")
        self.assertEqual(plan["worker"]["model"], "qwen/qwen3-coder:free")


if __name__ == "__main__":
    unittest.main()
