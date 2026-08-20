#!/usr/bin/env python3
"""Write generated text artifacts with LF endings on every host OS.

`Path.write_text()` and a bare `open(path, "w")` create a text stream with
`newline=None`, which on Windows silently rewrites every LF the caller
emits into CRLF. The translation is invisible locally -- the JSON still
parses, the report still reads fine -- and only shows up once the artifact
reaches somewhere that compares bytes.

That is exactly what happened to the public export: running
tools/inkdrop_public_repo_export.py from Windows produced a
PUBLIC_REPO_MANIFEST.json whose 1822 line endings were all CRLF, which
would land in the public repo as a ~3600-line diff against the same
manifest generated on Linux, for a publish run that changed nothing.

Every generated artifact in the export/publish pipeline has the same
exposure, so the fix is a single writer the whole pipeline shares rather
than a per-call `newline=` argument that the next writer forgets. Bytes
are formed in memory and handed to `write_bytes()`, so no text stream --
and therefore no newline translation -- is ever involved.
"""

from __future__ import annotations

from pathlib import Path

__all__ = ["normalize_lf", "write_text_lf"]


def normalize_lf(text: str) -> str:
    """`text` with CRLF and lone-CR endings collapsed to LF.

    Callers routinely pass text they captured from a subprocess or read
    back off disk, which can already carry CRLF; normalizing here means
    the artifact is LF-only regardless of where its content came from,
    not merely free of endings this process introduced.
    """
    return text.replace("\r\n", "\n").replace("\r", "\n")


def write_text_lf(path, text: str, *, encoding: str = "utf-8") -> Path:
    """Write `text` to `path` with LF endings, on Windows as on Linux."""
    target = Path(path)
    target.write_bytes(normalize_lf(text).encode(encoding))
    return target
