"""Android phone setting: parsing the address, the adb environment agents and checks get, direct connect.

    python -m unittest tests.test_phone -v
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
os.environ.setdefault("RELAY_DATA_DIR", tempfile.mkdtemp(prefix="relay-phone-test-"))
sys.path.insert(0, str(ROOT))

from orchestrator import environment, runner  # noqa: E402


class PhoneSocket(unittest.TestCase):
    def test_pc_mode_addresses(self):
        cases = {
            "100.85.19.9:5037": "tcp:100.85.19.9:5037",
            "100.85.19.9": "tcp:100.85.19.9:5037",
            " tcp:host.docker.internal:5037 ": "tcp:host.docker.internal:5037",
            "[fd7a:115c::1]:5038": "tcp:[fd7a:115c::1]:5038",
            "": "", "bad host!": "", "1.2.3.4:70000": "", "1.2.3.4:0": "",
        }
        for raw, want in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(environment.phone_socket({"phone_adb_server": raw}), want)
        self.assertEqual(environment.phone_address({"phone_adb_server": "100.85.19.9:5037"}), "")

    def test_direct_mode(self):
        cfg = {"phone_adb_mode": "direct", "phone_adb_server": "100.122.155.50:41234"}
        self.assertEqual(environment.phone_socket(cfg), "")
        self.assertEqual(environment.phone_address(cfg), "100.122.155.50:41234")
        self.assertEqual(environment.phone_address({**cfg, "phone_adb_server": "100.122.155.50"}), "100.122.155.50:5555")

    def test_env_for_turns_and_checks(self):
        env = {}
        runner._with_phone(env, {"phone_adb_server": "100.85.19.9"})
        self.assertEqual(env["ADB_SERVER_SOCKET"], "tcp:100.85.19.9:5037")
        env = {}
        runner._with_phone(env, {})
        self.assertNotIn("ADB_SERVER_SOCKET", env)

    def test_direct_connect_is_throttled(self):
        cfg = {"phone_adb_mode": "direct", "phone_adb_server": "100.122.155.51:5555"}
        done = mock.Mock(stdout="connected to 100.122.155.51:5555", stderr="")
        with mock.patch.object(environment.shutil, "which", return_value="/usr/bin/adb"), \
             mock.patch.object(environment.subprocess, "run", return_value=done) as run:
            self.assertIn("connected", environment.phone_connect(cfg))
            self.assertEqual(environment.phone_connect(cfg), "")
            environment.phone_connect(cfg, force=True)
        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args[0][0], ["/usr/bin/adb", "connect", "100.122.155.51:5555"])


if __name__ == "__main__":
    unittest.main()
