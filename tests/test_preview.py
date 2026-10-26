# tests/test_preview.py
"""The details panel: what a one-line row cannot say."""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "plugin"))

from formatting import TIME_24  # noqa: E402
from preview import browser_label, build_preview  # noqa: E402

NOW = datetime(2026, 10, 25, 19, 43)


class FakeBrowser:
    def __init__(self, name):
        self.name = name


class Item:
    def __init__(self, url, title="All workflows", browser=None, when=None):
        self.url = url
        self.title = title
        self.browser = browser
        self._when = when or (NOW - timedelta(minutes=5))

    def timestamp(self):
        return self._when


class Contents(unittest.TestCase):
    def preview(self, item, **kwargs):
        return build_preview(item, now=NOW, **kwargs)

    def test_the_description_is_plain_text(self):
        """Flow 2.1.4 renders this panel as text; markdown would show literally."""
        description = self.preview(Item("https://example.com/a"))["Description"]
        self.assertNotIn("**", description)
        self.assertNotIn("`", description)
        self.assertFalse(
            [line for line in description.splitlines() if line.startswith(("#", "- ", "* "))],
            description,
        )

    def test_no_content_type_is_sent(self):
        """`ContentType` only exists in unreleased Flow; sending it is noise."""
        self.assertNotIn("ContentType", self.preview(Item("https://example.com/a")))

    def test_the_whole_address_is_shown_even_when_the_row_hides_it(self):
        """The row may say 'github.com'; the panel says where on GitHub."""
        url = "https://github.com/JdotCarver/PersonalQMK/actions"
        description = self.preview(Item(url))["Description"]
        self.assertTrue(description.startswith(url))

    def test_the_title_is_not_repeated(self):
        """The panel already shows it; repeating it is what made it useless."""
        description = self.preview(Item("https://example.com/a", "All workflows"))["Description"]
        self.assertNotIn("All workflows", description)

    def test_the_exact_time_is_there_as_well_as_the_age(self):
        description = self.preview(Item("https://example.com/a"))["Description"]
        self.assertIn("5 min ago", description)
        self.assertIn("Oct 25, 2026", description)

    def test_the_time_setting_is_respected(self):
        description = self.preview(Item("https://example.com/a"), time_format=TIME_24)["Description"]
        self.assertIn("19:38", description)

    def test_an_old_visit_does_not_say_the_date_twice(self):
        item = Item("https://example.com/a", when=datetime(2025, 3, 4, 9, 0))
        description = self.preview(item)["Description"]
        self.assertEqual(description.count("Mar 04, 2025"), 1)

    def test_the_browser_is_named_readably(self):
        description = self.preview(
            Item("https://example.com/a", browser=FakeBrowser("brave_profile_0"))
        )["Description"]
        self.assertIn("Browser: Brave", description)

    def test_a_custom_profile_keeps_its_name(self):
        label = browser_label(Item("x", browser=FakeBrowser("custom profile 2")))
        self.assertEqual(label, "Custom Profile 2")

    def test_a_named_browser_keeps_its_own_wording(self):
        for name, expected in (
            ("brave_profile_0", "Brave"),
            ("brave_nightly_profile_3", "Brave Nightly"),
            ("firefox", "Firefox"),
            ("zen", "Zen"),
        ):
            self.assertEqual(browser_label(Item("x", browser=FakeBrowser(name))), expected)

    def test_no_browser_means_no_line(self):
        description = self.preview(Item("https://example.com/a"))["Description"]
        self.assertNotIn("Browser:", description)

    def test_an_icon_becomes_the_panel_image(self):
        preview = self.preview(Item("https://example.com/a"), icon="C:/icons/x.png")
        self.assertEqual(preview["PreviewImagePath"], "C:/icons/x.png")

    def test_without_an_icon_flow_falls_back_to_the_row_icon(self):
        self.assertNotIn("PreviewImagePath", self.preview(Item("https://example.com/a")))

    def test_a_non_web_url_still_renders(self):
        description = self.preview(Item("chrome://settings/"))["Description"]
        self.assertIn("chrome://settings/", description)


if __name__ == "__main__":
    unittest.main()
