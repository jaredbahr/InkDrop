#!/usr/bin/env python3
"""The About page's release catalog enforces field limits at runtime; a
string past its limit throws while the page builds and every release note
disappears (shipped live in alpha.95: one 208-character highlight against
the 200 limit). A syntax check can't catch this -- the limits here mirror
the ones the page enforces, over the same data."""

import json
import re
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / "web" / "static" / "js" / "inkdrop-version-about.js"
LIMITS = {"version": 64, "slug": 64, "title": 100, "summary": 280, "highlight": 200}
MAX_HIGHLIGHTS = 8


def require(condition, message):
    if not condition:
        raise AssertionError(message)


text = SOURCE.read_text(encoding="utf-8")
for name, value in re.findall(r'(\w+):\s*"((?:[^"\\]|\\.)*)"', text):
    if name not in ("version", "slug", "title", "summary"):
        continue
    decoded = json.loads(f'"{value}"')
    require(
        len(decoded) <= LIMITS[name],
        f"{name} exceeds {LIMITS[name]} chars ({len(decoded)}): {decoded[:80]}...",
    )

blocks = re.findall(r"highlights:\s*\[(.*?)\]", text, re.S)
require(blocks, "no highlights found -- the extraction regex rotted")
for block in blocks:
    items = re.findall(r'"((?:[^"\\]|\\.)*)"', block)
    require(len(items) <= MAX_HIGHLIGHTS, f"{len(items)} highlights in one release")
    for item in items:
        decoded = json.loads(f'"{item}"')
        require(
            len(decoded) <= LIMITS["highlight"],
            f"highlight exceeds {LIMITS['highlight']} chars ({len(decoded)}): {decoded[:80]}...",
        )

print("inkdrop-about-release-limits-smoke: ok")
