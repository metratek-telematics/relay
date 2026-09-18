"""OpenRouter provider: catalog parsing, ranking, launch recipes, gateway, errors, cost and capacity.

Runs against tools/mock_openrouter.py in-process; no network and no real key are needed.

    RELAY_DATA_DIR is set to a temporary folder before Relay is imported, so nothing touches real settings.
    python -m unittest tests.test_openrouter -v
"""
from __future__ import annotations

import http.client
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = tempfile.mkdtemp(prefix="relay-or-test-")
os.environ["RELAY_DATA_DIR"] = TMP
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import mock_openrouter as MOCK  # noqa: E402

KEY = "sk-or-v1-unittest-" + "k" * 24
_srv = MOCK.serve(0, "127.0.0.1", KEY)
threading.Thread(target=_srv.serve_forever, daemon=True).start()
PORT = _srv.server_address[1]
os.environ["RELAY_OPENROUTER_BASE_URL"] = f"http://127.0.0.1:{PORT}/api/v1"
os.environ["RELAY_OPENROUTER_API_KEY"] = KEY

from orchestrator import autopilot, openrouter as OR, openrouter_launch as ORL, openrouter_proxy as GW  # noqa: E402
from orchestrator import outcomes, scorecard  # noqa: E402

NOW = 1_790_000_000.0


def row(mid, **kw):
    d = {"id": mid, "name": kw.pop("name", mid), "context_length": kw.pop("ctx", 200000),
         "pricing": {"prompt": kw.pop("pin", "0.000001"), "completion": kw.pop("pout", "0.000004"), "request": "0"},
         "supported_parameters": kw.pop("params", ["tools", "tool_choice", "reasoning", "structured_outputs"]),
         "architecture": {"input_modalities": ["text"], "output_modalities": kw.pop("out", ["text"])},
         "created": kw.pop("created", NOW - 30 * 86400)}
    d.update(kw)
    return OR.parse_model(d)


def mock_reset(**cfg):
    body = {"clear_log": True, "force": {}, "credits": 25.0, "usage": 1.25, **cfg}
    c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=5)
    c.request("POST", "/__mock/config", body=json.dumps(body), headers={"Content-Type": "application/json"})
    c.getresponse().read()


def mock_log():
    c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=5)
    c.request("GET", "/__mock/log")
    return json.loads(c.getresponse().read())["log"]


