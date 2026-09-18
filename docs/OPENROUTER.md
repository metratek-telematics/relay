# OpenRouter

Relay can run any role (supervisor, worker, reviewer) on [OpenRouter](https://openrouter.ai) instead of the agent's own
sign-in. The agent CLI stays the same: its tools, its loop and its session. Only its model calls go to OpenRouter, so a
team can be "OpenCode on `qwen/qwen3-coder`", "Claude Code on `anthropic/claude-sonnet-4.5` via OpenRouter", or
"Codex on the automatic best free model".

## Set it up

1. Create a key at openrouter.ai/settings/keys. A per-key credit limit there is a second safety net under Relay's own
   caps.
2. In Relay, open **Settings → Model providers** (admins only) and paste the key. **Test** calls OpenRouter's
   `/api/v1/key` and `/api/v1/credits` and shows the limit, the usage, the tier (free or paid), today's free requests
   and the credits left. The key is stored in the organisation settings on this server (file mode 0600), is masked in
   every response and in the audit log, and is never handed to an agent process (see *How agents connect*). Like
   Relay's other secrets it lives in Relay's data folder, so an agent running as the same user could in principle read
   that file; a per-key credit limit on openrouter.ai caps the damage either way.
3. In a team (New task → Team, or Settings → Team and workflow), pick an agent for a role and set **Runs on →
   OpenRouter**. Choose a model from the list, **Browse** the OpenRouter catalog, or keep **Auto · best free model**.

A deployment can provide the key instead of the settings page: `RELAY_OPENROUTER_API_KEY`, or
`RELAY_OPENROUTER_API_KEY_FILE` pointing at a file (for Docker secrets). A key saved in the settings wins. Relay reads
these once at start and removes them from its own environment, so agents and checks, which inherit that environment,
never see them.

## Which agents can run on OpenRouter

Every recipe below was checked against the installed CLI, first with the mock server (`tools/mock_openrouter.py`) in
real Relay tasks, then with a live free-tier key and a free model. Nothing touches the owner's own CLI logins or config:
everything is an environment variable or a file in the task's run folder.

| Agent | How it is launched on OpenRouter |
|---|---|
| Claude Code | `ANTHROPIC_BASE_URL` + `ANTHROPIC_AUTH_TOKEN` (OpenRouter's Anthropic-compatible endpoint), `ANTHROPIC_API_KEY` empty, every model class (`ANTHROPIC_DEFAULT_*_MODEL`, small/fast, subagents) set to the chosen model, and a **private `CLAUDE_CONFIG_DIR` per run**, so the owner's Claude subscription login is neither used nor changed. Claude Code is built for Anthropic models; other models may not follow its tool protocol (Relay says so in the conversation). OpenRouter recommends Anthropic's own endpoints for Claude Code: put `anthropic` first under *Provider order*. |
| Codex | A custom model provider: `-c model_provider="relay_openrouter" -c model_providers.relay_openrouter={base_url=…, env_key=…, wire_api="responses"}`. OpenRouter serves the Responses API, which is the only wire API current Codex accepts. Codex's own `--search` tool is OpenAI-only and is left off. |
| OpenCode, Kilo | The built-in `openrouter` provider through `OPENCODE_CONFIG_CONTENT` / `KILO_CONFIG_CONTENT` (base URL, key from an env variable, routing preferences per model, `small_model` pinned so titles do not use another model); model `openrouter/<id>`. The task's MCP servers are merged into the same config. |
| Cline | An OpenAI-compatible provider in a per-run `--data-dir` (`settings/providers.json`, mode 0600, removed after the turn). Cline's own `openrouter` provider cannot change its base URL. |
| Goose | `GOOSE_PROVIDER=openrouter`, `OPENROUTER_HOST`, `OPENROUTER_API_KEY`, `GOOSE_MODEL`. |
| Aider | litellm's `openrouter/<id>` with `OPENROUTER_API_BASE`; headers and routing in a per-run `--model-settings-file`; the weak and editor models pinned to the same model. |
| Crush | An `openai-compat` provider in a per-run global config (`CRUSH_GLOBAL_CONFIG`) with `extra_headers` and `extra_body`. Crush's `openrouter` type ignores `base_url`. |
| Qwen Code | `--auth-type openai` with `OPENAI_BASE_URL`, `OPENAI_API_KEY`, `OPENAI_MODEL`. |
| Continue | A per-run `config.yaml` (`provider: openrouter`, `apiBase`, key from `${{ secrets.… }}`, `requestOptions` for headers and routing) passed with `--config`. |
| GitHub Copilot | Bring-your-own-key: `COPILOT_PROVIDER_BASE_URL`, `COPILOT_PROVIDER_API_KEY`, `COPILOT_MODEL`, `COPILOT_OFFLINE=true` (no GitHub sign-in needed; Copilot's web tools are off in this mode). |
| Gemini CLI | Not supported: it only speaks Google's Gemini API, and OpenRouter offers OpenAI- and Anthropic-style APIs. |
| Amp | Not supported: Amp picks its own models on its own service and has no custom provider setting. |
| Cursor | Not supported: Cursor's agent only runs on Cursor's backend. |

The Agents page has **Test on OpenRouter**: one short turn per installed agent through the same path a task takes.

## How agents connect

**Through Relay's gateway (default).** Relay runs a small gateway inside its own process on `127.0.0.1` (a random
port). Each agent turn gets a random token that works only there, only for model calls, and only while that turn runs;
the gateway swaps it for the real key. Because every CLI goes through one place, Relay can apply the same policy to all
of them:

- routing and privacy preferences on every request (privacy settings always override what a CLI asks for);
- attribution headers (`X-Title`, and `HTTP-Referer` when Relay has a public URL);
- pacing of free models under OpenRouter's 20 requests a minute;
- waiting out `429` responses (honouring `Retry-After`) up to the configured time;
- moving to the next model when one is rate limited, down (`502`/`503`) or gone (`404`); a model left behind is not
  retried by the same turn, and at most six switches happen per turn;
- side calls a CLI makes on its own (titles, summaries, "small fast" models) stay on the role's model, so a turn never
  spends on a model nobody chose;
- fields that pick models or paid add-ons (`models`, `route`, `plugins`, `transforms`, `preset`) are removed from
  what an agent sends: only Relay chooses the model;
- the spend caps: a request that would pass one is refused with a `402` that says which cap. Every request is charged
  to a monthly ledger as soon as it completes, so stopped, interrupted or timed-out turns, retrospectives, agent tests
  and later-deleted tasks all count;
- the real cost of every request (see *Cost*).

**Directly.** The CLI gets the key in its environment and talks to OpenRouter itself. Routing preferences then only
apply where the CLI can send them (OpenCode, Kilo, Crush, Aider, Continue), there is no pacing, rotation or cap
enforcement per request, and cost comes from what the CLI reports (or the catalog price, marked as an estimate).

## Models

The model browser (**Agents → OpenRouter → Models & usage**, **Browse** in a team, or **OpenRouter models** in any
supported agent's Models & usage dialog) reads OpenRouter's public `/api/v1/models` catalog, cached for six hours. It
shows price per million tokens (input / output), context, tool calling, reasoning, structured outputs and image input,
with filters (free only, tool calling, reasoning, recommended, starred, minimum context) and sorting by price, context
or age. **★** adds a model to the OpenRouter pickers of every team.

**Recommended for coding** is computed, not a list of names: tool calling, a context of at least 100k, then points for
context size, reasoning, structured outputs, tool choice, age, a family's larger tier and value for money (a coding turn
reads far more than it writes, so input price weighs three times output). Variants such as `:batch` are listed through
their base model, and at most two models per vendor make the shortlist. Each entry shows its reasons.

### Auto · best free model

`openrouter:auto-free` asks Relay to pick. A candidate must be free (`:free` or zero price), support tool calling,
output text and have at least the configured context (64k by default). Candidates are scored on the same capability
signals as above, plus Relay's own history with each model (share of tasks delivered, share of turns that worked) and
recent trouble (rate limits and failures in the last 24 hours). OpenRouter's own free router, `openrouter/free`, closes
the list as a last resort. The model browser shows the current ranking with the reasons.

The pick is made when a role starts and kept for the session. When a model is rate limited or unavailable the gateway
rotates to the next one in the ranking for the rest of the turn and puts the failed model on a short cooldown (rate
limits back off 2, 4, 8 … 60 minutes; a model that is gone waits six hours), so the next turn starts elsewhere. Which
model actually answered each turn is shown in the conversation and on the task's **Sessions** tab.

## Cost and usage

OpenRouter reports the cost of each generation in its usage block; Relay records it per request, so a turn's cost is
what OpenRouter charged, not an estimate. A stream that carries no cost is looked up at `/api/v1/generation?id=…` when
the turn ends; only if that fails too does Relay fall back to the catalog price, marked with `~`. Free models record $0.

Each turn on OpenRouter is kept in the task's turn log with the provider, the models that answered, the cost per model,
the number of requests and any error, so spend can be read per role, agent, task and project:

- the conversation shows one line per turn (models, requests, cost, any switch);
- the task's **Sessions** tab has an **OpenRouter spend** card (per turn and per model);
- the OpenRouter **Models & usage** dialog shows the account (credits left, key limit, tier, free requests today,
  OpenRouter's own usage today and this month) next to what Relay spent today and this month, by model;
- **Settings → Usage** includes the OpenRouter caps as budget meters with the usual 50/80/100 % alerts.

### Caps and alerts

- **Monthly cap, all projects** and **per project** (Settings → Model providers; only owners change caps, like other
  budgets). At a cap, paid OpenRouter models stop:
  the gateway refuses paid requests and autopilot does not start tasks whose roles need them (it switches to a fallback
  agent or waits for the month to turn). Free models keep working.
- **Low credits**: one notification a day when the credits left fall below the threshold (only for accounts that have
  bought credits).

### Autopilot

Before a task starts, each role on OpenRouter is checked: a key is set, the account has credits for a paid model (free
models are fine at zero credits), today's free requests are not used up (the wait lasts until midnight UTC), and no cap
blocks it. A blocked role uses the role's fallback chain (Settings → Autopilot; `opencode+openrouter:qwen/qwen3-coder:free`
names an agent on OpenRouter) or the task waits. The CLI's own sign-in does not matter for a role on OpenRouter.

## Errors

OpenRouter's status codes become sentences a person can act on, in the conversation and the task error, and failure
categories for the autopsy:

| Status | What Relay says and does |
|---|---|
| 401 | The key was rejected: check Settings → Model providers. Not retried (`provider_auth`). |
| 402 | Out of credits, or the key's own limit, or one of Relay's caps (named). Not retried; autopilot falls back or waits (`provider_credits`). |
| 403 | A moderation flag or a guardrail on the key. |
| 404 | The model is gone (rotates, cooldown), no endpoint matches the data policy, or no provider supports a parameter the CLI sends (not the model's fault: no rotation, no cooldown; turn off *Only providers that support every parameter*). |
| 429 | Rate limited: waited out, then the next model; free-model limits are spelled out (20 a minute; 50 a day, 1000 once the account bought 10 credits) (`rate_limit`, retried). |
| 502 / 503 | The provider is down or none matches the routing (privacy settings narrow the choice): next model (`provider_unavailable`, retried). |

## Privacy

- **Deny data collection** routes only to providers that do not store or train on prompts.
- **Zero data retention** restricts routing to ZDR endpoints.
- **Provider order** and **never use** lists take OpenRouter provider slugs.
- These are sent as OpenRouter's `provider` preferences with every request through the gateway, on all three APIs
  (OpenRouter's Anthropic-compatible endpoint validates and honours them too). The account-wide
  defaults at openrouter.ai/settings/privacy apply as well; a request can only narrow them.

## Development

`RELAY_OPENROUTER_BASE_URL` (environment only, never in the UI) points Relay at another OpenRouter-compatible server.
Settings shows a warning while it is set. `tools/mock_openrouter.py` is such a server: it speaks chat completions, the
Responses API and Anthropic messages (streaming or not), plus `/models`, `/key`, `/credits` and `/generation`, plays a
scripted Relay team so real tasks finish, logs every request at `/__mock/log`, and can force failures through
`/__mock/config`. The unit tests (`python -m unittest tests.test_openrouter`) run it in-process.
