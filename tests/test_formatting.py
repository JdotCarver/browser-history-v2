# tests/test_formatting.py
"""The subtitle rules, at every boundary the reader can notice."""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "plugin"))

import formatting  # noqa: E402
from formatting import (  # noqa: E402
    URL_DOMAIN_PATH,
    DATE_ABSOLUTE,
    DATE_RELATIVE,
    LAYOUT_BRACKETED,
    LAYOUT_DASHED,
    TIME_12,
    TIME_24,
    TIME_SYSTEM,
    URL_DOMAIN,
    URL_FULL,
    display_url,
    format_clock,
    format_date,
    format_subtitle,
)

NOW = datetime(2026, 10, 25, 19, 43)  # a Sunday


def ago(**kwargs) -> datetime:
    return NOW - timedelta(**kwargs)


class DefaultsAreUnchanged(unittest.TestCase):
    """The whole point of the defaults: nobody who upgrades sees a difference."""

    def test_default_subtitle_matches_the_original_format(self):
        moment = datetime(2026, 10, 25, 19, 43)
        self.assertEqual(
            format_subtitle(moment, "https://www.youtube.com/@Level1Linux"),
            "[Oct 25, 2026 07:43 PM] https://www.youtube.com/@Level1Linux",
        )

    def test_defaults_are_the_module_constants(self):
        self.assertEqual(
            format_subtitle(NOW, "https://example.com/"),
            format_subtitle(
                NOW,
                "https://example.com/",
                date_display=DATE_ABSOLUTE,
                time_format=TIME_12,
                url_display=URL_FULL,
                subtitle_layout=LAYOUT_BRACKETED,
            ),
        )


class ClockFormat(unittest.TestCase):
    def test_twelve_hour_keeps_the_leading_zero(self):
        self.assertEqual(format_clock(NOW, TIME_12), "07:43 PM")

    def test_twenty_four_hour(self):
        self.assertEqual(format_clock(NOW, TIME_24), "19:43")

    def test_system_follows_the_locale(self):
        # Patching time.strftime itself would not work: datetime.strftime
        # delegates to it, so the rest of the formatting would break too.
        with mock.patch.object(formatting, "_system_prefers_24h", return_value=False):
            self.assertEqual(format_clock(NOW, TIME_SYSTEM), "07:43 PM")
        with mock.patch.object(formatting, "_system_prefers_24h", return_value=True):
            self.assertEqual(format_clock(NOW, TIME_SYSTEM), "19:43")

    def test_absolute_date_uses_the_chosen_clock(self):
        self.assertEqual(
            format_date(NOW, NOW, relative=False, time_format=TIME_24),
            "Oct 25, 2026 19:43",
        )


class RelativeDates(unittest.TestCase):
    def relative(self, moment, **kwargs):
        return format_date(moment, NOW, relative=True, **kwargs)

    def test_under_a_minute(self):
        self.assertEqual(self.relative(ago(seconds=0)), "Just now")
        self.assertEqual(self.relative(ago(seconds=59)), "Just now")

    def test_minutes(self):
        self.assertEqual(self.relative(ago(minutes=1)), "1 min ago")
        self.assertEqual(self.relative(ago(minutes=12)), "12 min ago")
        self.assertEqual(self.relative(ago(minutes=59, seconds=59)), "59 min ago")

    def test_today_shows_the_time(self):
        self.assertEqual(
            self.relative(ago(hours=3)), "Today at 04:43 PM"
        )

    def test_yesterday(self):
        self.assertEqual(
            self.relative(ago(days=1)), "Yesterday at 07:43 PM"
        )

    def test_yesterday_is_a_calendar_day_not_twenty_four_hours(self):
        # 00:10 today, a visit at 23:50 last night: 20 minutes ago, and the
        # minute form is the more useful of the two.
        now = datetime(2026, 10, 25, 0, 10)
        self.assertEqual(
            format_date(datetime(2026, 10, 24, 23, 50), now, relative=True),
            "20 min ago",
        )
        # An hour and a half back, though, still reads best as yesterday.
        self.assertEqual(
            format_date(datetime(2026, 10, 24, 22, 40), now, relative=True),
            "Yesterday at 10:40 PM",
        )

    def test_earlier_this_week_names_the_day(self):
        self.assertEqual(
            self.relative(ago(days=2)), "Friday at 07:43 PM"
        )
        self.assertEqual(
            self.relative(ago(days=6)), "Monday at 07:43 PM"
        )

    def test_a_week_or_more_drops_the_time(self):
        self.assertEqual(self.relative(ago(days=7)), "Oct 18")
        self.assertEqual(
            format_date(datetime(2026, 1, 5, 9, 0), NOW, relative=True), "Jan 05"
        )

    def test_previous_years_keep_the_year(self):
        self.assertEqual(
            format_date(datetime(2025, 12, 31, 23, 0), NOW, relative=True),
            "Dec 31, 2025",
        )

    def test_relative_dates_respect_the_clock_setting(self):
        self.assertEqual(
            self.relative(ago(days=1), time_format=TIME_24),
            "Yesterday at 19:43",
        )

    def test_a_future_visit_falls_back_to_a_plain_date(self):
        self.assertEqual(
            self.relative(NOW + timedelta(hours=2)),
            "Oct 25, 2026 09:43 PM",
        )


