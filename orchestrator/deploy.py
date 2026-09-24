"""Deploy recipes: what "deployed" means for a repository, as executable configuration.

A merged pull request only changes production if something runs afterwards, and what to run
differs per service: one repository's own Actions workflow deploys on merge, another needs a
`workflow_dispatch`, a third needs an image pulled and one compose service recreated on a host,
a fourth is built on the host from a checkout, and several cannot be automated at all.

    definition (DATA_DIR/deploy.json, owner-editable, seeded from the host and CI scans)
      hosts    {edge|z2: {ssh, note}}  - blank ssh means "nothing may run there yet"
      targets  [{id, repo, host, service|site, method, commands[], never_pull, critical,
                 risk, enabled, auto_on_merge, workflow, workflow_repo, branch, last_run}]

    methods (one function each, all returning the same record shape)
      actions_watch     the repository's own workflow already runs on merge: follow it, never trigger
      actions_dispatch  trigger a workflow_dispatch workflow, then follow it to completion
      image_pull        pull the image on the host and recreate that one compose service
      compose_build     build the image on the host from the checkout, then recreate the service
      manual            run nothing; report the steps a person must take and why

    rules that no task text, agent output or request body can bypass
      * commands come from this file only. Nothing is ever read from a task, an agent or a body.
      * every command is checked against an allow-list of programs before it runs.
      * a target the owner has not enabled never runs, automatically or from the button.
      * `never_pull` refuses image_pull outright: those images exist only on a host, and a pull
        would replace a running service with something older or break it.
      * `critical` never runs automatically - only from the button, and the record says so.
      * one deployment at a time across the whole instance (LOCK).

SSH: issue #54 adds an `ssh` connector. When it lands, set `deploy.SSH_RUNNER` to a callable
`(alias: str, command: str, timeout: int) -> (exit_code: int, output: str)` and this module will
use it. Until then it shells out to `ssh -o BatchMode=yes <alias> <command>` with the same
allow-list applied to the command, so the two can be reconciled without changing the recipes.
"""
from __future__ import annotations

import re
import shlex
import subprocess
import threading
import time
from pathlib import Path

from .util import DATA_DIR, RUNTIME_DIR, now, read_json, truncate, write_json, write_text

FILE = DATA_DIR / "deploy.json"
SCANS_DIR = DATA_DIR / "knowledge" / "deploy"
SCAN_FILES = ("edge-deploy.json", "z2-deploy.json", "ci-deploy.json")
ORG = "metratek-telematics"
OUTPUT_TAIL = 12_000
METHODS = ("actions_watch", "actions_dispatch", "image_pull", "compose_build", "manual")
HOSTS = ("edge", "z2", "github")

# One deployment at a time across the whole instance. Re-entrant so a merge hook can hold it
# while it runs several targets in order.
LOCK = threading.RLock()
_write_lock = threading.RLock()

# Only these programs may start a deploy command. Everything else is refused before anything runs.
ALLOWED_PROGRAMS = {"cd", "docker", "gh", "git", "echo", "true", "sleep", "gunzip", "gzip", "test"}
_FORBIDDEN = ("$(", "`", "<", ">")  # no substitution, no redirection, and no `<placeholder>` left in
SSH_RUNNER = None  # set by the ssh connector (#54); see the module docstring

# The flags a person may change through the API. Commands, method and risk are file-level
# configuration: the UI shows them, it does not post them back.
EDITABLE = ("enabled", "auto_on_merge")


class DeployError(RuntimeError):
    pass


# ============================================================================ definition
def _blank() -> dict:
    return {"version": 1, "seeded_at": None, "seeded_from": [],
            "hosts": {"edge": {"ssh": "", "note": "the edge host (docker-lxc, 192.168.100.100)"},
                      "z2": {"ssh": "", "note": "Z2 / DESFA production (10.100.0.2) - live gas terminal"}},
            "targets": []}


def load() -> dict:
    d = read_json(FILE, None)
    if not isinstance(d, dict) or not isinstance(d.get("targets"), list):
        return _blank()
    base = _blank()
    base.update({k: v for k, v in d.items() if k in base or k in ("seeded_at", "seeded_from")})
    return base


