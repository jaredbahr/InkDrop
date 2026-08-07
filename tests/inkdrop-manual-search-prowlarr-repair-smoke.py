#!/usr/bin/env python3
"""Regression for Manual Search's shared Prowlarr credential/search contract."""

from __future__ import annotations

import io
import json
import os
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path
from unittest import mock

from core import inkdrop_manual_search as manual
from core import inkdrop_manual_search_executor as executor
from core import inkdrop_source_worker_adapters as adapters
from core import inkdrop_source_worker_cli as cli
from core import inkdrop_source_worker_http as source_http


def require(value, message):
    if not value:
        raise AssertionError(message)


class FixtureResponse:
    status = 200
    headers = {"Content-Type": "application/json"}

    def __init__(self, body=b"[]"):
        self._body = io.BytesIO(body)

    def read(self, size=-1):
        return self._body.read(size)

    def close(self):
        return None


def fixture_db(root: Path) -> Path:
    path = root / "inkdrop.sqlite3"
    with closing(sqlite3.connect(path)) as con:
        con.execute("create table provider_configs(id text primary key, settings_json text not null)")
        con.execute(
            "insert into provider_configs(id,settings_json) values(?,?)",
            (
                "prowlarr",
                json.dumps({"api_key": "", "secret_fields": ["api_key"]}),
            ),
        )
        con.commit()
    return path


def provider_result(provider_id, status, *, reason="", candidates=None, fetch=None):
    candidates = list(candidates or [])
    return {
        "provider_id": provider_id,
        "result_status": status,
        "reason": reason,
        "fetch": fetch or {},
        "runtime_results": [
            {
                "query": "private query",
                "candidates": candidates,
                "verdicts": [{} for _ in candidates],
                "attempts": [{} for _ in candidates],
            }
        ] if candidates else [],
    }


def run_group(results):
    rows = [{"provider_id": provider_id} for provider_id in results]
    jobs = {
        provider_id: {
            "provider_id": provider_id,
            "adapter_family": "prowlarr_indexer",
            "job_status": "ready",
            "fetch_plan": {"adapter_family": "prowlarr_indexer", "requests": [{}]},
        }
        for provider_id in results
    }

    def job_for_row(row, *_args, **_kwargs):
        return jobs[row["provider_id"]]

    def result_for_job(job, **_kwargs):
        return results[job["provider_id"]]

    with (
        mock.patch.object(executor, "provider_rows", return_value=rows),
        mock.patch.object(executor, "_http_client", return_value=lambda *_args, **_kwargs: {}),
        mock.patch.object(executor.inkdrop_source_worker_plan, "source_worker_plan_for_row", return_value={}),
        mock.patch.object(executor.inkdrop_source_worker_jobs, "source_job_for_row", side_effect=job_for_row),
        mock.patch.object(executor.inkdrop_source_worker_jobs, "run_source_job", side_effect=result_for_job),
    ):
        return executor.run_provider("fixture.sqlite3", "prowlarr", {}, [{"query": "private query"}], {})


