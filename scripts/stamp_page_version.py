"""Stamp the logger page (index.html) with its version: date + short hash.

    python scripts/stamp_page_version.py          # update the stamp if the page changed
    python scripts/stamp_page_version.py --check  # exit 1 if the stamp is out of date

The version lives in <meta name="page-version" content="YYYY-MM-DD.abcdef1">.
The hash is the first 7 characters of the SHA-256 of index.html with the
version left blank (line endings ignored), so it changes whenever the page
changes and can be checked by anyone. (A git commit hash can't go in the file
it is part of: GitHub Pages serves index.html straight from main, with no
build step to insert one.)

The page shows this at the bottom and compares it with the live copy when it
opens; a different value means "New version available". The test suite runs
--check, so a page change without a fresh stamp fails the tests.
"""

from __future__ import annotations

import hashlib
import os
import re
import sys
from datetime import date

INDEX = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "index.html")
META_RE = re.compile(r'(<meta name="page-version" content=")([^"]*)(">)')


def content_hash(text: str) -> str:
    blank = META_RE.sub(r"\g<1>\g<3>", text.replace("\r\n", "\n"), count=1)
    return hashlib.sha256(blank.encode("utf-8")).hexdigest()[:7]


def current_version(text: str) -> str | None:
    m = META_RE.search(text)
    return m.group(2) if m else None


def is_current(text: str) -> bool:
    v = current_version(text) or ""
    return v.endswith("." + content_hash(text))


def stamp(text: str, today: date | None = None) -> str:
    if not META_RE.search(text):
        raise SystemExit('index.html has no <meta name="page-version" content="..."> tag')
    if is_current(text):
        return text
    version = f"{(today or date.today()).isoformat()}.{content_hash(text)}"
    return META_RE.sub(lambda m: m.group(1) + version + m.group(3), text, count=1)


def main(argv: list[str]) -> int:
    with open(INDEX, encoding="utf-8", newline="") as fh:
        text = fh.read()
    if "--check" in argv:
        if is_current(text):
            print(f"page version {current_version(text)} is current")
            return 0
        print(f"page version {current_version(text)} is out of date: run python scripts/stamp_page_version.py")
        return 1
    new = stamp(text)
    if new != text:
        with open(INDEX, "w", encoding="utf-8", newline="") as fh:
            fh.write(new)
    print(f"page version {current_version(new)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
