#!/usr/bin/env python3
"""MangaDex mature content must be ranked, not hidden.

Reported by a tester: searching MangaDex for Berserk returned
nothing. Berserk is rated ``erotica`` on MangaDex, and InkDrop defaulted its
``contentRating[]`` request filter to safe+suggestive -- stricter than MangaDex's
own default, and silent, because a rating excluded in the request is not
down-ranked or flagged, it simply does not exist as far as the user can tell.
That was 8,959 titles wide, not one book.

Offline on purpose: these are the two policy decisions, not MangaDex's data.
"""

import json
import sqlite3

from core import inkdrop_state as state
from core import inkdrop_web as web
from core import inkdrop_source_worker_adapters as adapters


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def score(query, title, rating, *, languages=("en",), status="completed"):
    return web.mangadex_result_score(
        query,
        title,
        {
            "contentRating": rating,
            "availableTranslatedLanguages": list(languages),
            "status": status,
        },
        preferred_languages=["en"],
    )["score"]


# --- the request filter must match MangaDex's own default -------------------

require(
    "erotica" in web.MANGADEX_DEFAULT_CONTENT_RATINGS,
    "erotica is excluded by default again -- acclaimed titles rated erotica "
    "(Berserk) vanish from search entirely",
)
require(
    "pornographic" not in web.MANGADEX_DEFAULT_CONTENT_RATINGS,
    "pornographic must stay out of the default request filter",
)
require(
    tuple(adapters.MANGADEX_DEFAULT_CONTENT_RATINGS) == tuple(web.MANGADEX_DEFAULT_CONTENT_RATINGS),
    "the source worker and the web search must apply the same default ratings",
)

# An operator who narrows the setting is still obeyed; only the fallback moved.
require(
    web.setting_list_values(["safe"], web.MANGADEX_DEFAULT_CONTENT_RATINGS) == ["safe"],
    "an explicit content_ratings setting must win over the default",
)
require(
    adapters._mangadex_content_ratings({"content_ratings": ["safe"]}) == ["safe"],
    "the source worker must honour an explicit content_ratings setting",
)
require(
    adapters._mangadex_content_ratings({}) == list(web.MANGADEX_DEFAULT_CONTENT_RATINGS),
    "the source worker must fall back to the shared default",
)

# --- the mature penalty ranks within a tier, never across one ---------------

exact_mature = score("Berserk", "Berserk", "erotica")
partial_safe = score("Berserk", "Berserk Gaiden", "safe")
exact_safe = score("Berserk", "Berserk", "safe")

require(
    exact_mature > partial_safe,
    f"an exact mature match ({exact_mature}) must outrank a partial safe match "
    f"({partial_safe}) -- this is what put the real Berserk fourth",
)
require(
    exact_safe > exact_mature,
    f"an equally exact safe title ({exact_safe}) must still outrank the mature "
    f"one ({exact_mature}); the penalty has to still do something",
)
require(
    web.MANGADEX_MATURE_RATING_PENALTY < 12,
    "the penalty must stay below the narrowest relevance tier gap (exact 100 vs "
    "title-contains-query 88), or it reorders across tiers again",
)

# Pornographic is penalised the same way for anyone who opts it back in.
require(
    score("Berserk", "Berserk", "pornographic") == exact_mature,
    "pornographic must carry the same ranking penalty as erotica",
)

# --- the corrected default must reach installs that persisted the old one ----
#
# Changing the fallback alone did nothing on production: first-run seeding writes
# content_ratings into provider_configs, so the corrected build still searched with
# ["safe", "suggestive"] and Berserk was still missing. Caught by deploying it.

def mangadex_db(ratings):
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    state.init_schema(con)
    if ratings is not None:
        con.execute(
            "insert into provider_configs(id, provider_type, display_name, settings_json) "
            "values('mangadex','download_source','MangaDex',?)",
            (json.dumps({"content_ratings": ratings, "translated_languages": ["en"]}),),
        )
    con.execute("delete from schema_meta where key='mangadex_content_ratings_erotica_default'")
    return con


def stored_ratings(con):
    row = con.execute("select settings_json from provider_configs where id='mangadex'").fetchone()
    return json.loads(row["settings_json"])["content_ratings"]


def stored_settings(con):
    row = con.execute("select settings_json from provider_configs where id='mangadex'").fetchone()
    return json.loads(row["settings_json"])


con = mangadex_db(["safe", "suggestive"])
state.upgrade_mangadex_default_content_ratings(con)
require(
    stored_ratings(con) == list(web.MANGADEX_DEFAULT_CONTENT_RATINGS),
    "an install still holding the superseded default must be upgraded, or the fix "
    "never reaches anyone who already ran InkDrop",
)
require(
    stored_settings(con).get("translated_languages") == ["en"],
    "the migration must not disturb the provider's other settings",
)

con = mangadex_db(["safe"])
state.upgrade_mangadex_default_content_ratings(con)
require(stored_ratings(con) == ["safe"], "a deliberately narrowed setting must be left alone")

con = mangadex_db(["safe", "suggestive", "erotica", "pornographic"])
state.upgrade_mangadex_default_content_ratings(con)
require(
    stored_ratings(con) == ["safe", "suggestive", "erotica", "pornographic"],
    "a deliberately broadened setting must be left alone",
)

# Runs once: narrowing back after the upgrade has to stick.
con = mangadex_db(["safe", "suggestive"])
state.upgrade_mangadex_default_content_ratings(con)
con.execute(
    "update provider_configs set settings_json=? where id='mangadex'",
    (json.dumps({"content_ratings": ["safe", "suggestive"]}),),
)
state.upgrade_mangadex_default_content_ratings(con)
require(
    stored_ratings(con) == ["safe", "suggestive"],
    "the upgrade must be one-shot, or it overrides the operator every startup",
)

# A database with no MangaDex row at all must not raise.
state.upgrade_mangadex_default_content_ratings(mangadex_db(None))

# The upgrade must actually be WIRED INTO schema init, not merely correct when
# called. An earlier version of this file called it directly, so deleting the call
# from init_schema left the suite green while every install stayed broken.
con = mangadex_db(["safe", "suggestive"])
state.init_schema(con)
require(
    stored_ratings(con) == list(web.MANGADEX_DEFAULT_CONTENT_RATINGS),
    "init_schema must run the content-ratings upgrade; a correct function nothing "
    "calls fixes nobody",
)

print("inkdrop mangadex content rating smoke: PASS")
