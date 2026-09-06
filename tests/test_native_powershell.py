"""Optional native parser consumer; fixture commands are parsed, never executed."""
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "runtime/skills/powershell-exact-action/scripts/Test-PowerShellExactAction.ps1"


class NativePowerShell(unittest.TestCase):
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
