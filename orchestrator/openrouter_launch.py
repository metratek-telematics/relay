"""How each agent CLI runs one turn on OpenRouter, and what Relay does around that turn.

plan()        the per-CLI launch configuration: the model argument in the form that CLI takes, environment variables,
              per-run config files and extra flags. Each recipe was checked against the installed CLI (see
              docs/OPENROUTER.md for the list and how each was verified). Nothing is written to the owner's own CLI
              config or login: files go to the task's run folder, and the Claude Code recipe uses a private config
              folder so a subscription login is never used or changed for an OpenRouter turn.
begin_turn()  resolves the model (the automatic free pick included), opens a gateway session (or, in direct mode,
              hands the CLI the key), and returns what the runner applies around adapter.build().
end_turn()    closes the gateway session and turns its tally into the turn's real cost, the models that actually
              answered, a readable error, cooldowns for models that failed, and a note in the conversation.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from . import openrouter as OR

TOKEN_VAR = "RELAY_OPENROUTER_TOKEN"


def _state(run_dir, name: str) -> Path:
    d = Path(run_dir) / "agent-state" / name
    d.mkdir(parents=True, exist_ok=True)
    return d


def _private(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    return path


def _yaml_str(s: str) -> str:
    return json.dumps(str(s))  # a JSON string is a valid YAML scalar


def plan(agent: str, model: str, conn: dict, run_dir, role: str, cfg: dict, env_base: dict | None = None,
         context: int | None = None) -> dict:
    """The launch recipe for `agent` on OpenRouter with `model` (a concrete OpenRouter id).

    conn: {"openai_base": ".../api/v1", "anthropic_base": ".../api", "token": key or gateway token,
           "headers": {...}, "routing": {...}, "direct": bool}
    Returns {"model_arg", "env", "unset", "cfg", "cleanup", "notes"}."""
    if not OR.supports(agent):
        raise RuntimeError(f"{agent} cannot run on OpenRouter: {(OR.SUPPORT.get(agent) or {}).get('why') or 'not supported'}")
    env_base = env_base or {}
    tok, base, headers, prefs = conn["token"], conn["openai_base"], dict(conn.get("headers") or {}), dict(conn.get("routing") or {})
    host = base[:-len("/api/v1")] if base.endswith("/api/v1") else base
    out = {"model_arg": model, "env": {TOKEN_VAR: tok}, "unset": [], "cfg": {}, "cleanup": [], "notes": []}
    env, extra = out["env"], lambda key, vals: out["cfg"].__setitem__(key, list(cfg.get(key) or []) + vals)
    slug = re.sub(r"[^\w.-]+", "-", role or "agent")

    if agent == "claude":
        cdir = _state(run_dir, f"claude-openrouter-{slug}")
        env.update({"ANTHROPIC_BASE_URL": conn["anthropic_base"], "ANTHROPIC_AUTH_TOKEN": tok, "ANTHROPIC_API_KEY": "",
                    # A private config folder: the owner's subscription login is neither used nor touched.
                    "CLAUDE_CONFIG_DIR": str(cdir), "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
                    # Every model class Claude Code reaches for (helpers, subagents, the "small fast" model) is this one.
                    "ANTHROPIC_MODEL": model, "ANTHROPIC_DEFAULT_OPUS_MODEL": model, "ANTHROPIC_DEFAULT_SONNET_MODEL": model,
                    "ANTHROPIC_DEFAULT_HAIKU_MODEL": model, "ANTHROPIC_DEFAULT_FABLE_MODEL": model, "ANTHROPIC_SMALL_FAST_MODEL": model,
                    "CLAUDE_CODE_SUBAGENT_MODEL": model})
        if headers:
            env["ANTHROPIC_CUSTOM_HEADERS"] = "\n".join(f"{k}: {v}" for k, v in headers.items())
        if not model.startswith(("anthropic/", "~anthropic/")):
            out["notes"].append("Claude Code is built for Anthropic models; this one may not follow its tool protocol.")

    elif agent == "codex":
        provider = {"name": "OpenRouter", "base_url": base, "env_key": TOKEN_VAR, "wire_api": "responses"}
        if headers:
            provider["http_headers"] = headers
        table = "{" + ", ".join(f"{k}={_toml(v)}" for k, v in provider.items()) + "}"
        extra("codex_extra_args", ["-c", 'model_provider="relay_openrouter"', "-c", f"model_providers.relay_openrouter={table}"])
        out["cfg"]["agent_web_search"] = False  # the Responses web_search tool is OpenAI's own, not OpenRouter's
        if not model.startswith("openai/"):
            out["cfg"]["token_codex_verbosity"] = ""  # text.verbosity is a GPT-5 parameter

    elif agent in ("opencode", "kilo"):
        var = "KILO_CONFIG_CONTENT" if agent == "kilo" else "OPENCODE_CONFIG_CONTENT"
        try:
            content = json.loads(env_base.get(var) or "{}")
        except ValueError:
            content = {}
        prov = dict((content.get("provider") or {}).get("openrouter") or {})
        opts = {**(prov.get("options") or {}), "baseURL": base, "apiKey": "{env:%s}" % TOKEN_VAR}
        if headers:
            opts["headers"] = headers
        prov["options"] = opts
        if prefs:
            prov["models"] = {**(prov.get("models") or {}), model: {"options": {"provider": prefs}}}
        content.setdefault("provider", {})["openrouter"] = prov
        content["model"] = f"openrouter/{model}"
        content["small_model"] = f"openrouter/{model}"  # titles and summaries stay on the chosen model
        env[var] = json.dumps(content)
        env["OPENROUTER_API_KEY"] = tok
        out["model_arg"] = f"openrouter/{model}"

    elif agent == "cline":
        data = _state(run_dir, f"cline-openrouter-{slug}")
        providers = {"version": 1, "lastUsedProvider": "openai-compatible", "modes": {},
                     "providers": {"openai-compatible": {"settings": {"provider": "openai-compatible", "apiKey": tok, "model": model,
                                                                      "baseUrl": base}, "tokenSource": "manual",
                                                         # Without updatedAt Cline ignores the entry and falls back to api.openai.com.
                                                         "updatedAt": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())}}}
        f = _private(data / "settings" / "providers.json", json.dumps(providers, indent=2))
        out["cleanup"].append(str(f))
        env["CLINE_DATA_DIR"] = str(data)
        extra("cline_extra_args", ["--data-dir", str(data)])
        out["model_arg"] = f"openai-compatible:{model}"

    elif agent == "goose":
        env.update({"GOOSE_PROVIDER": "openrouter", "GOOSE_MODEL": model, "OPENROUTER_API_KEY": tok, "OPENROUTER_HOST": host,
                    "GOOSE_DISABLE_KEYRING": "1"})
        out["model_arg"] = f"openrouter:{model}"

    elif agent == "aider":
        name = f"openrouter/{model}"
        params = {}
        if headers:
            params["extra_headers"] = headers
        if prefs:
            params["extra_body"] = {"provider": prefs}
        f = _state(run_dir, f"aider-openrouter-{slug}") / "model-settings.yml"
        f.write_text(f"- name: {_yaml_str(name)}\n  extra_params: {json.dumps(params)}\n", encoding="utf-8")
        env.update({"OPENROUTER_API_KEY": tok, "OPENROUTER_API_BASE": base})
        extra("aider_extra_args", ["--model-settings-file", str(f), "--weak-model", name, "--editor-model", name])
        out["model_arg"] = name

    elif agent == "crush":
        gdir = _state(run_dir, f"crush-openrouter-{slug}")
        provider = {"name": "OpenRouter (Relay)", "type": "openai-compat", "base_url": base, "api_key": "$" + TOKEN_VAR,
                    "models": [{"id": model, "name": model, "context_window": int(context or 128000), "default_max_tokens": 8192}]}
        if headers:
            provider["extra_headers"] = headers
        if prefs:
            provider["extra_body"] = {"provider": prefs}
        conf = {"providers": {"relay-openrouter": provider}, "options": {"disable_provider_auto_update": True},
                "models": {"large": {"model": model, "provider": "relay-openrouter"}, "small": {"model": model, "provider": "relay-openrouter"}}}
        (gdir / "crush.json").write_text(json.dumps(conf, indent=2), encoding="utf-8")
        env["CRUSH_GLOBAL_CONFIG"] = str(gdir)
        out["model_arg"] = f"relay-openrouter/{model}"

    elif agent == "qwen":
        env.update({"OPENAI_API_KEY": tok, "OPENAI_BASE_URL": base, "OPENAI_MODEL": model})
        extra("qwen_extra_args", ["--auth-type", "openai"])

    elif agent == "continue":
        lines = ["name: Relay OpenRouter", "version: 1.0.0", "schema: v1", "models:",
                 f"  - name: {_yaml_str(model)}", "    provider: openrouter", f"    model: {_yaml_str(model)}",
                 f"    apiBase: {_yaml_str(base)}", "    apiKey: ${{ secrets." + TOKEN_VAR + " }}",
                 "    roles: [chat, edit, apply]", "    capabilities: [tool_use]"]
        req = {}
        if headers:
            req["headers"] = headers
        if prefs:
            req["extraBodyProperties"] = {"provider": prefs}
        if req:
            lines.append(f"    requestOptions: {json.dumps(req)}")
        f = _state(run_dir, f"continue-openrouter-{slug}") / "config.yaml"
        f.write_text("\n".join(lines) + "\n", encoding="utf-8")
        extra("continue_extra_args", ["--config", str(f)])
        out["model_arg"] = ""  # the model comes from that config; --model would look it up on Continue's hub

    elif agent == "copilot":
        env.update({"COPILOT_PROVIDER_BASE_URL": base, "COPILOT_PROVIDER_TYPE": "openai", "COPILOT_PROVIDER_API_KEY": tok,
                    "COPILOT_MODEL": model, "COPILOT_OFFLINE": "true"})
        if context:
            env["COPILOT_PROVIDER_MAX_PROMPT_TOKENS"] = str(int(context))
        if headers:
            env["COPILOT_PROVIDER_HEADERS"] = "\n".join(f"{k}: {v}" for k, v in headers.items())
        out["unset"] += ["COPILOT_PROVIDER_BEARER_TOKEN", "COPILOT_PROVIDER_API_KEY_COMMAND"]
        out["model_arg"] = ""
    return out


def _toml(v) -> str:
    if isinstance(v, dict):
        return "{" + ", ".join(f"{json.dumps(str(k))}={_toml(x)}" for k, x in v.items()) + "}"
    return json.dumps(v)


# ============================================================================ around one turn
class TurnPlan:
    def __init__(self):
        self.model = ""          # the OpenRouter model this turn starts on
        self.requested = ""      # what the role asked for (may be the automatic free pick)
        self.auto = False
        self.pick = None         # the ranking entry of an automatic pick
        self.gateway = None      # openrouter_proxy.Turn in proxy mode
        self.recipe = {}
        self.cfg = {}
        self.direct = False

    @property
    def model_arg(self):
        return self.recipe.get("model_arg", self.model)

    def apply(self, env: dict, args: list) -> list:
        """Put the recipe into what adapter.build() returned (env is changed in place)."""
        if env is not None:
            for k in self.recipe.get("unset") or []:
                env.pop(k, None)
            env.update(self.recipe.get("env") or {})
            if "AIDER_SET_ENV" in env or any(k.startswith("OPENROUTER_") for k in (self.recipe.get("env") or {})):
                _aider_protect(env)
        return args


def _aider_protect(env: dict):
    """Aider loads a repository's .env over the environment; --set-env (AIDER_SET_ENV) makes Relay's values win."""
    from .pack.text_based import AiderAdapter
    keep = [f"{k}={v}" for k, v in sorted(env.items()) if AiderAdapter._PROTECT.match(k) and v and not re.search(r"[,\[\]\n]", v)]
    if keep:
        env["AIDER_SET_ENV"] = "[" + ", ".join(keep) + "]"


def resolve_model(model: str, session: dict | None, tasks=None, s: dict | None = None) -> tuple[str, bool, dict | None, list]:
    """(concrete model, automatic?, ranking entry, rotation) for a role's model setting."""
    s = s or OR.settings()
    model = (model or "").strip() or (s.get("default_model") or OR.AUTO_FREE)
    if not OR.is_auto(model):
        return model, False, None, [m for m in s.get("fallback_models") or [] if m != model]
    sticky = (session or {}).get("or_model")
    rows = OR.catalog()["models"]
    first, rotation, entry = OR.pick_free(tasks, s, rows=rows)
    if sticky and sticky != first and not OR.cooling(sticky) and any(r["id"] == sticky and r.get("free") for r in rows):
        # Keep a working pick for the whole session; switching models mid-conversation costs context quality.
        rotation = [first] + [m for m in rotation if m != sticky]
        return sticky, True, {"id": sticky, "why": ["kept from earlier turns in this session"], "score": None}, rotation
    return first, True, entry, rotation