def main():
    with tempfile.TemporaryDirectory(prefix="inkdrop-prowlarr-manual-") as temp:
        root = Path(temp)
        db_path = fixture_db(root)
        config_path = root / "config.xml"
        config_path.write_text("<Config><ApiKey>fixture-prowlarr-secret</ApiKey></Config>", encoding="utf-8")
        environ = {"INKDROP_PROWLARR_CONFIG": str(config_path)}

        values = cli.provider_secret_values(db_path, environ=environ)
        require(values.get("prowlarr_api_key") == "fixture-prowlarr-secret", "empty DB api_key must use the existing Prowlarr config.xml fallback")
        with closing(sqlite3.connect(db_path)) as con:
            persisted = json.loads(con.execute("select settings_json from provider_configs where id='prowlarr'").fetchone()[0])
        require(persisted.get("api_key") == "", "config fallback must remain memory-only and never persist the API key")

        with closing(sqlite3.connect(db_path)) as con:
            con.execute(
                "update provider_configs set settings_json=? where id='prowlarr'",
                (json.dumps({"api_key": "fixture-db-secret", "secret_fields": ["api_key"]}),),
            )
            con.commit()
        values = cli.provider_secret_values(db_path, environ=environ)
        require(values.get("prowlarr_api_key") == "fixture-db-secret", "stored provider secret must take precedence over config.xml fallback")
        env_override = {**environ, "INKDROP_SOURCE_SECRET_PROWLARR_API_KEY": "fixture-env-secret"}
        values = cli.provider_secret_values(db_path, environ=env_override)
        require(values.get("prowlarr_api_key") == "fixture-env-secret", "explicit runtime secret environment must override the stored provider value")

        with closing(sqlite3.connect(db_path)) as con:
            con.execute(
                "update provider_configs set settings_json=? where id='prowlarr'",
                (json.dumps({"api_key": "", "secret_fields": ["api_key"]}),),
            )
            con.commit()
        missing_values = cli.provider_secret_values(
            db_path,
            environ={"INKDROP_PROWLARR_CONFIG": str(root / "missing-config.xml")},
        )
        require("prowlarr_api_key" not in missing_values, "missing DB/config credentials must fail closed")

        parent = {
            "provider_id": "prowlarr",
            "secret_ref": "InkDrop provider setting: api_key; fallback Prowlarr config.xml: ApiKey",
        }
        child = {"provider_id": "prowlarr_dognzb_comics", "source_kind": "prowlarr_indexer", "secret_ref": None}
        require(adapters._secret_ref(parent) == "prowlarr_api_key", "prose parent secret_ref must normalize to the stable alias")
        require(adapters._secret_ref(child) == "prowlarr_api_key", "Prowlarr child without a secret_ref must inherit the stable alias")

        captured = {}

        def opener(request, timeout=None):
            captured["api_key"] = request.get_header("X-api-key")
            return FixtureResponse()

        request = adapters.prowlarr_search_request(
            {**parent, "base_url": "http://prowlarr.fixture"},
            {"adapter_family": "prowlarr_indexer"},
            {"series_title": "Adventureman", "issue_number": "9"},
        )
        try:
            source_http.resolve_request_secrets(request, secret_values=missing_values)
        except source_http.SourceHttpError as exc:
            require(exc.reason == "unresolved_secret_ref", f"missing credential must retain a truthful safe reason: {exc.reason}")
            safe_failure = executor._provider_failure_code(
                {"result_status": "provider_wait", "reason": "http_request_failed", "fetch": {"error": f"SourceHttpError: {exc.reason}"}}
            )
            require(safe_failure == "provider_credentials_unavailable", "missing credential must project an actionable public failure code")
            require("fixture-" not in str(exc) and "missing-config" not in str(exc), "missing-credential error must not leak secret values or config paths")
        else:
            raise AssertionError("missing Prowlarr credential must not execute with an unresolved placeholder")
        with (
            mock.patch.dict(os.environ, environ, clear=False),
            mock.patch.object(source_http.urlrequest, "build_opener", return_value=mock.Mock(open=opener)),
        ):
            response = executor._http_client(db_path)(request)
        require(response.get("status_code") == 200, "Manual Search HTTP client must execute with the shared resolver")
        require(captured.get("api_key") == "fixture-prowlarr-secret", "Manual Search must resolve the Prowlarr header before HTTP execution")
        require("fixture-prowlarr-secret" not in json.dumps(source_http.redacted_request(request)), "request diagnostics must redact the API key contract")

    context = manual.structured_search_input(
        {
            "series_title": "Adventureman",
            "issue_number": "9",
            "unit_type": "issue",
            "year": 2020,
            "publisher": "Image",
            "media_type": "comic",
        }
    )
    contract_queries = manual.build_query_variants(context, provider_id="prowlarr", max_queries=6)
    wanted = executor._wanted_context(context, contract_queries)
    exact_queries = adapters.indexer_source_queries(wanted, max_queries=3)
    require(
        exact_queries == ["Adventureman 9", "Adventureman 2020", "Adventureman"],
        f"Adventureman must use bounded issue/year/series recall, not only zero-padding: {exact_queries}",
    )
    row = {
        **parent,
        "base_url": "http://prowlarr.fixture",
        "policy": {"max_query_variants": 3, "categories": [7030, 7000]},
    }
    requests = adapters.prowlarr_search_requests(row, {"adapter_family": "prowlarr_indexer"}, wanted, limit=20)
    require([request["params"]["query"] for request in requests[:3]] == exact_queries, "Prowlarr must execute the Adventureman contract variants in order")
    require(len(requests) <= 6 and all(request["method"] == "GET" for request in requests), "Manual Search must remain bounded and read-only")
    require(all(request["purpose"] == "search_prowlarr" for request in requests), "Manual Search must never plan a grab/download request")

    candidate = {"title": "Adventureman 009 (2022) (Digital).cbz", "guid": "fixture-adventureman-9"}
    for response in (
        {"json": [candidate], "status_code": 200, "headers": {"Content-Type": "application/json"}},
        {"json": {"results": [candidate]}, "status_code": 200, "headers": {"Content-Type": "application/json"}},
    ):
        fetched = adapters.fetch_payloads(
            row,
            {"adapter_family": "prowlarr_indexer"},
            wanted,
            http_get=lambda _request, fixture=response: fixture,
            limit=20,
        )
        require(fetched.get("ok") and fetched.get("payloads"), f"valid Prowlarr response variant must be accepted: {fetched}")
        require((fetched["payloads"][0].get("results") or [])[0]["title"].startswith("Adventureman"), "Prowlarr response variant must retain the candidate")

    failed_fetch = {
        "error": "SourceHttpError: unresolved_secret_ref",
        "partial_errors": [{"error": "SourceHttpError: unresolved_secret_ref"}],
    }
    mixed = run_group(
        {
            "prowlarr": provider_result("prowlarr", "searched_no_candidates"),
            "prowlarr_dognzb_comics": provider_result("prowlarr_dognzb_comics", "provider_wait", reason="http_request_failed", fetch=failed_fetch),
            "prowlarr_catalog": provider_result("prowlarr_catalog", "unsupported_adapter", reason="unsupported_adapter"),
            "prowlarr_manga": provider_result("prowlarr_manga", "blocked", reason="media_scope_blocked"),
        }
    )
    require(mixed["completed"] and not mixed["error"], "one valid zero row must not be poisoned by failed/unsupported/blocked siblings")
    require(mixed["diagnostics"]["provider_rows_succeeded"] == 1, "mixed group must count its successful row")
    require(mixed["diagnostics"]["provider_rows_failed"] == 2, "mixed group must retain failed and unsupported diagnostics")
    require(mixed["diagnostics"]["provider_rows_skipped"] == 1, "media-scope blocked row must remain an intentional skip")
    require("provider_credentials_unavailable" in json.dumps(mixed["diagnostics"]), "mixed diagnostics must expose a safe actionable credential code")

    visible = run_group(
        {
            "prowlarr": provider_result("prowlarr", "review", candidates=[{"title": "Adventureman 009", "guid": "visible"}]),
            "prowlarr_dognzb_comics": provider_result("prowlarr_dognzb_comics", "provider_wait", reason="http_request_failed", fetch=failed_fetch),
        }
    )
    require(visible["completed"] and len(visible["candidates"]) == 1, "visible candidates must survive a sibling provider failure")

    all_failed = run_group(
        {
            "prowlarr": provider_result("prowlarr", "provider_wait", reason="http_request_failed", fetch=failed_fetch),
            "prowlarr_dognzb_comics": provider_result("prowlarr_dognzb_comics", "provider_wait", reason="http_request_failed", fetch=failed_fetch),
        }
    )
    require(not all_failed["completed"], "all eligible rows failing must remain a provider failure")
    require(all_failed["error"] == "provider_credentials_unavailable", f"all-failure summary must be actionable and safe: {all_failed['error']}")
    serialized = json.dumps(all_failed)
    require("fixture-prowlarr-secret" not in serialized and "http://" not in serialized and "https://" not in serialized, "public provider diagnostics must not expose secrets or URLs")

    print("inkdrop Manual Search Prowlarr repair smoke: PASS")


if __name__ == "__main__":
    main()
