# tools/github_actions_runner/test_redact.py
"""Redaction must be total: a published log cannot be un-published."""

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import redact  # noqa: E402


class RedactionTests(unittest.TestCase):
    def test_windows_home_directory(self):
        redacted = redact.redact_bytes(rb"copy from C:\Users\IDLT\.gitconfig")
        self.assertEqual(redacted, rb"copy from C:\Users\<user>\.gitconfig")

    def test_escaped_backslashes_from_json_output(self):
        redacted = redact.redact_bytes(rb'"path": "C:\\\\Users\\\\IDLT\\\\AppData"')
        self.assertNotIn(b"IDLT", redacted)
        self.assertIn(rb"C:\\\\Users\\\\<user>", redacted)

    def test_windows_path_with_forward_slashes(self):
        redacted = redact.redact_bytes(b"python at C:/Users/IDLT/AppData/Local")
        self.assertEqual(redacted, b"python at C:/Users/<user>/AppData/Local")

    def test_posix_home_directory(self):
        redacted = redact.redact_bytes(b"wrote /home/alice/work/log.txt")
        self.assertEqual(redacted, b"wrote /home/<user>/work/log.txt")

    def test_bare_account_name(self):
        redacted = redact.redact_bytes(b"USERNAME=IDLT", account_names=["IDLT"])
        self.assertEqual(redacted, b"USERNAME=<user>")

    def test_bare_name_must_be_a_whole_word(self):
        redacted = redact.redact_bytes(b"IDLTX and wildIDLT", account_names=["IDLT"])
        self.assertEqual(redacted, b"IDLTX and wildIDLT")

    def test_generic_names_do_not_damage_paths(self):
        """An account called `user` must not turn `C:\\Users` into `C:\\<user>s`."""
        redacted = redact.redact_bytes(
            rb"C:\Users\user\.gitconfig", account_names=["user"]
        )
        self.assertEqual(redacted, rb"C:\Users\<user>\.gitconfig")

    def test_ordinary_log_text_is_untouched(self):
        line = b"PASS test_absent_term_falls_back_to_one_full_scan (0.004s)"
        self.assertEqual(redact.redact_bytes(line), line)

    def test_redaction_is_idempotent(self):
        once = redact.redact_bytes(rb"C:\Users\IDLT\file.txt")
        self.assertEqual(redact.redact_bytes(once), once)



class OperatingSystemVersions(unittest.TestCase):
    """A build number says which patch level, which is what an attacker wants."""

    def test_the_platform_string_keeps_only_the_family(self):
        self.assertEqual(
            redact.redact_bytes(b"platform: Windows-10-10.0.19045-SP0"),
            b"platform: Windows-10-<version>",
        )

    def test_a_bare_build_number_is_removed(self):
        self.assertEqual(redact.redact_bytes(b"version 10.0.19045"), b"version 10.0.<version>")

    def test_a_labelled_build_number_is_removed(self):
        self.assertEqual(redact.redact_bytes(b"Build 22631 ready"), b"Build <version> ready")

    def test_a_release_id_and_its_build_are_removed(self):
        self.assertEqual(
            redact.redact_bytes(b"Windows 10 22H2 (19045)"), b"Windows 10 <release> (<version>)"
        )

    def test_each_redaction_can_be_applied_twice_safely(self):
        once = redact.redact_bytes(b"Windows-10-10.0.19045-SP0")
        self.assertEqual(redact.redact_bytes(once), once)

    def test_other_platforms_keep_only_the_family(self):
        self.assertEqual(redact.redact_bytes(b"macOS-14.5-arm64"), b"macOS-<version>")
        self.assertEqual(redact.redact_bytes(b"Linux-6.8.0-45-generic"), b"Linux-<version>")

    def test_the_family_can_be_masked_too(self):
        original = os.environ.get("REDACT_OS_FAMILY")
        os.environ["REDACT_OS_FAMILY"] = "1"
        try:
            self.assertEqual(
                redact.redact_bytes(b"Windows 10 on x64"), b"Windows <version> on x64"
            )
            self.assertEqual(
                redact.redact_bytes(b"Windows-10-10.0.19045-SP0"), b"Windows-<version>"
            )
        finally:
            if original is None:
                os.environ.pop("REDACT_OS_FAMILY", None)
            else:
                os.environ["REDACT_OS_FAMILY"] = original

    def test_ordinary_numbers_are_left_alone(self):
        """A log is full of timings and counts; none of them are OS versions."""
        for text in (b"idle 107.3 ms", b"results=25", b"port 8080", b"build 42 done"):
            self.assertEqual(redact.redact_bytes(text), text)


if __name__ == "__main__":
    unittest.main()
