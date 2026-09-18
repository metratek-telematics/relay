"""Design exploration with an internal focus group, for UI and design work only.

When a task is design work (rules/DESIGN_RESEARCH.md applies, see Pipeline.needs_design_research) Relay does not
build the first idea. After the plan and its design research, and before any implementation:

    mockups   the worker, in a separate session, draws 2 or 3 genuinely different design DIRECTIONS as
              self-contained HTML/CSS mockups in <worktree>/.relay_mockups/<A|B|C>/ (git-excluded)
    render    Relay copies them to <run_dir>/mockups/ and renders each with the headless browser at desktop
              1440 and phone 420, light and dark (tools/render_mockups.cjs, time-bounded)
    panel     an internal focus group of personas (settings `design_personas`) scores every direction on a fixed
              rubric; one turn per persona ("full") or one turn simulating the panel ("lean"), on an agent other
              than the designer when there is one. Vision-capable agents get the screenshots.
    decide    scores are aggregated (weighted mean + variance); the winner, or a hybrid, is chosen WITHOUT asking.
              The owner is asked only when it is a real coin flip (top two within `design_close_margin`, materially
              different, and `design_ask_when_close` allows it), or when the task says "Show me mockups before
              building". A question times out after `design_pick_timeout_minutes` and the panel's pick is used.
    build     the worker's instructions point at the chosen mockup; acceptance gains "matches the chosen direction".

Everything lands in task meta `exploration` (compact, for the UI) and FOCUS_GROUP.md. The owner can switch
direction later with one click (Manager.choose_direction); overrides feed the outcome dataset so persona weights
can learn which voices match the owner's taste (persona_weights).

Pure functions come first (unit-tested without a pipeline); `ExplorationFlow` is the Pipeline mixin.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
import secrets
import shutil
import subprocess
import time
from pathlib import Path

from .util import APP_DIR, DATA_DIR, new_id, now, read_text, truncate, write_text

LETTERS = "ABC"
MOCKUP_DIR = ".relay_mockups"
SHOTS = (("desktop", 1440, 900), ("phone", 420, 900))
THEMES = ("light", "dark")
CRITERIA = (
    ("clarity", "Clarity and hierarchy"),
    ("efficiency", "Task efficiency"),
    ("accessibility", "Accessibility (WCAG 2.2 AA)"),
    ("consistency", "Consistency with the design system"),
    ("research_fit", "Fit with the research principles"),
    ("feasibility", "Feasibility in this codebase"),
)
CRITERION_KEYS = tuple(k for k, _ in CRITERIA)
CRITERION_LABEL = dict(CRITERIA)
DEFAULT_PERSONAS = [
    "First-time user: has never seen this product; judges whether the purpose, the main action and every label are obvious without help.",
    "Daily power user: works in {product} all day; judges scanning speed, information density, keyboard use and the fewest steps for frequent jobs.",
    "Accessibility auditor: WCAG 2.2 AA; contrast, 24x24 px targets, visible focus, keyboard order, screen-reader semantics, reflow at 320 px, reduced motion.",
    "Product owner and brand keeper: consistency with the repository's design system, DESIGN.md, tokens and voice, and fit with the request and the research.",
]
ALLOWED_EXT = {".html", ".htm", ".css", ".js", ".mjs", ".svg", ".png", ".jpg", ".jpeg", ".webp", ".gif", ".woff", ".woff2", ".ttf",
               ".otf", ".json", ".md", ".txt", ".ico"}
MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_DIR_BYTES = 30 * 1024 * 1024
SIMILAR_STRUCTURE = 0.80    # at or above: two mockups are variations of one layout
TRADEOFF_POINTS = 1.0       # a criterion lead that counts as a real trade-off


# ----------------------------------------------------------------------------- settings
def _num(v, default, lo=None, hi=None, cast=float):
    try:
        x = cast(v)
    except (TypeError, ValueError):
        x = default
    if isinstance(x, float) and (math.isnan(x) or math.isinf(x)):
        x = default
    if lo is not None:
        x = max(lo, x)
    if hi is not None:
        x = min(hi, x)
    return x


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(s or "").lower()).strip("-")[:40] or "persona"


def parse_personas(rows) -> list[dict]:
    """Settings rows ("Name: what they judge", or {name, lens}) as [{id, name, lens}], at most 6, unique names."""
    out, seen = [], set()
    for r in rows if isinstance(rows, list) else []:
        if isinstance(r, dict):
            name, lens = str(r.get("name") or "").strip(), str(r.get("lens") or r.get("description") or "").strip()
        else:
            text = str(r or "").strip()
            if not text:
                continue
            name, _, lens = text.partition(":")
            name, lens = name.strip(), lens.strip()
        if not name:
            continue
        name = name[:60]
        if name.lower() in seen:
            continue
        seen.add(name.lower())
        out.append({"id": slug(name), "name": name, "lens": lens[:400]})
        if len(out) >= 6:
            break
    return out


def ask_when_close(value, question_policy: str) -> bool:
    """`design_ask_when_close`: true/false, or "auto" = only when the question policy is not "blocked"."""
    if isinstance(value, bool):
        return value
    v = str(value or "auto").strip().lower()
    if v in ("on", "true", "yes", "1", "always"):
        return True
    if v in ("off", "false", "no", "0", "never"):
        return False
    return str(question_policy or "blocked").lower() != "blocked"


def settings(cfg: dict, workflow: dict | None = None) -> dict:
    cfg, wf = cfg or {}, workflow or {}
    mode = str(cfg.get("design_exploration") or "auto").lower()
    policy = str(wf.get("question_policy") or cfg.get("question_policy") or "blocked").lower()
    personas = parse_personas(cfg.get("design_personas")) or parse_personas(DEFAULT_PERSONAS)
    return {
        "mode": mode if mode in ("auto", "off") else "auto",
        "directions": int(_num(cfg.get("design_directions"), 3, 2, 3, int)),
        "personas": personas,
        "cost_mode": "lean" if str(cfg.get("design_focus_group_mode") or "full").lower() == "lean" else "full",
        "ask_when_close": ask_when_close(cfg.get("design_ask_when_close", "auto"), policy),
        "margin": _num(cfg.get("design_close_margin"), 0.5, 0.0, 5.0),
        "timeout_minutes": _num(cfg.get("design_pick_timeout_minutes"), 240, 0, 10080),
        "render_timeout": _num(cfg.get("design_render_timeout_seconds"), 180, 20, 900),
        "show_mockups": bool(wf.get("show_mockups")),
    }


def wanted(st: dict, is_design_work: bool) -> tuple[bool, str]:
    """Does this task explore directions before building? (trigger rules)"""
    if st.get("show_mockups"):
        return True, "the task asks to see mockups before building"
    if st.get("mode") == "off":
        return False, "design exploration is off in settings"
    if is_design_work:
        return True, "design work: a new design, redesign, layout or visual change"
    return False, "not design work"


def fill_personas(personas: list[dict], product: str) -> list[dict]:
    return [{**p, "lens": p["lens"].replace("{product}", product or "this product")} for p in personas]


# ----------------------------------------------------------------------------- reviews
def _score(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(x):
        return None
    if 0 < x <= 1 and not float(x).is_integer():   # a 0..1 scale slipped in
        x *= 10
    return round(max(1.0, min(10.0, x)), 2)


def _strs(v, n=6, width=300) -> list[str]:
    if isinstance(v, str):
        v = [v]
    return [truncate(str(x).strip(), width) for x in (v or []) if str(x).strip()][:n] if isinstance(v, list) else []


def _letter(v, letters) -> str:
    s = str(v or "").strip().upper()
    m = re.match(r"^(?:DIRECTION\s+)?([A-Z])\b", s)
    return m.group(1) if m and m.group(1) in letters else ""


def normalize_review(raw: dict, letters: str, persona: str = "") -> dict | None:
    """One persona's review: {persona, scores:{A:{criterion:score}}, notes:{A:{strengths,problems,verdict}}, preferred, borrow}."""
    if not isinstance(raw, dict):
        return None
    scores, notes = {}, {}
    rows = raw.get("reviews") or raw.get("directions") or []
    if isinstance(rows, dict):
        rows = [{"direction": k, **(v if isinstance(v, dict) else {})} for k, v in rows.items()]
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        d = _letter(row.get("direction") or row.get("id"), letters)
        if not d:
            continue
        sc = row.get("scores") if isinstance(row.get("scores"), dict) else {}
        vals = {k: _score(sc.get(k)) for k in CRITERION_KEYS}
        vals = {k: v for k, v in vals.items() if v is not None}
        if not vals and _score(row.get("score")) is not None:
            vals = {k: _score(row.get("score")) for k in CRITERION_KEYS}
        if not vals:
            continue
        scores[d] = vals
        notes[d] = {"strengths": _strs(row.get("strengths")), "problems": _strs(row.get("problems")),
                    "verdict": truncate(str(row.get("verdict") or "").strip(), 300)}
    if not scores:
        return None
    borrow = []
    for b in raw.get("borrow") or [] if isinstance(raw.get("borrow"), list) else []:
        if isinstance(b, dict):
            d, what = _letter(b.get("from"), letters), truncate(str(b.get("what") or "").strip(), 160)
            if d and what:
                borrow.append({"from": d, "what": what})
    preferred = _letter(raw.get("preferred"), letters)
    if not preferred:
        preferred = max(scores, key=lambda d: (overall(scores[d]), -ord(d)))
    return {"persona": truncate(str(raw.get("persona") or persona or "reviewer").strip(), 60), "scores": scores, "notes": notes,
            "preferred": preferred, "borrow": borrow[:4]}


