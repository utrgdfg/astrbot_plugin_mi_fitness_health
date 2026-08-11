"""Repository and release privacy gates reject only high-confidence leaks."""

from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path

from scripts.privacy_gate import (
    inspect_payload,
    scan_archive,
    scan_tracked_tree,
)


class PrivacyGateTest(unittest.TestCase):
    def test_current_tracked_tree_is_clean(self) -> None:
        repository_root = Path(__file__).parents[1]
        self.assertEqual(scan_tracked_tree(repository_root), [])

    def test_placeholders_and_safety_documentation_are_allowed(self) -> None:
        payload = (
            b"passToken=synthetic-secret-value\n"
            b"Never publish Cookie, token, .env, screenshots, or logs.\n"
            b"Use ${HOME} or /path/to/plugin in documentation.\n"
        )
        self.assertEqual(inspect_payload("docs/example.md", payload, "test"), [])

    def test_private_artifact_names_are_rejected(self) -> None:
        for path in (
            ".env.local",
            "logs/session.log",
            "screenshots/account.png",
            "backup/health.sqlite3",
            "photo.jpg",
        ):
            with self.subTest(path=path):
                self.assertTrue(inspect_payload(path, b"safe", "test"))

    def test_personal_paths_and_real_shaped_secrets_are_rejected(self) -> None:
        drive_path = "C:" + r"\Users\alice\health.sqlite3"
        posix_path = "/" + "home/alice/health.sqlite3"
        token = "gh" + "p_" + ("A" * 24)
        secret_assignment = "pass" + "Token=" + ("R" * 24)
        platform_log = "[20" + "26-08-11 12:00:00] [Core] [event_bus:1]: message"
        for payload in (
            drive_path,
            posix_path,
            token,
            secret_assignment,
            platform_log,
        ):
            with self.subTest(kind=payload[:8]):
                self.assertTrue(inspect_payload("debug.txt", payload.encode(), "test"))

    def test_diagnostics_never_repeat_the_rejected_secret(self) -> None:
        from scripts.privacy_gate import assert_clean

        secret = "gh" + "p_" + ("A" * 24)
        violations = inspect_payload("debug.txt", secret.encode(), "test")
        with self.assertRaises(ValueError) as raised:
            assert_clean(violations)
        self.assertNotIn(secret, str(raised.exception))

    def test_unapproved_logo_content_is_rejected(self) -> None:
        violations = inspect_payload("logo.png", b"not-the-approved-logo", "test")
        self.assertTrue(violations)
        self.assertEqual(violations[0].rule, "unapproved-logo-content")

    def test_archive_is_scanned_without_extracting(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            archive_path = Path(directory) / "unsafe.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("main.py", "print('safe')")
                archive.writestr("debug.log", "private")
            violations = scan_archive(archive_path)
        self.assertTrue(violations)
        self.assertEqual(violations[0].path, "debug.log")


if __name__ == "__main__":
    unittest.main()
