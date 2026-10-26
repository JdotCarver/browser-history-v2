# tools/machine_label.py
"""A description of this machine that is safe to publish.

`platform.platform()` returns things like `Windows-10-10.0.XXXXX-SP0`: the exact
build, which is the first thing anyone checks against a list of known
vulnerabilities. Job logs on a public repository are world-readable, and a log
is published before anyone can look at it.

So the tools report this instead:

    Windows 11 / AMD64          (family, architecture)
    Linux / x86_64
"""

from __future__ import annotations

import platform


def describe_machine() -> str:
    """The operating system family and architecture, with no version in it."""
    family = platform.system() or "unknown"
    machine = platform.machine() or "unknown"
    return f"{family} / {machine}"


def describe_python() -> str:
    """The interpreter version, which is what plugin bugs usually depend on."""
    return platform.python_version()