class DomainDisplay(unittest.TestCase):
    def test_full_url_is_the_default(self):
        url = "https://www.youtube.com/@Level1Linux"
        self.assertEqual(display_url(url), url)

    def test_www_is_dropped(self):
        self.assertEqual(
            display_url("https://www.youtube.com/@Level1Linux", URL_DOMAIN),
            "youtube.com",
        )

    def test_subdomains_are_kept(self):
        self.assertEqual(
            display_url("https://mail.google.com/mail/u/0/#inbox", URL_DOMAIN),
            "mail.google.com",
        )

    def test_port_is_kept(self):
        self.assertEqual(
            display_url("http://localhost:8080/admin", URL_DOMAIN),
            "localhost:8080",
        )

    def test_scheme_is_lowercased_out_of_the_way(self):
        self.assertEqual(
            display_url("HTTPS://GitHub.com/A/B", URL_DOMAIN), "github.com"
        )

    def test_credentials_are_dropped(self):
        self.assertEqual(
            display_url("https://user:pw@example.com/x", URL_DOMAIN), "example.com"
        )

    def test_hosts_without_a_site_name_stay_whole(self):
        for url in ("chrome://settings/", "file:///C:/notes.txt", "about:blank"):
            self.assertEqual(display_url(url, URL_DOMAIN), url)

    def test_a_bare_www_host_is_not_mangled(self):
        self.assertEqual(display_url("http://www.com/", URL_DOMAIN), "www.com")

    def test_international_names_are_only_lowercased(self):
        self.assertEqual(
            display_url("https://XN--BCHER-KVA.example/x", URL_DOMAIN),
            "xn--bcher-kva.example",
        )


class DomainAndPath(unittest.TestCase):
    """The middle setting: a site name alone cannot tell two pages apart."""

    def test_the_path_is_kept(self):
        self.assertEqual(
            display_url(
                "https://github.com/JdotCarver/PersonalQMK/actions", URL_DOMAIN_PATH
            ),
            "github.com/JdotCarver/PersonalQMK/actions",
        )

    def test_the_scheme_and_query_are_dropped(self):
        self.assertEqual(
            display_url("https://example.com/a/b?q=1#frag", URL_DOMAIN_PATH),
            "example.com/a/b",
        )

    def test_a_trailing_slash_is_not_shown(self):
        self.assertEqual(display_url("https://example.com/a/", URL_DOMAIN_PATH), "example.com/a")
        self.assertEqual(display_url("https://example.com/", URL_DOMAIN_PATH), "example.com")

    def test_a_long_path_is_shortened_in_the_middle(self):
        long_url = "https://example.com/" + "/".join(["segment"] * 12)
        shown = display_url(long_url, URL_DOMAIN_PATH)
        self.assertLessEqual(len(shown), 60)
        self.assertIn("...", shown)
        self.assertTrue(shown.startswith("example.com/segment"))
        self.assertTrue(shown.endswith("segment"))

    def test_the_site_only_mode_is_unchanged(self):
        self.assertEqual(
            display_url("https://github.com/JdotCarver/PersonalQMK", URL_DOMAIN), "github.com"
        )

    def test_sites_without_a_name_are_left_whole(self):
        for url in ("chrome://settings/", "file:///C:/notes.txt"):
            self.assertEqual(display_url(url, URL_DOMAIN_PATH), url)


class SubtitleLayout(unittest.TestCase):
    def test_bracketed(self):
        self.assertEqual(
            format_subtitle(NOW, "https://example.com/x"),
            "[Oct 25, 2026 07:43 PM] https://example.com/x",
        )

    def test_dashed(self):
        self.assertEqual(
            format_subtitle(NOW, "https://example.com/x", subtitle_layout=LAYOUT_DASHED),
            "Oct 25, 2026 07:43 PM - https://example.com/x",
        )

    def test_the_requested_look_end_to_end(self):
        self.assertEqual(
            format_subtitle(
                NOW - timedelta(days=1),
                "https://www.youtube.com/@Level1Linux",
                now=NOW,
                date_display=DATE_RELATIVE,
                url_display=URL_DOMAIN,
                subtitle_layout=LAYOUT_DASHED,
                time_format=TIME_24,
            ),
            "Yesterday at 19:43 - youtube.com",
        )


if __name__ == "__main__":
    unittest.main()
