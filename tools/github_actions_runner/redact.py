# tools/github_actions_runner/redact.py
"""Strip personal paths and account names out of job logs before they are published.

The exporter republishes whole job logs to the `actions-logs` branch. On a public
repository that branch is world-readable, and a Windows job log is full of the
signed-in account: `C:\\Users\\<name>\\.gitconfig`, the Python install path under
`AppData`, and occasionally `USERNAME=<name>`. None of that is needed to read a
log, so it is replaced with `<user>` on the way out.

The replacements are deliberately dumb and total: they must never depend on a
particular machine's setup, because the log is published before anyone can
inspect it.
"""

from __future__ import annotations

import os
import re
from typing import Iterable, Sequence

#: Where a Windows log names an account: `C:\Users\name`, `C:\\Users\\name`,
#: `C:/Users/name`, and the `\\?\C:\Users\name` long-path form.
WINDOWS_BACKSLASH_ACCOUNT = re.compile(rb"(?i)([A-Z]:\\+Users\\+)([^\\/\s\"']+)")
WINDOWS_FORWARD_ACCOUNT = re.compile(rb"(?i)([A-Z]:/+Users/+)([^\\/\s\"']+)")

#: The POSIX equivalents, which also appear in logs from Ubuntu planner jobs. A
#: colon is excluded from the lookbehind so that a `C:/Users/name` path, already
#: redacted above, is not rewritten a second time.
POSIX_ACCOUNT = re.compile(rb"(?<![\w.:])(/(?:home|Users)/)([^/\s\"']+)")

ACCOUNT_NAME = b"<user>"

#: Operating-system versions. A build number says exactly which patch level a
#: machine is on, which is the first thing an attacker checks against a list of
#: known vulnerabilities. The family (`Windows 10`) is what a log needs in order
#: to be readable; the number is not.
#:
#: `platform.platform()` on the owner's machine prints
#: `Windows-10-10.0.19045-SP0`, which leaks twice over (build and service pack).
WINDOWS_PLATFORM = re.compile(rb"(?i)\bWindows-(\d+(?:\.\d+)?)-\d+(?:\.\d+)*-SP\d+")
WINDOWS_BUILD = re.compile(rb"(?i)\b10\.0\.\d{4,5}\b")
WINDOWS_BUILD_LABEL = re.compile(rb"(?i)\b(build)\s+\d{4,5}\b")
WINDOWS_RELEASE = re.compile(rb"(?i)\b(\d{2})H([12])\b")
#: A release id followed by its build in brackets: `22H2 (19045)`.
WINDOWS_RELEASE_BUILD = re.compile(rb"(?i)\b(\d{2}H[12])\s*\(?\s*\d{4,5}\s*\)?")
MACOS_PLATFORM = re.compile(rb"(?i)\b(macOS|Mac OS X)-(\d+(?:\.\d+)*)(?:-[A-Za-z0-9_]+)?")
LINUX_PLATFORM = re.compile(rb"(?i)\bLinux-(\d+\.\d+(?:\.\d+)*)(?:-[A-Za-z0-9_\-.]+)?")
DARWIN_KERNEL = re.compile(rb"(?i)\bDarwin Kernel Version \d+(?:\.\d+)*")

OS_FAMILY_TAG = b"<version>"

#: `Windows 10` / `Windows 11`. Kept by default so timings stay interpretable;
#: set REDACT_OS_FAMILY=1 to remove the family as well.
WINDOWS_FAMILY = re.compile(rb"(?i)\bWindows (?:10|11)\b")
WINDOWS_HYPHEN_FAMILY = re.compile(rb"(?i)\bWindows-\d+(?:\.\d+)*(?=-)")

#: Names that are simultaneously ordinary words or path components. Scrubbing
#: them as bare words would rewrite text such as `C:\Users` into `C:\<user>s`,
#: and the path patterns above have already removed them where they matter.
GENERIC_NAMES = frozenset(
    {"user", "users", "home", "root", "runner", "github", "administrator", "public"}
)


def _account_names(explicit: Sequence[str] | None = None) -> Iterable[bytes]:
    """Account names to scrub wherever they appear on their own.

    Comes from the environment so this works for any runner without configuration:
    `USERNAME` on Windows, `USER` elsewhere.
    """
    candidates = list(explicit or ())
    for variable in ("USERNAME", "USER", "LOGNAME"):
        value = os.environ.get(variable)
        if value:
            candidates.append(value)

    seen = set()
    for name in candidates:
        encoded = name.encode("utf-8", "replace")
        # A one- or two-character name would match ordinary words in a log, and a
        # generic one would damage path text that is already handled above.
        if len(encoded) < 3 or encoded.lower() in seen:
            continue
        if encoded.decode("utf-8", "replace").lower() in GENERIC_NAMES:
            continue
        seen.add(encoded.lower())
        yield encoded


def _mask_family() -> bool:
    """Whether the OS family should go too (REDACT_OS_FAMILY=1)."""
    return os.environ.get("REDACT_OS_FAMILY", "").strip().lower() in ("1", "true", "yes")


def _redact_os_versions(contents: bytes) -> bytes:
    """Replace exact OS versions with the family name and a marker.

    Order matters: the compound `platform.platform()` form is rewritten first,
    so the bare build number inside it is never matched on its own.
    """
    contents = WINDOWS_PLATFORM.sub(rb"Windows-\1-" + OS_FAMILY_TAG, contents)
    contents = WINDOWS_RELEASE_BUILD.sub(rb"\1 (<version>)", contents)
    contents = WINDOWS_BUILD.sub(b"10.0." + OS_FAMILY_TAG, contents)
    contents = WINDOWS_BUILD_LABEL.sub(rb"\1 " + OS_FAMILY_TAG, contents)
    contents = WINDOWS_RELEASE.sub(b"<release>", contents)
    if _mask_family():
        # Both spellings: `Windows 10` and the hyphenated `Windows-10` left by
        # the platform string above.
        contents = WINDOWS_HYPHEN_FAMILY.sub(b"Windows", contents)
        contents = WINDOWS_FAMILY.sub(b"Windows <version>", contents)
    contents = MACOS_PLATFORM.sub(rb"\1-" + OS_FAMILY_TAG, contents)
    contents = LINUX_PLATFORM.sub(rb"Linux-" + OS_FAMILY_TAG, contents)
    contents = DARWIN_KERNEL.sub(b"Darwin Kernel Version <version>", contents)
    return contents


def redact_bytes(contents: bytes, account_names: Sequence[str] | None = None) -> bytes:
    """Replace account names in a log with `<user>`, leaving everything else alone."""
    contents = WINDOWS_BACKSLASH_ACCOUNT.sub(rb"\1" + ACCOUNT_NAME, contents)
    contents = WINDOWS_FORWARD_ACCOUNT.sub(rb"\1" + ACCOUNT_NAME, contents)
    contents = POSIX_ACCOUNT.sub(rb"\1" + ACCOUNT_NAME, contents)

    contents = _redact_os_versions(contents)

    for name in _account_names(account_names):
        # Only whole words: an account name is also a substring of ordinary text.
        contents = re.sub(
            rb"(?<![A-Za-z0-9])" + re.escape(name) + rb"(?![A-Za-z0-9])",
            ACCOUNT_NAME,
            contents,
            flags=re.IGNORECASE,
        )

    return contents
