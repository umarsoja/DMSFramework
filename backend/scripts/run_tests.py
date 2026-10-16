"""Run the explicit Django suite and reject zero-test success."""

import os
import re
import subprocess
import sys


settings = os.environ.get("APGC_TEST_SETTINGS", "config.settings.testing")
command = [
    sys.executable,
    "manage.py",
    "test",
    "tests",
    f"--settings={settings}",
    *sys.argv[1:],
]
result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
sys.stdout.write(result.stdout)
if result.returncode:
    raise SystemExit(result.returncode)
if not re.search(r"Ran [1-9][0-9]* tests? in", result.stdout):
    print("ERROR: Django did not report a nonzero discovered test count.", file=sys.stderr)
    raise SystemExit(2)