def begin_turn(task: dict, role: str, agent: str, model: str, cfg: dict, run_dir, session: dict | None, env_base=None,
               tasks=None, project: str | None = None) -> TurnPlan:
    s = OR.settings()
    if not s.get("enabled", True):
        raise RuntimeError("OpenRouter is turned off in Settings → Model providers.")
    key = OR.api_key(s)
    if not key:
        raise RuntimeError("OpenRouter has no API key: an admin adds one under Settings → Model providers.")
    p = TurnPlan()
    p.requested = (model or "").strip() or (s.get("default_model") or OR.AUTO_FREE)
    p.model, p.auto, p.pick, rotation = resolve_model(p.requested, session, tasks, s)
    p.direct = s.get("connection") == "direct"
    row = OR.model_row(p.model) or {}
    if p.direct:  # the CLI talks to OpenRouter itself, with the key
        base = OR.base_url()
        root = base[:-len("/v1")] if base.endswith("/v1") else base
        conn = {"openai_base": base, "anthropic_base": root, "token": key, "headers": OR.attribution_headers(s),
                "routing": OR.routing(s), "direct": True}
    else:
        from . import openrouter_proxy as GW
        p.gateway = GW.open_turn(task.get("id") or "", project, role, agent, p.model, rotation, p.auto)
        # The gateway adds attribution and routing itself; the CLI only needs where and which token.
        conn = {"openai_base": GW.base_openai(), "anthropic_base": GW.base_anthropic(), "token": p.gateway.token,
                "headers": {}, "routing": OR.routing(s) if (OR.SUPPORT.get(agent) or {}).get("native_routing") else {}, "direct": False}
    p.recipe = plan(agent, p.model, conn, run_dir, role, cfg, env_base or {}, context=row.get("context"))
    p.cfg = {**cfg, **p.recipe["cfg"]}
    return p


