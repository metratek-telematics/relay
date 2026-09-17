// API docs: the /api/v1 reference rendered from Relay's own OpenAPI document, with curl examples and recipes.
import { $, $$, esc, icon, toast, skeleton } from "../../ui.js";
import { request } from "../../api.js";
import { orgApi, xicon, bindCopy } from "./org.js";

const code = (text, lang = "") => `<div class="org-code-wrap"><pre class="org-code" data-lang="${esc(lang)}">${esc(text)}</pre><button class="btn xs" data-copy="${esc(text)}">${icon("copy")}Copy</button></div>`;
const slug = (method, path) => `op-${method}-${path.replace(/[^a-z0-9]+/gi, "-")}`.replace(/-+$/, "");

function resolve(spec, s) {
  let guard = 0;
  while (s && s.$ref && guard++ < 10) s = s.$ref.split("/").slice(1).reduce((o, k) => (o || {})[k], spec);
  return s || {};
}
function typeOf(spec, s) {
  s = resolve(spec, s);
  if (s.type === "array") return `array of ${typeOf(spec, s.items)}`;
  return (s.type || "object") + (s.enum ? ` (${s.enum.slice(0, 6).join(" | ")}${s.enum.length > 6 ? " | …" : ""})` : "");
}
function fieldsTable(spec, schema) {
  const s = resolve(spec, schema);
  const props = Object.entries(s.properties || {});
  if (!props.length) return `<span class="muted">${esc(typeOf(spec, s))}</span>`;
  const req = new Set(s.required || []);
  return `<div class="org-table-wrap"><table class="org-params"><tbody>${props.map(([k, v]) => {
    const r = resolve(spec, v);
    return `<tr><td>${esc(k)}${req.has(k) ? ' <span class="org-req" title="required">*</span>' : ""}</td><td>${esc(typeOf(spec, v))}</td><td>${esc(r.description || v.description || "")}${r.default !== undefined ? ` <span class="muted">default ${esc(JSON.stringify(r.default))}</span>` : ""}</td></tr>`;
  }).join("")}</tbody></table></div>`;
}

function curlFor(method, path, op, base) {
  const url = base + path.replace("{id}", "$TASK_ID") + (method === "get" && (op.parameters || []).some((p) => p.in === "query") ? "" : "");
  const ex = op.requestBody?.content?.["application/json"]?.example;
  const lines = [`curl -sS${method === "get" ? "" : ` -X ${method.toUpperCase()}`} "${url}" \\`, `  -H "Authorization: Bearer $RELAY_TOKEN"`];
  if (method !== "get") {
    lines[lines.length - 1] += " \\";
    lines.push(`  -H "Content-Type: application/json" \\`, `  -d '${JSON.stringify(ex || {})}'`);
  }
  return lines.join("\n");
}

const WORKFLOW = `# .github/workflows/relay.yml
# Label an issue "relay" and Relay's agents pick it up.
name: Queue issue in Relay
on:
  issues:
    types: [labeled]

jobs:
  queue:
    if: github.event.label.name == 'relay'
    runs-on: ubuntu-latest
    steps:
      - name: Create a Relay task for this issue
        env:
          RELAY_URL: \${{ vars.RELAY_URL }}         # e.g. https://relay.example.com
          RELAY_TOKEN: \${{ secrets.RELAY_TOKEN }}  # a token with the tasks:write scope
        run: |
          curl -sS --fail-with-body -X POST "$RELAY_URL/api/v1/issues/tasks" \\
            -H "Authorization: Bearer $RELAY_TOKEN" \\
            -H "Content-Type: application/json" \\
            -d '{"repo": "\${{ github.repository }}", "numbers": [\${{ github.event.issue.number }}], "mode": "parallel"}'
`;

const PY_VERIFY = `import hashlib, hmac, time

def verify(secret: str, headers, raw_body: bytes) -> bool:
    ts = headers.get("X-Relay-Timestamp", "")
    sig = headers.get("X-Relay-Signature", "")
    if not ts.isdigit() or abs(time.time() - int(ts)) > 300:
        return False  # missing or older than 5 minutes: possible replay
    mac = hmac.new(secret.encode(), ts.encode() + b"." + raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest("sha256=" + mac, sig)
`;

const NODE_VERIFY = `const crypto = require("crypto");

// rawBody must be the exact bytes received (e.g. express.raw({ type: "application/json" })).
function verify(secret, headers, rawBody) {
  const ts = headers["x-relay-timestamp"] || "";
  const sig = headers["x-relay-signature"] || "";
  if (!/^\\d+$/.test(ts) || Math.abs(Date.now() / 1000 - Number(ts)) > 300) return false;
  const mac = crypto.createHmac("sha256", secret).update(ts + "." ).update(rawBody).digest("hex");
  const expected = Buffer.from("sha256=" + mac), given = Buffer.from(sig);
  return expected.length === given.length && crypto.timingSafeEqual(expected, given);
}
`;