def save(doc: dict) -> dict:
    with _write_lock:
        write_json(FILE, doc)
    return doc


def list_targets() -> list[dict]:
    return list(load().get("targets") or [])


def get_target(tid: str):
    return next((t for t in list_targets() if t.get("id") == tid), None)


def hosts() -> dict:
    return load().get("hosts") or {}


def targets_for_repo(repo: str) -> list[dict]:
    """Every target of a repository, in file order (which is deployment order)."""
    name = repo_name(repo)
    return [t for t in list_targets() if repo_name(t.get("repo")) == name] if name else []


def auto_targets_for_repo(repo: str) -> list[dict]:
    """The targets a merge may run by itself: enabled, automatic, and not critical."""
    return [t for t in targets_for_repo(repo) if automatic_allowed(t)]


def automatic_allowed(t: dict) -> bool:
    return bool(t.get("enabled")) and bool(t.get("auto_on_merge")) and not t.get("critical")


def repo_name(repo) -> str:
    return str(repo or "").strip().rstrip("/").split("/")[-1].lower()


def full_repo(repo) -> str:
    repo = str(repo or "").strip()
    return repo if "/" in repo else (f"{ORG}/{repo}" if repo else "")


def set_flags(tid: str, changes: dict) -> dict:
    """Flip the owner's switches. Only `enabled` and `auto_on_merge` can be set this way."""
    with _write_lock:
        doc = load()
        for t in doc.get("targets") or []:
            if t.get("id") == tid:
                for k in EDITABLE:
                    if k in changes:
                        t[k] = bool(changes[k])
                save(doc)
                return t
    raise DeployError(f"No deploy target {tid}")


def record_run(tid: str, record: dict) -> None:
    with _write_lock:
        doc = load()
        for t in doc.get("targets") or []:
            if t.get("id") == tid:
                t["last_run"] = {k: record.get(k) for k in
                                 ("status", "exit_code", "trigger", "started_at", "finished_at", "duration", "task", "detail")}
                save(doc)
                return


# ============================================================================ seeding
_WF = re.compile(r"\b((?:deploy|apply)[a-z0-9._-]*\.yml)\b")
_GH_RUN = re.compile(r"gh workflow run\s+([A-Za-z0-9._-]+\.yml)\s+-R\s+([A-Za-z0-9._/-]+)")
_REPO = re.compile(r"metratek-telematics/([A-Za-z0-9._-]+)")
_LOCAL_IMAGE = ("local", "not on hub", "no repodigest", "never pushed", "never on any registry", "loaded")
_CRITICAL_WORDS = ("critical", "never recreate", "never restart", "drops every", "takes the whole stack",
                   "unrecoverable", "mid-operation", "mid-berthing", "scada", "berthing", "data loss")
# Services whose restart is felt outside a browser: lasers, berthing, the databases, the routers.
_CRITICAL_SERVICES = {"lidar-service", "operation-orchestrator", "dockassist", "dockassist-complete",
                      "driftscout", "dmon", "postgres", "postgrest", "postgrest-public", "traefik", "nginx",
                      "ais-decoder", "ais-decoder-rust", "mlms-recorder", "pegasus-display-panel",
                      "pager-service", "sms-sender", "ptz-vessel-tracker", "sam3-service", "nvr-proxy",
                      "authentik", "sof-worker"}
_NO_DEPLOY = ("nothing", "ci only", "no deployment", "would ")


def _txt(v) -> str:
    return " ".join(v) if isinstance(v, list) else str(v or "")


def _is_local_image(s: dict) -> bool:
    src = (str(s.get("image_source") or "") + " " + str(s.get("image") or "")).lower()
    return any(w in src for w in _LOCAL_IMAGE)


