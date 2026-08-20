"""Fail when a credential-shaped value is present in a publishable repository file."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PATTERNS = {
    "private key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
    "Google API key": re.compile(r"AIza[0-9A-Za-z_-]{35}"),
    "Google OAuth token": re.compile(r"ya29\.[0-9A-Za-z_-]{20,}"),
    "GitHub token": re.compile(r"gh[pousr]_[0-9A-Za-z]{30,}"),
    "Grafana Cloud token": re.compile(r"glc_[0-9A-Za-z_-]{20,}"),
    "Grafana service-account token": re.compile(r"glsa_[0-9A-Za-z_-]{20,}"),
    "AWS access key": re.compile(r"(?:AKIA|ASIA)[0-9A-Z]{16}"),
    "Slack token": re.compile(r"xox[baprs]-[0-9A-Za-z-]{20,}"),
}
TEXT_SUFFIXES = {
    "",
    ".cfg",
    ".conf",
    ".css",
    ".csv",
    ".env",
    ".hcl",
    ".html",
    ".ini",
    ".js",
    ".json",
    ".jsonl",
    ".md",
    ".py",
    ".sh",
    ".srt",
    ".tf",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
}


def publishable_files() -> list[Path]:
    proc = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    paths = []
    for raw in proc.stdout.decode("utf-8", errors="surrogateescape").split("\0"):
        if not raw:
            continue
        path = ROOT / raw
        if (
            path.is_file()
            and path.suffix.lower() in TEXT_SUFFIXES
            and path.stat().st_size < 5_000_000
        ):
            paths.append(path)
    return paths


def main() -> int:
    findings: list[str] = []
    for path in publishable_files():
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for line_number, line in enumerate(text.splitlines(), 1):
            for name, pattern in PATTERNS.items():
                if pattern.search(line):
                    findings.append(f"{path.relative_to(ROOT)}:{line_number}: {name}")
    if findings:
        print("credential-shaped values found:")
        for finding in findings:
            print(f"  {finding}")
        return 1
    print(f"secret scan passed ({len(publishable_files())} publishable text files)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