def reviews_from_envelope(env: dict, letters: str, personas: list[dict]) -> list[dict]:
    """A focus_review envelope from one persona, or a "panel" list from the lean mode."""
    if not isinstance(env, dict):
        return []
    rows = env.get("panel") if isinstance(env.get("panel"), list) else [env]
    out = []
    for i, row in enumerate(rows):
        fallback = personas[i]["name"] if i < len(personas) else f"reviewer {i + 1}"
        r = normalize_review(row, letters, fallback if len(rows) > 1 or not personas else personas[0]["name"])
        if r:
            out.append(r)
    return out


def overall(scores: dict) -> float:
    vals = [v for v in (scores or {}).values() if isinstance(v, (int, float))]
    return round(sum(vals) / len(vals), 3) if vals else 0.0


def _wmean(pairs):
    tw = sum(w for _, w in pairs)
    return sum(v * w for v, w in pairs) / tw if tw else 0.0


def aggregate(reviews: list[dict], letters: str, weights: dict | None = None) -> dict:
    """Mean and variance per direction across personas, per-criterion means, ranking, winner, trade-offs, hybrid notes."""
    weights = weights or {}
    dirs = {}
    for d in letters:
        per = [(r["persona"], overall(r["scores"][d]), max(0.1, float(weights.get(r["persona"], 1.0)))) for r in reviews if d in r["scores"]]
        if not per:
            continue
        mean = _wmean([(v, w) for _, v, w in per])
        var = _wmean([((v - mean) ** 2, w) for _, v, w in per])
        crit = {}
        for k in CRITERION_KEYS:
            vals = [(r["scores"][d][k], max(0.1, float(weights.get(r["persona"], 1.0)))) for r in reviews if d in r["scores"] and k in r["scores"][d]]
            if vals:
                crit[k] = round(_wmean(vals), 2)
        dirs[d] = {"mean": round(mean, 2), "variance": round(var, 2), "n": len(per), "criteria": crit,
                   "by_persona": {p: round(v, 2) for p, v, _ in per},
                   "votes": sum(1 for r in reviews if r.get("preferred") == d),
                   "problems": sum(len((r.get("notes") or {}).get(d, {}).get("problems") or []) for r in reviews)}
    ranking = sorted(dirs, key=lambda d: (-dirs[d]["mean"], dirs[d]["variance"], -dirs[d]["votes"], dirs[d]["problems"], d))
    winner = ranking[0] if ranking else ""
    runner = ranking[1] if len(ranking) > 1 else ""
    margin = round(dirs[winner]["mean"] - dirs[runner]["mean"], 2) if runner else None
    tradeoffs = []
    if runner:
        for k in CRITERION_KEYS:
            a, b = dirs[winner]["criteria"].get(k), dirs[runner]["criteria"].get(k)
            if a is not None and b is not None and b - a >= TRADEOFF_POINTS:
                tradeoffs.append({"criterion": k, "leader": runner, "by": round(b - a, 2)})
    agg = {"directions": dirs, "ranking": ranking, "winner": winner, "runner_up": runner, "margin": margin, "tradeoffs": tradeoffs,
           "personas": [r["persona"] for r in reviews]}
    agg["hybrid"] = hybrid_notes(reviews, agg)
    return agg


def hybrid_notes(reviews: list[dict], agg: dict) -> list[dict]:
    """What to borrow into the winner: suggestions from the panel (most frequent first) and clear criterion leads."""
    winner = agg.get("winner")
    if not winner:
        return []
    counts: dict = {}
    for r in reviews:
        for b in r.get("borrow") or []:
            if b["from"] == winner:
                continue
            key = (b["from"], b["what"].strip().lower())
            row = counts.setdefault(key, {"from": b["from"], "what": b["what"], "votes": 0})
            row["votes"] += 1
    out = sorted(counts.values(), key=lambda x: (-x["votes"], x["from"], x["what"]))[:3]
    for t in agg.get("tradeoffs") or []:
        if len(out) >= 4:
            break
        what = CRITERION_LABEL[t["criterion"]].lower()
        if not any(o["from"] == t["leader"] and what in o["what"].lower() for o in out):
            out.append({"from": t["leader"], "what": f"its stronger {what} (+{t['by']:g})", "votes": 0, "auto": True})
    return out


