# Browser VS Code for Relay: open a task's worktree, edit, use a terminal and run the
# dev server with a preview link through code-server's port proxy (/absproxy/<port>/).
# It carries the same Node toolchain as Relay so `npm run dev` works in its terminal.
FROM codercom/code-server:latest

USER root
ARG NODE_MAJOR=22
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl gnupg git \
 && curl -fsSL "https://deb.nodesource.com/setup_${NODE_MAJOR}.x" | bash - \
 && apt-get install -y --no-install-recommends nodejs \
 && rm -rf /var/lib/apt/lists/*

# relay-preview: run a task's dev server and get a working preview link through the port proxy,
# including dev servers that only speak HTTPS.
COPY relay-preview.mjs /usr/local/lib/relay-preview.mjs
RUN printf '#!/bin/sh\nexec node /usr/local/lib/relay-preview.mjs "$@"\n' > /usr/local/bin/relay-preview \
 && chmod 755 /usr/local/bin/relay-preview

# Authentication is the reverse proxy's job (Relay's deployment puts it behind SSO);
# never publish this port directly.
ENTRYPOINT ["/usr/bin/entrypoint.sh"]
CMD ["--auth", "none", "--bind-addr", "0.0.0.0:8080", "--disable-telemetry", "--disable-update-check"]
