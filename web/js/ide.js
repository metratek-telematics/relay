// Opening a task's folders. A Relay running on a server cannot open windows on the viewer's desktop, so with
// browser VS Code configured every open goes there; otherwise the local open works for desktop installs.
import { S } from "./state.js";
import { api } from "./api.js";
import { toast } from "./ui.js";

export const ideBase = () => (S.config.ide_url || "").replace(/\/$/, "");

export function folderFor(t, what) {
  return { worktree: t.worktree, vscode: t.worktree, run: t.run_dir, repo: t.repo }[what] || "";
}

export function openTaskFolder(t, what) {
  const path = folderFor(t, what);
  if (!path) { toast("info", "Not available yet", "The task creates its worktree when it starts."); return; }
  if (ideBase()) { window.open(`${ideBase()}/?folder=${encodeURIComponent(path)}`, "_blank", "noopener"); return; }
  api.open(t.id, what).catch((e) => toast("error", "Cannot open", e.message));
}