def call_gateway(token, path, body, stream=False):
    """A CLI's request to the gateway; returns (status, text)."""
    port = GW.ensure_started()
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    c.request("POST", path, body=json.dumps({**body, "stream": stream}),
              headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
    r = c.getresponse()
    return r.status, r.read().decode()


class CatalogTest(unittest.TestCase):
    def test_parse_prices_and_capabilities(self):
        r = row("acme/coder-70b", pin="0.000003", pout="0.000015")
        self.assertEqual((r["input"], r["output"]), (3.0, 15.0))
        self.assertTrue(r["tools"] and r["reasoning"] and r["structured"] and r["text_out"])
        self.assertFalse(r["free"])
        self.assertEqual(r["size_b"], 70)

    def test_free_detection(self):
        self.assertTrue(row("acme/x:free")["free"])
        self.assertTrue(row("acme/zero", pin="0", pout="0")["free"])
        router = row("openrouter/auto", pin="-1", pout="-1")
        self.assertTrue(router["router"])
        self.assertFalse(router["free"])
        self.assertIsNone(router["input"])

    def test_mixture_size(self):
        self.assertEqual(OR._params_b("mistral/mixtral-8x7b", ""), 56)

    def test_estimate_cost_uses_catalog(self):
        rows = [row("acme/p", pin="0.000002", pout="0.00001")]
        self.assertAlmostEqual(OR.estimate_cost("acme/p", 1_000_000, 100_000, 0, rows), 2.0 + 1.0)
        self.assertEqual(OR.estimate_cost("acme/p:free", 1000, 1000, 0, rows + [row("acme/p:free", pin="0", pout="0")]), 0.0)
        self.assertIsNone(OR.estimate_cost("acme/unknown", 1, 1, 0, rows))

    def test_live_catalog_shape_from_mock(self):
        cat = OR.catalog(refresh=True)
        ids = [r["id"] for r in cat["models"]]
        self.assertIn("qwen/qwen3-coder:free", ids)
        self.assertFalse(cat["stale"])


class RankingTest(unittest.TestCase):
    def setUp(self):
        self.rows = [
            row("big/pro-coder-400b:free", ctx=1_000_000, pin="0", pout="0", name="Pro Coder"),
            row("small/tiny-3b:free", ctx=128000, pin="0", pout="0"),
            row("x/no-tools:free", pin="0", pout="0", params=["max_tokens"]),
            row("x/short:free", pin="0", pout="0", ctx=32000),
            row("mid/model-120b:free", ctx=262144, pin="0", pout="0"),
            row("openrouter/free", pin="0", pout="0"),
            row("paid/great", ctx=400000, pin="0.000001", pout="0.000004"),
            row("paid/great:batch", ctx=400000, pin="0.0000005", pout="0.000002"),
            row("paid/pricey", ctx=400000, pin="0.00003", pout="0.00012"),
        ]

    def test_free_ranking_filters_and_orders(self):
        ranked = OR.rank_free(self.rows, {}, 64000, NOW, cooldowns={})
        ids = [r["id"] for r in ranked]
        self.assertEqual(ids[0], "big/pro-coder-400b:free")
        self.assertNotIn("x/no-tools:free", ids)   # no tool calling
        self.assertNotIn("x/short:free", ids)      # context under 64k
        self.assertEqual(ids[-1], OR.FREE_ROUTER)  # the router closes the list
        self.assertLess(ids.index("mid/model-120b:free"), ids.index("small/tiny-3b:free"))
        self.assertTrue(all(r["why"] for r in ranked))

    def test_history_and_cooldown(self):
        hist = {"big/pro-coder-400b:free": {"turns": 6, "failed_turns": 5, "rate_limited_24h": 3, "failed_24h": 3, "tasks": 4, "tasks_ok": 0}}
        ranked = OR.rank_free(self.rows, hist, 64000, NOW, cooldowns={})
        self.assertNotEqual(ranked[0]["id"], "big/pro-coder-400b:free")
        cool = OR.rank_free(self.rows, {}, 64000, NOW, cooldowns={"big/pro-coder-400b:free": {"until": NOW + 600, "reason": "429"}})
        self.assertEqual(cool[-2]["id"], "big/pro-coder-400b:free")  # cooling models sort last, before the router
        self.assertIn("cooldown_until", cool[-2])

    def test_recommended_for_coding(self):
        rec = [r["id"] for r in OR.recommended_for_coding(self.rows, now=NOW)]
        self.assertIn("paid/great", rec)
        self.assertNotIn("paid/great:batch", rec)  # variants are listed through their base model
        self.assertFalse(any(i.endswith(":free") for i in rec))
        self.assertLess(rec.index("paid/great"), rec.index("paid/pricey"))

    def test_cooldown_backoff(self):
        m = "unit/cool-me:free"
        OR.clear_cooldown(m)
        a = OR.cooldown(m, "rate limited", 429)
        b = OR.cooldown(m, "rate limited", 429)
        self.assertGreater(b["until"] - time.time(), a["until"] - time.time() + 60)
        self.assertTrue(OR.cooling(m))
        OR.clear_cooldown(m)
        self.assertIsNone(OR.cooling(m))

    def test_pick_free_resolves_auto_and_is_sticky(self):
        model, auto, entry, rotation = ORL.resolve_model(OR.AUTO_FREE, {}, tasks=[])
        self.assertTrue(auto)
        self.assertTrue(model.endswith(":free") or model == OR.FREE_ROUTER)
        again, _, entry2, _ = ORL.resolve_model(OR.AUTO_FREE, {"or_model": model}, tasks=[])
        self.assertEqual(again, model)
        plain, auto2, _, rot2 = ORL.resolve_model("anthropic/claude-sonnet-4.5", {}, tasks=[])
        self.assertEqual((plain, auto2), ("anthropic/claude-sonnet-4.5", False))


class ErrorsTest(unittest.TestCase):
    def test_messages(self):
        self.assertEqual(OR.describe_error(401)["category"], "provider_auth")
        d = OR.describe_error(402, "Insufficient credits", {"limit_source": "openrouter_key_limit"})
        self.assertIn("spending limit", d["message"])
        self.assertTrue(d["config"])
        self.assertIn("20 requests a minute", OR.describe_error(429, "", None, "a/b:free")["message"])
        self.assertTrue(OR.describe_error(503)["retry"])
        self.assertEqual(OR.describe_error(404, "No endpoints found matching your data policy")["category"], "provider_unavailable")

    def test_failure_categories(self):
        self.assertEqual(scorecard.classify_failure("OpenRouter: out of credits (402 payment required)"), "provider_credits")
        self.assertEqual(scorecard.classify_failure("OpenRouter rejected the API key (401 unauthorized)"), "provider_auth")
        self.assertEqual(scorecard.classify_failure("OpenRouter: no provider available for this model (503)"), "provider_unavailable")
        self.assertEqual(autopilot.failure_category("OpenRouter rate limit (429 too many requests)"), "rate_limit")


class CapacityTest(unittest.TestCase):
    def acc(self, **kw):
        return {"ok": True, "credits_remaining": 5.0, "limit": None, "free_requests": {"used": 1, "limit": 50, "remaining": 49}, **kw}

    def test_states(self):
        s = OR.settings({"api_key": "sk-or-v1-x"})
        self.assertTrue(OR.capacity_state("paid/model", None, [], self.acc(), s)["ok"])
        st = OR.capacity_state("paid/model", None, [], self.acc(credits_remaining=0.0), s)
        self.assertEqual(st["kind"], "balance")
        self.assertTrue(OR.capacity_state("x/y:free", None, [], self.acc(credits_remaining=0.0), s)["ok"])
        st = OR.capacity_state(OR.AUTO_FREE, None, [], self.acc(free_requests={"used": 50, "limit": 50, "remaining": 0}), s)
        self.assertEqual(st["kind"], "limit")
        self.assertGreater(st["resets_at"], time.time())
        self.assertTrue(OR.capacity_state("paid/model", None, [], None, s)["ok"])  # no reading yet: never block

    def test_monthly_cap(self):
        s = OR.settings({"api_key": "sk-or-v1-x", "monthly_cap_usd": 1.0})
        tasks = [{"id": "t1", "metrics": {"log": [{"provider": "openrouter", "end": time.time(), "cost_usd": 1.5, "role": "worker", "agent": "opencode"}]}}]
        st = OR.capacity_state("paid/model", None, tasks, self.acc(), s)
        self.assertEqual(st["kind"], "budget")
        self.assertTrue(OR.capacity_state("x/y:free", None, tasks, self.acc(), s)["ok"])  # free models keep working

    def test_plan_capacity_falls_back_by_provider(self):
        roles = {"supervisor": {"agent": "codex", "model": "paid/m", "provider": "openrouter"}, "worker": {"agent": "claude"}}

        def state_of(agent, model, role, provider=""):
            if provider == "openrouter" and model == "paid/m":
                return {"ok": False, "reason": "no credits", "resets_at": None, "kind": "balance"}
            return {"ok": True}
        res = autopilot.plan_capacity(roles, state_of, {"fallbacks": {"supervisor": ["opencode+openrouter:x/y:free"]}})
        self.assertTrue(res["ok"])
        self.assertEqual(res["roles"]["supervisor"], {"agent": "opencode", "model": "x/y:free", "provider": "openrouter", "effort": ""})
        self.assertEqual(autopilot.parse_fallback_full("kilo:kilo/a/b:free"), ("kilo", "kilo/a/b:free", ""))


class LaunchTest(unittest.TestCase):
    CONN = {"openai_base": "http://127.0.0.1:9/api/v1", "anthropic_base": "http://127.0.0.1:9/api", "token": "sk-or-v1-relay-tok",
            "headers": {"X-Title": "Relay"}, "routing": {"sort": "price", "data_collection": "deny"}}

    def plan(self, agent, model="acme/m:free", cfg=None, env=None):
        d = tempfile.mkdtemp(dir=TMP)
        return ORL.plan(agent, model, self.CONN, d, "worker", cfg or {}, env or {}, context=200000), d

    def test_claude_isolated_from_the_owners_login(self):
        p, d = self.plan("claude")
        e = p["env"]
        self.assertEqual(e["ANTHROPIC_BASE_URL"], "http://127.0.0.1:9/api")
        self.assertEqual(e["ANTHROPIC_AUTH_TOKEN"], "sk-or-v1-relay-tok")
        self.assertEqual(e["ANTHROPIC_API_KEY"], "")
        self.assertTrue(e["CLAUDE_CONFIG_DIR"].startswith(d))  # a private config folder: no subscription login involved
        self.assertEqual(e["ANTHROPIC_SMALL_FAST_MODEL"], "acme/m:free")
        self.assertTrue(p["notes"])  # a non-Anthropic model is flagged

    def test_codex_provider_override(self):
        p, _ = self.plan("codex", "openai/gpt-5.1-codex", cfg={"codex_extra_args": ["--x"]})
        args = p["cfg"]["codex_extra_args"]
        self.assertEqual(args[0], "--x")
        self.assertIn('model_provider="relay_openrouter"', args)
        table = next(a for a in args if a.startswith("model_providers.relay_openrouter="))
        self.assertIn('env_key="RELAY_OPENROUTER_TOKEN"', table)
        self.assertIn('wire_api="responses"', table)
        self.assertIn('base_url="http://127.0.0.1:9/api/v1"', table)
        self.assertFalse(p["cfg"]["agent_web_search"])
        self.assertEqual(p["env"]["RELAY_OPENROUTER_TOKEN"], "sk-or-v1-relay-tok")

    def test_opencode_and_kilo_config_content(self):
        for agent, var in (("opencode", "OPENCODE_CONFIG_CONTENT"), ("kilo", "KILO_CONFIG_CONTENT")):
            p, _ = self.plan(agent, env={var: json.dumps({"mcp": {"x": {"type": "local"}}})})
            self.assertEqual(p["model_arg"], "openrouter/acme/m:free")
            c = json.loads(p["env"][var])
            self.assertEqual(c["mcp"], {"x": {"type": "local"}})  # the task's MCP servers survive
            o = c["provider"]["openrouter"]
            self.assertEqual(o["options"]["baseURL"], "http://127.0.0.1:9/api/v1")
            self.assertEqual(o["options"]["apiKey"], "{env:RELAY_OPENROUTER_TOKEN}")
            self.assertEqual(o["models"]["acme/m:free"]["options"]["provider"]["data_collection"], "deny")
            self.assertEqual(c["small_model"], "openrouter/acme/m:free")

    def test_cline_provider_file(self):
        p, d = self.plan("cline")
        self.assertEqual(p["model_arg"], "openai-compatible:acme/m:free")
        f = Path(p["cleanup"][0])
        data = json.loads(f.read_text())
        ent = data["providers"]["openai-compatible"]
        self.assertEqual(ent["settings"]["baseUrl"], "http://127.0.0.1:9/api/v1")
        self.assertIn("updatedAt", ent)  # without it Cline ignores the entry
        self.assertEqual(oct(f.stat().st_mode & 0o777), "0o600")
        self.assertIn("--data-dir", p["cfg"]["cline_extra_args"])

    def test_goose_aider_crush_qwen_continue_copilot(self):
        g, _ = self.plan("goose")
        self.assertEqual(g["model_arg"], "openrouter:acme/m:free")
        self.assertEqual(g["env"]["OPENROUTER_HOST"], "http://127.0.0.1:9")
        a, _ = self.plan("aider")
        self.assertEqual(a["model_arg"], "openrouter/acme/m:free")
        self.assertEqual(a["env"]["OPENROUTER_API_BASE"], "http://127.0.0.1:9/api/v1")
        settings = Path(a["cfg"]["aider_extra_args"][1]).read_text()
        self.assertIn('"provider": {"sort": "price"', settings)
        c, _ = self.plan("crush")
        conf = json.loads((Path(c["env"]["CRUSH_GLOBAL_CONFIG"]) / "crush.json").read_text())
        self.assertEqual(conf["providers"]["relay-openrouter"]["type"], "openai-compat")
        self.assertEqual(conf["providers"]["relay-openrouter"]["api_key"], "$RELAY_OPENROUTER_TOKEN")
        self.assertEqual(c["model_arg"], "relay-openrouter/acme/m:free")
        q, _ = self.plan("qwen")
        self.assertEqual((q["env"]["OPENAI_BASE_URL"], q["cfg"]["qwen_extra_args"]), ("http://127.0.0.1:9/api/v1", ["--auth-type", "openai"]))
        n, _ = self.plan("continue")
        yml = Path(n["cfg"]["continue_extra_args"][1]).read_text()
        self.assertIn("provider: openrouter", yml)
        self.assertIn("${{ secrets.RELAY_OPENROUTER_TOKEN }}", yml)
        self.assertEqual(n["model_arg"], "")
        cp, _ = self.plan("copilot")
        self.assertEqual((cp["env"]["COPILOT_PROVIDER_BASE_URL"], cp["env"]["COPILOT_OFFLINE"]), ("http://127.0.0.1:9/api/v1", "true"))

    def test_unsupported(self):
        for agent in ("gemini", "amp", "cursor"):
            with self.assertRaises(RuntimeError):
                self.plan(agent)

    def test_no_secret_in_recipe_files(self):
        """The real key never lands in a per-run file in gateway mode (the files hold the turn token at most)."""
        for agent in ("aider", "crush", "continue", "opencode", "codex"):
            p, d = self.plan(agent)
            for f in Path(d).rglob("*"):
                if f.is_file():
                    self.assertNotIn(KEY, f.read_text(errors="replace"))


class GatewayTest(unittest.TestCase):
    def setUp(self):
        mock_reset()
        OR.clear_cooldown("qwen/qwen3-coder:free")

    def test_chat_stream_cost_routing_and_key_swap(self):
        from orchestrator.org import settings as OS
        OS.save({"providers": {"openrouter": {"routing": {"data_collection": "deny", "sort": "price"}}}})
        self.addCleanup(lambda: OS.save({"providers": {"openrouter": {"routing": {"data_collection": "allow", "sort": ""}}}}))
        t = GW.open_turn("task1", None, "worker", "opencode", "anthropic/claude-sonnet-4.5")
        st, text = call_gateway(t.token, "/api/v1/chat/completions", {"model": "other/side-model", "messages": [{"role": "user", "content": "hi"}]}, stream=True)
        self.assertEqual(st, 200)
        self.assertIn("[DONE]", text)
        tally = GW.close_turn(t.token)
        self.assertEqual(tally["requests"], 1)
        self.assertGreater(tally["cost_usd"], 0)
        self.assertTrue(tally["cost_exact"])
        self.assertEqual(tally["rerouted"], 1)  # the side model was kept on the role's model
        log = mock_log()[-1]
        self.assertEqual(log["model"], "anthropic/claude-sonnet-4.5")
        self.assertEqual(log["title"], "Relay")
        self.assertEqual(log["provider"], {"sort": "price", "data_collection": "deny"})
        self.assertEqual(log["usage_opt"], {"include": True})
        self.assertNotEqual(log["auth_prefix"], "sk-or-v1-relay"[:8] + "x")
        # the token dies with the turn
        st, _ = call_gateway(t.token, "/api/v1/chat/completions", {"model": "a/b", "messages": []})
        self.assertEqual(st, 401)

    def test_responses_and_messages(self):
        t = GW.open_turn("task1", None, "worker", "codex", "openai/gpt-5.1-codex")
        st, _ = call_gateway(t.token, "/api/v1/responses", {"model": "openai/gpt-5.1-codex", "input": "hi"}, stream=True)
        self.assertEqual(st, 200)
        st, _ = call_gateway(t.token, "/api/v1/messages", {"model": "x", "max_tokens": 10, "messages": [{"role": "user", "content": "hi"}]}, stream=True)
        self.assertEqual(st, 200)
        st, _ = call_gateway(t.token, "/api/v1/messages", {"model": "x", "max_tokens": 10, "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(st, 200)
        tally = GW.close_turn(t.token)
        self.assertEqual(tally["ok_requests"], 3)
        self.assertGreater(tally["input"], 0)
        self.assertGreater(tally["cost_usd"], 0)

    def test_unknown_token_and_paths(self):
        st, _ = call_gateway("sk-or-v1-not-a-turn", "/api/v1/chat/completions", {"model": "a/b", "messages": []})
        self.assertEqual(st, 401)
        t = GW.open_turn("task1", None, "worker", "opencode", "a/b")
        st, _ = call_gateway(t.token, "/api/v1/embeddings", {"model": "a/b"})
        self.assertEqual(st, 404)
        GW.close_turn(t.token)

    def test_rate_limit_rotates_the_automatic_pick(self):
        s_before = os.environ.get("RELAY_TEST_WAIT")
        mock_reset(force={"status": 429, "times": 50, "model": "qwen/qwen3-coder:free"})
        from orchestrator.org import settings as OS
        OS.save({"providers": {"openrouter": {"rate_limit_wait_seconds": 0}}})
        try:
            t = GW.open_turn("task1", None, "worker", "opencode", "qwen/qwen3-coder:free", rotation=["anthropic/claude-sonnet-4.5"], auto=True)
            st, _ = call_gateway(t.token, "/api/v1/chat/completions", {"model": "qwen/qwen3-coder:free", "messages": [{"role": "user", "content": "hi"}]})
            self.assertEqual(st, 200)
            tally = GW.close_turn(t.token)
            self.assertEqual(tally["rotations"][0]["to"], "anthropic/claude-sonnet-4.5")
            self.assertIn("anthropic/claude-sonnet-4.5", tally["models"])
            self.assertTrue(OR.cooling("qwen/qwen3-coder:free"))
        finally:
            OS.save({"providers": {"openrouter": {"rate_limit_wait_seconds": 60}}})
            OR.clear_cooldown("qwen/qwen3-coder:free")
            _ = s_before

    def test_parameter_errors_do_not_rotate_or_blame_the_model(self):
        mock_reset(force={"status": 404, "times": 5, "message": "No endpoints found that support the provided 'tool_choice' value."})
        t = GW.open_turn("task1", None, "worker", "codex", "qwen/qwen3-coder:free", rotation=["anthropic/claude-sonnet-4.5"], auto=True)
        st, _ = call_gateway(t.token, "/api/v1/responses", {"model": "x", "input": "hi"})
        self.assertEqual(st, 404)
        tally = GW.close_turn(t.token)
        self.assertEqual(tally["rotations"], [])
        self.assertIsNone(OR.cooling("qwen/qwen3-coder:free"))
        self.assertIn("'tool_choice' parameter", tally["last_error"]["message"])

    def test_rotation_is_bounded_within_a_turn(self):
        mock_reset(force={"status": 503, "times": 100})
        t = GW.open_turn("task1", None, "worker", "opencode", "a/one:free", rotation=["b/two:free", "c/three:free"], auto=True)
        for _ in range(4):  # a CLI retrying on its own
            call_gateway(t.token, "/api/v1/chat/completions", {"model": "x", "messages": [{"role": "user", "content": "hi"}]})
        tally = GW.close_turn(t.token)
        sent = [e["model_sent"] for e in tally["errors"]]
        # Models left behind are never retried by the turn; only the last one is, when the CLI itself retries.
        self.assertEqual((sent.count("a/one:free"), sent.count("b/two:free")), (1, 1))
        self.assertEqual(len(tally["rotations"]), 2)
        for m in ("a/one:free", "b/two:free", "c/three:free"):
            OR.clear_cooldown(m)

    def test_errors_pass_through_with_a_reason(self):
        mock_reset(credits=0.0, usage=0.0)
        t = GW.open_turn("task1", None, "worker", "opencode", "anthropic/claude-sonnet-4.5")
        st, text = call_gateway(t.token, "/api/v1/chat/completions", {"model": "x", "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(st, 402)
        tally = GW.close_turn(t.token)
        self.assertEqual(tally["last_error"]["status"], 402)
        self.assertIn("out of credits", tally["last_error"]["message"])

    def test_spend_cap_refuses_paid_requests(self):
        from orchestrator.org import settings as OS
        OS.save({"providers": {"openrouter": {"monthly_cap_usd": 0.5}}})
        GW.TASKS_FN = lambda: [{"id": "t", "metrics": {"log": [{"provider": "openrouter", "end": time.time(), "cost_usd": 0.6}]}}]
        try:
            t = GW.open_turn("task1", None, "worker", "opencode", "anthropic/claude-sonnet-4.5")
            st, text = call_gateway(t.token, "/api/v1/chat/completions", {"model": "x", "messages": [{"role": "user", "content": "hi"}]})
            self.assertEqual(st, 402)
            self.assertIn("monthly cap", text)
            GW.close_turn(t.token)
            t = GW.open_turn("task1", None, "worker", "opencode", "qwen/qwen3-coder:free")
            st, _ = call_gateway(t.token, "/api/v1/chat/completions", {"model": "x", "messages": [{"role": "user", "content": "hi"}]})
            self.assertEqual(st, 200)  # free models are not capped
            GW.close_turn(t.token)
        finally:
            GW.TASKS_FN = None
            OS.save({"providers": {"openrouter": {"monthly_cap_usd": 0}}})


class AccountingTest(unittest.TestCase):
    def test_end_turn_records_real_cost(self):
        mock_reset()
        p = ORL.begin_turn({"id": "t"}, "worker", "opencode", "anthropic/claude-sonnet-4.5", {}, tempfile.mkdtemp(dir=TMP), {}, tasks=[])
        call_gateway(p.gateway.token, "/api/v1/chat/completions", {"model": "x", "messages": [{"role": "user", "content": "hi"}]}, stream=True)
        usage = {"input": 0, "output": 0, "cost_usd": 0.0}
        res = {"ok": True}
        info = ORL.end_turn(p, res, usage)
        self.assertGreater(usage["cost_usd"], 0)
        self.assertTrue(usage["cost_exact"])
        extra = ORL.log_extra(info, usage)
        self.assertEqual((extra["provider"], extra["or_models"]), ("openrouter", ["anthropic/claude-sonnet-4.5"]))
        self.assertIn("OpenRouter · anthropic/claude-sonnet-4.5", ORL.turn_note(info))

    def test_manager_trusts_exact_costs(self):
        from orchestrator.manager import Manager

        class Stub:
            def cfg(self):
                return {"pricing": {"opencode": {"input": 5, "output": 25}}, "show_estimated_cost": True}
        self.assertEqual(Manager.estimate_cost(Stub(), "opencode", {"input": 1000, "output": 1000, "cost_usd": 0.0, "cost_exact": True}), (0.0, False))
        self.assertEqual(Manager.estimate_cost(Stub(), "opencode", {"input": 1, "output": 1, "cost_usd": 0.2, "cost_exact": False}), (0.2, True))

    def test_usage_report_and_history(self):
        now = time.time()
        tasks = [{"id": "t1", "status": "done", "metrics": {"log": [
            {"provider": "openrouter", "end": now, "cost_usd": 0.5, "role": "worker", "agent": "codex", "or_models": ["a/b"], "or_model_costs": {"a/b": 0.5}, "or_requests": 2},
            {"provider": "openrouter", "end": now, "cost_usd": 0.0, "role": "supervisor", "agent": "opencode", "or_models": ["c/d:free"], "or_error": True, "or_status": 429},
            {"agent": "claude", "end": now, "cost_usd": 9.0}]}}]
        rep = OR.usage_report(tasks)
        self.assertAlmostEqual(rep["month"]["cost_usd"], 0.5)
        self.assertEqual(rep["by_model"][0]["key"], "a/b")
        self.assertEqual({r["key"] for r in rep["by_role"]}, {"worker", "supervisor"})
        h = OR.model_history(tasks)
        self.assertEqual(h["c/d:free"]["rate_limited_24h"], 1)
        self.assertEqual(h["a/b"]["tasks_ok"], 1)


class SettingsTest(unittest.TestCase):
    def test_secret_masked_and_kept(self):
        from orchestrator.org import settings as OS
        from orchestrator.org.common import MASK
        OS.save({"providers": {"openrouter": {"api_key": "sk-or-v1-savedsavedsaved1234", "routing": {"data_collection": "deny", "order": "a, b"}}}})
        pub = OR.public()
        self.assertEqual(pub["api_key"], MASK)
        self.assertEqual(pub["key_hint"], "sk-or-v1…1234")
        self.assertEqual(OS.public()["providers"]["openrouter"]["api_key"], MASK)
        OS.save({"providers": {"openrouter": {"api_key": MASK}}})
        self.assertEqual(OR.settings()["api_key"], "sk-or-v1-savedsavedsaved1234")
        self.assertEqual(OR.routing()["order"], ["a", "b"])
        with self.assertRaises(ValueError):
            OS.save({"providers": {"openrouter": {"connection": "carrier-pigeon"}}})
        OS.save({"providers": {"openrouter": {"api_key": ""}}})

    def test_team_keys_carry_the_provider(self):
        team = {"supervisor": {"agent": "codex", "model": "openrouter:auto-free", "provider": "openrouter"}, "worker": {"agent": "claude", "model": "opus"}}
        key = outcomes.team_key(team)
        self.assertEqual(key, "codex+openrouter:openrouter:auto-free>claude:opus")
        back = outcomes.team_from_key(key)
        self.assertEqual(back["supervisor"]["provider"], "openrouter")
        self.assertEqual(back["supervisor"]["model"], "openrouter:auto-free")
        self.assertIn("via OpenRouter", outcomes.team_label(key))


if __name__ == "__main__":
    unittest.main()
