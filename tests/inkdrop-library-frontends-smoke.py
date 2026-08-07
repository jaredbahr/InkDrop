#!/usr/bin/env python3
"""Smoke checks for InkDrop library frontend sync helpers."""

from pathlib import Path

from core import inkdrop_library_frontends


ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "core" / "inkdrop_web.py"
IMPORTER = ROOT / "core" / "inkdrop_completed_import.py"
PACK_IMPORT = ROOT / "core" / "inkdrop_pack_import.py"
RECONCILE = ROOT / "core" / "inkdrop_reconcile_imports.py"
PLAN = ROOT / "docs" / "inkdrop" / "folder-based-completion-plan.md"


def fail(message):
    raise AssertionError(message)


def require(text, needle, label):
    if needle not in text:
        fail(f"missing {label}: {needle}")


def main():
    calls = []
    logs = []

    def kavita_scan(folder, force_library_scan=False):
        calls.append(("kavita", str(folder), bool(force_library_scan)))
        return {"folder": str(folder), "status_code": 200, "mode": "folder_scan"}

    def load_komga_settings():
        return {"enabled": True}

    def komga_scan(folder):
        calls.append(("komga", str(folder), False))
        return {"folder": str(folder), "status_code": 202, "mode": "library_scan"}

    sync = inkdrop_library_frontends.sync_library_frontends(
        ["/library/A", "/library/A/", "/library/B"],
        frontend_sync_enabled=True,
        kavita_scan_enabled=True,
        trigger_kavita_scan=kavita_scan,
        load_komga_settings=load_komga_settings,
        trigger_komga_scan=komga_scan,
        force_library_scan_folders=["/library/B"],
        log_event=logs.append,
        event_prefix="smoke_",
    )
    if sync.get("requested") != 4:
        fail(f"enabled sync should request two frontends for two unique folders: {sync}")
    if calls != [
        ("kavita", "/library/A", False),
        ("kavita", "/library/B", True),
        ("komga", "/library/A", False),
        ("komga", "/library/B", False),
    ]:
        fail(f"frontend sync calls were not deterministic or force-aware: {calls}")
    if not sync.get("library_scan_tasks") or set(sync["library_scan_tasks"]) != {"kavita", "komga"}:
        fail(f"sync did not expose grouped library scan tasks: {sync}")
    if not any(row.get("event") == "smoke_kavita_scan_folder_queued" for row in logs):
        fail(f"sync did not log Kavita scan events: {logs}")
    if not any(row.get("event") == "smoke_komga_scan_folder_queued" for row in logs):
        fail(f"sync did not log Komga scan events: {logs}")

    disabled = inkdrop_library_frontends.sync_library_frontends(
        ["/library/C"],
        frontend_sync_enabled=False,
        kavita_scan_enabled=True,
        trigger_kavita_scan=kavita_scan,
        load_komga_settings=load_komga_settings,
        trigger_komga_scan=komga_scan,
    )
    if disabled.get("requested") != 0:
        fail(f"disabled frontend sync should not request scans: {disabled}")
    for provider in ("kavita", "komga"):
        rows = disabled.get(provider) or []
        if not rows or rows[0].get("reason") != "frontend_sync_disabled":
            fail(f"disabled sync did not return {provider} skip evidence: {disabled}")

    kavita_disabled = inkdrop_library_frontends.sync_library_frontends(
        ["/library/D"],
        frontend_sync_enabled=True,
        kavita_scan_enabled=False,
        trigger_kavita_scan=kavita_scan,
        load_komga_settings=lambda: {"enabled": False},
        trigger_komga_scan=komga_scan,
    )
    if (kavita_disabled.get("kavita") or [{}])[0].get("reason") != "kavita_scan_after_import_disabled":
        fail(f"Kavita-disabled sync did not return adapter skip evidence: {kavita_disabled}")
    if kavita_disabled.get("requested") != 0:
        fail(f"Kavita-disabled and Komga-disabled sync should not request scans: {kavita_disabled}")

    visibility_calls = []
    kavita_wins = inkdrop_library_frontends.check_library_visibility(
        "/library/A/Visible.cbz",
        kavita_enabled=True,
        check_kavita_visibility=lambda path: visibility_calls.append(("kavita", str(path))) or {"visible": True, "status": "library_visible", "kavita_path": "/data/comics/Visible.cbz"},
        komga_enabled=True,
        check_komga_visibility=lambda path: visibility_calls.append(("komga", str(path))) or {"visible": True, "status": "library_visible"},
    )
    if kavita_wins.get("library_visibility_provider") != "kavita" or not kavita_wins.get("kavita_visible"):
        fail(f"Kavita should satisfy library visibility first: {kavita_wins}")
    if visibility_calls != [("kavita", "/library/A/Visible.cbz")]:
        fail(f"Komga should not be queried after Kavita visibility succeeds: {visibility_calls}")

    komga_wins = inkdrop_library_frontends.check_library_visibility(
        "/library/A/Fallback.cbz",
        kavita_enabled=True,
        check_kavita_visibility=lambda path: {"visible": False, "status": "not_visible"},
        komga_enabled=True,
        check_komga_visibility=lambda path: {"visible": True, "status": "library_visible", "book_id": "komga-book"},
    )
    if komga_wins.get("library_visibility_provider") != "komga" or not komga_wins.get("komga_visible"):
        fail(f"Komga should satisfy visibility after Kavita misses: {komga_wins}")
    if komga_wins.get("komga_visibility", {}).get("book_id") != "komga-book":
        fail(f"Komga visibility detail was not preserved: {komga_wins}")

    no_visibility = inkdrop_library_frontends.check_library_visibility(
        "/library/A/Missing.cbz",
        kavita_enabled=False,
        check_kavita_visibility=lambda path: fail("disabled Kavita checker should not be called"),
        komga_enabled=False,
        check_komga_visibility=lambda path: fail("disabled Komga checker should not be called"),
    )
    if no_visibility.get("library_visible") or no_visibility.get("library_visibility_provider"):
        fail(f"disabled visibility adapters should not satisfy visibility: {no_visibility}")

    comic_adapter_path = inkdrop_library_frontends.host_path_to_frontend_path(
        "/library/comics/Series/Series #001.cbz",
        comic_root="/library/comics",
        manga_root="/library/manga",
        frontend_comic_root="/data/comics",
        frontend_manga_root="/data/manga",
    )
    if comic_adapter_path != "/data/comics/Series/Series #001.cbz":
        fail(f"comic frontend path mapping failed: {comic_adapter_path}")
    manga_adapter_path = inkdrop_library_frontends.host_path_to_frontend_path(
        "/library/manga/Gantz - E/Gantz - E c001.cbz",
        comic_root="/library/comics",
        manga_root="/library/manga",
        frontend_comic_root="/data/comics",
        frontend_manga_root="/data/manga",
    )
    if manga_adapter_path != "/data/manga/Gantz - E/Gantz - E c001.cbz":
        fail(f"manga frontend path mapping failed: {manga_adapter_path}")
    ids, media_type = inkdrop_library_frontends.library_ids_for_host_folder(
        "/library/manga/Gantz - E",
        {"manga_library_ids": ["manga-lib"], "comic_library_ids": ["comic-lib"]},
        comic_root="/library/comics",
        manga_root="/library/manga",
    )
    if ids != ["manga-lib"] or media_type != "manga":
        fail(f"library id root selection failed: {ids} {media_type}")

    class FakeKavitaConn:
        def __init__(self):
            self.params = None

        def execute(self, sql, params):
            self.params = params
            return self

        def fetchone(self):
            return (1,) if self.params == ("/data/comics/Series/Series #001.cbz",) else None

    fake_conn = FakeKavitaConn()
    kavita_visible = inkdrop_library_frontends.kavita_file_visible_for_host_path(
        "/library/comics/Series/Series #001.cbz",
        conn=fake_conn,
        comic_root="/library/comics",
        manga_root="/library/manga",
        kavita_comic_root="/data/comics",
        kavita_manga_root="/data/manga",
    )
    if not kavita_visible.get("visible") or kavita_visible.get("provider") != "kavita":
        fail(f"neutral Kavita visibility adapter did not map and query the frontend path: {kavita_visible}")

    series_ids = inkdrop_library_frontends.kavita_series_ids_for_folder(
        "/data/comics/Series",
        [
            (7, "/data/comics/Series", "/data/comics/Series"),
            (8, "/data/comics/Series/Sub", "/data/comics/Series/Sub"),
            (9, "/data/comics/Other", "/data/comics/Other"),
        ],
    )
    if series_ids != [7, 8]:
        fail(f"neutral Kavita series folder matching failed: {series_ids}")
    library_id = inkdrop_library_frontends.kavita_library_id_for_folder(
        "/data/comics/Series/Sub",
        [(1, "/data"), (2, "/data/comics"), (3, "/data/comics/Series")],
    )
    if library_id != 3:
        fail(f"neutral Kavita library root matching should choose the deepest root: {library_id}")

    class FakeKavitaResponse:
        def __init__(self, status_code, payload=None):
            self.status_code = status_code
            self.payload = payload if isinstance(payload, dict) else {}

        def raise_for_status(self):
            if self.status_code >= 400:
                raise RuntimeError(f"HTTP {self.status_code}")

        def json(self):
            return self.payload

    class FakeKavitaRequests:
        def __init__(self):
            self.calls = []

        def post(self, url, **kwargs):
            self.calls.append((url, kwargs))
            if url.endswith("/Library/scan-folder"):
                return FakeKavitaResponse(403, {})
            if url.endswith("/Plugin/authenticate"):
                return FakeKavitaResponse(200, {"token": "plugin-token"})
            if url.endswith("/Series/scan"):
                return FakeKavitaResponse(202, {})
            if url.endswith("/library/scan"):
                return FakeKavitaResponse(200, {})
            return FakeKavitaResponse(500, {})

    old_requests = inkdrop_library_frontends._requests
    fake_requests = FakeKavitaRequests()
    inkdrop_library_frontends._requests = fake_requests
    try:
        kavita_scan = inkdrop_library_frontends.trigger_kavita_scan_folder(
            "/library/comics/Series",
            settings={
                "base_url": "http://kavita.example/api",
                "api_key": "api-key",
                "plugin_name": "inkdrop",
            },
            comic_root="/library/comics",
            manga_root="/library/manga",
            kavita_comic_root="/data/comics",
            kavita_manga_root="/data/manga",
            series_ids_for_folder=lambda folder: [77],
            library_id_for_folder=lambda folder: 5,
        )
    finally:
        inkdrop_library_frontends._requests = old_requests
    if kavita_scan.get("mode") != "series_scan" or kavita_scan.get("series") != [{"series_id": 77, "status_code": 202}]:
        fail(f"neutral Kavita scan adapter did not fall back to series scan after folder auth failure: {kavita_scan}")
    if not any(call[0].endswith("/Plugin/authenticate") for call in fake_requests.calls):
        fail(f"neutral Kavita scan adapter did not authenticate plugin token: {fake_requests.calls}")
    series_scan_call = next((call for call in fake_requests.calls if call[0].endswith("/Series/scan")), None)
    if not series_scan_call or series_scan_call[1].get("headers", {}).get("Authorization") != "Bearer plugin-token":
        fail(f"neutral Kavita series scan did not use plugin auth header: {fake_requests.calls}")

    secret_kavita_key = "kavita-secret-value"

    class FailedKavitaAuthResponse:
        status_code = 401

        def raise_for_status(self):
            raise RuntimeError(
                "HTTP 401 for "
                f"https://operator:password@kavita.example/Plugin/authenticate?apiKey={secret_kavita_key}&pluginName=inkdrop"
            )

    class FailedKavitaAuthRequests:
        def post(self, _url, **_kwargs):
            return FailedKavitaAuthResponse()

    old_requests = inkdrop_library_frontends._requests
    inkdrop_library_frontends._requests = FailedKavitaAuthRequests()
    try:
        try:
            inkdrop_library_frontends.kavita_plugin_token(
                {
                    "base_url": "https://kavita.example",
                    "api_key": secret_kavita_key,
                    "plugin_name": "inkdrop",
                }
            )
            fail("failed Kavita plugin authentication unexpectedly succeeded")
        except RuntimeError as exc:
            auth_error = str(exc)
    finally:
        inkdrop_library_frontends._requests = old_requests
    if secret_kavita_key in auth_error or "operator:password" in auth_error:
        fail(f"Kavita plugin authentication exposed credentials: {auth_error}")
    if "[REDACTED]" not in auth_error:
        fail(f"Kavita plugin authentication did not preserve redacted diagnostics: {auth_error}")

    failure_logs = []
    failed_sync = inkdrop_library_frontends.sync_library_frontends(
        ["/library/A", "/library/B"],
        frontend_sync_enabled=True,
        kavita_scan_enabled=True,
        trigger_kavita_scan=lambda folder, **_kwargs: (_ for _ in ()).throw(
            RuntimeError(f"apiKey={secret_kavita_key} failed for {folder}")
        ),
        load_komga_settings=lambda: {"enabled": True},
        trigger_komga_scan=lambda folder: (_ for _ in ()).throw(
            RuntimeError(f"password=komga-secret-value failed for {folder}")
        ),
        log_event=failure_logs.append,
        event_prefix="failure_",
    )
    failure_text = repr({"sync": failed_sync, "logs": failure_logs})
    if secret_kavita_key in failure_text or "komga-secret-value" in failure_text:
        fail(f"frontend scan failure status or logging exposed credentials: {failure_text}")
    if len(failed_sync.get("errors") or []) != 4:
        fail(f"frontend failures should be returned without escaping or dropping later folders: {failed_sync}")
    if failed_sync.get("requested") != 0:
        fail(f"failed optional frontend scans must not be reported as accepted requests: {failed_sync}")
    if len(failure_logs) != 4:
        fail(f"all isolated frontend failures should retain secret-safe diagnostic events: {failure_logs}")

    secret_komga_password = "komga-basic-secret"

    class PartialKomgaResponse:
        def __init__(self, status_code, url):
            self.status_code = status_code
            self.url = url

        def raise_for_status(self):
            if self.status_code >= 400:
                raise RuntimeError(
                    f"HTTP {self.status_code} password={secret_komga_password} for {self.url}"
                )

    class PartialKomgaRequests:
        def post(self, url, **kwargs):
            library_id = url.split("/libraries/", 1)[1].split("/", 1)[0]
            return PartialKomgaResponse(202 if library_id == "good" else 503, url)

    old_requests = inkdrop_library_frontends._requests
    inkdrop_library_frontends._requests = PartialKomgaRequests()
    try:
        partial_komga = inkdrop_library_frontends.trigger_komga_scan_folder(
            "/library/comics/Series",
            settings={
                "enabled": True,
                "add_after_import": True,
                "base_url": "https://komga.example",
                "username": "operator",
                "password": secret_komga_password,
                "comic_library_ids": ["good", "bad"],
            },
            comic_root="/library/comics",
            manga_root="/library/manga",
        )
    finally:
        inkdrop_library_frontends._requests = old_requests
    if partial_komga.get("status_code") != 202 or len(partial_komga.get("errors") or []) != 1:
        fail(f"Komga partial scan success should retain bounded per-library error evidence: {partial_komga}")
    if secret_komga_password in repr(partial_komga):
        fail(f"Komga partial scan evidence exposed its password: {partial_komga}")

    unavailable_password = "komga-unavailable-secret"

    class UnavailableKomgaRequests:
        def __init__(self):
            self.calls = []

        def get(self, url, **kwargs):
            self.calls.append(("get", url, kwargs))
            raise RuntimeError(f"password={unavailable_password} connection refused")

        def post(self, url, **kwargs):
            self.calls.append(("post", url, kwargs))
            raise RuntimeError(f"password={unavailable_password} connection refused")

    unavailable_settings = {
        "enabled": True,
        "add_after_import": True,
        "base_url": "https://unavailable-komga.example",
        "username": "operator",
        "password": unavailable_password,
        "comic_library_ids": ["comics"],
        "failure_backoff_seconds": 30,
    }
    old_requests = inkdrop_library_frontends._requests
    unavailable_requests = UnavailableKomgaRequests()
    inkdrop_library_frontends._requests = unavailable_requests
    inkdrop_library_frontends._clear_komga_failure(unavailable_settings)
    try:
        first_unavailable = inkdrop_library_frontends.komga_file_visible_for_host_path(
            "/library/comics/Series/Series #001.cbz",
            settings=unavailable_settings,
            comic_root="/library/comics",
            manga_root="/library/manga",
        )
        second_unavailable = inkdrop_library_frontends.komga_file_visible_for_host_path(
            "/library/comics/Series/Series #002.cbz",
            settings=unavailable_settings,
            comic_root="/library/comics",
            manga_root="/library/manga",
        )
        scan_during_backoff = inkdrop_library_frontends.trigger_komga_scan_folder(
            "/library/comics/Series",
            settings=unavailable_settings,
            comic_root="/library/comics",
            manga_root="/library/manga",
        )
    finally:
        inkdrop_library_frontends._clear_komga_failure(unavailable_settings)
        inkdrop_library_frontends._requests = old_requests
    if len(unavailable_requests.calls) != 1:
        fail(
            "one Komga outage should make one bounded probe, not two searches plus a listing per file: "
            f"{unavailable_requests.calls}"
        )
    if first_unavailable.get("reason") != "komga_temporarily_unavailable" or first_unavailable.get("status") != "error":
        fail(f"first Komga outage did not return retryable visibility evidence: {first_unavailable}")
    if second_unavailable.get("reason") != "komga_temporarily_unavailable" or not second_unavailable.get("retry_after_seconds"):
        fail(f"subsequent Komga visibility did not honor the endpoint backoff: {second_unavailable}")
    if not scan_during_backoff.get("skipped") or scan_during_backoff.get("reason") != "komga_temporarily_unavailable":
        fail(f"Komga scan trigger did not preserve explicit retry evidence during backoff: {scan_during_backoff}")
    outage_text = repr((first_unavailable, second_unavailable, scan_during_backoff))
    if unavailable_password in outage_text:
        fail(f"Komga outage evidence exposed its password: {outage_text}")

    komga_disabled = inkdrop_library_frontends.trigger_komga_scan_folder(
        "/library/comics/Series",
        settings={"enabled": False},
        comic_root="/library/comics",
        manga_root="/library/manga",
    )
    if not komga_disabled.get("skipped") or komga_disabled.get("reason") != "komga_disabled":
        fail(f"neutral Komga scan adapter did not preserve disabled skip evidence: {komga_disabled}")

    komga_credentials = inkdrop_library_frontends.komga_file_visible_for_host_path(
        "/library/comics/Series/Series #001.cbz",
        settings={"enabled": True, "username": "", "password": "", "comic_library_ids": ["comic-lib"]},
        comic_root="/library/comics",
        manga_root="/library/manga",
    )
    if komga_credentials.get("reason") != "komga_credentials_missing":
        fail(f"neutral Komga visibility adapter did not preserve credential skip evidence: {komga_credentials}")

    importer = IMPORTER.read_text(encoding="utf-8")
    web = WEB.read_text(encoding="utf-8")
    pack_import = PACK_IMPORT.read_text(encoding="utf-8")
    reconcile = RECONCILE.read_text(encoding="utf-8")
    plan = PLAN.read_text(encoding="utf-8")
    frontends = (ROOT / "core" / "inkdrop_library_frontends.py").read_text(encoding="utf-8")
    require(importer, "import inkdrop_library_frontends", "completed-import frontend helper import")
    require(importer, "def sync_library_frontend_folders", "completed-import frontend sync wrapper")
    require(importer, "def kavita_file_visible_for_host_path", "completed-import Kavita visibility adapter")
    require(importer, "return inkdrop_library_frontends.kavita_file_visible_for_host_path", "completed-import Kavita visibility wrapper")
    require(importer, "return inkdrop_library_frontends.komga_file_visible_for_host_path", "completed-import Komga visibility wrapper")
    require(importer, "check_library_visibility(", "completed-import shared visibility helper call")
    require(importer, "sync_library_frontend_folders(", "completed-import frontend sync call")
    require(pack_import, "def sync_pack_library_frontends", "pack-import frontend sync wrapper")
    require(pack_import, "sync_pack_library_frontends(importer, scan_folders)", "pack-import frontend sync call")
    require(reconcile, "sync_library_frontend_folders", "reconcile timeout recovery frontend sync call")
    require(frontends, "def check_library_visibility", "library frontend visibility helper")
    require(frontends, "def trigger_komga_scan_folder", "neutral Komga scan adapter")
    require(frontends, "def komga_file_visible_for_host_path", "neutral Komga visibility adapter")
    require(frontends, "def kavita_file_visible_for_host_path", "neutral Kavita visibility adapter")
    require(frontends, "def trigger_kavita_scan_folder", "neutral Kavita scan adapter")
    require(frontends, "def kavita_plugin_token", "neutral Kavita plugin auth adapter")
    require(frontends, "def kavita_series_ids_for_folder", "neutral Kavita series folder matcher")
    require(frontends, "def kavita_library_id_for_folder", "neutral Kavita library root matcher")
    require(importer, "return inkdrop_library_frontends.trigger_kavita_scan_folder", "completed-import Kavita scan wrapper")
    require(importer, "return inkdrop_library_frontends.kavita_plugin_token", "completed-import Kavita auth wrapper")
    require(web, "def run_inkdrop_library_frontend_sync", "web operator frontend sync helper")
    require(web, "/api/inkdrop-state/library-frontends/sync", "web operator frontend sync endpoint")
    require(web, "syncSeriesReaderVisibility", "Series detail reader sync action")
    require(web, "library_frontend_sync_requested", "reader sync history event")
    require(web, "frontend_sync_enabled=True", "operator reader sync bypasses automatic after-import toggle")
    require(frontends, "/libraries/{quote(library_id, safe='')}/scan", "neutral Komga library scan endpoint")
    require(frontends, "/Library/scan-folder", "neutral Kavita folder scan endpoint")
    require(plan, "library frontend sync helper", "folder completion plan frontend sync helper note")
    require(plan, "Library frontend visibility now uses the shared adapter boundary", "folder completion plan frontend visibility note")
    print("LIBRARY_FRONTENDS_OK: library frontend sync helper contract is present")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
