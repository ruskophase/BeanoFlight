import os
import subprocess
import sys
import unittest
from pathlib import Path


class CommandLineEntryPointTests(unittest.TestCase):
    def test_cli_module_executes_main(self):
        root = Path(__file__).resolve().parents[1]
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(root / "src")
        result = subprocess.run(
            [sys.executable, "-m", "beanoflight.cli", "--help"],
            cwd=root,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("usage: beano-flight", result.stdout)


if __name__ == "__main__":
    unittest.main()
