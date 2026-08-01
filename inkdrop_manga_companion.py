"""Dual-provider manga companion orchestration.

Provider catalog/add behavior remains owned by the existing web/state helpers;
this module only applies exact-title linking and bounded companion refresh policy.
"""

import re
import unicodedata


MIXED_MODEL = "mixed_volume_preferred"
PUBLIC_UNIT_LABEL = "Volumes and chapters"


def normalized_title(value):
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return re.sub(r"[^\w]+", " ", text, flags=re.UNICODE).strip()


def _warning(code, message, **details):
    return {"code": code, "message": message, **details}


def provider_label(provider):
    return {"mangadex": "MangaDex", "comicvine": "ComicVine"}.get(str(provider or "").lower(), str(provider or "Provider"))


def resolve_counterpart(
    *,
    seed_provider,
    seed_result,
    title,
    search_counterpart,
    schedule_counterpart,
    set_unit_model,
):
    """Resolve exactly one normalized-title counterpart without failing the seed add."""
    seed_provider = str(seed_provider or "").strip().lower()
    counterpart_provider = "mangadex" if seed_provider == "comicvine" else "comicvine"
    key = normalized_title(title)
    outcome = {
        "ok": True,
        "status": "warning",
        "seedProvider": seed_provider,
        "counterpartProvider": counterpart_provider,
        "unitModel": MIXED_MODEL,
        "unitLabel": PUBLIC_UNIT_LABEL,
        "warnings": [],
    }
    try:
        set_unit_model(title, MIXED_MODEL)
    except Exception as exc:
        outcome["warnings"].append(_warning("unit_model_persist_failed", f"Series was added, but its volumes-and-chapters preference could not be saved: {exc}"))
    try:
        candidates = list(search_counterpart(title) or [])
    except Exception as exc:
        outcome["warnings"].append(_warning("counterpart_search_failed", f"Series was added, but {provider_label(counterpart_provider)} companion lookup failed: {exc}"))
        return outcome
    exact = [row for row in candidates if isinstance(row, dict) and normalized_title(row.get("name") or row.get("title")) == key]
    if len(exact) != 1:
        code = "counterpart_ambiguous" if len(exact) > 1 else "counterpart_not_found"
        reason = "returned more than one exact title match" if len(exact) > 1 else "did not return one exact title match"
        outcome["warnings"].append(_warning(code, f"Series was added, but {provider_label(counterpart_provider)} {reason}; no companion was linked.", exactMatchCount=len(exact)))
        return outcome
    try:
        scheduled = schedule_counterpart(exact[0]) or {}
        if scheduled.get("ok") is False:
            raise RuntimeError(scheduled.get("error") or "companion work was not scheduled")
    except Exception as exc:
        outcome["warnings"].append(_warning("counterpart_schedule_failed", f"Series was added, but its {provider_label(counterpart_provider)} companion could not be scheduled: {exc}"))
        return outcome
    work_status = str(scheduled.get("status") or "scheduled")
    outcome.update(
        {
            "status": work_status,
            "companion": {
                "provider": counterpart_provider,
                "status": work_status,
                "jobId": scheduled.get("jobId"),
                "metadataId": scheduled.get("metadataId"),
                "linkId": scheduled.get("linkId"),
            },
        }
    )
    return outcome


def refresh_due_mangadex_companions(
    *,
    due_links,
    existing_metadata_ids,
    fetch_feed,
    record_catalog,
    upsert_wanted,
    enqueue_wanted,
    record_refresh,
    covered_metadata_ids=None,
    limit=3,
):
    results = []
    for link in list(due_links(limit=limit) or [])[: max(1, int(limit or 1))]:
        link_id = link.get("id")
        try:
            feed = fetch_feed(link["mangadex_id"], title=link.get("mangadex_title"))
            chapters = [row for row in feed.get("chapters") or [] if isinstance(row, dict)]
            known = existing_metadata_ids(link["mangadex_series_id"], "mangadex")
            new_chapters = [row for row in chapters if str(row.get("metadataId") or "") not in known]
            catalog = record_catalog(link, new_chapters)
            issue_by_metadata = {str(row.get("metadata_id") or ""): row for row in catalog.get("issues") or []}
            covered = set(covered_metadata_ids(link, new_chapters) or []) if covered_metadata_ids else set()
            missing = []
            for chapter in new_chapters:
                if not chapter.get("downloadable"):
                    continue
                if str(chapter.get("metadataId") or "") in covered:
                    continue
                issue = issue_by_metadata.get(str(chapter.get("metadataId") or ""))
                if issue:
                    missing.append({**issue, "raw": chapter})
            wanted = upsert_wanted(link, missing)
            queue = enqueue_wanted(wanted.get("wanted_ids") or [])
            summary = {
                "newChapters": len(new_chapters),
                "coveredChapters": len(covered),
                "wantedAdded": wanted.get("wanted_recorded", 0),
                "queued": queue.get("queued", 0),
            }
            record_refresh(link_id, status="ok", raw=summary)
            results.append({"ok": True, "linkId": link_id, **summary})
        except Exception as exc:
            record_refresh(link_id, status="failed", raw={"error": str(exc)})
            results.append({"ok": False, "linkId": link_id, "error": str(exc)})
    return {"ok": all(row.get("ok") for row in results), "processed": len(results), "results": results}