# ----------------------------------------------------------------------------- close calls
def _features(html: str) -> set:
    """Layout fingerprint of a mockup: tag 3-grams in document order, class names and layout declarations."""
    html = re.sub(r"<!--.*?-->", " ", html or "", flags=re.S)
    body = re.sub(r"<(script|style)\b.*?</\1>", " ", html, flags=re.S | re.I)
    tags = [t.lower() for t in re.findall(r"<\s*([a-zA-Z][a-zA-Z0-9-]*)", body)]
    feats = {"t:" + "/".join(tags[i:i + 3]) for i in range(max(0, len(tags) - 2))}
    for cls in re.findall(r"class\s*=\s*[\"']([^\"']+)", body):
        feats.update("c:" + c for c in cls.split())
    css = " ".join(re.findall(r"<style\b[^>]*>(.*?)</style>", html, flags=re.S | re.I))
    for prop, val in re.findall(r"(display|grid-template-columns|grid-template-areas|flex-direction|position)\s*:\s*([^;}{]+)", css, flags=re.I):
        val = " ".join(val.strip().lower().split())[:60]
        feats.add(f"l:{prop.lower()}:{val}")
    return feats


def structural_similarity(html_a: str, html_b: str):
    """Jaccard similarity of two mockups' layout fingerprints (0 = unrelated, 1 = the same structure); None if unknown."""
    a, b = _features(html_a), _features(html_b)
    if not a or not b:
        return None
    return round(len(a & b) / len(a | b), 3)


def close_call(agg: dict, margin: float, similarity) -> dict:
    """A coin flip: the top two are within `margin` AND materially different (different structure, or a real trade-off)."""
    if not agg.get("runner_up") or agg.get("margin") is None:
        return {"close": False, "materially_different": False, "why": "only one direction was scored"}
    close = agg["margin"] <= margin + 1e-9
    tradeoff = bool(agg.get("tradeoffs"))
    if similarity is None:
        different = tradeoff
    else:
        different = similarity < SIMILAR_STRUCTURE or (similarity < 0.92 and tradeoff)
    w, r = agg["winner"], agg["runner_up"]
    why = (f"{w} leads {r} by {agg['margin']:g} (margin {margin:g})"
           + (f"; layout similarity {similarity:.2f}" if similarity is not None else "")
           + (f"; {r} is stronger on " + ", ".join(CRITERION_LABEL[t['criterion']].lower() for t in agg['tradeoffs']) if tradeoff else ""))
    return {"close": close, "materially_different": different, "why": why}


def decide(agg: dict, st: dict, cc: dict, can_ask: bool) -> dict:
    """Ask the owner or decide. Asking needs a real coin flip and the owner's opt-in, unless the task forces a look."""
    if st.get("show_mockups"):
        return {"ask": can_ask, "reason": "you asked to see mockups before building" if can_ask else
                "you asked to see mockups, but questions are off for this run (unattended or quiet hours); the panel decided"}
    if cc.get("close") and cc.get("materially_different"):
        if st.get("ask_when_close") and can_ask:
            return {"ask": True, "reason": "a close call between materially different directions: " + cc.get("why", "")}
        return {"ask": False, "reason": "a close call (" + cc.get("why", "") + "), decided by the panel because asking on close calls is off"}
    return {"ask": False, "reason": "the panel's choice is clear: " + cc.get("why", "")}


def parse_pick(text: str, extra: dict | None, letters: str, titles: dict | None = None) -> str:
    """The direction an owner's answer names: extra.direction, "B", "Use B", "direction b", or a title."""
    d = _letter((extra or {}).get("direction"), letters)
    if d:
        return d
    s = str(text or "").strip()
    m = re.search(r"\b(?:use|pick|choose|go with|direction|option)\s+([A-Ca-c])\b", s, re.I) or re.match(r"^\s*([A-Ca-c])\b", s)
    if m and m.group(1).upper() in letters:
        return m.group(1).upper()
    low = s.lower()
    for d, title in (titles or {}).items():
        if title and title.lower() in low and d in letters:
            return d
    return ""


# ----------------------------------------------------------------------------- learning
def persona_weights(records: list[dict], lo: float = 0.5, hi: float = 1.5, min_signal: float = 2.0) -> dict:
    """Weight personas by how often their preferred direction matched the owner's final choice.

    An explicit owner choice (asked, or an override) counts 1; silent acceptance of the panel's pick counts 0.25.
    Personas with too little signal keep weight 1.0."""
    tally: dict = {}
    for rec in records or []:
        ex = (rec or {}).get("exploration") or {}
        final = ex.get("final")
        prefs = ex.get("preferred") or {}
        if not final or not prefs:
            continue
        w = 1.0 if ex.get("owner_chose") else 0.25
        for persona, pick in prefs.items():
            t = tally.setdefault(persona, [0.0, 0.0])
            t[1] += w
            if pick == final:
                t[0] += w
    out = {}
    for persona, (hit, n) in tally.items():
        if n >= min_signal:
            out[persona] = round(lo + (hi - lo) * (hit / n), 2)
    return out


def outcome_record(task: dict) -> dict | None:
    """The exploration part of an outcome record (orchestrator/outcomes.py)."""
    ex = (task or {}).get("exploration") or {}
    if not ex.get("directions"):
        return None
    final = ex.get("chosen") or ""
    panel = ex.get("panel_pick") or ""
    return {
        "directions": len(ex.get("directions") or []),
        "panel_pick": panel, "final": final, "chosen_by": ex.get("chosen_by") or "",
        "owner_chose": ex.get("chosen_by") in ("owner", "owner_override"),
        "overridden": bool(ex.get("override")) or (bool(final) and bool(panel) and final != panel),
        "asked": bool(ex.get("asked")),
        "preferred": {p.get("persona"): p.get("preferred") for p in ex.get("reviews") or [] if p.get("persona")},
        "scores": {d.get("id"): d.get("mean") for d in ex.get("directions") or []},
        "persona_scores": {p.get("persona"): p.get("overall") or {} for p in ex.get("reviews") or []},
        "cost_usd": round(sum(float((c or {}).get("cost_usd") or 0) for c in (ex.get("cost") or {}).values()), 4),
    }