def _is_critical(host: str, method: str, service: str, risks: str) -> bool:
    # Z2 is a live gas terminal whose compose file is drifted and names an image that is not the one
    # running, so every compose operation there is dangerous by default.
    if host == "z2" and method in ("image_pull", "compose_build"):
        return True
    name = service.lower()
    if any(c == name or c in name.split() or name.startswith(c) for c in _CRITICAL_SERVICES):
        return True
    low = risks.lower()
    return any(w in low for w in _CRITICAL_WORDS)


def _tid(*parts) -> str:
    return re.sub(r"[^a-z0-9]+", "-", "-".join(str(p or "") for p in parts).lower()).strip("-")


def _target(**kw) -> dict:
    t = {"id": "", "repo": "", "host": "edge", "service": "", "site": "", "method": "manual",
         "commands": [], "never_pull": False, "critical": False, "risk": "", "note": "",
         "workflow": "", "workflow_repo": "", "branch": "", "compose_dir": "", "image": "",
         "enabled": False, "auto_on_merge": False, "last_run": None}
    t.update(kw)
    return t


def _runnable(cmd: str) -> bool:
    """A command the scan recorded as something a machine can run verbatim.

    The scans also carry prose ("(CI) staged swap…"), placeholders (`up -d <labelled-service>`)
    and programs Relay may not run (`cp`, `sudo systemctl`). None of those become a recipe: the
    target stays manual and prints them as the steps a person takes.
    """
    cmd = str(cmd or "").strip()
    if not cmd or cmd.startswith(("(", "#")) or "<" in cmd:
        return False
    try:
        check_command(cmd)
    except DeployError:
        return False
    return True


def _from_host_scan(doc: dict, host: str) -> list[dict]:
    out = []
    for s in doc.get("services") or []:
        service = str(s.get("service") or "")
        m = _REPO.search(str(s.get("repo") or ""))
        if not m:
            continue  # no repository means no merge can ever reach it
        src = str(s.get("image_source") or "").strip().lower()
        if src.startswith(("none", "n/a")):
            continue  # a docroot or a migration, not a container: its recipe lives in the CI scan
        repo = m.group(1)
        commands = [c for c in (s.get("commands") or []) if str(c).strip()]
        risks = _txt(s.get("risks"))
        # A target is only automatable when every command the scan recorded can be run verbatim.
        automatable = bool(commands) and all(_runnable(c) for c in commands)
        joined = " ".join(str(c) for c in commands)
        dispatch = _GH_RUN.search(joined)
        local = _is_local_image(s)
        if dispatch:
            method, workflow, wf_repo = "actions_dispatch", dispatch.group(1), dispatch.group(2)
        elif automatable and ("docker build" in joined or "--build" in joined):
            method, workflow, wf_repo = "compose_build", "", ""
        elif automatable and re.search(r"docker compose.*\bpull\b", joined):
            method, workflow, wf_repo = "image_pull", "", ""
        else:
            method, workflow, wf_repo = "manual", "", ""
        out.append(_target(
            id=_tid(host, service or repo), repo=repo, host=host, service=service, method=method,
            commands=commands, never_pull=local, workflow=workflow, workflow_repo=wf_repo,
            critical=_is_critical(host, method, service, risks), risk=risks,
            note=str(s.get("deploy") or ""), image=str(s.get("image") or "")))
    return out


def _from_ci_scan(doc: dict) -> list[dict]:
    out = []
    for repo, v in doc.items():
        if repo.startswith("_") or not isinstance(v, dict):
            continue
        on_merge = str(v.get("on_merge") or "")
        low = on_merge.lower()
        branch = str(v.get("default_branch") or "")
        if low and not any(w in low for w in _NO_DEPLOY):
            wf = _WF.search(on_merge)
            out.append(_target(
                id=_tid("ci", repo), repo=repo, host="github", method="actions_watch",
                workflow=wf.group(1) if wf else "", workflow_repo=full_repo(repo), branch=branch,
                site=on_merge, risk=str(v.get("notes") or ""), note=on_merge,
                critical=repo in ("ais-decoder", "dockassist-complete")))
        manual = str(v.get("manual_steps") or "")
        if "dispatch" in manual.lower():
            wf_repo = full_repo("navitrak-vue") if "navitrak-vue" in manual else full_repo(repo)
            for wf in dict.fromkeys(_WF.findall(manual)):
                host = "z2" if wf.endswith("-z2.yml") or "z2" in wf else "edge"
                out.append(_target(
                    id=_tid("dispatch", repo, wf.replace(".yml", "")), repo=repo, host=host,
                    method="actions_dispatch", workflow=wf, workflow_repo=wf_repo, branch=branch,
                    note=manual, risk=str(v.get("notes") or ""),
                    critical=host == "z2" and "sof" not in repo))
    return out


