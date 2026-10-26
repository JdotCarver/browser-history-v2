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


def redact_bytes(contents: bytes, account_names: Sequence[str] | None = None) -> bytes:
    """Replace account names in a log with `<user>`, leaving everything else alone."""
    contents = WINDOWS_BACKSLASH_ACCOUNT.sub(rb"\1" + ACCOUNT_NAME, contents)
    contents = WINDOWS_FORWARD_ACCOUNT.sub(rb"\1" + ACCOUNT_NAME, contents)
    contents = POSIX_ACCOUNT.sub(rb"\1" + ACCOUNT_NAME, contents)

    for name in _account_names(account_names):
        # Only whole words: an account name is also a substring of ordinary text.
        contents = re.sub(
            rb"(?<![A-Za-z0-9])" + re.escape(name) + rb"(?![A-Za-z0-9])",
            ACCOUNT_NAME,
            contents,
            flags=re.IGNORECASE,
        )

    return contents