# ----------------------------------------------------------------------------- mockup files
def copy_mockups(src_root: Path, dst_root: Path, letters: str) -> dict:
    """Copy each direction folder (allowed web files, size-capped) from the worktree to the run folder."""
    found = {}
    for d in letters:
        src = Path(src_root) / d
        if not (src / "index.html").is_file():
            continue
        dst = Path(dst_root) / d
        shutil.rmtree(dst, ignore_errors=True)
        total = 0
        for f in sorted(src.rglob("*")):
            if not f.is_file() or f.is_symlink() or "shots" in f.relative_to(src).parts[:1]:
                continue
            if f.suffix.lower() not in ALLOWED_EXT:
                continue
            size = f.stat().st_size
            if size > MAX_FILE_BYTES or total + size > MAX_DIR_BYTES:
                continue
            total += size
            out = dst / f.relative_to(src)
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, out)
        if (dst / "index.html").is_file():
            found[d] = str(dst)
    return found


def valid_mockup(path: Path) -> str:
    """"" when the mockup looks usable, else why not."""
    p = Path(path) / "index.html"
    if not p.is_file():
        return "index.html is missing"
    text = read_text(p)
    if len(text) < 400:
        return "index.html is nearly empty"
    if "<" not in text or not re.search(r"<(body|main|div|section)\b", text, re.I):
        return "index.html has no page markup"
    return ""


def render(dirs: dict, timeout: float = 180.0, node: str = "node") -> dict:
    """Screenshots of each direction at desktop/phone × light/dark. Returns {letter: {shot_key: path}} (missing = failed)."""
    script = APP_DIR / "tools" / "render_mockups.cjs"
    if not dirs or not script.exists() or not shutil.which(node):
        return {}
    job = {"items": [{"id": d, "html": str(Path(p) / "index.html"), "out_dir": str(Path(p) / "shots")} for d, p in dirs.items()],
           "sizes": [{"name": n, "width": w, "height": h} for n, w, h in SHOTS], "themes": list(THEMES),
           "page_timeout_ms": 20000, "max_height": 2400}
    job_file = Path(next(iter(dirs.values()))).parent / "render_job.json"
    write_text(job_file, json.dumps(job))
    try:
        proc = subprocess.run([node, str(script), str(job_file)], capture_output=True, text=True, timeout=timeout)
        log = (proc.stdout or "") + (proc.stderr or "")
    except subprocess.TimeoutExpired as e:
        log = f"renderer timed out after {int(timeout)} s: {(e.stdout or '')[-500:] if isinstance(e.stdout, str) else ''}"
    except Exception as e:  # a missing browser must never stop the task
        log = f"renderer failed: {e}"
    write_text(job_file.parent / "render.log", log)
    out = {}
    for d, p in dirs.items():
        shots = {}
        for n, _, _ in SHOTS:
            for th in THEMES:
                f = Path(p) / "shots" / f"{n}-{th}.png"
                if f.is_file() and f.stat().st_size > 500:
                    shots[f"{n}-{th}"] = str(f)
        if shots:
            out[d] = shots
    return out


# ----------------------------------------------------------------------------- signed view links
def _view_key() -> bytes:
    p = DATA_DIR / "mockup_view.key"
    try:
        if p.exists():
            k = p.read_text(encoding="utf-8").strip()
            if len(k) >= 32:
                return k.encode()
        p.parent.mkdir(parents=True, exist_ok=True)
        k = secrets.token_hex(32)
        p.write_text(k, encoding="utf-8")
        try:
            p.chmod(0o600)
        except OSError:
            pass
        return k.encode()
    except OSError:
        return b"relay-mockup-view-fallback-key-" + str(DATA_DIR).encode()


def view_token(tid: str, hours: float = 12, at: float | None = None) -> str:
    """A short-lived capability for opening a task's live mockups in a sandboxed tab (the page cannot reach Relay)."""
    exp = int((at or time.time()) + hours * 3600)
    sig = hmac.new(_view_key(), f"{tid}.{exp}".encode(), hashlib.sha256).hexdigest()[:40]
    return f"{exp}.{sig}"


def check_view_token(tid: str, token: str, at: float | None = None) -> bool:
    try:
        exp_s, sig = str(token or "").split(".", 1)
        exp = int(exp_s)
    except ValueError:
        return False
    if exp < (at or time.time()):
        return False
    good = hmac.new(_view_key(), f"{tid}.{exp}".encode(), hashlib.sha256).hexdigest()[:40]
    return hmac.compare_digest(good, sig)


MOCKUP_CSP = ("sandbox allow-scripts; default-src 'none'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
              "font-src 'self' data: https://fonts.gstatic.com; img-src 'self' data: blob:; media-src 'self' data:; connect-src 'none'; "
              "form-action 'none'; frame-ancestors 'self'; base-uri 'none'")


# ----------------------------------------------------------------------------- prompts
def mockups_prompt(task: dict, plan: dict, n: int, wt, product: str, design_rules: str, guidance: str = "", fix: str = "") -> str:
    letters = LETTERS[:n]
    research = plan.get("design_research") or []
    rlines = "\n".join(f"- {r.get('principle', '')}: {r.get('applies_how', '')}" if isinstance(r, dict) else f"- {r}" for r in research[:12])
    root = Path(wt) / MOCKUP_DIR
    return f"""You are the DESIGNER in a multi-agent team run by Relay. Before anything is built, draw {n} genuinely different
design DIRECTIONS for this request as static HTML/CSS mockups. An internal focus group scores them and the team builds the winner.

TASK
{(task.get('requirements') or task.get('name') or '').strip()}

PRODUCT: {product}

PLAN
{truncate(str(plan.get('plan') or plan.get('summary') or ''), 3000)}

DESIGN RESEARCH (principles the team agreed to apply)
{rlines or '(none recorded)'}
{('ASSUMPTIONS' + chr(10) + chr(10).join('- ' + str(a) for a in (plan.get('assumptions') or [])[:10])) if plan.get('assumptions') else ''}

WHAT TO PRODUCE
- One folder per direction: {', '.join(f'{root}/{d}/index.html' for d in letters)}. Each folder is self-contained: copy the
  repository's real token CSS (colours, spacing, radii, type scale) and font files it needs into the folder and link them
  relatively; no build step, no framework CDN, no remote images. Inline SVG for icons is fine.
- Use the repository's real design system: read its tokens, components, DESIGN.md / FRONTEND rules and existing screens first,
  and reuse their class names, spacing and type scale where possible. Content must be realistic for this product (real-looking
  names, numbers, dates, long values), never lorem ipsum.
- Directions must differ in substance (layout model, information hierarchy, navigation, density or interaction model), not just
  colour. In each folder add notes.md: the idea in two sentences, what it optimises for, and its known weaknesses.
- Show every key state on the page (stack them as labelled sections if needed): default, empty, loading, error, long content,
  and a selected/hover/focus state.
- Light and dark: support `@media (prefers-color-scheme: dark)` AND `:root[data-theme="dark"]` / `[data-theme="light"]`
  (Relay screenshots both). Must work from 420 px to 1440 px wide with no horizontal scroll.
- Accessible markup: landmarks, headings in order, labelled controls, visible focus, contrast at WCAG 2.2 AA.
- Do NOT change any file of the repository outside {root}. This step only draws; implementation comes later.
- Look at your mockups with relay-screenshot if you have time (file://…/index.html); Relay renders them anyway.

{design_rules}
{('USER GUIDANCE' + chr(10) + guidance) if guidance else ''}
{fix}
End with exactly one fenced ```json envelope:
{{"type":"mockups","summary":"one line","directions":[{{"id":"A","title":"short name","idea":"one sentence","optimises_for":"…","differs_by":"how it differs from the others"}}]}}"""