def seed_from_scans(scan_dir=None, force: bool = False) -> dict:
    """Build the deployment map from the three read-only scans. Run once; `force` rebuilds.

    Everything it produces is disabled and non-automatic. Enabling is the owner's decision,
    taken in the UI, one target at a time.
    """
    scan_dir = Path(scan_dir or SCANS_DIR)
    doc = load()
    if doc.get("targets") and not force:
        raise DeployError("The deployment map already exists; pass force to rebuild it.")
    missing = [f for f in SCAN_FILES if not (scan_dir / f).is_file()]
    if missing:
        raise DeployError(f"Scan files missing from {scan_dir}: {', '.join(missing)}")
    targets, seen = [], set()
    for name, host in (("edge-deploy.json", "edge"), ("z2-deploy.json", "z2")):
        targets += _from_host_scan(read_json(scan_dir / name, {}) or {}, host)
    targets += _from_ci_scan(read_json(scan_dir / "ci-deploy.json", {}) or {})
    uniq = []
    for t in targets:
        # The same workflow reached from two scans is one target; two services on one host are two.
        key = ((repo_name(t["repo"]), t["method"], t["workflow"], t["host"])
               if t["method"].startswith("actions_") else (t["host"], t["service"]))
        if key in seen:
            continue
        seen.add(key)
        # keep an owner's switches across a rebuild
        old = next((o for o in (doc.get("targets") or []) if o.get("id") == t["id"]), None)
        if old:
            t["enabled"], t["auto_on_merge"], t["last_run"] = bool(old.get("enabled")), bool(old.get("auto_on_merge")), old.get("last_run")
        uniq.append(t)
    doc["targets"] = uniq
    doc["seeded_at"] = now()
    doc["seeded_from"] = [str(scan_dir / f) for f in SCAN_FILES]
    save(doc)
    return {"targets": len(uniq), "file": str(FILE)}


# ============================================================================ command safety
def check_command(cmd: str) -> None:
    """Refuse anything that is not a plain chain of allow-listed programs."""
    cmd = str(cmd or "").strip()
    if not cmd:
        raise DeployError("Empty command")
    for bad in _FORBIDDEN:
        if bad in cmd:
            raise DeployError(f"Command substitution is not allowed in a deploy command: {bad}")
    for segment in re.split(r"&&|\|\||\||;", cmd):
        segment = segment.strip()
        if not segment or segment.startswith("#"):
            continue
        try:
            head = (shlex.split(segment) or [""])[0]
        except ValueError as exc:
            raise DeployError(f"Could not parse the command: {exc}")
        program = Path(head).name
        if program not in ALLOWED_PROGRAMS:
            raise DeployError(f"'{program}' is not an allowed deploy program")


def _run(argv: list[str], timeout: int) -> tuple[int, str]:
    try:
        p = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout)
        out = (p.stdout or "") + ("\n" if p.stdout and p.stderr else "") + (p.stderr or "")
        return p.returncode, out
    except subprocess.TimeoutExpired:
        return -1, f"Timed out after {timeout} seconds."
    except Exception as exc:
        return -1, str(exc)


def run_on_host(host: str, command: str, timeout: int = 900) -> tuple[int, str]:
    """Run one allow-listed command on a host over SSH.

    There is no local fallback on purpose: a host with no SSH alias configured deploys nothing,
    so a fresh installation cannot touch anybody's production by accident.
    """
    check_command(command)
    alias = str((hosts().get(host) or {}).get("ssh") or "").strip()
    if not alias:
        raise DeployError(f"No SSH host is configured for '{host}'. Nothing was run.")
    if SSH_RUNNER is not None:  # the ssh connector from #54, once it exists
        return SSH_RUNNER(alias, command, timeout)
    return _run(["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new", alias, command], timeout)


