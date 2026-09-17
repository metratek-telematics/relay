"""Design gate: rules the orchestrator enforces on what the agents changed.

Guidance in DESIGN.md can be ignored; these checks cannot. They look only at lines
added since the task began, so existing code is never blamed, and they run as part of
verification, so a violation blocks the done decision like a failing test.

Usable from the command line so a worker can check itself before reporting:
    python3 <relay>/orchestrator/designcheck.py --base <commit> [--config file.json] [--forbid term ...]
"""
from __future__ import annotations

import fnmatch
import json
import re
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from orchestrator.util import quiet  # noqa: E402
else:
    from .util import quiet

STYLE_EXT = {".css", ".scss", ".sass", ".less"}
MARKUP_EXT = {".vue", ".svelte", ".html", ".htm", ".jsx", ".tsx", ".astro"}
SCRIPT_EXT = {".js", ".mjs", ".ts"}
UI_EXT = STYLE_EXT | MARKUP_EXT | SCRIPT_EXT

DEFAULT_CONFIG = {
    # Files that legitimately define raw colour values: the token layer and generated themes.
    "token_files": ["*token*.css", "*token*.scss", "*tokens*", "*theme-variables*", "*/themes/generated/*", "*tailwind.config*"],
    "fonts": ["Hanken Grotesk", "Hanken Grotesk Variable", "JetBrains Mono", "JetBrains Mono Variable"],
    "ignore": ["*.min.css", "*/vendor/*", "*/node_modules/*", "*/dist/*", "package-lock.json", "*.svg"],
    "warn_important": True,
    "warn_deep": True,
    "warn_backdrop": True,
}

# A CSS declaration whose value can carry a colour.
_COLOR_PROP = re.compile(r"(?:^|[\s;{\"'`])(?:color|background(?:-color)?|border(?:-\w+)*|outline(?:-color)?|fill|stroke|box-shadow|text-shadow|caret-color|accent-color|--[\w-]+)\s*:\s*([^;{}]*)", re.I)
_HEX = re.compile(r"(?<![\w&])#(?:[0-9a-fA-F]{3,4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})\b")
_FUNC = re.compile(r"\b(?:rgba?|hsla?|oklch|oklab|lab|lch|hwb)\(\s*(?!var\()", re.I)
_NAMED_BW = re.compile(r"(?<![\w-])(?:black|white)(?![\w-])", re.I)
_FONT = re.compile(r"font-family\s*:\s*([^;{}]+)", re.I)
_FONT_SHORTHAND = re.compile(r"(?<![\w-])font\s*:\s*([^;{}]+)", re.I)
_GENERIC_FONTS = {"inherit", "initial", "unset", "revert", "system-ui", "sans-serif", "serif", "monospace", "ui-monospace",
                  "ui-sans-serif", "-apple-system", "blinkmacsystemfont", "segoe ui", "roboto", "helvetica neue", "arial",
                  "menlo", "consolas", "sfmono-regular", "liberation mono", "courier new", "cursive", "fantasy", "emoji",
                  "apple color emoji", "segoe ui emoji"}


def _match(path: str, globs) -> bool:
    p = "/" + path
    return any(fnmatch.fnmatch(p, g if g.startswith("*") or g.startswith("/") else "*/" + g) or fnmatch.fnmatch(path, g) for g in globs)


def load_config(repo_root: Path, extra: dict | None = None) -> dict:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    local = repo_root / ".relay" / "design-checks.json"
    if local.is_file():
        try:
            data = json.loads(local.read_text(encoding="utf-8"))
            for k, v in data.items():
                if isinstance(v, list) and isinstance(cfg.get(k), list) and not k.endswith("!"):
                    cfg[k] = list(dict.fromkeys(cfg[k] + v))
                else:
                    cfg[k.rstrip("!")] = v
        except (OSError, ValueError):
            pass
    for k, v in (extra or {}).items():
        cfg[k] = v
    return cfg


def added_lines(wt: Path, base: str) -> dict[str, list[tuple[int, str]]]:
    """Lines added since `base`, committed or not, plus every line of new untracked files."""
    out: dict[str, list[tuple[int, str]]] = {}
    diff = quiet(["git", "diff", "-U0", "--no-color", "--no-ext-diff", base, "--"], cwd=wt, timeout=120).stdout or ""
    path, line_no = None, 0
    for raw in diff.splitlines():
        if raw.startswith("+++ "):
            name = raw[4:].strip()
            path = None if name == "/dev/null" else name[2:] if name.startswith("b/") else name
            continue
        if raw.startswith("@@"):
            m = re.search(r"\+(\d+)(?:,(\d+))?", raw)
            line_no = int(m.group(1)) if m else 0
            continue
        if path and raw.startswith("+") and not raw.startswith("+++"):
            out.setdefault(path, []).append((line_no, raw[1:]))
            line_no += 1
    for name in (quiet(["git", "ls-files", "--others", "--exclude-standard"], cwd=wt).stdout or "").splitlines():
        fp = wt / name
        if name.strip() and fp.is_file() and fp.stat().st_size < 2_000_000:
            try:
                out[name] = list(enumerate(fp.read_text(encoding="utf-8", errors="replace").splitlines(), 1))
            except OSError:
                pass
    return out


