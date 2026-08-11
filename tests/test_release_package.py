"""The maintained release builder must produce an installable, clean archive."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

from scripts.build_release import build_release, metadata_version


class ReleasePackageTest(unittest.TestCase):
    def test_release_archive_is_reproducible_and_importable(self) -> None:
        repository_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            temporary_root = Path(directory)
            first = build_release(repository_root, temporary_root / "first")
            second = build_release(repository_root, temporary_root / "second")
            self.assertEqual(first.read_bytes(), second.read_bytes())
            self.assertEqual(
                first.name,
                f"astrbot_plugin_mi_fitness_health-{metadata_version(repository_root)}.zip",
            )

            package_root = temporary_root / "astrbot_plugin_mi_fitness_health"
            package_root.mkdir()
            with zipfile.ZipFile(first) as archive:
                names = set(archive.namelist())
                archive.extractall(package_root)
            self.assertIn("main.py", names)
            self.assertIn("compat/runner_privacy_guard.py", names)
            self.assertIn("features/health_commands.py", names)
            self.assertFalse(any(name.startswith("tests/") for name in names))
            self.assertFalse(any("__pycache__" in name for name in names))

            environment = os.environ.copy()
            environment["PYTHONPATH"] = os.pathsep.join(
                (str(repository_root / "tests"), str(temporary_root))
            )
            completed = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "import astrbot_test_stub; "
                    "from astrbot_plugin_mi_fitness_health.main import MiFitnessHealthPlugin; "
                    "assert MiFitnessHealthPlugin.__name__ == 'MiFitnessHealthPlugin'",
                ],
                cwd=temporary_root,
                env=environment,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)


if __name__ == "__main__":
    unittest.main()
