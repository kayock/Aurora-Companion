"""Regression coverage for the Portable installer check() wrapper.

The test extracts only the check function and runs it with a mocked
as_service function; it never runs the installer or modifies a host.
"""

import re
import subprocess
from pathlib import Path

INSTALLER = (Path(__file__).resolve().parents[1] / "deploy" / "install.sh").read_text()


def test_installer_check_shifts_description_before_running_command():
    match = re.search(r"(?ms)^check\(\) \{.*?^\}", INSTALLER)
    assert match, "installer check() function missing"
    script = (
        "set -euo pipefail\n"
        "VERIFY_OK=1\n"
        'as_service() { "$@"; }\n'
        + match.group(0) + "\n"
        'check "label is not an executable" test 2 -eq 2\n'
        'check "expected failure" test 2 -eq 3\n'
        'test "$VERIFY_OK" -eq 0\n'
    )
    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "ok   label is not an executable" in result.stdout
    assert "FAIL expected failure" in result.stdout
