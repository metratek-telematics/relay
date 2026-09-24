"""SSH connectors: what is refused before anything is sent, and what a permitted command does.

No real host is involved: the one test that runs a command puts a fake `ssh` on PATH and checks the
argv Relay built, so the allow-list, the shell-operator rule and the prod confirmation are proved
without touching a machine.

    RELAY_DATA_DIR is set to a temporary folder before Relay is imported, so nothing touches real settings.
    python -m unittest tests.test_ssh_connector -v
"""
from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = tempfile.mkdtemp(prefix="relay-ssh-test-")
os.environ["RELAY_DATA_DIR"] = TMP
sys.path.insert(0, str(ROOT))

from orchestrator import connectors as K  # noqa: E402

FAKE_SSH = """#!/bin/sh
printf '%s\\n' "$@" > "$ARGV_OUT"
echo "z2-host"
"""


def conn(**over) -> dict:
    cfg = dict(K.DEFAULTS["ssh"], host="10.0.0.9", user="ops", allowed_commands=list(K.SSH_READ_COMMANDS))
    cfg.update(over.pop("config", {}))
    c = {"name": "z2-ssh", "type": "ssh", "environment": "dev", "access": "read", "prod_write_enabled": False, "config": cfg}
    c.update(over)
    return c


class ConfigTests(unittest.TestCase):
    def test_ssh_is_a_type_with_read_only_defaults(self):
        self.assertIn("ssh", K.TYPES)
        d = K.DEFAULTS["ssh"]
        self.assertEqual(d["port"], 22)
        self.assertTrue(d["strict_host_key"])
        self.assertFalse(d["allow_shell_operators"])
        self.assertEqual(d["write_commands"], [])
        self.assertIn("docker ps*", d["allowed_commands"])
        self.assertIn("uptime", d["allowed_commands"])

    def test_no_key_material_is_stored(self):
        self.assertEqual(K.SECRET_KEYS["ssh"], ())
        self.assertNotIn("key", K.DEFAULTS["ssh"])
        c = K.save({"name": "edge-ssh", "type": "ssh", "environment": "dev", "access": "read",
                    "config": {"host": "10.0.0.9", "user": "ops", "key_path": "/root/.ssh/id_ed25519"}})
        try:
            self.assertEqual(c["config"]["key_path"], "/root/.ssh/id_ed25519")
            self.assertEqual(c["config"]["port"], 22)
            self.assertNotIn("BEGIN", json.dumps(K.load_all()))
            self.assertEqual(K.target(c), "ops@10.0.0.9")
        finally:
            K.delete("edge-ssh")

    def test_a_silly_host_or_user_is_rejected(self):
        for bad in ({"host": "10.0.0.9 rm -rf /"}, {"user": "ops; id"}):
            with self.assertRaises(ValueError):
                K.save({"name": "bad-ssh", "type": "ssh", "config": dict({"host": "h", "user": "u"}, **bad)})


