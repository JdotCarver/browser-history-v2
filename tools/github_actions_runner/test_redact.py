# tools/github_actions_runner/test_redact.py
"""Redaction must be total: a published log cannot be un-published."""

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


if __name__ == "__main__":
    unittest.main()
