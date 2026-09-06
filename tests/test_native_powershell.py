"""Optional native parser consumer; fixture commands are parsed, never executed."""
import contextlib
import importlib.util
import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "runtime/skills/powershell-exact-action/scripts/Test-PowerShellExactAction.ps1"


class NativePowerShell(unittest.TestCase):
    def skillbook(self):
        path = ROOT / "runtime/skills/master-guided-skill-resolver/scripts/test_skillbook.py"
        spec = importlib.util.spec_from_file_location("optional_native_skillbook", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_host_discovery_preserves_available_aliases(self):
        skillbook = self.skillbook()
        for available, expected in (
            ({}, []),
            ({"pwsh": "/host/pwsh"}, ["/host/pwsh"]),
            ({"powershell": "/host/powershell"}, ["/host/powershell"]),
            ({"powershell": "/host/powershell", "pwsh": "/host/pwsh"}, ["/host/powershell", "/host/pwsh"]),
            ({"powershell": "/host/shared", "pwsh": "/host/shared"}, ["/host/shared"]),
        ):
            with self.subTest(available=available), mock.patch.object(skillbook.shutil, "which", side_effect=available.get):
                self.assertEqual(skillbook.powershell_hosts(), expected)

    def test_no_native_host_keeps_portable_skillbook_tail(self):
        skillbook = self.skillbook()
        output = io.StringIO()
        with mock.patch.object(skillbook, "powershell_hosts", return_value=[]), mock.patch.object(sys, "argv", ["test_skillbook.py"]), contextlib.redirect_stdout(output):
            self.assertEqual(skillbook.main(), 0)
        receipt = json.loads(output.getvalue())
        self.assertEqual(receipt["failed"], 0)
        self.assertEqual({item["name"] for item in receipt["skipped"]}, {
            "powershell-foreach-block", "powershell-intermediate-safe",
            "powershell-quoted-false-positive", "powershell-literal-dollar-block",
        })
        self.assertTrue(all(item["reason"] for item in receipt["skipped"]))
        self.assertIn("effect-typed-normal-path", {item["name"] for item in receipt["tests"]})
        self.assertFalse(any(item["name"].startswith("powershell-") for item in receipt["tests"]))

    def test_available_native_hosts_preserve_member_decisions(self):
        hosts = list(dict.fromkeys(path for name in ("pwsh", "powershell") if (path := shutil.which(name))))
        if not hosts:
            self.skipTest("Optional native PowerShell runtime is not installed on this host.")
        members = [
            {"id": "direct-pipe", "command": "foreach ($item in $items) { $item } | Select-Object -First 1"},
            {"id": "normal-pipe", "command": "Get-ChildItem | Select-Object -First 1"},
            {"id": "quoted-example", "command": "Write-Output 'foreach ($i in $items) { $i } | Select-Object'"},
            {"id": "separate-pipe", "command": "foreach ($i in $items) { $i }; Get-ChildItem | Select-Object -First 1"},
            {"id": "literal-dollar-block", "command": 'Write-Output "$name"', "literal_dollar_required": True, "literal_tokens": ["$name"]},
            {"id": "literal-dollar-safe", "command": "Write-Output '$name'", "literal_dollar_required": True, "literal_tokens": ["$name"]},
        ]
        expected = ["BLOCK", "PASS", "PASS", "PASS", "BLOCK", "PASS"]
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "members.json"
            source.write_text(json.dumps({"members": members}), encoding="utf-8")
            for host in hosts:
                with self.subTest(host=Path(host).name):
                    result = subprocess.run([host, "-NoProfile", "-File", str(SCRIPT), "-InputJsonPath", str(source)],
                                            text=True, capture_output=True, check=False)
                    self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                    receipt = json.loads(result.stdout)
                    self.assertEqual([member["decision"] for member in receipt["members"]], expected)
                    self.assertFalse(receipt["prevented_claim_eligible"])


if __name__ == "__main__":
    unittest.main()