def end_turn(p: TurnPlan, res: dict, usage: dict) -> dict:
    """After the CLI exited: the real cost and what ran. Returns the note for the conversation and log row extras."""
    for f in p.recipe.get("cleanup") or []:
        try:
            Path(f).unlink(missing_ok=True)  # per-run files that held a credential
        except OSError:
            pass
    info = {"provider": OR.PROVIDER, "model": p.model, "requested": p.requested, "auto": p.auto}
    if p.gateway is not None:
        from . import openrouter_proxy as GW
        tally = GW.close_turn(p.gateway.token)
        info.update(tally=tally)
        # Every model call of the turn went through the gateway, so its tally is the bill, not the CLI's own estimate
        # (Claude Code, for one, prices unknown models from its own table).
        usage["cost_usd"] = round(tally["cost_usd"], 8)
        usage["cost_exact"] = bool(tally["cost_exact"])
        for k in ("input", "output", "cached"):
            if tally[k] and not int(usage.get(k) or 0):
                usage[k] = tally[k]
        models = tally["models"] or [p.model]
        info.update(models=models, model_costs=tally["model_costs"], requests=tally["requests"], rotations=tally["rotations"],
                    final_model=tally.get("final_model") or p.model)
        err = tally.get("last_error")
        if err and not res.get("ok"):
            res["error"] = f"{err.get('message') or 'OpenRouter error'}" + (f"\n{res['error']}" if res.get("error") else "")
        info["error"] = err
    else:
        # Direct: the CLI's own cost report, else the catalog price (an estimate), free models cost nothing.
        reported = float(usage.get("cost_usd") or 0)
        if OR.is_free(p.model):
            usage["cost_usd"], usage["cost_exact"] = 0.0, True
        elif not reported:
            est = OR.estimate_cost(p.model, int(usage.get("input") or 0), int(usage.get("output") or 0), int(usage.get("cached") or 0))
            if est is not None:
                usage["cost_usd"], usage["cost_exact"] = est, False
        info.update(models=[p.model], model_costs={p.model: float(usage.get("cost_usd") or 0)}, requests=0, rotations=[], final_model=p.model)
        text = f"{res.get('error') or ''}"
        m = re.search(r"\b(401|402|403|404|429|502|503)\b", text)
        info["error"] = {"status": int(m.group(1)), "message": OR.describe_error(int(m.group(1)), "", None, p.model)["message"]} if (m and not res.get("ok")) else None
        if info["error"]:
            res["error"] = info["error"]["message"] + "\n" + text
    err = info.get("error")
    if err and err.get("status") in (404, 429, 503) and p.auto:
        OR.cooldown(err.get("model") or info["final_model"], err.get("message") or "", err.get("status"))
    return info


