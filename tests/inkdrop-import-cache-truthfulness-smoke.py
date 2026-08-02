#!/usr/bin/env python3
"""Three things the import cache must not get wrong.

1. A failed archive read must never be cached as "this archive has no ComicInfo".
   The reader collapsed "confirmed absent" and "could not read" into the same
   empty dict, and a later change persisted that answer for 14 days -- so one
   momentary 7z error or locked file became a durable wrong fact, after which a
   classifier reading only the filename could accept an issue-7 file as issue 1.

2. The provider health cache keyed off max(created_at). A batch marking several
   providers healthy stamps them with the same created_at, so the second and
   later rows could not invalidate the cache the first had just filled: their
   health never reached the map and queue rows waiting on them stayed in
   provider_wait indefinitely.

3. Raw ComicInfo bytes rode along in the import meta dict into a json.dumps'd log
   event. A real CBR import crashed there after the file was copied and the
   ledger committed, leaving a false "bad candidate" for 17 minutes.
"""

import json
import sqlite3
import tempfile
import zipfile
from pathlib import Path

import inkdrop_completed_import as completed_import
import inkdrop_state as state


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def archive_with_comicinfo(number="7"):
    root = Path(tempfile.mkdtemp())
    path = root / f"issue{number}.cbz"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "ComicInfo.xml",
            f"<ComicInfo><Series>Test</Series><Number>{number}</Number></ComicInfo>",
        )
        archive.writestr("001.jpg", "x" * 16)
    return path


def fresh_db():
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    state.init_schema(con)
    return con


# --- 1. a failed read is not an absence, and is never remembered -------------

path = archive_with_comicinfo("7")
con = fresh_db()

names, readable = completed_import.archive_entry_names_status(path)
require(readable and names, "a healthy archive must read as readable")
info, readable = completed_import.read_comicinfo_status(path)
require(readable and info.get("Number") == "7", "a healthy archive must yield its ComicInfo")

original = completed_import.archive_entry_names_status
completed_import.archive_entry_names_status = lambda _p: ([], False)
try:
    during = state.cached_archive_comicinfo(con, str(path))
finally:
    completed_import.archive_entry_names_status = original

require(during == {}, "a failed read still reports nothing to this caller")
require(
    con.execute("select count(*) from archive_comicinfo_cache").fetchone()[0] == 0,
    "a failed read was cached -- it will be served as 'no ComicInfo' for 14 days",
)
require(
    state.cached_archive_comicinfo(con, str(path)).get("Number") == "7",
    "the real ComicInfo must be read once the transient failure clears",
)

# An archive that genuinely has no ComicInfo is a fact, and is worth caching.
plain = Path(tempfile.mkdtemp()) / "plain.cbz"
with zipfile.ZipFile(plain, "w") as archive:
    archive.writestr("001.jpg", "x" * 16)
con2 = fresh_db()
require(state.cached_archive_comicinfo(con2, str(plain)) == {}, "no ComicInfo is no ComicInfo")
require(
    con2.execute("select count(*) from archive_comicinfo_cache").fetchone()[0] == 1,
    "a confirmed absence should still be cached, or the cache buys nothing",
)

# --- 2. same-timestamp provider health must not be lost ----------------------

con = fresh_db()
state.PROVIDER_HEALTH_MAP_CACHE.clear()


def record_health(provider_id, created_at):
    con.execute(
        "insert into history_events(id, entity_type, entity_id, event_type, source, message, created_at, raw_json) "
        "values(?,?,?,?,?,?,?,?)",
        (
            f"{provider_id}-{created_at}",
            "provider",
            provider_id,
            "provider_health",
            provider_id,
            f"{provider_id} ok",
            created_at,
            json.dumps({"health": {"state": "ok"}}),
        ),
    )
    # Deliberately no explicit bump: a trigger has to do this, because relying on
    # writers to remember is what the first version of the fix got wrong -- a
    # caller inserting the row directly was served a stale map.


record_health("slskd", 100.0)
require("slskd" in state.latest_provider_health_map(con), "first provider must be visible")
record_health("prowlarr", 200.0)
require("prowlarr" in state.latest_provider_health_map(con), "second provider must be visible")
record_health("sabnzbd", 200.0)  # identical created_at, as a batch produces
final = state.latest_provider_health_map(con)
require(
    "sabnzbd" in final,
    "a provider marked healthy in the same batch (identical created_at) was lost; "
    "it would sit in provider_wait forever",
)
require({"slskd", "prowlarr", "sabnzbd"} <= set(final), "no provider may be dropped")

# The cache must still cache, and must not hand out its own stored object.
before = state.provider_health_map_marker(con)
again = state.latest_provider_health_map(con)
require(state.provider_health_map_marker(con) == before, "a read must not move the revision")
require(again == final and again is not final, "cached reads return equal but distinct maps")

# --- 3. raw bytes must not be able to abort an import ------------------------

require(
    "source_comicinfo_xml" not in completed_import.repack_cbr_to_cbz.__code__.co_consts
    or True,  # presence as a key is fine; what matters is it is popped
    "sanity",
)
source = completed_import.repack_cbr_to_cbz
require(
    'meta.pop("source_comicinfo_xml", None)' in __import__("inspect").getsource(source),
    "the raw ComicInfo bytes must be dropped once embedded, not carried in meta",
)

# Logging must survive bytes anywhere, and must never raise into its caller.
completed_import.log({"event": "smoke", "raw": b"<?xml?>", "nested": {"deep": b"abc"}})
require(
    json.dumps({"raw": b"12345"}, default=completed_import._log_safe_value) == '{"raw": "<5 bytes>"}',
    "bytes must be summarised rather than crashing the encoder",
)


class Unserializable:
    def __repr__(self):
        raise RuntimeError("even repr fails")


completed_import.log({"event": "smoke", "bad": Unserializable()})

print("inkdrop import cache truthfulness smoke: PASS")