# ============================================================================ recipes
def _record(target: dict, **kw) -> dict:
    r = {"target": target.get("id"), "repo": target.get("repo"), "host": target.get("host"),
         "method": target.get("method"), "service": target.get("service") or target.get("site") or "",
         "status": "failed", "exit_code": None, "output": "", "log_path": None, "duration": 0.0,
         "started_at": now(), "finished_at": None, "trigger": "manual", "detail": "", "commands": []}
    r.update(kw)
    return r


def _finish(record: dict, started: float, output: str, log_dir=None) -> dict:
    record["duration"] = round(time.time() - started, 2)
    record["finished_at"] = now()
    record["output"] = truncate(output, OUTPUT_TAIL, tail=True)
    if log_dir:
        path = Path(log_dir) / f"deploy-{record['target']}.log"
        write_text(path, output)
        record["log_path"] = str(path)
    return record


def _gh(args: list[str], timeout: int) -> tuple[int, str]:
    return _run(["gh", *args], timeout)


def _latest_run(repo: str, workflow: str, branch: str, timeout: int = 120):
    args = ["run", "list", "-R", repo, "--limit", "1", "--json", "databaseId,status,conclusion,url,headSha"]
    if workflow:
        args += ["--workflow", workflow]
    if branch:
        args += ["--branch", branch]
    code, out = _gh(args, timeout)
    if code != 0:
        return None, out
    try:
        import json as _json
        runs = _json.loads(out or "[]")
    except Exception:
        return None, out
    return (runs[0] if runs else None), out


def recipe_actions_watch(target: dict, ctx: dict) -> dict:
    """The repository deploys itself on merge. Follow that run; never start another one."""
    started = time.time()
    rec = _record(target, trigger=ctx.get("trigger", "manual"))
    repo = full_repo(target.get("workflow_repo") or target.get("repo"))
    run, out = _latest_run(repo, target.get("workflow") or "", target.get("branch") or "", ctx.get("timeout", 120))
    log = [f"$ gh run list -R {repo} --workflow {target.get('workflow') or '(any)'}", out]
    if not run:
        rec["status"] = "failed"
        rec["detail"] = "No workflow run found to follow."
        return _finish(rec, started, "\n".join(log), ctx.get("log_dir"))
    rid = str(run.get("databaseId"))
    code, watch = _gh(["run", "watch", rid, "-R", repo, "--exit-status"], ctx.get("timeout", 1800))
    log += [f"$ gh run watch {rid} -R {repo} --exit-status", watch]
    _, view = _gh(["run", "view", rid, "-R", repo], 120)
    log += [f"$ gh run view {rid} -R {repo}", view]
    rec["exit_code"] = code
    rec["status"] = "succeeded" if code == 0 else "failed"
    rec["detail"] = f"run {rid} · {run.get('url') or ''}"
    rec["commands"] = [f"gh run watch {rid} -R {repo}"]
    return _finish(rec, started, "\n".join(log), ctx.get("log_dir"))