def turn_note(info: dict) -> str:
    """One line for the conversation: which model really ran this turn, and what it cost."""
    models = info.get("models") or [info.get("model")]
    parts = [f"OpenRouter · {', '.join(models)}"]
    if info.get("auto"):
        parts.append("automatic free pick")
    rot = info.get("rotations") or []
    if rot:
        parts.append("switched " + ", ".join(f"{r['from']} → {r['to']} ({r['status']})" for r in rot[:3]))
    if info.get("requests"):
        parts.append(f"{info['requests']} request{'s' if info['requests'] != 1 else ''}")
    tally = info.get("tally") or {}
    if tally.get("rerouted"):
        parts.append(f"{tally['rerouted']} side call{'s' if tally['rerouted'] != 1 else ''} kept on this model")
    return " · ".join(parts)


def log_extra(info: dict, usage: dict) -> dict:
    """Fields for the task's turn log (metrics.log), read by usage reports, the model history and learning."""
    err = info.get("error") or {}
    return {"provider": OR.PROVIDER, "model": info.get("final_model") or info.get("model"), "or_requested": info.get("requested"),
            "or_models": info.get("models") or [], "or_model_costs": info.get("model_costs") or {},
            "or_requests": int(info.get("requests") or 0), "or_auto": bool(info.get("auto")),
            "or_error": bool(err), "or_status": err.get("status") if err else None, "cost_exact": bool(usage.get("cost_exact")),
            "at": time.time()}