def _direction_lines(directions: list[dict], images: bool) -> str:
    rows = []
    for d in directions:
        shots = d.get("shots") or {}
        rows.append(f"Direction {d['id']}: {d.get('title') or ''} · {d.get('idea') or ''}\n"
                    f"  HTML: {d.get('html')}" + (f"\n  notes: {d.get('notes_path')}" if d.get("notes_path") else "")
                    + ("\n  screenshots: " + ", ".join(f"{k} {v}" for k, v in shots.items()) if shots else "\n  screenshots: none (read the HTML)"))
    return "\n".join(rows)


RUBRIC = "\n".join(f"- {k}: {label}" for k, label in CRITERIA)


def review_prompt(task: dict, personas: list[dict], directions: list[dict], research: list, product: str, images: str) -> str:
    """One persona (full mode) or the whole panel (lean mode)."""
    lean = len(personas) > 1
    who = ("You simulate an internal FOCUS GROUP of these personas, each judging independently in their own voice:\n"
           + "\n".join(f"- {p['name']}: {p['lens']}" for p in personas)) if lean else \
        f"You are one member of an internal FOCUS GROUP. Your persona: {personas[0]['name']}. {personas[0]['lens']}\nJudge only through this lens."
    rlines = "\n".join(f"- {r.get('principle', '')}" if isinstance(r, dict) else f"- {r}" for r in (research or [])[:10])
    look = {"attached": "The screenshots are attached to this message (desktop 1440 and phone 420, light and dark); read the HTML for states they do not show.",
            "read": "Open the screenshot PNG files listed below with your file-reading tool (you can see images); read the HTML for states they do not show.",
            "html": "Read each direction's HTML (and its notes.md); picture it at 1440 px and 420 px wide in light and dark."}[images]
    letters = [d["id"] for d in directions]
    one = ('{"persona":"<name>","reviews":[' + ",".join(
        '{"direction":"%s","scores":{%s},"strengths":["…"],"problems":["…"],"verdict":"one line"}' % (d, ",".join(f'"{k}":7' for k in CRITERION_KEYS))
        for d in letters[:1]) + ', …one per direction],"preferred":"' + letters[0] + '","borrow":[{"from":"' + letters[-1] + '","what":"an element worth taking into your preferred direction"}]}')
    env = ('{"type":"focus_review","panel":[' + one + ", …one per persona]}") if lean else ('{"type":"focus_review",' + one[1:])
    return f"""{who}

PRODUCT: {product}
REQUEST: {truncate((task.get('requirements') or task.get('name') or '').strip(), 1500)}

RESEARCH PRINCIPLES THE DESIGN SHOULD FOLLOW
{rlines or '(none recorded)'}

DIRECTIONS
{_direction_lines(directions, images != 'html')}

{look}
Score EVERY direction from 1 (poor) to 10 (excellent) on each criterion:
{RUBRIC}
Be concrete and critical: name the element and the problem. Scores must discriminate; do not give every direction the same numbers.
Do not modify any file. Keep it short.

End with exactly one fenced ```json envelope:
{env}"""


def focus_group_md(ex: dict) -> str:
    dirs = ex.get("directions") or []
    lines = ["# Focus group", "", f"Chosen: **{ex.get('chosen') or '?'}** ({ex.get('chosen_by') or 'panel'}) · panel pick {ex.get('panel_pick') or '?'}", ""]
    if ex.get("rationale"):
        lines += [ex["rationale"], ""]
    if ex.get("hybrid"):
        lines += ["## Hybrid", ""] + [f"- Take {h['from']}'s {h['what']}" for h in ex["hybrid"]] + [""]
    lines += ["## Scores (mean ± variance across personas)", "", "| Direction | Mean | Variance | " + " | ".join(label for _, label in CRITERIA) + " |",
              "|---|---|---|" + "---|" * len(CRITERIA)]
    for d in dirs:
        crit = d.get("criteria") or {}
        lines.append(f"| {d['id']} {d.get('title', '')} | {d.get('mean', '')} | {d.get('variance', '')} | "
                     + " | ".join(str(crit.get(k, "")) for k in CRITERION_KEYS) + " |")
    lines += ["", "## Personas", ""]
    for r in ex.get("reviews") or []:
        lines.append(f"### {r.get('persona')} · prefers {r.get('preferred')}")
        for d, note in (r.get("notes") or {}).items():
            lines.append(f"- **{d}** ({(r.get('overall') or {}).get(d, '')}): {note.get('verdict', '')}"
                         + (f" Strengths: {'; '.join(note.get('strengths') or [])}." if note.get("strengths") else "")
                         + (f" Problems: {'; '.join(note.get('problems') or [])}." if note.get("problems") else ""))
        lines.append("")
    if ex.get("decision"):
        lines += ["## Decision", "", ex["decision"], ""]
    if ex.get("cost"):
        lines += ["## Cost", ""] + [f"- {k}: {v.get('input', 0)} input / {v.get('output', 0)} output tokens · ${v.get('cost_usd', 0):.4f}" for k, v in ex["cost"].items()]
    return "\n".join(lines) + "\n"


def direction_block(ex: dict, wt) -> str:
    """The build target for the worker, the supervisor and the reviewer."""
    d = ex.get("chosen")
    if not d:
        return ""
    row = next((x for x in ex.get("directions") or [] if x["id"] == d), {})
    base = Path(wt) / MOCKUP_DIR / d if wt else Path(MOCKUP_DIR) / d
    lines = [f"DESIGN DIRECTION · build to mockup {d}" + (f" “{row.get('title')}”" if row.get("title") else "")
             + f" (chosen by {'the owner' if ex.get('chosen_by', '').startswith('owner') else 'the internal focus group'})",
             f"- Target: {base}/index.html (notes: {base}/notes.md; screenshots: {base}/shots/desktop-light.png, phone-dark.png, …)"]
    if row.get("idea"):
        lines.append(f"- Idea: {row['idea']}")
    for h in ex.get("hybrid") or []:
        lines.append(f"- Hybrid: also take {h['from']}'s {h['what']} (see {Path(wt) / MOCKUP_DIR / h['from'] if wt else h['from']})")
    if ex.get("owner_note"):
        lines.append(f"- Owner note: {ex['owner_note']}")
    lines.append("- The mockup is the target for layout, information hierarchy, spacing rhythm, colour roles and states. Build it with the "
                 "repository's own tokens, components and fonts (where the repository's rules and the mockup disagree, the repository wins). "
                 f"Never copy {MOCKUP_DIR}/ into the repository or commit it. Compare your result with relay-screenshot against the mockup's shots.")
    return "\n".join(lines)


