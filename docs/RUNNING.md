# Running Relay

Relay runs three ways. All three drive the same agent CLIs with the same accounts.

| Mode | Best for | Agent CLIs run |
|---|---|---|
| [Windows](#windows) | Your own workstation | On the host |
| [Ubuntu, native](#ubuntu-native) | A server or Linux workstation | On the host |
| [Docker](#docker) | One reproducible container on an Ubuntu host | In the container, using the host's logins, settings and repositories |

Whichever you pick, **run only one Relay per data folder**. Two instances sharing the same task
list overwrite each other; Relay refuses to start a second copy on the same port for this reason.

## Windows

### Install and sign in once

```powershell
winget install Python.Python.3.12 Git.Git GitHub.cli   # if missing
npm install -g @openai/codex @anthropic-ai/claude-code @google/gemini-cli
codex          # sign in with ChatGPT, then exit
claude         # /login, then exit
gemini         # choose Google login (Workspace accounts also need $env:GOOGLE_CLOUD_PROJECT)
gh auth login  # optional: issue intake and draft pull requests
```

### Start

Double-click `run.bat`, or run `python web_app.py`, then open <http://127.0.0.1:8767>.

## Ubuntu, native

This is the simplest way to use your host's CLIs on Linux: Relay just launches the ones you installed.

### Install and sign in once

```bash
sudo apt update
sudo apt install -y python3 python3-venv git curl
# Node 20 or newer, for the agent CLIs
curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
sudo apt install -y nodejs
sudo npm install -g @openai/codex @anthropic-ai/claude-code @google/gemini-cli
sudo apt install -y gh          # optional, see https://cli.github.com for the apt source

codex            # sign in, then exit
claude           # /login, then exit
gemini           # sign in, then exit
gh auth login    # optional
```

Sign in as the same Linux user that will run Relay. The logins live in that user's home directory.

On a headless server with no browser, sign-in prints a URL or a device code. Open it on your own computer to finish.

### Start

```bash
git clone https://github.com/metratek-telematics/relay.git ~/relay
cd ~/relay
./run.sh
```

`run.sh` creates a virtual environment on first run, checks for Git and the agent CLIs, and starts Relay on
`127.0.0.1:8767`. If it reports a missing venv module, install `python3-venv`.

### Keep it running with systemd

```bash
mkdir -p ~/.config/systemd/user
cp deploy/relay.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now relay
sudo loginctl enable-linger "$USER"      # keep running after you log out
journalctl --user -u relay -f            # follow the logs
```

The unit runs as your user, so agents keep using your logins. Stopping the service stops every agent process with
it. If your CLIs are installed somewhere other than the usual npm locations, edit the `PATH` line in the unit.

### Reach the UI from your computer

Relay has no login, so it listens on the server's loopback only. Use an SSH tunnel rather than opening a port:

```bash
ssh -L 8767:127.0.0.1:8767 you@your-server
```

Then open <http://127.0.0.1:8767> on your own machine.

## Docker

### How "using the host's agents" works

A container cannot execute programs installed on the host. So the image installs Codex, Claude Code and Gemini CLI
itself, and `docker-compose.yml` mounts everything that makes them *yours*:

| From the host | Mounted as | Why |
|---|---|---|
| `~/.codex`, `~/.claude`, `~/.claude.json`, `~/.gemini` | same, in the container user's home | Your logins, settings and MCP servers |
| `~/.config/gh` or `GH_TOKEN` | same | Pushing branches and opening draft pull requests |
| `~/.gitconfig`, `~/.ssh` | same, read-only | Your Git identity and push keys |
| `RELAY_REPOS` | **the same absolute path** | Your repositories |
| `RELAY_DATA` | **the same absolute path** | Relay's tasks, conversations and worktrees |

Repositories and data use identical paths inside and outside the container because Git records each task worktree's
location as an absolute path in your repository. Matching paths keep those links valid from both sides, so you can
open a task branch on the host too.

The CLI *versions* come from the image, not the host. Pin them in `.env` if you need them to match.

### Set up

Docker mode targets Linux hosts. On Windows use `run.bat` instead: Windows paths cannot be mounted at the same path
inside a Linux container.

1. Sign in to each CLI on the host first, as described in [Ubuntu, native](#ubuntu-native). **This matters:** if a
   login file such as `~/.claude.json` does not exist, Docker creates an empty directory in its place, and that CLI
   breaks until you remove it.
2. Configure:

   ```bash
   cp .env.example .env
   ```

   Set `RELAY_REPOS`, `RELAY_DATA` and `HOST_HOME`, and set `RELAY_UID` and `RELAY_GID` to the output of `id -u` and
   `id -g`. Matching ids keep files that agents write owned by you.

3. Build and start:

   ```bash
   docker compose up -d --build
   docker compose logs -f relay
   ```

   The first lines of the log report which host logins the container can see, and warn about anything missing.

4. Open <http://127.0.0.1:8767>, or use the SSH tunnel above from another computer. The port is published on the
   host's loopback only.

### Docker specifics

- **Codex runs unsandboxed inside the container.** Its own sandbox relies on kernel features Docker blocks, and the
  container already isolates it. Override with `RELAY_CFG_codex_sandbox` if your host allows it.
- **Opening folders or VS Code** from the UI is unavailable, since the container has no desktop. The UI shows the
  path instead.
- **Token refresh.** The CLIs refresh tokens into the mounted login folders. Avoid signing in again on the host while
  a task runs in the container.
- **Extra toolchains.** Verification commands run inside the container. The image includes Node, Python and Git. For
  Java, Go, .NET or others, extend the image with a `FROM relay:local` Dockerfile.

## Configuration through the environment

| Variable | Default | Effect |
|---|---|---|
| `RELAY_PORT` | `8767` | HTTP port |
| `RELAY_HOST` | `127.0.0.1`, or `0.0.0.0` in Docker | Listen address. Anything but loopback exposes an unauthenticated tool. |
| `RELAY_DATA_DIR` | the app folder | Where `config.json`, `state/`, `runtime/` and `worktrees/` live |
| `RELAY_BROWSE_ROOT` | your home folder | Where the task wizard's folder browser starts on Linux |
| `RELAY_CFG_<setting>` | none | Forces any setting, for example `RELAY_CFG_max_turns=6`. Values are parsed as JSON when possible, and are never written to `config.json`. |

## First task

1. **Agents** page, then **Test all** to confirm each CLI answers and streams correctly.
2. **New task**: choose a Git repository with at least one commit, describe the change, pick a preset, then
   **Create & queue**.
3. Watch the team conversation. Use the composer to guide, answer questions or approve delivery.
4. When delivered, open the draft pull request, or inspect the branch in the worktree.

## Presets

| Preset | Supervisor | Worker | Reviewer |
|---|---|---|---|
| Codex supervises Claude (default) | Codex | Claude | – |
| Claude supervises Codex | Claude | Codex | – |
| Codex → Claude + independent Codex review | Codex | Claude | Codex |
| Three-agent panel | Codex | Claude | Gemini |
| Claude supervises Gemini | Claude | Gemini | – |
| Claude pair / Codex pair | same vendor, two sessions | | – |

Any role can be remapped per task. Models are free text, passed straight to the CLI.

## Models, effort and usage

Every role picks its own model and reasoning effort, in the new-task wizard or under Settings → Workflow. Defaults per
agent live on each card under Settings → Agents.

- **Model**: the agent's catalog plus "Custom…" for any exact id. Blank means the CLI's own default, shown in the label.
- **Reasoning effort**: Codex `low` to `xhigh`, Claude `low` to `max`. Gemini CLI has no effort control.

### Keeping usage down

Settings → **Usage & budget**:

- **Cheaper subagents.** Claude Code runs its helper agents on a small model through `CLAUDE_CODE_SUBAGENT_MODEL`,
  default `haiku`. Codex and Gemini CLI have no equivalent setting.
- **Lean prompts.** Each role gets only the rules it needs, plus rule files matching the task.
- **Prompt budget.** Caps on the reviewer diff, echoed worker reports, failed-check output, changed-file lists and
  stored tool output. Passing checks send only their verdict.
- **Thrifty and Thorough presets**, one click each.

The largest lever is the loop itself: every extra work package or review round re-sends the whole session. Keep work
packages at 4 to 6 and review rounds at 1 or 2 unless the change is risky.

## Where things are

All under `RELAY_DATA_DIR`, which defaults to the app folder:

- `config.json`: settings, also editable in the UI
- `state/tasks.json`: tasks, with rolling backups in `state/backups/`
- `runtime/<task-id>/`: conversation, artifacts and the raw agent log
- `worktrees/`: isolated checkouts, removed when you delete a task
- `managed-repos/`: clones created by GitHub intake

## Troubleshooting

- **Backend offline.** Keep `run.bat`, `run.sh` or the service running. The browser reconnects by itself.
- **Relay says it is already running.** Another copy owns the port. Use it, or stop it first.
- **Agent "not ready".** Run the CLI once in a terminal to finish signing in. Gemini with a Workspace account needs
  `GOOGLE_CLOUD_PROJECT`.
- **Task interrupted after a restart.** It resumes automatically from its checkpoint when Relay starts.
- **Turn budget exhausted.** Raise *Max work packages* under Edit task settings, then Resume.
- **Docker: a CLI fails to sign in.** Check the startup log. The usual causes are a login file that did not exist on
  the host, now an empty directory, or `RELAY_UID` not matching the owner of the mounted folders.
- **Stray empty file named `CRLF` in a worktree.** A global Claude Code hook that echoes prompts through `cmd.exe`
  turns the text `LF->CRLF` into a shell redirect. Fix the hook, or set Claude's setting sources to project and local
  only under Settings → Agents.
