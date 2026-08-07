"""Read the .nfo files that ride along with comic/manga pack releases.

A .nfo is a plain text file a release group drops next to the payload. There is
no standard for what goes in one. In this library, real ones fall into exactly
two shapes:

  * Sixteen of nineteen contain one line: the release folder's own name. They
    tell us nothing the folder name did not already tell us.
  * Three contain `ls -R` output -- a per-directory listing of every comic
    file in the pack. Those three are the multi-series weekly bundles, and
    their listings matched the files on disk exactly: 658/658, 710/710 and
    658/658 across DC 2025, Image 2025 and DC 2024. (For DC 2025 that 658 is
    423 .cbz + 224 .cbr + 11 .pdf; the .cbz+.cbr subset alone is 647.)

That second shape is the useful one. Those bundles hold 136, 158 and 166
distinct series, so knowing the contents is the difference between importing
one wanted issue and dragging in a hundred-plus series nobody asked for.

Everything here treats the .nfo as an untrusted string from a stranger. It is
a *claim* about the pack, never proof. Callers get evidence and reason codes;
they do not get a verdict, and nothing in this module is allowed to outrank
what the download client actually reports on disk. Parsing is pure text --
no eval, no archive extraction, no filesystem walking, no network.

Standalone on purpose: nothing imports this yet. See
docs/inkdrop/nfo-pack-monitoring-proposal.md for the wiring plan.
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path

# A .nfo that legitimately lists a huge pack still fits in a few hundred KB.
# The biggest real one here is 44 KB. Anything past a megabyte is not a
# listing we need, and reading it costs more than it can possibly be worth.
MAX_NFO_BYTES = 1_048_576
MAX_NFO_LINES = 20_000
MAX_LINE_CHARS = 1_000

# Extensions that count as a comic the pack could actually deliver. Matches
# COMIC_EXTS in inkdrop_pack_import.py, plus the two rarer archive flavours
# that show up in older scene packs.
COMIC_ENTRY_EXTS = (".cbz", ".cbr", ".pdf", ".cb7", ".cbt", ".epub")

# Encodings to try in order. Scene .nfo files are historically CP437 (that is
# what draws the ASCII-art boxes); modern ones are UTF-8. Latin-1 never fails,
# so it is the backstop that guarantees we always return *something*. Any BOM
# is stripped before this runs, so plain UTF-8 is reported as plain UTF-8.
DECODE_CHAIN = ("utf-8", "cp437", "latin-1")

# `ls -R` writes a "./Some Dir:" header before each directory's contents.
_LS_DIR_HEADER = re.compile(r"^\.?[/\\]?(?P<name>.+?):\s*$")

# Trailing group/scanner credit in a comic filename, e.g. "(Digital) (Pyrate-DCP)".
_TRAILING_PARENS = re.compile(r"\s*\([^()]*\)\s*$")

# "Batman 156", "Akira 01", "Vagabond Vol. 3" -- the issue/volume number that
# separates the series name from everything after it.
_ISSUE_SPLIT = re.compile(
    r"^(?P<series>.+?)\s+(?:(?P<vol_word>v|vol|volume|chapter|ch)\.?\s*)?(?P<number>\d{1,4})(?:\.\d+)?\s*(?:\(|$)",
    re.IGNORECASE,
)


class NfoTooLarge(ValueError):
    """The file is past MAX_NFO_BYTES, so we refused to read it."""


def _strip_controls(text):
    """Drop control characters that would corrupt logs or terminal output.

    Keeps newline and tab. Everything else in the C0/C1 ranges goes, along
    with anything Unicode calls a format character -- that covers the
    bidi-override and zero-width tricks that make a filename render as one
    thing and compare as another.
    """
    out = []
    for ch in text:
        if ch in "\n\t":
            out.append(ch)
            continue
        if ch == "\r":
            continue
        category = unicodedata.category(ch)
        if category in ("Cc", "Cf", "Co", "Cs"):
            continue
        out.append(ch)
    return "".join(out)


def looks_binary(blob):
    """True if these bytes are not text we should try to decode.

    A NUL byte in the first chunk is the reliable tell -- no text .nfo has
    one, and every mislabelled JPEG/executable does.
    """
    return b"\x00" in blob[:8192]


def decode_nfo_bytes(blob):
    """Turn raw .nfo bytes into clean text plus a note on how we got there.

    Returns (text, meta). `meta["encoding"]` says which member of DECODE_CHAIN
    won; `meta["replaced_chars"]` counts characters we could not represent.
    Never raises on bad bytes -- latin-1 accepts anything.
    """
    meta = {
        "byte_count": len(blob),
        "encoding": None,
        "binary": False,
        "replaced_chars": 0,
        "truncated_lines": False,
    }
    if looks_binary(blob):
        meta["binary"] = True
        return "", meta

    if blob.startswith(b"\xef\xbb\xbf"):
        blob = blob[3:]
        meta["had_bom"] = True

    text = None
    for encoding in DECODE_CHAIN:
        try:
            text = blob.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
        meta["encoding"] = encoding
        break
    if text is None:
        text = blob.decode("latin-1", errors="replace")
        meta["encoding"] = "latin-1"
        meta["replaced_chars"] = text.count("�")

    text = _strip_controls(text)

    lines = text.split("\n")
    if len(lines) > MAX_NFO_LINES:
        lines = lines[:MAX_NFO_LINES]
        meta["truncated_lines"] = True
    clipped = []
    for line in lines:
        if len(line) > MAX_LINE_CHARS:
            line = line[:MAX_LINE_CHARS]
            meta["truncated_lines"] = True
        clipped.append(line.rstrip())
    return "\n".join(clipped), meta


def read_nfo(path):
    """Read one .nfo from disk, size-capped. Returns (text, meta).

    Raises NfoTooLarge past MAX_NFO_BYTES. Any other read failure comes back
    as empty text with meta["error"] set, because a missing or unreadable
    .nfo is a normal condition, not an exception the caller should handle.
    """
    path = Path(path)
    try:
        size = path.stat().st_size
    except OSError as exc:
        return "", {"error": f"{type(exc).__name__}: {exc}", "byte_count": 0}
    if size > MAX_NFO_BYTES:
        raise NfoTooLarge(f"{path.name} is {size} bytes, over the {MAX_NFO_BYTES} cap")
    try:
        blob = path.read_bytes()
    except OSError as exc:
        return "", {"error": f"{type(exc).__name__}: {exc}", "byte_count": 0}
    text, meta = decode_nfo_bytes(blob)
    meta["source_name"] = path.name
    return text, meta


def normalize(value):
    """Lowercase alphanumeric tokens joined by single spaces.

    Deliberately identical to inkdrop_pack_import.normalize so keys built here
    compare cleanly against keys built there.
    """
    return " ".join(re.findall(r"[a-z0-9]+", str(value or "").lower()))


def _entry_extension(line):
    lowered = line.lower()
    for ext in COMIC_ENTRY_EXTS:
        if lowered.endswith(ext):
            return ext
    return None


def _is_path_unsafe(name):
    """True if this listed name tries to escape the pack directory.

    Nothing here writes files, but a listing entry flows onward into review
    UIs and logs, and a `../../etc` entry is a signal the .nfo is hostile or
    broken. Callers should treat any hit as a reason to distrust the file.
    """
    if not name:
        return True
    if name.startswith(("/", "\\")):
        return True
    if re.match(r"^[a-zA-Z]:[\\/]", name):
        return True
    parts = re.split(r"[\\/]", name)
    return ".." in parts


def split_comic_filename(name):
    """Pull (series, number) out of a release filename. Best effort.

    "Batman 156 (2025) (Digital) (Pyrate-DCP).cbz" -> ("Batman", "156")
    "Vagabond Vol. 3 (Coloured) (TwZ).cbz"         -> ("Vagabond", "3")

    Returns (series, number) with either or both None when the name does not
    fit the usual shape. This is intentionally simpler than InkDrop's real
    filename parser -- when this module gets wired in, the caller should hand
    entries to the existing parser instead of relying on this one.
    """
    stem = name
    for ext in COMIC_ENTRY_EXTS:
        if stem.lower().endswith(ext):
            stem = stem[: -len(ext)]
            break
    stem = stem.replace("_", " ").strip()

    match = _ISSUE_SPLIT.match(stem)
    if not match:
        # No number at all -- strip trailing credits and call the rest a series.
        cleaned = stem
        while True:
            stripped = _TRAILING_PARENS.sub("", cleaned)
            if stripped == cleaned:
                break
            cleaned = stripped
        cleaned = cleaned.strip(" -.")
        return (cleaned or None), None

    series = match.group("series").strip(" -.")
    number = match.group("number").lstrip("0") or "0"
    return (series or None), number


def parse_nfo(text, *, release_name=None):
    """Read .nfo text into a structured, still-untrusted view of the pack.

    `release_name` is the folder or release title we already know. Passing it
    lets the parser recognise the very common case where the whole .nfo is
    just that name echoed back, and report `kind="echo"` instead of inventing
    a one-item manifest out of it.

    Returned dict:
      kind          -- "empty" | "echo" | "manifest" | "descriptive"
      header        -- first non-blank line, or None
      entries       -- list of {name, directory, series, number, ext}
      directories   -- directory names the listing described
      series_index  -- {normalized series: [entry, ...]}
      warnings      -- reason codes describing everything that looked wrong
    """
    warnings = []
    lines = [line for line in (text or "").split("\n")]
    non_blank = [line.strip() for line in lines if line.strip()]

    result = {
        "kind": "empty",
        "header": non_blank[0] if non_blank else None,
        "entries": [],
        "directories": [],
        "series_index": {},
        "warnings": warnings,
        "line_count": len(lines),
    }
    if not non_blank:
        return result

    # The self-referential case: the .nfo lists itself alongside real entries.
    # Recognising it keeps the listed-vs-present count honest.
    self_names = set()
    if release_name:
        self_names.add(normalize(release_name))
        self_names.add(normalize(f"{release_name}.nfo"))

    entries = []
    directories = []
    current_dir = None
    saw_self_reference = False

    for raw in lines:
        line = raw.strip()
        if not line:
            continue

        header = _LS_DIR_HEADER.match(line)
        if header and _entry_extension(line) is None:
            current_dir = header.group("name").strip()
            if current_dir and current_dir not in directories:
                directories.append(current_dir)
            continue

        ext = _entry_extension(line)
        if ext is None:
            if line.lower().endswith(".nfo") and normalize(line) in self_names:
                saw_self_reference = True
            continue

        name = line
        if _is_path_unsafe(name):
            warnings.append("nfo_entry_path_unsafe")
            continue

        # A release folder can be named "...(Digital).pdf" -- five of them are
        # in this library. Echoed into the .nfo, that line looks exactly like a
        # one-file manifest, and believing it would invent contents the pack
        # has not promised. A line that only repeats the release name is an
        # echo, whatever it ends in.
        if normalize(name) in self_names:
            saw_self_reference = True
            continue

        series, number = split_comic_filename(Path(name.replace("\\", "/")).name)
        entries.append(
            {
                "name": name,
                "directory": current_dir,
                "series": series,
                "number": number,
                "ext": ext,
            }
        )

    if saw_self_reference and entries:
        warnings.append("nfo_lists_itself")

    if entries:
        result["kind"] = "manifest"
        if len(entries) == 1 and not directories:
            # One line naming one file is not much of a table of contents.
            # Callers should not treat it as proof of what the pack holds.
            warnings.append("nfo_single_entry_manifest")
    elif len(non_blank) == 1 and release_name and normalize(non_blank[0]) == normalize(release_name):
        result["kind"] = "echo"
    elif len(non_blank) == 1:
        result["kind"] = "echo"
        warnings.append("nfo_single_line_unverified")
    else:
        result["kind"] = "descriptive"

    unnamed = sum(1 for entry in entries if not entry["series"])
    if unnamed:
        warnings.append("nfo_entries_without_series")
    if entries and not directories:
        warnings.append("nfo_flat_listing")

    index = {}
    for entry in entries:
        key = normalize(entry["series"])
        if not key:
            continue
        index.setdefault(key, []).append(entry)

    result["entries"] = entries
    result["directories"] = directories
    result["series_index"] = index
    return result


def series_summary(parsed, limit=None):
    """Series in the manifest, most issues first.

    This is the fan-out surface: how many distinct series a pack would drag in
    if it were imported whole.
    """
    index = (parsed or {}).get("series_index") or {}
    rows = []
    for key, entries in index.items():
        numbers = [entry["number"] for entry in entries if entry["number"]]
        rows.append(
            {
                "series_key": key,
                "series": entries[0]["series"],
                "count": len(entries),
                "numbers": sorted(set(numbers), key=lambda value: (len(value), value)),
            }
        )
    rows.sort(key=lambda row: (-row["count"], row["series_key"]))
    return rows[:limit] if limit else rows


def compare_manifest_to_listing(parsed, actual_names):
    """Check the .nfo's claim against the file list we actually observed.

    `actual_names` is whatever the download client, archive index, or disk
    scan reported -- the thing we genuinely know. Agreement is what upgrades
    the .nfo from "a stranger's claim" to "corroborated evidence"; anything
    else leaves it as a claim.

    Returns agreement ratio plus both difference sets.
    """
    claimed = {Path(entry["name"].replace("\\", "/")).name for entry in (parsed or {}).get("entries") or []}
    observed = {Path(str(name).replace("\\", "/")).name for name in (actual_names or [])}
    observed = {name for name in observed if _entry_extension(name)}

    if not claimed:
        return {
            "status": "no_manifest",
            "agreement": 0.0,
            "claimed_count": 0,
            "observed_count": len(observed),
            "claimed_not_observed": [],
            "observed_not_claimed": sorted(observed),
        }

    both = claimed & observed
    missing = sorted(claimed - observed)
    extra = sorted(observed - claimed)
    union = claimed | observed
    agreement = len(both) / len(union) if union else 0.0

    if not observed:
        status = "nothing_observed_yet"
    elif not missing and not extra:
        status = "exact"
    elif agreement >= 0.95:
        status = "close"
    elif agreement >= 0.5:
        status = "partial"
    else:
        status = "contradicted"

    return {
        "status": status,
        "agreement": round(agreement, 4),
        "claimed_count": len(claimed),
        "observed_count": len(observed),
        "claimed_not_observed": missing,
        "observed_not_claimed": extra,
    }


def evaluate_pack_scope(parsed, target_titles, *, comparison=None, target_aliases=None):
    """Say what the .nfo implies about grabbing this pack for these targets.

    `target_titles` are the series we actually want. `comparison` is the
    result of compare_manifest_to_listing when we have observed files to check
    against -- without it, every finding stays advisory.

    Returns evidence, not a decision. `confidence` is "corroborated" only when
    an observed file listing backed the manifest up; otherwise it is "claimed"
    and the caller must not let it outrank source evidence. Reason codes are
    meant to be read straight into the existing pack review UI.
    """
    parsed = parsed or {}
    reason_codes = []

    def finish(payload):
        """Every exit carries the parser's own warnings.

        These include nfo_entry_path_unsafe, which matters most on exactly the
        paths that return early -- a hostile .nfo is usually also a
        contradicted one, and dropping its warnings there would hide the
        evidence at the moment it counts.
        """
        codes = list(payload.get("reason_codes") or [])
        for warning in parsed.get("warnings") or []:
            if warning not in codes:
                codes.append(warning)
        payload["reason_codes"] = codes
        return payload

    wanted_keys = {normalize(title) for title in (target_titles or []) if normalize(title)}
    for alias in target_aliases or []:
        key = normalize(alias)
        if key:
            wanted_keys.add(key)

    summary = series_summary(parsed)
    kind = parsed.get("kind")

    if kind != "manifest":
        reason_codes.append(f"nfo_{kind or 'absent'}_no_contents")
        return finish({
            "usable": False,
            "confidence": "none",
            "reason_codes": reason_codes,
            "matched_series": [],
            "unrelated_series": [],
            "unrelated_series_count": 0,
            "matched_entry_count": 0,
            "total_entry_count": len(parsed.get("entries") or []),
        })

    matched = [row for row in summary if row["series_key"] in wanted_keys]
    unrelated = [row for row in summary if row["series_key"] not in wanted_keys]

    matched_entries = sum(row["count"] for row in matched)
    total_entries = len(parsed.get("entries") or [])

    confidence = "claimed"
    status = (comparison or {}).get("status")
    if status in ("exact", "close"):
        confidence = "corroborated"
        reason_codes.append("nfo_manifest_matches_observed_files")
    elif status == "partial":
        reason_codes.append("nfo_manifest_partially_matches_observed_files")
    elif status == "contradicted":
        reason_codes.append("nfo_manifest_contradicts_observed_files")
        return finish({
            "usable": False,
            "confidence": "contradicted",
            "reason_codes": reason_codes,
            "matched_series": matched,
            "unrelated_series": unrelated,
            "unrelated_series_count": len(unrelated),
            "matched_entry_count": matched_entries,
            "total_entry_count": total_entries,
        })
    else:
        reason_codes.append("nfo_manifest_unverified")

    if wanted_keys and not matched:
        reason_codes.append("nfo_manifest_lacks_target_series")
    elif matched:
        reason_codes.append("nfo_manifest_contains_target_series")

    if len(unrelated) >= 1 and matched:
        reason_codes.append("nfo_manifest_multi_series_pack")

    return finish({
        "usable": True,
        "confidence": confidence,
        "reason_codes": reason_codes,
        "matched_series": matched,
        "unrelated_series": unrelated[:50],
        "unrelated_series_count": len(unrelated),
        "matched_entry_count": matched_entries,
        "total_entry_count": total_entries,
        "wanted_share": round(matched_entries / total_entries, 4) if total_entries else 0.0,
    })