class RefusalTests(unittest.TestCase):
    def test_chaining_and_redirection_are_refused(self):
        c = conn()
        for cmd in ("uptime; rm -rf /", "uptime && reboot", "uptime || reboot", "docker ps | sh",
                    "cat /etc/hosts > /tmp/x", "cat < /etc/hosts", "uptime `id`", "uptime $(id)", "uptime\nreboot"):
            with self.assertRaises(K.Refused) as e:
                K.check_ssh(c, cmd)
            self.assertIn("One command per call", str(e.exception))

    def test_shell_operators_can_be_allowed_deliberately(self):
        c = conn(config={"allow_shell_operators": True, "allowed_commands": ["uptime; uptime"]})
        self.assertEqual(K.check_ssh(c, "uptime; uptime"), "uptime; uptime")

    def test_a_command_outside_the_allowlist_is_refused(self):
        c = conn()
        for cmd in ("reboot", "docker rm api", "rm -rf /opt", "curl http://evil"):
            with self.assertRaises(K.Refused) as e:
                K.check_ssh(c, cmd)
            self.assertIn("matches no command allowed", str(e.exception))

    def test_an_allowlisted_command_passes(self):
        c = conn()
        for cmd in ("docker ps --format '{{.Names}}'", "uptime", "df -h", "git -C /opt/stack status --short"):
            self.assertEqual(K.check_ssh(c, cmd), cmd)

    def test_a_write_command_is_refused_with_read_access(self):
        c = conn(config={"write_commands": ["docker compose pull*"]})
        with self.assertRaises(K.Refused) as e:
            K.check_ssh(c, "docker compose pull api")
        self.assertIn("read-only", str(e.exception))

    def test_a_prod_write_needs_confirm_prod(self):
        c = conn(environment="prod", access="write", prod_write_enabled=True,
                 config={"write_commands": ["docker compose pull*"]})
        with self.assertRaises(K.Refused) as e:
            K.check_ssh(c, "docker compose pull api")
        self.assertIn("--confirm-prod", str(e.exception))
        self.assertEqual(K.check_ssh(c, "docker compose pull api", confirm_prod=True), "docker compose pull api")

    def test_a_prod_read_needs_nothing_extra(self):
        c = conn(environment="prod")
        self.assertEqual(K.check_ssh(c, "docker ps"), "docker ps")

    def test_a_refused_call_comes_back_as_a_refusal_not_a_crash(self):
        res = K.run(conn(), "ssh", {"command": "reboot"})
        self.assertEqual(res["call_status"], "refused")
        self.assertTrue(res["refused"])
        self.assertIn("reboot", res["operation"])

    def test_another_type_cannot_be_driven_over_ssh(self):
        res = K.run({"name": "api", "type": "http", "environment": "dev", "access": "read", "config": {}}, "ssh", {"command": "uptime"})
        self.assertEqual(res["call_status"], "refused")


class RunTests(unittest.TestCase):
    def setUp(self):
        self.bin = Path(tempfile.mkdtemp(prefix="relay-ssh-bin-"))
        (self.bin / "ssh").write_text(FAKE_SSH)
        (self.bin / "ssh").chmod(0o755 | stat.S_IXUSR)
        self.argv_out = self.bin / "argv.txt"
        self.old_path, self.old_argv = os.environ.get("PATH", ""), os.environ.get("ARGV_OUT")
        os.environ["PATH"] = f"{self.bin}:{self.old_path}"
        os.environ["ARGV_OUT"] = str(self.argv_out)

    def tearDown(self):
        os.environ["PATH"] = self.old_path
        os.environ.pop("ARGV_OUT", None) if self.old_argv is None else os.environ.update(ARGV_OUT=self.old_argv)

    def test_a_permitted_command_runs_and_is_sent_as_one_argument(self):
        res = K.run(conn(), "ssh", {"command": "docker ps  --format '{{.Names}}'"})
        self.assertEqual(res["call_status"], "ok")
        self.assertTrue(res["ok"])
        self.assertEqual(res["exit_code"], 0)
        self.assertIn("z2-host", res["output"])
        argv = self.argv_out.read_text().splitlines()
        self.assertEqual(argv[-1], "docker ps --format '{{.Names}}'")  # one argument, never a local shell
        self.assertEqual(argv[-2], "ops@10.0.0.9")
        self.assertIn("BatchMode=yes", argv)
        self.assertIn("StrictHostKeyChecking=yes", argv)
        self.assertTrue(any(a.startswith("ConnectTimeout=") for a in argv))

    def test_a_workdir_is_applied_by_relay_after_the_check(self):
        res = K.run(conn(config={"workdir": "/opt/stack", "port": 2222, "strict_host_key": False}), "ssh", {"command": "uptime"})
        self.assertEqual(res["call_status"], "ok")
        argv = self.argv_out.read_text().splitlines()
        self.assertEqual(argv[-1], "cd /opt/stack && uptime")
        self.assertIn("StrictHostKeyChecking=accept-new", argv)
        self.assertIn("2222", argv)

    def test_a_missing_key_file_is_unavailable_and_nothing_runs(self):
        res = K.run(conn(config={"key_path": "/nope/id_ed25519"}), "ssh", {"command": "uptime"})
        self.assertEqual(res["call_status"], "error")
        self.assertTrue(res["unavailable"])
        self.assertFalse(self.argv_out.exists())

    def test_the_health_check_asks_only_for_hostname_and_uptime(self):
        res = K.test(conn())
        self.assertEqual(res["call_status"], "ok")
        self.assertEqual(self.argv_out.read_text().splitlines()[-1], "hostname; uptime")


if __name__ == "__main__":
    unittest.main()