def _style_lines(fp: Path, ext: str) -> set[int] | None:
    """Line numbers inside <style> blocks of a markup file; None means the whole file is style."""
    if ext in STYLE_EXT:
        return None
    lines, inside = set(), False
    try:
        text = fp.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return set()
    for i, line in enumerate(text, 1):
        low = line.lower()
        if "<style" in low:
            inside = True
        if inside:
            lines.add(i)
        if "</style>" in low:
            inside = False
    return lines


def _font_ok(value: str, fonts) -> bool:
    if "var(" in value:
        return True
    allowed = {f.lower() for f in fonts} | _GENERIC_FONTS
    names = [n.strip().strip("'\"").lower() for n in value.split(",")]
    named = [n for n in names if n and n not in _GENERIC_FONTS]
    return all(n in allowed for n in named)


def check(wt, base: str, forbidden_terms=(), config: dict | None = None) -> dict:
    wt = Path(wt)
    cfg = load_config(wt, config)
    terms = [t.strip() for t in forbidden_terms if t and t.strip()]
    # Words of a term may be joined by nothing, spaces, punctuation or regex escapes, so "acme widgets"
    # also catches "AcmeWidgets", "acme_widgets" and a test written as /acme\\s*widgets/.
    sep = r"(?:[\s_\-.]|\\[sSwWbB][*+?]?)*"
    term_res = [(t, re.compile(sep.join(re.escape(w) for w in t.split()), re.I)) for t in terms]
    errors, warnings = [], []
    # A repository whose own files and folders are named after a term is an integration for it: its
    # modules and tests must carry the name, so the term is not enforced there (see _in_tree).
    established = [t for t, rx in term_res if base and _in_tree(wt, base, t, rx)]
    term_res = [(t, rx) for t, rx in term_res if t not in established]

    def hit(bucket, path, line, rule, message, text):
        bucket.append({"file": path, "line": line, "rule": rule, "message": message, "text": text.strip()[:160]})

    for path, lines in sorted(added_lines(wt, base).items()):
        if _match(path, cfg["ignore"]):
            continue
        # Forbidden terms apply to every file type: code, tests, docs, fixtures.
        for no, text in lines:
            for term, rx in term_res:
                if rx.search(text):
                    hit(errors, path, no, "forbidden-term", f"Forbidden term “{term[0]}…” must not appear in the repository", "(line hidden)")
        ext = Path(path).suffix.lower()
        if ext not in UI_EXT or _match(path, cfg["token_files"]):
            continue
        style = _style_lines(wt / path, ext)
        for no, text in lines:
            in_style = style is None or no in style
            code = re.sub(r"/\*.*?\*/", "", text)
            if not in_style:
                # Outside stylesheets only inline style declarations are checked (style="", :style, style objects).
                if not re.search(r"style|css|\b(?:color|background|border|fill|stroke)\s*:", code, re.I):
                    continue
            decls = [m.group(1) for m in _COLOR_PROP.finditer(code)]
            for value in decls:
                if "url(" in value:
                    continue
                if _HEX.search(value) or _FUNC.search(value) or _NAMED_BW.search(value):
                    hit(errors, path, no, "hardcoded-color", "Colour must come from a design token (var(--…)), not a literal value", text)
                    break
            for m in list(_FONT.finditer(code)):
                if not _font_ok(m.group(1), cfg["fonts"]):
                    hit(errors, path, no, "font-family", "Font must be a design-system font or a font token", text)
            for m in _FONT_SHORTHAND.finditer(code):
                # The family list follows the size in the shorthand: "600 14px/1.4 'Some Font', sans-serif".
                size = re.search(r"\d[\d.]*(?:px|rem|em|%|pt|vw|ch)(?:\s*/\s*[\d.]+\w*)?\s+", m.group(1))
                if size and not _font_ok(m.group(1)[size.end():], cfg["fonts"]):
                    hit(errors, path, no, "font-family", "Font shorthand must use a design-system font or a font token", text)
            if re.search(r"background-clip\s*:\s*text|-webkit-text-fill-color\s*:\s*transparent", code, re.I):
                hit(errors, path, no, "gradient-text", "Gradient or clipped-background text is not allowed", text)
            if cfg.get("warn_important") and "!important" in code:
                hit(warnings, path, no, "important", "!important override", text)
            if cfg.get("warn_deep") and re.search(r":deep\(|::v-deep|>>>", code):
                hit(warnings, path, no, "deep-override", "Deep selector override of a child component; restyle the component instead", text)
            if cfg.get("warn_backdrop") and "backdrop-filter" in code:
                hit(warnings, path, no, "glass", "backdrop-filter (glassmorphism) needs a reason", text)
    return {"ok": not errors, "errors": errors, "warnings": warnings, "established_terms": len(established)}


def _in_tree(wt: Path, base: str, term: str, rx) -> bool:
    """Whether the repository at `base` already names files or folders after `term`.

    That marks a codebase built around the term (an integration or client for it), where new modules and
    tests inevitably carry the name. A mention in content alone does not count, so an ordinary repo keeps
    the rule even if an old line slipped through.
    """
    names = (quiet(["git", "ls-tree", "-r", "--name-only", base], cwd=wt, timeout=60).stdout or "")
    return bool(rx.search(names))
