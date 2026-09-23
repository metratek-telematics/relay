"""Knowledge docs: front-matter, sections kept by hand, staleness against GitHub, the one-turn refresh, delivery marks,
the Knowledge section of task prompts, the system-map rescan of one repository, and the HTTP API.

GitHub, the shallow clone and the agent are stubbed, so no network, CLI or real settings are touched.

    python -m unittest tests.test_knowledge -v
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = tempfile.mkdtemp(prefix="relay-knowledge-test-")
os.environ.setdefault("RELAY_DATA_DIR", TMP)
sys.path.insert(0, str(ROOT))

from orchestrator import knowledge as K, playbooks, systemmap  # noqa: E402

OLD, NEW = "a" * 40, "b" * 40

DOC = f"""---
repo: acme/infra
source_commit: {OLD}
source_branch: main
updated: 2026-09-01
summary: Compose files and proxy config for the acme stack
platform_docs: [ARCH]
human_sections: [Gotchas]
---
# infra

> Intro line.

## Purpose

Runs the stack.

## Changes and deployment

- `docker compose up -d`

## Gotchas

- hand-written: never touch the proxy

## Conventions

- conventional commits
"""


def git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def reset_dir():
    import shutil
    shutil.rmtree(K.KNOWLEDGE_DIR, ignore_errors=True)
    K.REPOS_DIR.mkdir(parents=True, exist_ok=True)
    if K.STATE_PATH.exists():
        K.STATE_PATH.unlink()
    (K.KNOWLEDGE_DIR / "ARCH.md").write_text("# ARCH: the acme platform\n\nThe platform.\n")
    (K.KNOWLEDGE_DIR / "HOSTS.md").write_text("# HOSTS: the servers\n\nServers.\n")
    (K.REPOS_DIR / "infra.md").write_text(DOC)


class FakeGH:
    """`gh api` answers for one repository: the branch head and a compare result."""

    def __init__(self, head=NEW, behind=3, files=("docker-compose.yml", "README.md"), fail_compare=False):
        self.head, self.behind, self.files, self.fail_compare, self.calls = head, behind, list(files), fail_compare, []

    def __call__(self, args, timeout=60):
        self.calls.append(args)
        path = args[1]
        if path.endswith("/commits/main"):
            return {"sha": self.head}
        if "/compare/" in path:
            if self.fail_compare:
                raise RuntimeError("HTTP 404: No common ancestor")
            return {"ahead_by": self.behind, "files": [{"filename": f, "patch": f"@@ change in {f}"} for f in self.files],
                    "commits": [{"commit": {"message": f"commit {i}\n\nbody"}} for i in range(self.behind)]}
        if path.startswith("repos/") and path.count("/") == 2:
            return {"default_branch": "main"}
        raise AssertionError(f"unexpected gh call {args}")


class FrontMatterTest(unittest.TestCase):
    def test_round_trip_keeps_keys_and_lists(self):
        meta, body = K.split_front(DOC)
        self.assertEqual(meta["repo"], "acme/infra")
        self.assertEqual(meta["platform_docs"], ["ARCH"])
        self.assertEqual(meta["human_sections"], ["Gotchas"])
        self.assertTrue(body.startswith("# infra"))
        again = K.join_front(meta, body)
        self.assertEqual(K.split_front(again), (meta, body))

    def test_no_front_matter(self):
        self.assertEqual(K.split_front("# x\n\ntext"), ({}, "# x\n\ntext"))

    def test_sections_ignore_headings_inside_code_fences(self):
        body = "# t\n\n## A\n\n```\n## not a heading\n```\n\n## B\n\nb\n"
        self.assertEqual([h for h, _ in K.sections(body)], [None, "A", "B"])

    def test_apply_sections_keeps_protected_and_appends_new(self):
        meta, body = K.split_front(DOC)
        prot = K.protected_sections(meta, body)
        new, changed, skipped = K.apply_sections(body, {"Changes and deployment": "- `docker compose -p acme up -d`",
                                                        "Gotchas": "- agent text", "Test and verify": "- run `make test`"}, prot)
        self.assertEqual(changed, ["Changes and deployment", "Test and verify"])
        self.assertEqual(skipped, ["Gotchas"])
        self.assertIn("docker compose -p acme up -d", new)
        self.assertIn("hand-written: never touch the proxy", new)
        self.assertNotIn("agent text", new)
        self.assertTrue(new.rstrip().endswith("run `make test`"))
        self.assertIn("Runs the stack.", new)    # untouched sections stay byte for byte
        self.assertIn("> Intro line.", new)

    def test_human_marker_protects_a_section(self):
        body = "# t\n\n## Layout\n\n<!-- human -->\n- mine\n"
        self.assertIn("layout", K.protected_sections({}, body))

    def test_summary_fallbacks(self):
        self.assertEqual(K.summary_of({}, "# t\n\n> quote\n\nFirst sentence here. Second one.\n\n## A\n", "t"), "First sentence here.")
        self.assertEqual(K.summary_of({}, "# t\n\n## At a glance\n\n| | |\n|---|---|\n| Purpose | Sea routes between ports |\n", "t"),
                         "Sea routes between ports")
        self.assertEqual(K.summary_of({}, "# EDGE: the host\n\nVerified today.\n", "EDGE", "platform"), "EDGE: the host")


class KeyFilesTest(unittest.TestCase):
    def test_categories(self):
        cases = {"package.json": "manifest", "backend/requirements-dev.txt": "manifest", "Dockerfile": "docker",
                 "svc/Dockerfile.prod": "docker", "docker-compose.override.yml": "compose", "navistack.yml": "compose",
                 "sof-stack.yml": "compose", ".github/workflows/ci.yml": "ci", "db/migrations/001_init.py": "migration",
                 "sql/nv_sof.sql": "migration", "nginx/conf.d/site.conf": "config", ".gitattributes": "config",
                 "traefik/dynamic.yml": "config", "src/App.vue": "", "README.md": "", "docs/guide.md": ""}
        for path, cat in cases.items():
            self.assertEqual(K.key_file_category(path), cat, path)


class DocsTest(unittest.TestCase):
    def setUp(self):
        reset_dir()

    def test_list_and_read(self):
        rows = {r["path"]: r for r in K.list_docs()}
        self.assertEqual(set(rows), {"ARCH.md", "HOSTS.md", "repos/infra.md"})
        self.assertEqual(rows["repos/infra.md"]["summary"], "Compose files and proxy config for the acme stack")
        self.assertEqual(rows["ARCH.md"]["kind"], "platform")
        d = K.read_doc("repos/infra.md")
        self.assertEqual(d["sha"], K.sha(DOC))

    def test_paths_outside_the_folder_are_refused(self):
        for bad in ("../config.json", "repos/../../x.md", "/etc/passwd", "repos/.index-c.md", "x/y.md", "repos/a/b.md", "ARCH.txt"):
            with self.assertRaises(ValueError, msg=bad):
                K.read_doc(bad)

    def test_save_records_changed_sections_as_human_and_detects_conflicts(self):
        d = K.read_doc("repos/infra.md")
        text = d["text"].replace("- conventional commits", "- conventional commits, imperative mood")
        saved = K.save_doc("repos/infra.md", text, d["sha"], by="owner")
        self.assertEqual(saved["human_sections"], ["Gotchas", "Conventions"])
        self.assertIn("human_sections: [Gotchas, Conventions]", saved["text"])
        with self.assertRaises(RuntimeError):
            K.save_doc("repos/infra.md", text + "\nmore", d["sha"])   # stale base sha

    def test_search(self):
        res = K.search("proxy compose")
        self.assertEqual([r["path"] for r in res], ["repos/infra.md"])
        self.assertTrue(res[0]["hits"])
        self.assertEqual(K.search("nothing-matches-this"), [])

    def test_block_lists_repo_doc_then_named_platform_docs(self):
        block = K.block_for(K.docs_for(["acme/infra"], []))
        lines = block.splitlines()
        self.assertTrue(lines[0].startswith("KNOWLEDGE DOCS"))
        self.assertIn(str(K.REPOS_DIR / "infra.md"), lines[1])
        self.assertIn("written from aaaaaaa on 2026-09-01", lines[1])
        self.assertIn(str(K.KNOWLEDGE_DIR / "ARCH.md"), block)
        self.assertNotIn("HOSTS.md", block)           # platform_docs names only ARCH
        self.assertLess(len(block), K.BLOCK_CHARS + 200)

    def test_platform_docs_none_and_unknown_repo(self):
        (K.REPOS_DIR / "tool.md").write_text("---\nrepo: acme/tool\nplatform_docs: none\n---\n# tool\n\nA tool.\n")
        block = K.block_for(K.docs_for(["acme/tool"], []))
        self.assertIn("tool.md", block)
        self.assertNotIn("ARCH.md", block)
        self.assertEqual(K.block_for(K.docs_for(["acme/unknown"], [])), "")

    def test_related_repo_docs_are_marked(self):
        (K.REPOS_DIR / "api.md").write_text("---\nrepo: acme/api\nplatform_docs: none\n---\n# api\n\nThe API.\n")
        block = K.block_for(K.docs_for(["acme/infra"], ["acme/api"]))
        self.assertIn("api.md: The API. (related repository)", block)


class StalenessTest(unittest.TestCase):
    def setUp(self):
        reset_dir()
        self.meta = K.doc_meta(K.REPOS_DIR / "infra.md")

    def run_check(self, gh, min_commits=10):
        orig = K._gh_json
        K._gh_json = gh
        try:
            return K.check_repo("acme/infra", self.meta, min_commits)
        finally:
            K._gh_json = orig

    def test_same_head_is_current(self):
        info = self.run_check(FakeGH(head=OLD))
        self.assertFalse(info["stale"])
        self.assertEqual(info["behind"], 0)

    def test_key_file_change_is_stale(self):
        info = self.run_check(FakeGH(behind=2, files=["docker-compose.yml", "src/app.py"]))
        self.assertTrue(info["stale"])
        self.assertEqual([k["path"] for k in info["key_files"]], ["docker-compose.yml"])
        self.assertIn("docker-compose.yml", info["patches"])

    def test_small_code_only_change_is_current_until_n_commits(self):
        self.assertFalse(self.run_check(FakeGH(behind=3, files=["src/app.py"]))["stale"])
        info = self.run_check(FakeGH(behind=12, files=["src/app.py"]))
        self.assertTrue(info["stale"])
        self.assertIn("12 commits", info["reasons"][0])

    def test_unknown_base_is_stale_and_no_base_is_untracked(self):
        self.assertTrue(self.run_check(FakeGH(fail_compare=True))["stale"])
        self.meta = {"repo": "acme/infra", "source_branch": "main"}
        info = self.run_check(FakeGH())
        self.assertFalse(info["stale"])
        self.assertTrue(info["untracked"])

    def test_commit_inferred_from_text(self):
        p = K.REPOS_DIR / "old.md"
        p.write_text("# old\n\n> Verified against GitHub `main@c73c722` today.\n")
        meta = K.doc_meta(p)
        self.assertEqual(meta["source_commit"], "c73c722")
        self.assertIn("source_commit", meta["_inferred"])

    def test_delivery_marks_only_key_files_of_documented_repos(self):
        self.assertIsNone(K.mark_delivery("acme/infra", ["src/app.py"], "t1"))
        self.assertIsNone(K.mark_delivery("acme/nodoc", ["Dockerfile"], "t1"))
        mark = K.mark_delivery("acme/infra", ["Dockerfile", "src/app.py"], "t2", "https://github.com/acme/infra/pull/9")
        self.assertEqual(mark["key_files"], ["Dockerfile"])
        st = K.load_state()["repos"]["acme/infra"]
        self.assertEqual([m["task_id"] for m in st["delivery_marks"]], ["t2"])
        row = next(r for r in K.list_docs() if r["path"] == "repos/infra.md")
        self.assertIn("may be stale", K.block_for([{**row, "why": "this task"}]))


class RefreshTest(unittest.TestCase):
    def setUp(self):
        reset_dir()
        import shutil
        shutil.rmtree(playbooks.PLAYBOOKS_DIR, ignore_errors=True)
        pb = {"repo": "acme/infra", "repo_label": "infra", "created_at": "x", "sections": {
            "run": {"text": "- old run", "source": "knowledge", "edited": True},
            "gotchas": {"text": "- person gotcha", "source": "person", "edited": True},
            "conventions": {"text": "- conv", "source": "seed", "edited": False}}}
        playbooks.save(pb)
        self.gh = FakeGH(behind=4, files=["docker-compose.yml"])
        self.orig = K._gh_json
        K._gh_json = self.gh
        self.clone = Path(tempfile.mkdtemp(dir=TMP))
        self.prompts = []

    def tearDown(self):
        K._gh_json = self.orig

    def agent(self, reply):
        def run(agent, model, effort, text, cfg, workdir, timeout, provider=""):
            self.prompts.append((agent, model, text, Path(workdir)))
            return {"ok": True, "text": "```json\n" + json.dumps(reply) + "\n```", "usage": {"input": 100, "output": 50}}
        return run

    def cfg(self, **learning):
        return {"roles": {"supervisor": {"agent": "claude"}}, "subagent_models": {"claude": "haiku"}, "retro_timeout_seconds": 30,
                "design_forbidden_terms": ["Bad Name"], "learning": learning}

    def test_refresh_updates_only_affected_sections_and_keeps_human_ones(self):
        reply = {"type": "knowledge", "sections": {"Changes and deployment": "- `docker compose -p acme up -d` (project name now required)",
                                                    "Gotchas": "- agent rewrite"},
                 "summary": "", "changes": ["compose now needs -p acme"],
                 "playbook": {"run": "- `docker compose -p acme up -d`", "gotchas": "- agent gotcha"}}
        res = K.refresh_repo(self.cfg(), "acme/infra", run_agent=self.agent(reply), clone=lambda repo, branch: self.clone)
        self.assertEqual(res["status"], "refreshed")
        self.assertEqual(res["sections_changed"], ["Changes and deployment"])
        self.assertEqual(res["sections_kept"], ["Gotchas"])
        text = (K.REPOS_DIR / "infra.md").read_text()
        meta, body = K.split_front(text)
        self.assertEqual(meta["source_commit"], NEW)
        self.assertEqual(meta["summary"], "Compose files and proxy config for the acme stack")
        self.assertIn("project name now required", body)
        self.assertIn("hand-written: never touch the proxy", body)
        self.assertIn("- conventional commits", body)
        # the prompt: one cheap turn with the configured cheap model, in the clone, naming the protected section
        agent, model, prompt, cwd = self.prompts[0]
        self.assertEqual((agent, model, cwd), ("claude", "haiku", self.clone))
        self.assertIn("docker-compose.yml (compose)", prompt)
        self.assertIn("must NOT touch (a person edited them): Gotchas", prompt)
        # playbook: the knowledge section is rewritten, the person's section only gets a suggestion
        pb = playbooks.load("acme/infra")
        self.assertEqual(pb["sections"]["run"]["text"], "- `docker compose -p acme up -d`")
        self.assertEqual(pb["sections"]["run"]["source"], "knowledge")
        self.assertTrue(pb["sections"]["run"]["edited"])
        self.assertEqual(pb["sections"]["gotchas"]["text"], "- person gotcha")
        self.assertEqual(pb["sections"]["gotchas"]["suggestion"], "- agent gotcha")
        self.assertEqual(pb["sections"]["conventions"]["text"], "- conv")
        st = K.load_state()["repos"]["acme/infra"]
        self.assertFalse(st["stale"])
        self.assertEqual(st["history"][-1]["changes"], ["compose now needs -p acme"])
        self.assertEqual(st["history"][-1]["playbook_changed"], ["run"])

    def test_current_doc_is_not_refreshed_unless_forced(self):
        self.gh.head = OLD
        res = K.refresh_repo(self.cfg(), "acme/infra", run_agent=self.agent({"sections": {}}), clone=lambda r, b: self.clone)
        self.assertEqual(res["status"], "fresh")
        self.assertEqual(self.prompts, [])
        res = K.refresh_repo(self.cfg(), "acme/infra", force=True, run_agent=self.agent({"type": "knowledge", "sections": {}}),
                             clone=lambda r, b: self.clone)
        self.assertEqual(res["status"], "refreshed")
        self.assertEqual(len(self.prompts), 1)

    def test_forbidden_terms_are_rejected(self):
        reply = {"type": "knowledge", "sections": {"Purpose": "Pulls data from bad-name's API"}, "playbook": {"run": "- uses BADNAME"}}
        res = K.refresh_repo(self.cfg(), "acme/infra", run_agent=self.agent(reply), clone=lambda r, b: self.clone)
        self.assertEqual(res["sections_rejected"], ["Purpose"])
        self.assertIn("Runs the stack.", (K.REPOS_DIR / "infra.md").read_text())
        self.assertEqual(playbooks.load("acme/infra")["sections"]["run"]["text"], "- old run")

    def test_bad_reply_records_the_error(self):
        def run(*a, **k):
            return {"ok": True, "text": "no json here"}
        with self.assertRaises(RuntimeError):
            K.refresh_repo(self.cfg(), "acme/infra", run_agent=run, clone=lambda r, b: self.clone)
        self.assertIn("did not return", K.load_state()["repos"]["acme/infra"]["error"])
        self.assertIn("aaaaaaa", (K.REPOS_DIR / "infra.md").read_text()[:200])   # doc untouched

    def test_openrouter_auto_free_when_configured(self):
        agent, model, effort, provider = K.choose_agent(self.cfg(knowledge_refresh_provider="openrouter"))
        self.assertEqual((agent, provider, model), ("claude", "openrouter", "openrouter:auto-free"))

    def test_check_all_and_due(self):
        cfg = self.cfg()
        self.assertTrue(K.due(cfg))
        out = K.check_all(cfg, refresh=False)
        self.assertEqual(out[0]["status"], "stale")
        self.assertFalse(K.due(cfg))
        self.assertFalse(K.due(self.cfg(knowledge_refresh=False)))


class SystemMapRescanTest(unittest.TestCase):
    def test_rescan_one_repository_from_a_checkout(self):
        import shutil
        systemmap.save(systemmap.empty())
        api = Path(tempfile.mkdtemp(dir=TMP)) / "api"
        api.mkdir()
        (api / "main.py").write_text("from fastapi import FastAPI\napp = FastAPI()\n\n@app.get('/vessels/{id}')\ndef v(id):\n    return {}\n")
        web = Path(tempfile.mkdtemp(dir=TMP)) / "web"
        web.mkdir()
        (web / "client.js").write_text("export const load = (id) => fetch(`${API_URL}/vessels/${id}`)\n")
        for d in (api, web):
            git("init", "-q", "-b", "main", cwd=d)
        data = systemmap.load()
        data["components"] = [
            {"id": "api", "name": "api", "repo": "acme/api", "path": str(api), "kind": "api", "description": "", "runs": "", "provides": {}, "locked": []},
            {"id": "web", "name": "web", "repo": "acme/web", "path": "/nonexistent/web", "kind": "frontend", "description": "d", "runs": "",
             "provides": {}, "locked": []}]
        systemmap.save(data)
        systemmap.rescan_repo("acme/api", api)          # the provider first, so its endpoints are stored
        res = systemmap.rescan_repo("acme/web", web)    # then the client from a throwaway checkout
        self.assertEqual(res["component"], "web")
        self.assertEqual(res["edges_added"], ["web → api (http)"])
        after = systemmap.load()
        web_c = systemmap.component(after, "web")
        self.assertEqual(web_c["path"], "/nonexistent/web")   # the checkout never replaces the component's clone
        self.assertEqual(web_c["description"], "d")
        (web / "client.js").write_text("export const x = 1\n")
        res = systemmap.rescan_repo("acme/web", web)
        self.assertEqual(res["edges_dropped"], ["web → api (http)"])
        shutil.rmtree(api.parent, ignore_errors=True)


class PromptTest(unittest.TestCase):
    """A real (stubbed-agent) solo run: the kickoff prompt carries the Knowledge section."""

    def setUp(self):
        reset_dir()
        from orchestrator import config as C
        (K.REPOS_DIR / "app.md").write_text("---\nrepo: acme/app\nsource_commit: " + OLD + "\nsummary: The acme app, deploy rules inside\n"
                                            "platform_docs: [ARCH]\n---\n# app\n")
        self.repo = Path(tempfile.mkdtemp(dir=TMP)) / "app"
        self.repo.mkdir()
        (self.repo / "app.py").write_text("x = 1\n")
        git("init", "-q", "-b", "main", cwd=self.repo)
        git("remote", "add", "origin", "https://github.com/acme/app.git", cwd=self.repo)
        git("-c", "user.email=t@t", "-c", "user.name=t", "add", "-A", cwd=self.repo)
        git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init", cwd=self.repo)
        C.update({"github_auto_push_on_pass": False, "github_auto_create_pr": False, "retro_enabled": False, "env_prepare": False,
                  "auto_detect_verification": False, "design_gate": False, "triage_rating": False, "lessons_inject": False})

    def test_solo_kickoff_has_knowledge_block_and_delivery_marks_doc(self):
        from orchestrator import manager as M
        from orchestrator.pipeline import orchestrate
        from orchestrator.runner import Runner
        seen = []

        class R(Runner):
            def run_agent(self, role, agent_name, prompt, cwd, cfg, model, session, run_dir, label="", turn=None, effort=""):
                seen.append(str(prompt))
                (Path(cwd) / "Dockerfile").write_text("FROM python:3.12\n")
                text = "```json\n" + json.dumps({"type": "report", "status": "complete", "summary": "added a Dockerfile",
                                                 "acceptance": [{"id": "A1", "criterion": "Dockerfile exists", "how_to_verify": "inspection: Dockerfile:1",
                                                                 "required": True, "status": "met", "evidence": "Dockerfile:1 FROM python"}],
                                                 "complexity": {"level": "simple", "reason": "one file"}, "files": ["Dockerfile"]}) + "\n```"
                return {"ok": True, "session": {**(session or {}), "agent": agent_name, "id": "s1", "turns": 1}, "text": text, "last_message": text}

        m = M.Manager(lambda *a, **k: None)
        t = m.create_task({"repo": str(self.repo), "requirements": "add a Dockerfile for app.py", "queue": False,
                           "workflow": {"roles": {"supervisor": {"agent": "claude"}, "worker": {"agent": "codex"}, "reviewer": {"agent": ""}}}})
        r = R(t["id"], m)
        m.store.update(t["id"], status="running", started_at=time.strftime("%Y-%m-%dT%H:%M:%S"))
        orchestrate(m.store.get(t["id"]), r, m)
        t = m.store.get(t["id"])
        self.assertEqual(t["status"], "done", t.get("error"))
        self.assertIn("KNOWLEDGE DOCS", seen[0])
        self.assertIn(str(K.REPOS_DIR / "app.md") + ": The acme app, deploy rules inside", seen[0])
        self.assertIn(str(K.KNOWLEDGE_DIR / "ARCH.md"), seen[0])
        self.assertNotIn("HOSTS.md", seen[0])
        self.assertEqual(t["knowledge_docs"], [str(K.REPOS_DIR / "app.md"), str(K.KNOWLEDGE_DIR / "ARCH.md")])
        marks = K.load_state()["repos"]["acme/app"]["delivery_marks"]
        self.assertEqual(marks[0]["key_files"], ["Dockerfile"])

    def test_no_block_when_injection_is_off(self):
        self.assertEqual(K.task_block({"github_repo": "acme/app"}, {"learning": {"knowledge_inject": False}}), "")
        self.assertIn("app.md", K.task_block({"github_repo": "acme/app"}, {}))


HAVE_FLASK = importlib.util.find_spec("flask") is not None   # installed in Relay's image; the API tests skip without it


@unittest.skipUnless(HAVE_FLASK, "Flask is not installed")
class ApiTest(unittest.TestCase):
    def setUp(self):
        reset_dir()
        from flask import Flask
        import web_knowledge
        self.events = []

        class Mgr:
            def cfg(self):
                return {}
        app = Flask(__name__)
        app.register_blueprint(web_knowledge.init(Mgr(), lambda kind, payload: self.events.append((kind, payload))))
        web_knowledge._can_edit = lambda: True
        self.c = app.test_client()

    def test_list_read_search_save(self):
        d = self.c.get("/api/knowledge/docs").get_json()
        self.assertEqual([x["path"] for x in d["docs"]], ["ARCH.md", "HOSTS.md", "repos/infra.md"])
        self.assertTrue(d["can_edit"])
        doc = self.c.get("/api/knowledge/doc?path=repos/infra.md").get_json()
        self.assertEqual(doc["repo"], "acme/infra")
        self.assertEqual(self.c.get("/api/knowledge/doc?path=../config.json").status_code, 400)
        self.assertEqual(self.c.get("/api/knowledge/doc?path=NOPE.md").status_code, 404)
        self.assertEqual(self.c.get("/api/knowledge/search?q=platform").get_json()["results"][0]["path"], "ARCH.md")
        r = self.c.post("/api/knowledge/docs/save", json={"path": "ARCH.md", "text": "# ARCH\n\nEdited.\n", "base_sha": K.sha("# ARCH: the acme platform\n\nThe platform.\n")})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(r.get_json()["id"], "ARCH.md")
        self.assertEqual((K.KNOWLEDGE_DIR / "ARCH.md").read_text(), "# ARCH\n\nEdited.\n")
        self.assertEqual(self.events[-1][0], "knowledge")
        self.assertEqual(self.c.post("/api/knowledge/docs/save", json={"path": "ARCH.md", "text": "x", "base_sha": "0" * 64}).status_code, 409)
        self.assertEqual(self.c.post("/api/knowledge/refresh", json={"repo": "acme/none"}).status_code, 404)

    def test_writes_need_admin_and_are_audited_by_name(self):
        from orchestrator.org import rbac, web as org_web
        self.assertEqual(rbac.rule_for("POST", "/api/knowledge/docs/save")[0], "admin")
        self.assertEqual(rbac.rule_for("POST", "/api/knowledge/refresh")[0], "admin")
        self.assertEqual(rbac.rule_for("GET", "/api/knowledge/docs")[0], "viewer")
        self.assertFalse(rbac.allowed({"role": "member", "username": "m"}, "admin", "POST", "/api/knowledge/docs/save")[0])
        self.assertEqual(org_web.describe("POST", "/api/knowledge/docs/save")[0], "knowledge.docs_save")
        snap = org_web._snapshot("/api/knowledge/docs/save", "POST", {"path": "ARCH.md"})
        self.assertEqual(snap["sha256"], K.sha((K.KNOWLEDGE_DIR / "ARCH.md").read_text()))


if __name__ == "__main__":
    unittest.main()