def acceptance_row(ex: dict) -> dict:
    d = ex.get("chosen")
    row = next((x for x in ex.get("directions") or [] if x["id"] == d), {})
    hyb = "; plus " + "; ".join(f"{h['from']}'s {h['what']}" for h in ex.get("hybrid") or []) if ex.get("hybrid") else ""
    return {"id": "D1", "criterion": f"The UI matches the chosen design direction {d}" + (f" ({row.get('title')})" if row.get("title") else "")
            + f": layout, hierarchy, colour roles and key states as in {MOCKUP_DIR}/{d}/index.html, built with the repository's tokens{hyb}",
            "how_to_verify": f"screenshot: relay-screenshot the implemented screen at 1440 and 420 wide, light and dark, and compare with {MOCKUP_DIR}/{d}/shots/*.png",
            "required": True}


# ----------------------------------------------------------------------------- pipeline mixin
class ExplorationFlow:
    """Exploration phase hooks for the Pipeline. Relies on run_role, r, m, state, cfg, task, wt, run_dir."""

    def exploration_settings(self) -> dict:
        return settings(self.cfg, self.task.get("workflow") or {})

    def exploration(self) -> dict:
        return dict(self.task_meta().get("exploration") or {})

    def save_exploration(self, ex: dict):
        self.m.set_meta(self.tid, exploration=ex)

    def exploration_due(self) -> bool:
        """Only once, before the first work package, and only for design work (or when the task asks for mockups)."""
        if self.state.get("exploration_done") or self.state.get("phase") != "dialogue":
            return False
        if int(self.state.get("turn") or 1) != 1 or self.state.get("awaiting") != "worker" or int((self.sessions.get("worker") or {}).get("turns") or 0):
            return False
        want, why = wanted(self.exploration_settings(), self.needs_design_research())
        if not want:
            return False
        self.state["exploration_reason"] = why
        return True

    def _spent(self) -> dict:
        tot = (self.task_meta().get("metrics") or {}).get("total") or {}
        return {"input": int(tot.get("input") or 0), "output": int(tot.get("output") or 0), "cost_usd": float(tot.get("cost_usd") or 0),
                "turns": int(tot.get("turns") or 0)}

    def _cost_since(self, before: dict) -> dict:
        after = self._spent()
        return {k: round(after[k] - before.get(k, 0), 5) if k == "cost_usd" else after[k] - before.get(k, 0) for k in after}

    def product_blurb(self) -> str:
        repo = self.task.get("repo") or ""
        name = Path(repo).name or "this product"
        try:
            from . import systemmap
            comp = systemmap.for_repo(systemmap.load(), repo)
            if comp and comp.get("description"):
                return f"{comp.get('name') or name}: {truncate(comp['description'], 200)}"
        except Exception:
            pass
        for readme in ("README.md", "readme.md", "README"):
            p = Path(self.wt or repo) / readme
            if p.is_file():
                for para in read_text(p).split("\n\n"):
                    para = para.strip()
                    if para and not para.startswith(("#", "!", "[", "<", "```")):
                        return f"{name}: {truncate(' '.join(para.split()), 200)}"
        return name

    def focus_reviewer(self) -> tuple[str, str]:
        """(role, agent) for the panel: an agent different from the designer (the worker's agent) when there is one."""
        designer = self.role_agent("worker")[0]
        for role in ("reviewer", "supervisor"):
            a = self.role_agent(role)[0]
            if a and a != designer:
                return role, a
        return "worker", designer

    # ------------------------------------------------------------ the phase
    def exploration_phase(self):
        st = self.exploration_settings()
        while self.state.get("phase") == "explore":
            self.r.check_stop()
            stage = self.state.get("explore_stage") or "mockups"
            if stage == "mockups":
                if not self._explore_mockups(st):
                    return self._explore_skip("fewer than two usable mockups")
                self.state["explore_stage"] = "render"
            elif stage == "render":
                self._explore_render(st)
                self.state["explore_stage"] = "panel"
            elif stage == "panel":
                if not self._explore_panel(st):
                    return self._explore_skip("the focus group returned no usable scores")
                self.state["explore_stage"] = "decide"
            elif stage == "decide":
                self._explore_decide(st)
                return
            self.save()

    def _explore_skip(self, why: str):
        ex = self.exploration()
        ex.update(status="skipped", skipped=why)
        self.save_exploration(ex)
        self.r.timeline("system", "Design exploration skipped", why)
        self.r.msg(role="orchestrator", agent=None, kind="notice", turn=0,
                   content=f"Design exploration skipped ({why}). The team builds from the plan and its design research.")
        self.state.update({"phase": "dialogue", "exploration_done": True})
        self.save()

    def _explore_mockups(self, st) -> bool:
        from . import environment, protocol
        n = st["directions"]
        letters = LETTERS[:n]
        root = Path(self.wt) / MOCKUP_DIR
        environment.exclude_from_git(Path(self.wt), MOCKUP_DIR + "/")
        root.mkdir(exist_ok=True)
        ex = {"status": "drawing", "reason": self.state.get("exploration_reason", ""), "started": now(), "directions": [], "cost": {},
              "settings": {"directions": n, "cost_mode": st["cost_mode"], "personas": [p["name"] for p in st["personas"]],
                           "ask_when_close": st["ask_when_close"], "margin": st["margin"], "show_mockups": st["show_mockups"]}}
        self.save_exploration(ex)
        self.r.status("planning", f"Exploring {n} design directions")
        self.r.timeline("system", "Design exploration", f"{n} directions · {ex['reason']}")
        self.handoff("orchestrator", "worker", f"Draw {n} design directions", self.state.get("exploration_reason", ""), subtype="exploration")
        design_rules = protocol.rules_block(["DESIGN", "FRONTEND"])
        before = self._spent()
        prompt = mockups_prompt(self.task, self.state.get("plan") or {}, n, self.wt, self.product_blurb(), design_rules, self.take_guidance("worker"))
        env = {}
        for attempt in range(2):
            try:
                _, env = self.run_role("worker", prompt, f"Designer is drawing {n} directions", 0, expect={"mockups", "question"}, session_key="designer")
            except Exception as e:
                from .runner import Interrupted, Stopped
                if isinstance(e, (Stopped, Interrupted)):
                    raise
                self.r.timeline("worker", "Mockup turn failed", truncate(str(e), 200))
                env = {}
            if env.get("type") == "question":
                prompt = protocol.human_answer(env.get("question") or "", "Decide yourself and state your assumptions in notes.md; then draw the directions.")
                continue
            bad = {d: valid_mockup(root / d) for d in letters}
            usable = [d for d in letters if not bad[d]]
            if len(usable) >= min(2, n):
                break
            prompt = ("ORCHESTRATOR · the mockups are not usable yet: " + "; ".join(f"{d}: {why}" for d, why in bad.items() if why)
                      + f". Write each direction to {root}/<letter>/index.html (self-contained) and reply with the mockups envelope.")
        ex["cost"]["mockups"] = self._cost_since(before)
        found = copy_mockups(root, self.run_dir / "mockups", letters)
        found = {d: p for d, p in found.items() if not valid_mockup(Path(p))}
        meta = {str(x.get("id") or "").strip().upper()[:1]: x for x in env.get("directions") or [] if isinstance(x, dict)}
        rows = []
        for d in letters:
            if d not in found:
                continue
            m = meta.get(d) or {}
            notes = Path(found[d]) / "notes.md"
            rows.append({"id": d, "title": truncate(str(m.get("title") or f"Direction {d}"), 60), "idea": truncate(str(m.get("idea") or ""), 300),
                         "optimises_for": truncate(str(m.get("optimises_for") or ""), 200), "differs_by": truncate(str(m.get("differs_by") or ""), 200),
                         "html": str(Path(found[d]) / "index.html"), "notes_path": str(notes) if notes.is_file() else "",
                         "notes": truncate(read_text(notes), 1200) if notes.is_file() else "", "shots": {}})
        ex.update(directions=rows, status="rendering" if len(rows) >= 2 else "failed")
        self.save_exploration(ex)
        wrk = self.role_agent("worker")[0]
        if len(rows) >= 2:
            self.r.msg(role="worker", agent=wrk, kind="notice", turn=0,
                       content=f"**{len(rows)} design directions drawn**\n" + "\n".join(f"- **{x['id']} · {x['title']}**: {x['idea']}" for x in rows))
        return len(rows) >= 2

    def _explore_render(self, st):
        ex = self.exploration()
        dirs = {x["id"]: str(Path(x["html"]).parent) for x in ex.get("directions") or []}
        self.r.status("planning", "Rendering the mockups (desktop and phone, light and dark)")
        t0 = time.time()
        shots = render(dirs, timeout=st["render_timeout"])
        for x in ex["directions"]:
            x["shots"] = {k: str(Path(v).relative_to(self.run_dir / "mockups")) for k, v in (shots.get(x["id"]) or {}).items()}
            # The worker compares its build with these later; they live next to the mockup in the worktree too.
            src = Path(dirs[x["id"]]) / "shots"
            if src.is_dir():
                dst = Path(self.wt) / MOCKUP_DIR / x["id"] / "shots"
                shutil.rmtree(dst, ignore_errors=True)
                shutil.copytree(src, dst)
        n = sum(len(x["shots"]) for x in ex["directions"])
        ex.update(status="reviewing", rendered=n, render_seconds=round(time.time() - t0, 1))
        self.save_exploration(ex)
        if n:
            self.r.timeline("system", "Mockups rendered", f"{n} screenshots in {ex['render_seconds']} s")
        else:
            log = read_text(self.run_dir / "mockups" / "render.log")[-300:]
            self.r.timeline("system", "Mockups not rendered", truncate(log or "no headless browser available; the panel reads the HTML", 240))

    def _explore_panel(self, st) -> bool:
        ex = self.exploration()
        role, agent = self.focus_reviewer()
        vision = agent in ("claude", "codex")
        dirs = []
        for x in ex["directions"]:
            shots = {k: str(self.run_dir / "mockups" / v) for k, v in (x.get("shots") or {}).items()}
            dirs.append({**x, "shots": shots})
        # Two shots per direction keep image tokens bounded: desktop in light, phone in dark.
        attach = [s for x in dirs for k, s in x["shots"].items() if k in ("desktop-light", "phone-dark")]
        images = "html" if not (vision and attach) else ("attached" if agent == "codex" else "read")
        product = self.product_blurb()
        personas = fill_personas(st["personas"], product)
        research = (self.state.get("plan") or {}).get("design_research") or []
        letters = "".join(x["id"] for x in dirs)
        groups = [personas] if st["cost_mode"] == "lean" else [[p] for p in personas]
        reviews = []
        before = self._spent()
        self.r.status("reviewing", f"Focus group: {len(personas)} personas score {len(dirs)} directions")
        self.handoff("orchestrator", role, "Focus group", ", ".join(p["name"] for p in personas), subtype="review_request")
        for i, group in enumerate(groups, 1):
            self.r.check_stop()
            label = f"Focus group · {group[0]['name']}" if len(group) == 1 else "Focus group · panel"
            prompt = review_prompt(self.task, group, dirs, research, product, images)
            try:
                _, env = self.run_role(role, prompt, label, 0, expect={"focus_review"}, session_key=f"focus_{group[0]['id']}" if len(group) == 1 else "focus_panel",
                                       images=attach if images == "attached" else None)
            except Exception as e:
                from .runner import Interrupted, Stopped
                if isinstance(e, (Stopped, Interrupted)):
                    raise
                self.r.timeline(role, f"{label} failed", truncate(str(e), 200))
                continue
            got = reviews_from_envelope(env, letters, group)
            for r in got:
                if len(group) == 1:
                    r["persona"] = group[0]["name"]
                r["overall"] = {d: overall(s) for d, s in r["scores"].items()}
                reviews.append(r)
            self.r.timeline(role, label, ", ".join(f"{r['persona']} prefers {r['preferred']}" for r in got) or "no usable scores")
        ex["cost"]["focus_group"] = self._cost_since(before)
        ex["reviews"] = reviews
        ex["panel"] = {"role": role, "agent": agent, "mode": st["cost_mode"], "images": images}
        self.save_exploration(ex)
        return bool(reviews)

    def _explore_decide(self, st):
        ex = self.exploration()
        letters = "".join(x["id"] for x in ex["directions"])
        weights = {}
        engine = getattr(getattr(self.m, "learning", None), "engine", None)
        try:
            if engine:
                weights = persona_weights(engine.store.all())
        except Exception:
            weights = {}
        agg = aggregate(ex.get("reviews") or [], letters, weights)
        winner, runner = agg["winner"], agg["runner_up"]
        html = {x["id"]: read_text(x["html"]) for x in ex["directions"]}
        sim = structural_similarity(html.get(winner, ""), html.get(runner, "")) if runner else None
        cc = close_call(agg, st["margin"], sim)
        ap = getattr(self.m, "autopilot", None)
        can_ask = self.allow_questions and not (ap and ap.quiet())
        dec = decide(agg, st, cc, can_ask)
        for x in ex["directions"]:
            a = agg["directions"].get(x["id"]) or {}
            x.update(mean=a.get("mean"), variance=a.get("variance"), criteria=a.get("criteria") or {}, votes=a.get("votes", 0),
                     rank=agg["ranking"].index(x["id"]) + 1 if x["id"] in agg["ranking"] else None)
        titles = {x["id"]: x.get("title") for x in ex["directions"]}
        rationale = (f"{winner} “{titles.get(winner)}” scored {agg['directions'][winner]['mean']:g} (variance {agg['directions'][winner]['variance']:g}, "
                     f"{agg['directions'][winner]['votes']} of {len(ex.get('reviews') or [])} personas prefer it)"
                     + (f"; {runner} “{titles.get(runner)}” {agg['directions'][runner]['mean']:g}" if runner else "") + ".")
        if agg["hybrid"]:
            rationale += " Hybrid: take " + "; ".join(f"{h['from']}'s {h['what']}" for h in agg["hybrid"]) + f" into {winner}."
        ex.update(panel_pick=winner, runner_up=runner, margin=agg["margin"], similarity=sim, close_call=cc, hybrid=agg["hybrid"],
                  rationale=rationale, weights=weights, decision=dec["reason"], status="deciding")
        self.save_exploration(ex)
        choice, by, note = winner, "panel", ""
        if dec["ask"]:
            choice, by, note = self._ask_pick(ex, st, dec["reason"])
            ex = self.exploration()
        self._apply_choice(ex, choice, by, note)

    def _ask_pick(self, ex: dict, st: dict, reason: str) -> tuple[str, str, str]:
        qid = new_id("q")
        winner = ex["panel_pick"]
        dirs = ex["directions"]
        options = [f"{x['id']} · {x.get('title', '')}" for x in dirs]
        minutes = float(st["timeout_minutes"] or 0)
        question = (f"**{len(dirs)} design directions explored.** The focus group prefers **{winner}** ({ex.get('rationale', '')}).\n\n"
                    f"Why you are asked: {reason}.\n\nPick one, or write what to change."
                    + (f" If nobody answers within {int(minutes)} minutes, Relay builds {winner}." if minutes else ""))
        qmsg = self.r.msg(role="orchestrator", agent="orchestrator", kind="question", qid=qid, content=question, options=options,
                          answered=False, turn=0, subject="design_pick")
        pending = {"id": qid, "kind": "design_pick", "from": "orchestrator", "agent": "orchestrator", "question": question, "options": options,
                   "message_id": qmsg["id"], "time": now(), "auto": options[[x["id"] for x in dirs].index(winner)],
                   "exploration": {"winner": winner, "reason": reason, "timeout_minutes": minutes,
                                   "directions": [{"id": x["id"], "title": x.get("title"), "idea": x.get("idea"), "mean": x.get("mean"),
                                                   "variance": x.get("variance"), "shot": (x.get("shots") or {}).get("desktop-light", "")} for x in dirs]}}
        ex.update(asked=True, status="waiting")
        self.save_exploration(ex)
        self.m.ask_user(self.tid, pending)
        self.r.status("needs_input", "Pick a design direction")
        self.m.notify("warning", "Pick a design direction", f"{self.task.get('name')} · the focus group prefers {winner}", self.tid, kind="needs_input")
        ans = self.r.wait_for_answer(qid, timeout=minutes * 60 if minutes > 0 else None)
        self.m.clear_pending(self.tid)
        if ans is None:
            self.r.msg_update(qmsg["id"], answered=True, answer=f"(no answer within {int(minutes)} minutes) {winner}")
            self.r.timeline("judge", "No answer · the panel's pick is used", winner)
            return winner, "panel_timeout", ""
        text = (ans.get("text") or "").strip()
        titles = {x["id"]: x.get("title") for x in dirs}
        pick = parse_pick(text, ans.get("extra") or {}, "".join(titles), titles) or winner
        note = re.sub(r"^\s*(?:use|pick|choose|go with)?\s*(?:direction\s*)?[A-C]\b[\s·:,.-]*", "", text, flags=re.I).strip() if text else ""
        if titles.get(pick) and note.lower().startswith(str(titles[pick]).lower()):
            note = note[len(titles[pick]):].strip(" ·:,.-")
        self.r.msg_update(qmsg["id"], answered=True, answer=text or f"Direction {pick}")
        if text:
            self.r.msg(role="user", agent=None, kind="user", content=text, to="orchestrator", reply_to=qid, turn=0)
        self.r.timeline("user", f"Design direction picked: {pick}", truncate(note, 200))
        return pick, "owner", note

    def _apply_choice(self, ex: dict, choice: str, by: str, note: str = ""):
        from . import judge
        ex.update(chosen=choice, chosen_by=by, owner_note=note, status="chosen", decided_at=now())
        if choice != ex.get("panel_pick"):
            ex["hybrid"] = [h for h in ex.get("hybrid") or [] if h["from"] != choice]
        self.save_exploration(ex)
        self.artifact("focus_group", "FOCUS_GROUP.md", focus_group_md(ex))
        crit = [c for c in self.criteria() if c.get("id") != "D1"]
        self.set_criteria(crit + judge.normalize_acceptance([acceptance_row(ex)], source="exploration"))
        titles = {x["id"]: x.get("title") for x in ex["directions"]}
        others = [x["id"] for x in ex["directions"] if x["id"] != choice]
        head = f"{len(ex['directions'])} directions explored, {choice} chosen"
        self.r.msg(role="orchestrator", agent=None, kind="notice", turn=0, subject="exploration",
                   content=f"**{head}** ({'by you' if by == 'owner' else 'by the focus group, without asking you'}): {titles.get(choice)}.\n\n"
                           f"{ex.get('rationale', '')}\n\n{ex.get('decision', '')}. Change it any time from the Mockups tab.")
        self.r.timeline("judge", head, truncate(ex.get("decision", ""), 200))
        if by != "owner":
            # Informational, never blocking: one click switches the running task to another direction.
            self.m.notify("info", head + " · change?", f"{self.task.get('name')} · {titles.get(choice)}", self.tid, kind="design_choice",
                          actions=[{"label": f"Use {d} instead", "direction": d} for d in others])
        self.state["judge_note"] = (self.state.get("judge_note", "") + "\n\n" if self.state.get("judge_note") else "") + \
            "ORCHESTRATOR · the team explored design directions before building.\n" + direction_block(ex, self.wt) + \
            "\nJudge the work against this direction (acceptance D1)."
        self.state.update({"phase": "dialogue", "exploration_done": True, "explore_stage": "done"})
        self.save()

    # ------------------------------------------------------------ prompts during implementation
    def context_text(self, role: str) -> str:
        base = super().context_text(role)
        ex = self.task_meta().get("exploration") or {}
        block = direction_block(ex, self.wt) if ex.get("chosen") else ""
        return "\n\n".join(p for p in (base, block) if p)
