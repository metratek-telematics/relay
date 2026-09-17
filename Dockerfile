# Relay in Docker.
#
# The agent CLIs are installed in the image, but they use YOUR host's logins,
# settings, MCP servers and repositories, which docker-compose.yml mounts in.
# A container cannot execute programs installed on the host, so this is the
# closest faithful equivalent: same accounts, same config, same repos.

FROM node:22-bookworm-slim

ARG DEBIAN_FRONTEND=noninteractive
# Match your host user so files written into mounted repos and logins keep the
# right owner. docker-compose.yml passes these from RELAY_UID / RELAY_GID.
ARG UID=1000
ARG GID=1000
# Pin versions for reproducible builds, e.g. --build-arg CLAUDE_VERSION=2.1.272
ARG CODEX_VERSION=latest
ARG CLAUDE_VERSION=latest
ARG GEMINI_VERSION=latest

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      python3 python3-venv ca-certificates curl git openssh-client bash procps tini gnupg bzip2 \
 && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
      -o /usr/share/keyrings/githubcli-archive-keyring.gpg \
 && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
      > /etc/apt/sources.list.d/github-cli.list \
 && apt-get update && apt-get install -y --no-install-recommends gh \
 && rm -rf /var/lib/apt/lists/*

RUN npm install -g \
      "@openai/codex@${CODEX_VERSION}" \
      "@anthropic-ai/claude-code@${CLAUDE_VERSION}" \
      "@google/gemini-cli@${GEMINI_VERSION}" \
 && npm cache clean --force

# A headless browser so agents can look at the UI they build (relay-screenshot) and
# check it in both themes, instead of shipping a redesign nobody has seen.
ARG PLAYWRIGHT_VERSION=1.63.0
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright \
    NODE_PATH=/usr/local/lib/node_modules
RUN npm install -g "playwright-core@${PLAYWRIGHT_VERSION}" \
 && npx -y "playwright@${PLAYWRIGHT_VERSION}" install --with-deps chromium-headless-shell \
 && chmod -R a+rX /opt/ms-playwright \
 && npm cache clean --force \
 && rm -rf /var/lib/apt/lists/*

# The node image ships a "node" user at 1000:1000. Reuse or re-number it so the
# container user matches the host user that owns the mounted files.
RUN set -eux; \
    if getent group "${GID}" >/dev/null; then groupmod -n relay "$(getent group "${GID}" | cut -d: -f1)"; \
    else groupadd -g "${GID}" relay; fi; \
    if getent passwd "${UID}" >/dev/null; then \
      usermod -l relay -d /home/relay -m -g "${GID}" "$(getent passwd "${UID}" | cut -d: -f1)"; \
    else useradd -m -u "${UID}" -g "${GID}" -d /home/relay -s /bin/bash relay; fi; \
    mkdir -p /home/relay/.config /data /app; \
    chown -R "${UID}:${GID}" /home/relay /data /app

WORKDIR /app
COPY requirements.txt ./
RUN python3 -m venv /opt/venv \
 && /opt/venv/bin/pip install --no-cache-dir -r requirements.txt

COPY --chown=${UID}:${GID} . .
RUN chmod +x /app/docker/entrypoint.sh /app/run.sh \
 && printf '#!/bin/sh\nexec node /app/tools/screenshot.cjs "$@"\n' > /usr/local/bin/relay-screenshot \
 && chmod 755 /usr/local/bin/relay-screenshot \
 && printf '# Relay: agent shell tools run as login shells, which reset PATH here; keep the task PATH (worktree .venv, agents).\nif [ -n "${RELAY_PATH:-}" ]; then PATH="$RELAY_PATH"; export PATH; fi\n' > /etc/profile.d/relay-path.sh \
 && chmod 644 /etc/profile.d/relay-path.sh

ENV PATH=/opt/venv/bin:$PATH \
    HOME=/home/relay \
    PYTHONUNBUFFERED=1 \
    RELAY_IN_DOCKER=1 \
    RELAY_HOST=0.0.0.0 \
    RELAY_PORT=8767 \
    RELAY_DATA_DIR=/data \
    IS_SANDBOX=1
# IS_SANDBOX tells Claude Code it may run unattended: the container is the sandbox.

USER relay
EXPOSE 8767

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8767/api/ping >/dev/null || exit 1

# tini reaps the agent subprocesses and forwards stop signals to them.
ENTRYPOINT ["/usr/bin/tini", "--", "/app/docker/entrypoint.sh"]
CMD ["python", "web_app.py", "--no-browser"]
