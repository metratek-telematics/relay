#!/usr/bin/env node
// relay-preview: run a task's dev server inside browser VS Code and print a working preview link.
//
//   cd <worktree> && relay-preview [--port 5173]
//
// code-server's port proxy (/absproxy/<port>/) forwards plain HTTP only, and many dev servers
// (Vite with basic-ssl, for one) serve HTTPS with a self-signed certificate. So the dev server
// runs on <port> and a small bridge on <port+1000> accepts the proxy's HTTP and talks to the dev
// server over whichever protocol it speaks, including the live-reload websocket.
import { spawn } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import http from "node:http";
import https from "node:https";
import net from "node:net";
import tls from "node:tls";

const argv = process.argv.slice(2);
const port = Number(argv[argv.indexOf("--port") + 1]) || 5173;
const bridgePort = port + 1000;
const base = `/absproxy/${bridgePort}/`;
const origin = (process.env.RELAY_PUBLIC_URL || "").replace(/\/$/, "");

const pkg = existsSync("package.json") ? JSON.parse(readFileSync("package.json", "utf8")) : null;
if (!pkg) { console.error("relay-preview: run it inside a project folder with a package.json"); process.exit(2); }
const script = ["dev", "start", "serve"].find((s) => pkg.scripts && pkg.scripts[s]);
if (!script) { console.error("relay-preview: package.json has no dev, start or serve script"); process.exit(2); }
const deps = { ...pkg.dependencies, ...pkg.devDependencies };
const vite = "vite" in deps || ["vite.config.js", "vite.config.ts", "vite.config.mjs"].some(existsSync);

const args = ["run", script, "--", "--host", "127.0.0.1", "--port", String(port), "--strictPort"];
if (vite) args.push("--base", base);
console.log(`relay-preview: npm ${args.join(" ")}`);
const dev = spawn("npm", args, { stdio: "inherit" });
dev.on("exit", (code) => { console.log(`relay-preview: dev server exited (${code})`); process.exit(code ?? 1); });
for (const sig of ["SIGINT", "SIGTERM"]) process.on(sig, () => { dev.kill(sig); process.exit(0); });

// Learn the dev server's protocol once it answers.
let secure = null;
function probe() {
  const req = https.request({ host: "127.0.0.1", port, path: base, method: "HEAD", rejectUnauthorized: false, timeout: 1500 }, () => { secure = true; ready(); });
  req.on("error", (e) => {
    if (e.code === "ECONNREFUSED") return setTimeout(probe, 700);
    const plain = http.request({ host: "127.0.0.1", port, path: base, method: "HEAD", timeout: 1500 }, () => { secure = false; ready(); });
    plain.on("error", () => setTimeout(probe, 700));
    plain.end();
  });
  req.end();
}
setTimeout(probe, 800);

const target = () => ({ host: "127.0.0.1", port, rejectUnauthorized: false });

function ready() {
  const server = http.createServer((req, res) => {
    const headers = { ...req.headers, host: `127.0.0.1:${port}` };
    const upstream = (secure ? https : http).request({ ...target(), method: req.method, path: req.url, headers }, (up) => {
      res.writeHead(up.statusCode || 502, up.headers);
      up.pipe(res);
    });
    upstream.on("error", (e) => { res.writeHead(502); res.end(`relay-preview: ${e.message}`); });
    req.pipe(upstream);
  });
  // Live reload: replay the upgrade request to the dev server and splice the two sockets.
  server.on("upgrade", (req, socket, head) => {
    const conn = secure ? tls.connect(target()) : net.connect(target());
    conn.on(secure ? "secureConnect" : "connect", () => {
      const lines = [`${req.method} ${req.url} HTTP/1.1`];
      for (const [k, v] of Object.entries({ ...req.headers, host: `127.0.0.1:${port}` })) lines.push(`${k}: ${v}`);
      conn.write(lines.join("\r\n") + "\r\n\r\n");
      if (head && head.length) conn.write(head);
      socket.pipe(conn).pipe(socket);
    });
    conn.on("error", () => socket.destroy());
    socket.on("error", () => conn.destroy());
  });
  server.listen(bridgePort, "127.0.0.1", () => {
    console.log(`\nrelay-preview: dev server is ${secure ? "HTTPS" : "HTTP"} on ${port}, bridged on ${bridgePort}`);
    console.log(`relay-preview: open ${origin ? origin : ""}${base}\n`);
  });
}
