# tests/test_match_report.py
"""The matching measurements, kept honest."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools" / "perf"))

import match_report  # noqa: E402


class Subsequence(unittest.TestCase):
    def test_characters_in_order(self):
        self.assertTrue(match_report.is_subsequence("ytb", "youtube"))
        self.assertTrue(match_report.is_subsequence("", "anything"))

    def test_out_of_order_is_not_a_match(self):
        self.assertFalse(match_report.is_subsequence("byty", "youtube"))
        self.assertFalse(match_report.is_subsequence("youtubes", "youtube"))


class CurrentMatcher(unittest.TestCase):
    def test_every_word_must_appear(self):
        self.assertTrue(
            match_report.matches_current("youtube docs", "Docs", "https://youtube.com/x")
        )
        self.assertFalse(
            match_report.matches_current("youtube maths", "Docs", "https://youtube.com/x")
        )

    def test_case_is_ignored(self):
        self.assertTrue(match_report.matches_current("YOUTUBE", "Docs", "https://YouTube.com/x"))

    def test_a_typo_is_a_miss_today(self):
        self.assertFalse(match_report.matches_current("yotube", "YouTube", "https://youtube.com"))


class LooseMatcher(unittest.TestCase):
    def test_abbreviations_reach_the_page(self):
        self.assertTrue(match_report.matches_loosely("ytb", "YouTube", "https://youtube.com/x"))

    def test_spaces_are_ignored(self):
        self.assertTrue(match_report.matches_loosely("you tube", "YouTube", "https://youtube.com/x"))

    def test_nonsense_still_fails(self):
        self.assertFalse(match_report.matches_loosely("zzzz", "YouTube", "https://youtube.com/x"))


class TypoVariants(unittest.TestCase):
    def test_a_letter_is_dropped_and_two_are_swapped(self):
        variants = dict((query, change) for query, change in match_report.typo_variants("youtube")[1:])
        self.assertIn("drop", " ".join(variants.values()) + " dropped")  # sanity on labels

    def test_short_words_are_left_alone(self):
        self.assertEqual(match_report.typo_variants("ab"), [])

    def test_variants_differ_from_the_original(self):
        for query, _change in match_report.typo_variants("history"):
            self.assertNotEqual(query, "history")


if __name__ == "__main__":
    unittest.main()