def recipe_actions_dispatch(target: dict, ctx: dict) -> dict:
    """Trigger a workflow_dispatch workflow, then follow it to completion."""
    started = time.time()
    rec = _record(target, trigger=ctx.get("trigger", "manual"))
    repo = full_repo(target.get("workflow_repo") or target.get("repo"))
    workflow = str(target.get("workflow") or "").strip()
    if not workflow:
        rec["status"] = "failed"
        rec["detail"] = "No workflow file is recorded for this target."
        return _finish(rec, started, rec["detail"], ctx.get("log_dir"))
    before, _ = _latest_run(repo, workflow, target.get("branch") or "", 120)
    before_id = str((before or {}).get("databaseId") or "")
    args = ["workflow", "run", workflow, "-R", repo]
    if target.get("branch"):
        args += ["--ref", target["branch"]]
    code, out = _gh(args, ctx.get("timeout", 120))
    log = [f"$ gh workflow run {workflow} -R {repo}", out]
    rec["commands"] = [" ".join(["gh", *args])]
    if code != 0:
        rec["exit_code"] = code
        rec["detail"] = "The dispatch was refused."
        return _finish(rec, started, "\n".join(log), ctx.get("log_dir"))
    run, listing = None, ""
    for _ in range(int(ctx.get("dispatch_polls", 12))):
        run, listing = _latest_run(repo, workflow, target.get("branch") or "", 120)
        if run and str(run.get("databaseId")) != before_id:
            break
        time.sleep(ctx.get("dispatch_wait", 5))
    if not run or str(run.get("databaseId")) == before_id:
        rec["status"] = "failed"
        rec["detail"] = "The dispatch was accepted but no new run appeared."
        return _finish(rec, started, "\n".join(log + [listing]), ctx.get("log_dir"))
    rid = str(run.get("databaseId"))
    wcode, watch = _gh(["run", "watch", rid, "-R", repo, "--exit-status"], ctx.get("timeout", 1800))
    log += [f"$ gh run watch {rid} -R {repo} --exit-status", watch]
    rec["exit_code"] = wcode
    rec["status"] = "succeeded" if wcode == 0 else "failed"
    rec["detail"] = f"run {rid} · {run.get('url') or ''}"
    return _finish(rec, started, "\n".join(log), ctx.get("log_dir"))


def recipe_image_pull(target: dict, ctx: dict) -> dict:
    """Pull the image on the host and recreate that one compose service.

    Refused outright when the image is marked `never_pull`: those images were built on the host
    or loaded from a tar and do not exist on Docker Hub, so a pull either fails or silently
    replaces the running service with something older.
    """
    started = time.time()
    rec = _record(target, trigger=ctx.get("trigger", "manual"))
    if target.get("never_pull"):
        rec["status"] = "refused"
        rec["detail"] = (f"{target.get('image') or target.get('service')} is not on Docker Hub "
                         "(built on the host or loaded from a tar). Pulling it would replace the "
                         "running service with something older or break it.")
        return _finish(rec, started, rec["detail"], ctx.get("log_dir"))
    return _run_host_commands(target, ctx, rec, started)


def recipe_compose_build(target: dict, ctx: dict) -> dict:
    """Build the image on the host from the checkout, then recreate the service."""
    started = time.time()
    rec = _record(target, trigger=ctx.get("trigger", "manual"))
    return _run_host_commands(target, ctx, rec, started)


def _run_host_commands(target: dict, ctx: dict, rec: dict, started: float) -> dict:
    commands = [str(c) for c in (target.get("commands") or []) if str(c).strip() and not str(c).strip().startswith("#")]
    if not commands:
        rec["status"] = "manual"
        rec["detail"] = "No commands are recorded for this target; a person must do it."
        return _finish(rec, started, rec["detail"], ctx.get("log_dir"))
    log: list[str] = []
    rec["commands"] = commands
    for cmd in commands:
        try:
            check_command(cmd)
        except DeployError as exc:
            rec["status"] = "refused"
            rec["detail"] = str(exc)
            log.append(f"refused: {cmd}\n{exc}")
            return _finish(rec, started, "\n".join(log), ctx.get("log_dir"))
    for cmd in commands:
        log.append(f"$ [{target.get('host')}] {cmd}")
        try:
            code, out = run_on_host(target.get("host") or "edge", cmd, ctx.get("timeout", 900))
        except DeployError as exc:
            rec["status"] = "blocked"
            rec["detail"] = str(exc)
            log.append(str(exc))
            return _finish(rec, started, "\n".join(log), ctx.get("log_dir"))
        log.append(out)
        rec["exit_code"] = code
        if code != 0:
            rec["status"] = "failed"
            rec["detail"] = f"{cmd} exited {code}"
            return _finish(rec, started, "\n".join(log), ctx.get("log_dir"))
    rec["status"] = "succeeded"
    rec["detail"] = f"{len(commands)} command(s) on {target.get('host')}"
    return _finish(rec, started, "\n".join(log), ctx.get("log_dir"))