export function mountApiDocs(body) {
  let alive = true;
  body.innerHTML = skeleton("list", 6);
  const base = location.origin;

  (async () => {
    let spec;
    try { spec = await orgApi.openapi(); } catch (e) { if (alive) body.innerHTML = `<div class="empty">${icon("alert", "lg")}<p>${esc(e.message)}</p></div>`; return; }
    if (!alive) return;
    const ops = [];
    for (const [path, item] of Object.entries(spec.paths || {})) {
      for (const [method, op] of Object.entries(item)) ops.push({ path, method, op, tag: (op.tags || ["Other"])[0], id: slug(method, path) });
    }
    const tags = (spec.tags || []).map((t) => t.name).filter((t) => ops.some((o) => o.tag === t));
    for (const o of ops) if (!tags.includes(o.tag)) tags.push(o.tag);

    const opCard = ({ path, method, op, id }) => {
      const params = (op.parameters || []);
      const bodySchema = op.requestBody?.content?.["application/json"];
      const okCode = Object.keys(op.responses || {}).find((c) => c.startsWith("2")) || "200";
      const okSchema = op.responses?.[okCode]?.content?.["application/json"]?.schema;
      const canTry = method === "get" && !params.some((p) => p.in === "path");
      return `<article class="card org-endpoint" id="${id}">
        <div class="card-head"><span class="org-method ${method}">${method.toUpperCase()}</span><span class="org-endpoint-path">${esc(path)}</span>
          ${op["x-relay-scope"] ? `<span class="badge outline" title="Token scope needed">${xicon("key", "sm")}${esc(op["x-relay-scope"])}</span>` : ""}
          <span class="org-endpoint-summary">${esc(op.summary || "")}</span></div>
        <div class="card-body">
          ${op.description ? `<p class="hint" style="margin:0">${esc(op.description)}</p>` : ""}
          ${params.length ? `<div><h5>Parameters</h5><div class="org-table-wrap"><table class="org-params"><tbody>${params.map((p) => `<tr><td>${esc(p.name)}${p.required ? ' <span class="org-req">*</span>' : ""}</td><td>${esc(p.in)} · ${esc(p.schema?.type || "string")}</td><td>${esc(p.description || "")}</td></tr>`).join("")}</tbody></table></div></div>` : ""}
          ${bodySchema ? `<div><h5>Request body</h5>${fieldsTable(spec, bodySchema.schema)}${bodySchema.example ? `<div style="margin-top:8px">${code(JSON.stringify(bodySchema.example, null, 2), "json")}</div>` : ""}</div>` : ""}
          <div><h5>Response ${esc(okCode)}</h5>${okSchema ? fieldsTable(spec, okSchema) : '<span class="muted">JSON</span>'}</div>
          <div><h5>Example</h5>${code(curlFor(method, path, op, base), "shell")}</div>
          ${canTry ? `<div class="row wrap"><button class="btn sm" data-try="${esc(path)}">${icon("play")}Try it with your session</button><span class="muted" style="font-size:var(--fs-xs)">Runs as you, in this browser.</span></div><div data-out="${esc(path)}"></div>` : ""}
        </div></article>`;
    };

    body.innerHTML = `
      <div class="page-head"><div><h1>API docs</h1><p>${esc(spec.info?.title || "Relay API")} ${esc(spec.info?.version || "")}. A small, stable surface for scripts, CI and other tools. Fields may be added within v1, never removed or renamed.</p></div>
        <div class="page-actions"><a class="btn sm" href="/api/v1/openapi.json" target="_blank" rel="noopener">${icon("download")}OpenAPI JSON</a><a class="btn sm primary" href="#/org/tokens">${xicon("key")}Create a token</a></div></div>
      <div class="org-api">
        <nav class="org-api-toc" aria-label="Endpoints">
          <div class="group">Guides</div><a href="#" data-jump="api-auth">Authentication</a><a href="#" data-jump="api-actions">GitHub Actions</a><a href="#" data-jump="api-webhooks">Webhook signatures</a>
          ${tags.map((t) => `<div class="group">${esc(t)}</div>${ops.filter((o) => o.tag === t).map((o) => `<a href="#" data-jump="${o.id}"><span class="org-method ${o.method}">${o.method.toUpperCase()}</span><span class="truncate">${esc(o.path.replace("/api/v1", ""))}</span></a>`).join("")}`).join("")}
        </nav>
        <div class="stack" style="gap:14px;min-width:0">
          <section class="card org-api-section" id="api-auth"><div class="card-head"><h3>Authentication</h3></div><div class="card-body org-api-intro">
            <p class="hint">Create a personal access token under <a href="#/org/tokens">API tokens</a> and send it on every request. Tokens are shown once, stored only as a hash, can expire, and never do more than the person who created them.</p>
            ${code(`export RELAY_TOKEN=rly_...\ncurl -sS "${base}/api/v1/me" -H "Authorization: Bearer $RELAY_TOKEN"`, "shell")}
            <div class="org-kv"><div><dt>Base URL</dt><dd><span class="org-inline-code">${esc(base)}</span></dd></div><div><dt>Format</dt><dd>JSON; errors are {"error": "…"} with a 4xx status</dd></div></div>
            <div class="org-table-wrap"><table class="org-table"><thead><tr><th>Scope</th><th>Acts as at most</th><th>Allows</th></tr></thead><tbody>
              <tr><td><span class="badge outline">read</span></td><td>viewer</td><td>Projects, tasks, the queue and the digest.</td></tr>
              <tr><td><span class="badge outline">tasks:write</span></td><td>member</td><td>Also create, answer, start and stop tasks, start the queue, turn issues into tasks.</td></tr>
              <tr><td><span class="badge outline">admin</span></td><td>admin</td><td>Everything an admin can do through the API.</td></tr>
            </tbody></table></div>
            <div class="org-note">${icon("info", "sm")}<span>Behind a forward-auth proxy (for example Traefik with Authentik), let requests to <span class="org-inline-code">/api/v1/</span> through without the sign-in redirect so bearer tokens reach Relay. Relay still refuses them without a valid token.</span></div>
          </div></section>
          ${tags.map((t) => ops.filter((o) => o.tag === t).map(opCard).join("")).join("")}
          <section class="card org-api-section" id="api-actions"><div class="card-head"><div><h3>Queue a task from GitHub Actions</h3><p class="card-sub">Label an issue <span class="org-inline-code">relay</span> and a task is queued for it.</p></div></div><div class="card-body stack">
            <p class="hint" style="margin:0">Add a repository variable <span class="org-inline-code">RELAY_URL</span> and a secret <span class="org-inline-code">RELAY_TOKEN</span> (a token with the <b>tasks:write</b> scope). Relay must already have a checkout of the repository, or be able to clone it.</p>
            ${code(WORKFLOW, "yaml")}
          </div></section>
          <section class="card org-api-section" id="api-webhooks"><div class="card-head"><div><h3>Verify webhook signatures</h3><p class="card-sub">Webhooks Relay sends carry <span class="org-inline-code">X-Relay-Event</span>, <span class="org-inline-code">X-Relay-Delivery</span>, <span class="org-inline-code">X-Relay-Timestamp</span> and <span class="org-inline-code">X-Relay-Signature</span>.</p></div></div><div class="card-body stack">
            <p class="hint" style="margin:0">The signature is <span class="org-inline-code">sha256=</span> followed by the hex HMAC-SHA256 of <span class="org-inline-code">timestamp + "." + raw body</span>, keyed with the webhook's secret. Compare in constant time and reject timestamps older than five minutes.</p>
            <h5 style="margin:0">Python</h5>${code(PY_VERIFY, "python")}
            <h5 style="margin:0">Node.js</h5>${code(NODE_VERIFY, "javascript")}
          </div></section>
        </div>
      </div>`;
    bindCopy(body);
    $$("[data-jump]", body).forEach((a) => (a.onclick = (e) => { e.preventDefault(); document.getElementById(a.dataset.jump)?.scrollIntoView({ behavior: "smooth", block: "start" }); }));
    $$("[data-try]", body).forEach((b) => (b.onclick = async () => {
      const out = body.querySelector(`[data-out="${CSS.escape(b.dataset.try)}"]`);
      b.disabled = true;
      out.innerHTML = `<div class="muted" style="font-size:var(--fs-xs)">Calling ${esc(b.dataset.try)}…</div>`;
      const t0 = performance.now();
      try {
        const data = await request(b.dataset.try);
        out.innerHTML = `<div class="muted" style="font-size:var(--fs-xs);margin-bottom:4px">200 · ${Math.round(performance.now() - t0)} ms</div><pre class="org-code org-try-out">${esc(JSON.stringify(data, null, 2))}</pre>`;
      } catch (e) {
        out.innerHTML = `<div class="org-note warn">${icon("alert", "sm")}<span>${esc(e.status ? `${e.status}: ` : "")}${esc(e.message)}</span></div>`;
        toast("error", "Request failed", e.message);
      } finally { b.disabled = false; }
    }));
  })();

  return { update() {}, destroy() { alive = false; } };
}
