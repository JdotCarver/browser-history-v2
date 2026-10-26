# select_runner.py
"""Validate a runner request and emit GitHub Actions job outputs."""

from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

RUNNER_LABELS = {
    "cloud-windows": ["windows-latest"],
    "local-windows": ["self-hosted", "windows", "arena-session"],
}
REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:+-]{1,128}$")
DEFAULT_JOB_NAME = "Windows runner probe"


def validate_job_name(value: object) -> str:
    """Return a single-line display name safe for $GITHUB_OUTPUT."""

    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 100
        or value != value.strip()
        or "\r" in value
        or "\n" in value
    ):
        raise ValueError(
            "job_name must be 1-100 characters on one line, without leading or trailing whitespace."
        )
    return value


@dataclass(frozen=True)
class RunnerRequest:
    """A validated request for one explicitly selected execution target."""

    request_id: str
    target: str
    job_name: str

    @property
    def labels_json(self) -> str:
        """Return labels in the array syntax accepted by jobs.<job_id>.runs-on."""

        return json.dumps(RUNNER_LABELS[self.target], separators=(",", ":"))


def load_request(path: Path) -> RunnerRequest:
    """Load and validate a request without executing any repository code."""

    with path.open("rb") as request_file:
        values = tomllib.load(request_file)

    required_keys = {"request_id", "target"}
    allowed_keys = required_keys | {"job_name"}
    missing_keys = required_keys - values.keys()
    extra_keys = values.keys() - allowed_keys
    if missing_keys:
        raise ValueError(f"Missing request key(s): {', '.join(sorted(missing_keys))}")
    if extra_keys:
        raise ValueError(f"Unknown request key(s): {', '.join(sorted(extra_keys))}")

    request_id = values["request_id"]
    target = values["target"]
    if not isinstance(request_id, str) or not REQUEST_ID_PATTERN.fullmatch(request_id):
        raise ValueError(
            "request_id must be 1-128 characters using only letters, digits, "
            "'.', '_', ':', '+', or '-'."
        )
    if not isinstance(target, str) or target not in RUNNER_LABELS:
        allowed_targets = ", ".join(sorted(RUNNER_LABELS))
        raise ValueError(f"target must be one of: {allowed_targets}")

    job_name = validate_job_name(values.get("job_name", DEFAULT_JOB_NAME))
    return RunnerRequest(request_id=request_id, target=target, job_name=job_name)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("request", type=Path, help="Path to runner-request.toml")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Validate the request and write key=value lines for $GITHUB_OUTPUT."""

    args = parse_args(argv)
    try:
        request = load_request(args.request)
    except (OSError, tomllib.TOMLDecodeError, ValueError) as error:
        print(f"Invalid runner request: {error}", file=sys.stderr)
        return 2

    print(f"request_id={request.request_id}")
    print(f"target={request.target}")
    print(f"workload_name={request.job_name}")
    print(f"runner_labels={request.labels_json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