def recipe_manual(target: dict, ctx: dict) -> dict:
    """Run nothing. Say what a person must do, and why it is not automated."""
    started = time.time()
    rec = _record(target, trigger=ctx.get("trigger", "manual"), status="manual", exit_code=None)
    steps = [str(c) for c in (target.get("commands") or []) if str(c).strip()]
    why = target.get("risk") or target.get("note") or "This target has no safe automatic recipe."
    rec["detail"] = "Manual: " + (steps[0] if steps else (target.get("note") or "see the steps"))
    body = ["This target is not automated. A person must run:"]
    body += [f"  {i + 1}. {s}" for i, s in enumerate(steps)] or ["  (the scan records no command)"]
    body += ["", "Why it is not automated:", f"  {why}"]
    return _finish(rec, started, "\n".join(body), ctx.get("log_dir"))


RECIPES = {"actions_watch": recipe_actions_watch, "actions_dispatch": recipe_actions_dispatch,
           "image_pull": recipe_image_pull, "compose_build": recipe_compose_build, "manual": recipe_manual}


# ============================================================================ execution
def log_dir_for(task) -> Path:
    tid = (task or {}).get("id") if isinstance(task, dict) else task
    return RUNTIME_DIR / str(tid or "deploy")


def run_target(target: dict, *, trigger: str = "manual", task=None, automatic: bool = False, ctx=None) -> dict:
    """Run one target under the rules. The only entry point; recipes are not called directly.

    `automatic` marks a run the merge hook started: a target that is not enabled, not marked
    automatic, or critical is refused there and only ever runs from the button.
    """
    if not isinstance(target, dict) or target.get("method") not in METHODS:
        raise DeployError("Unknown deploy target")
    started = time.time()
    context = {"trigger": trigger, "log_dir": log_dir_for(task)}
    context.update(ctx or {})
    if not target.get("enabled"):
        rec = _record(target, trigger=trigger, status="skipped",
                      detail="This target is disabled. An owner enables it in Settings → Deploy.")
        return _finish(rec, started, rec["detail"], context.get("log_dir"))
    if automatic and not automatic_allowed(target):
        reason = ("This target is critical: it is never deployed automatically, only from the Run now "
                  "button, by a person who knows what is running." if target.get("critical")
                  else "This target is not set to deploy automatically on merge.")
        rec = _record(target, trigger=trigger, status="skipped", detail=reason)
        return _finish(rec, started, reason, context.get("log_dir"))
    with LOCK:  # one deployment at a time across the whole instance
        rec = RECIPES[target["method"]](target, context)
    rec["trigger"] = trigger
    rec["task"] = (task or {}).get("id") if isinstance(task, dict) else task
    rec["critical"] = bool(target.get("critical"))
    record_run(target["id"], rec)
    return rec


def run_repo_targets(repo: str, *, trigger: str = "pr_merged", task=None, automatic: bool = True) -> dict:
    """Every target of a repository, in order, under one lock. The merge hook calls this."""
    targets = targets_for_repo(repo)
    records = []
    with LOCK:
        for t in targets:
            if automatic and not automatic_allowed(t):
                continue
            records.append(run_target(t, trigger=trigger, task=task, automatic=automatic))
    status = "succeeded"
    if any(r["status"] == "failed" for r in records):
        status = "failed"
    elif any(r["status"] in ("refused", "blocked") for r in records):
        status = "blocked"
    elif not records:
        status = "none"
    elif all(r["status"] == "manual" for r in records):
        status = "manual"
    return {"repo": repo, "status": status, "trigger": trigger, "started_at": records[0]["started_at"] if records else now(),
            "finished_at": now(), "targets": records}


def public(t: dict) -> dict:
    """What the UI gets: the whole target. There are no secrets in it, only commands and flags."""
    return dict(t)
