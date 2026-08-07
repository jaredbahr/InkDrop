#!/usr/bin/env python3
"""ComicVine API key resolution must never fall back to reading the dead
Kapowarr.db file.

Root cause: InkDrop's own native ComicVine provider setting was empty in
production, and comicvine_edition_target_has_standalone_alternative()
(inkdrop_slskd_source_probe.py) plus load_comicvine_key()/
comicvine_provider_runtime_status() (inkdrop_web.py) all silently read a
real, working ComicVine API key out of Kapowarr's frozen, abandoned
database as a fallback. That key was moved into InkDrop's own provider
setting and the fallback code removed, so InkDrop resolves its ComicVine
key from its own settings only.
"""

from core import inkdrop_slskd_source_probe as probe
from core import inkdrop_web


def require(condition, message):
    if not condition:
        raise AssertionError(message)


# Neither module may reference a Kapowarr database path anymore in the
# ComicVine key resolution path.
import inspect

probe_source = inspect.getsource(probe.comicvine_edition_target_has_standalone_alternative)
require(
    "kapowarr" not in probe_source.lower(),
    f"comicvine_edition_target_has_standalone_alternative still references Kapowarr:\n{probe_source}",
)

web_key_source = inspect.getsource(inkdrop_web.load_comicvine_key)
require(
    "kapowarr" not in web_key_source.lower(),
    f"load_comicvine_key still references Kapowarr:\n{web_key_source}",
)

web_status_source = inspect.getsource(inkdrop_web.comicvine_provider_runtime_status)
require(
    "kapowarr" not in web_status_source.lower(),
    f"comicvine_provider_runtime_status still references Kapowarr:\n{web_status_source}",
)

print("inkdrop-no-kapowarr-comicvine-key-fallback-smoke: ok")
