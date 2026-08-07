#!/usr/bin/env python3
import base64
import contextlib
import json
import sqlite3
import tempfile
import zipfile
from pathlib import Path
from urllib.parse import quote, quote_plus

from core import inkdrop_diagnostic_artifacts as artifacts


def require(value, message):
    if not value:
        raise AssertionError(message)


def make_database(path):
    with contextlib.closing(sqlite3.connect(path)) as con:
        con.execute("pragma foreign_keys=on")
        con.execute("create table providers(id integer primary key, name text not null)")
        con.execute("create table app_settings(key text primary key, value_json text, provider_id integer references providers(id))")
        con.execute("create table large_cells(payload text)")
        con.execute("insert into providers(name) values ('SLSKD')")
        con.executemany(
            "insert into app_settings(key,value_json,provider_id) values (?,?,1)",
            [(f"setting.{index}", json.dumps(index)) for index in range(5)],
        )
        con.execute("insert into app_settings(key,value_json,provider_id) values (?,?,1)", ("auth.store_generation", json.dumps("generation-fixture")))
        con.execute("insert into app_settings(key,value_json,provider_id) values (?,?,1)", ("provider.api_key", "must-not-leak"))
        con.execute("insert into large_cells(payload) values (?)", ("X" * 70000,))
        con.commit()


def make_auth_database(path, generation="generation-fixture", include_api_keys=True):
    with contextlib.closing(sqlite3.connect(path)) as con:
        con.execute("create table schema_migrations(version integer primary key)")
        con.execute("create table auth_users(id integer primary key)")
        con.execute("create table auth_sessions(id integer primary key)")
        if include_api_keys:
            con.execute("create table api_keys(id integer primary key)")
        con.execute("create table auth_login_attempts(id integer primary key)")
        con.execute("create table auth_recovery_tokens(id integer primary key)")
        con.execute("create table auth_audit_events(id integer primary key)")
        con.execute("create table auth_recovery_requests(id integer primary key)")
        con.execute("create table auth_store_meta(key text primary key, value text)")
        con.execute("create table auth_bootstrap_credentials(id integer primary key)")
        con.execute("insert into auth_store_meta values ('generation', ?)", (generation,))
        con.commit()


def make_backup(
    path, database, auth_database, *, contains=None, omitted=(), schema_version=1,
    auth_payload=None, config_payload=None, secret_payload=None,
):
    contains = contains or {
        "state_db": True, "auth_db": True, "redacted_config_export": True, "secret_reference_manifest": True,
    }
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        members = {
            artifacts.MANIFEST_MEMBER: json.dumps({"schema_version": schema_version, "contains": contains}).encode(),
            artifacts.STATE_DB_MEMBER: Path(database).read_bytes(),
            artifacts.AUTH_DB_MEMBER: Path(auth_database).read_bytes() if auth_payload is None else auth_payload,
            artifacts.CONFIG_EXPORT_MEMBER: config_payload if config_payload is not None else json.dumps({
                "schema_version": 1, "exported_at": "2026-08-03T00:00:00Z", "values": {}, "secret_refs": {},
            }).encode(),
            artifacts.SECRET_REFS_MEMBER: secret_payload if secret_payload is not None else json.dumps({
                "schema_version": 1, "exported_at": "2026-08-03T00:00:00Z", "secrets": {},
                "note": "Secret values are not included.",
            }).encode(),
        }
        for name, payload in members.items():
            if name not in omitted:
                zf.writestr(name, payload)


def secret_variants(value):
    payload = value.encode("utf-8")
    return {
        value,
        quote(value, safe=""),
        quote_plus(value, safe=""),
        json.dumps(value)[1:-1],
        base64.b64encode(payload).decode("ascii"),
        base64.urlsafe_b64encode(payload).decode("ascii"),
    }


def main():
    with tempfile.TemporaryDirectory(prefix="inkdrop-diagnostic-artifacts-") as temp_dir:
        root = Path(temp_dir)
        database = root / "state.sqlite3"
        auth_database = root / "auth.sqlite3"
        archive = root / "backup.zip"
        make_database(database)
        make_auth_database(auth_database)
        make_backup(archive, database, auth_database)

        inspection = artifacts.inspect_backup_archive(
            archive,
            expected_tables=("app_settings", "providers"),
            query_specs=({
                "name": "settings_sample",
                "sql": "select key,value_json from app_settings order by key",
                "max_rows": 2,
                "safe_columns": ["key"],
            }, {
                "name": "alias_evasion",
                "sql": "select value_json as value from app_settings where key='provider.api_key'",
                "max_rows": 1,
            }, {
                "name": "numeric_alias_evasion",
                "sql": "select 12345678 as value",
                "max_rows": 1,
            }),
            max_query_rows=3,
        )
        require(inspection["ok"], inspection)
        require(inspection["state_database"]["quick_check"] == "ok", inspection)
        require(inspection["state_database"]["foreign_key_check"] == "ok", inspection)
        require(inspection["state_database"]["expected_table_counts"] == {"app_settings": 7, "providers": 1}, inspection)
        require(inspection["queries"][0]["row_count"] == 2, inspection)
        require(inspection["queries"][0]["truncated"] is True, inspection)
        require(inspection["queries"][1]["rows"] == [[artifacts.REDACTED]], inspection)
        require(inspection["queries"][2]["rows"] == [[artifacts.REDACTED]], inspection)

        too_many_queries = artifacts.inspect_backup_archive(
            archive,
            query_specs=tuple({"name": str(index), "sql": "select 1"} for index in range(33)),
        )
        require(not too_many_queries["ok"] and "too many diagnostic queries" in too_many_queries["errors"][0], too_many_queries)
        large_cell = artifacts.inspect_backup_archive(
            archive,
            query_specs=({"name": "large", "sql": "select payload from large_cells", "safe_columns": ["payload"]},),
            max_query_cell_bytes=1024,
        )
        require(not large_cell["ok"] and "cell exceeds byte limit" in large_cell["errors"][0], large_cell)
        deadline = artifacts.inspect_backup_archive(
            archive,
            query_specs=({
                "name": "slow",
                "sql": "with recursive n(x) as (select 1 union all select x+1 from n where x<100000000) select sum(x) from n",
            },),
            query_deadline_seconds=0.01,
        )
        require(not deadline["ok"] and any(item in deadline["errors"][0].lower() for item in ("interrupted", "deadline exceeded")), deadline)

        missing = artifacts.inspect_backup_archive(archive, expected_tables=("app_settings", "queue_items"))
        require(not missing["ok"] and "missing expected tables" in missing["errors"][0], missing)

        corrupt_database = root / "corrupt.sqlite3"
        corrupt_database.write_bytes(b"not sqlite")
        corrupt_archive = root / "corrupt.zip"
        make_backup(corrupt_archive, corrupt_database, auth_database)
        corrupt = artifacts.inspect_backup_archive(corrupt_archive)
        require(not corrupt["ok"], corrupt)

        invalid_fk_database = root / "invalid-fk.sqlite3"
        make_database(invalid_fk_database)
        with contextlib.closing(sqlite3.connect(invalid_fk_database)) as con:
            con.execute("pragma foreign_keys=off")
            con.execute("insert into app_settings(key,value_json,provider_id) values ('orphan','1',999)")
            con.commit()
        invalid_fk_archive = root / "invalid-fk.zip"
        make_backup(invalid_fk_archive, invalid_fk_database, auth_database)
        invalid_fk = artifacts.inspect_backup_archive(invalid_fk_archive)
        require(not invalid_fk["ok"] and "foreign_key_check failed" in invalid_fk["errors"][0], invalid_fk)
        traversal = root / "traversal.zip"
        with zipfile.ZipFile(traversal, "w") as zf:
            zf.writestr("../manifest.json", "{}")
            zf.write(database, artifacts.STATE_DB_MEMBER)
        unsafe = artifacts.inspect_backup_archive(traversal)
        require(not unsafe["ok"] and "unsafe archive member" in unsafe["errors"][0], unsafe)

        expansion = root / "expansion.zip"
        with zipfile.ZipFile(expansion, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(artifacts.MANIFEST_MEMBER, "{}")
            zf.writestr(artifacts.STATE_DB_MEMBER, b"0" * (32 * 1024 * 1024))
        require(expansion.stat().st_size < 100 * 1024, "fixture should be a small high-expansion ZIP")
        expansion_result = artifacts.inspect_backup_archive(
            expansion,
        )
        require(not expansion_result["ok"] and "compression ratio exceeds limit" in expansion_result["errors"][0], expansion_result)

        bad_schema = root / "bad-schema.zip"
        make_backup(bad_schema, database, auth_database, schema_version=999)
        bad_schema_result = artifacts.inspect_backup_archive(bad_schema)
        require(not bad_schema_result["ok"] and "schema_version is unsupported" in bad_schema_result["errors"][0], bad_schema_result)

        false_manifest = root / "false-manifest.zip"
        make_backup(false_manifest, database, auth_database, contains={
            "state_db": False, "auth_db": True, "redacted_config_export": True, "secret_reference_manifest": True,
        })
        false_manifest_result = artifacts.inspect_backup_archive(false_manifest)
        require(not false_manifest_result["ok"] and "contains.state_db=true" in false_manifest_result["errors"][0], false_manifest_result)

        corrupt_auth = root / "corrupt-auth.zip"
        make_backup(corrupt_auth, database, auth_database, auth_payload=b"not sqlite auth")
        corrupt_auth_result = artifacts.inspect_backup_archive(corrupt_auth)
        require(not corrupt_auth_result["ok"] and "auth database" in corrupt_auth_result["errors"][0], corrupt_auth_result)

        missing_config = root / "missing-config.zip"
        make_backup(missing_config, database, auth_database, omitted=(artifacts.CONFIG_EXPORT_MEMBER,))
        missing_config_result = artifacts.inspect_backup_archive(missing_config)
        require(not missing_config_result["ok"] and "missing required member" in missing_config_result["errors"][0], missing_config_result)

        missing_flag = root / "missing-flag.zip"
        make_backup(missing_flag, database, auth_database, contains={
            "state_db": True, "auth_db": True, "redacted_config_export": True,
        })
        missing_flag_result = artifacts.inspect_backup_archive(missing_flag)
        require(not missing_flag_result["ok"] and "secret_reference_manifest=true" in missing_flag_result["errors"][0], missing_flag_result)

        empty_config = root / "empty-config.zip"
        make_backup(empty_config, database, auth_database, config_payload=b"{}")
        empty_config_result = artifacts.inspect_backup_archive(empty_config)
        require(not empty_config_result["ok"] and "config export is missing required field" in empty_config_result["errors"][0], empty_config_result)

        empty_secrets = root / "empty-secrets.zip"
        make_backup(empty_secrets, database, auth_database, secret_payload=b"{}")
        empty_secrets_result = artifacts.inspect_backup_archive(empty_secrets)
        require(not empty_secrets_result["ok"] and "secret reference manifest is missing required field" in empty_secrets_result["errors"][0], empty_secrets_result)

        raw_ref = root / "raw-ref.zip"
        make_backup(raw_ref, database, auth_database, config_payload=json.dumps({
            "schema_version": 1, "exported_at": "2026-08-03T00:00:00Z",
            "values": {"INKDROP_API_KEY": "<set>"}, "secret_refs": {"INKDROP_API_KEY": "raw-secret"},
        }).encode())
        raw_ref_result = artifacts.inspect_backup_archive(raw_ref)
        require(not raw_ref_result["ok"] and "canonical objects" in raw_ref_result["errors"][0], raw_ref_result)

        false_configured_ref = root / "false-configured-ref.zip"
        make_backup(false_configured_ref, database, auth_database, config_payload=json.dumps({
            "schema_version": 1, "exported_at": "2026-08-03T00:00:00Z",
            "values": {"INKDROP_API_KEY": "<set>"},
            "secret_refs": {"INKDROP_API_KEY": {"configured": False, "value": "<redacted>"}},
        }).encode())
        false_configured_result = artifacts.inspect_backup_archive(false_configured_ref)
        require(not false_configured_result["ok"] and "invalid configured/value fields" in false_configured_result["errors"][0], false_configured_result)

        orphan_ref = root / "orphan-ref.zip"
        make_backup(orphan_ref, database, auth_database, config_payload=json.dumps({
            "schema_version": 1, "exported_at": "2026-08-03T00:00:00Z", "values": {},
            "secret_refs": {"INKDROP_API_KEY": {"configured": True, "value": "<redacted>"}},
        }).encode())
        orphan_ref_result = artifacts.inspect_backup_archive(orphan_ref)
        require(not orphan_ref_result["ok"] and "orphan reference" in orphan_ref_result["errors"][0], orphan_ref_result)

        missing_ref = root / "missing-ref.zip"
        make_backup(missing_ref, database, auth_database, config_payload=json.dumps({
            "schema_version": 1, "exported_at": "2026-08-03T00:00:00Z",
            "values": {"INKDROP_API_KEY": "<set>"}, "secret_refs": {},
        }).encode())
        missing_ref_result = artifacts.inspect_backup_archive(missing_ref)
        require(not missing_ref_result["ok"] and "secret markers and secret_refs do not match" in missing_ref_result["errors"][0], missing_ref_result)

        non_string_value = root / "non-string-value.zip"
        make_backup(non_string_value, database, auth_database, config_payload=json.dumps({
            "schema_version": 1, "exported_at": "2026-08-03T00:00:00Z", "values": {"INKDROP_PORT": 8080},
            "secret_refs": {},
        }).encode())
        non_string_result = artifacts.inspect_backup_archive(non_string_value)
        require(not non_string_result["ok"] and "only string keys and values" in non_string_result["errors"][0], non_string_result)

        bool_version = root / "bool-version.zip"
        make_backup(bool_version, database, auth_database, config_payload=json.dumps({
            "schema_version": True, "exported_at": "2026-08-03T00:00:00Z", "values": {}, "secret_refs": {},
        }).encode())
        bool_version_result = artifacts.inspect_backup_archive(bool_version)
        require(not bool_version_result["ok"] and "schema_version must be an integer" in bool_version_result["errors"][0], bool_version_result)

        mismatched_auth = root / "mismatched-auth.sqlite3"
        make_auth_database(mismatched_auth, generation="other-generation")
        mismatched_archive = root / "mismatched-generation.zip"
        make_backup(mismatched_archive, database, mismatched_auth)
        mismatch = artifacts.inspect_backup_archive(mismatched_archive)
        require(not mismatch["ok"] and "generation mismatch" in mismatch["errors"][0], mismatch)

        incomplete_auth = root / "incomplete-auth.sqlite3"
        make_auth_database(incomplete_auth, include_api_keys=False)
        incomplete_archive = root / "incomplete-auth.zip"
        make_backup(incomplete_archive, database, incomplete_auth)
        incomplete = artifacts.inspect_backup_archive(incomplete_archive)
        require(not incomplete["ok"] and "missing expected tables" in incomplete["errors"][0], incomplete)

        incomplete_current_auth = root / "incomplete-current-auth.sqlite3"
        make_auth_database(incomplete_current_auth)
        with contextlib.closing(sqlite3.connect(incomplete_current_auth)) as con:
            con.execute("drop table auth_login_attempts")
            con.commit()
        incomplete_current_archive = root / "incomplete-current-auth.zip"
        make_backup(incomplete_current_archive, database, incomplete_current_auth)
        incomplete_current = artifacts.inspect_backup_archive(incomplete_current_archive)
        require(not incomplete_current["ok"] and "auth_login_attempts" in incomplete_current["errors"][0], incomplete_current)

        unsafe_pragma = artifacts.inspect_backup_archive(
            archive, query_specs=({"name": "unsafe", "sql": "pragma writable_schema=on"},),
        )
        require(not unsafe_pragma["ok"] and "read-only" in unsafe_pragma["errors"][0], unsafe_pragma)
        oversized_sql = artifacts.inspect_backup_archive(
            archive, query_specs=({"name": "oversized", "sql": "select 1 /*" + ("X" * artifacts.MAX_SQL_BYTES) + "*/"},),
        )
        require(not oversized_sql["ok"] and "SQL exceeds byte limit" in oversized_sql["errors"][0], oversized_sql)
        too_many_params = artifacts.inspect_backup_archive(
            archive, query_specs=({"name": "params", "sql": "select 1", "params": [1] * 65},),
        )
        require(not too_many_params["ok"] and "too many parameters" in too_many_params["errors"][0], too_many_params)
        scalar_params = artifacts.inspect_backup_archive(
            archive, query_specs=({"name": "params", "sql": "select ?", "params": 42},),
        )
        require(not scalar_params["ok"] and "params must be a list or tuple" in scalar_params["errors"][0], scalar_params)

        secret = "p@ss word+/=with-punctuation"
        second_secret = "cookie-secret-987654"
        url_secret = "url-query-secret-24680"
        boundary_secret = "boundary-secret-ABCDEFGHIJ"
        long_secret = "LONG-SECRET-" + ("L" * 70000) + "-END"
        log = root / "inkdrop.log"
        prefix = "old-line\n" * 200
        encoded = base64.b64encode(secret.encode()).decode()
        log.write_text(
            prefix
            + f"Authorization: Bearer {secret}\n"
            + f"Cookie: session={second_secret}\n"
            + f"api_key={quote_plus(secret, safe='')}\n"
            + f"encoded={encoded}\n"
            + f"boundary={boundary_secret}\n"
            + f"long={long_secret}\n"
            + ("Z" * 100)
            + "url=https://al:pw12345@example.invalid/private\n"
            + "session=log-only-session-123\n"
            + "csrf=log-only-csrf-456\n"
            + "passphrase=log-only-passphrase-789\n"
            + "credential=log-only-credential-012\n"
            + "private_key=log-only-private-key-345\n"
            + "username=log-only-username-678\n"
            + "tail-marker\n",
            encoding="utf-8",
        )
        destination = root / "support.zip"
        built = artifacts.build_support_bundle(
            destination,
            diagnostics={
                "status": "watch",
                "api_key": secret,
                "nested": {"message": f"request failed token={second_secret}", "username": "structured-user-secret"},
                "base_url": f"https://support-user:url-password-13579@example.invalid/api?api_key={url_secret}",
            },
            query_results=inspection["queries"],
            log_paths=(log,),
            explicit_secrets=(secret, second_secret, boundary_secret, long_secret),
            per_log_cap_bytes=512,
            total_log_cap_bytes=512,
        )
        require(built["ok"] and destination.exists(), built)
        require(built["manifest"]["logs_truncated"] is True, built)
        with zipfile.ZipFile(destination, "r") as zf:
            require(zf.testzip() is None, "support bundle CRC should pass")
            require(set(zf.namelist()) == {"manifest.json", "diagnostics.json", "queries.json", "logs/00-inkdrop.log"}, zf.namelist())
            bodies = b"\n".join(zf.read(name) for name in zf.namelist())
            require(b"tail-marker" in bodies, "bounded log tail should preserve newest evidence")
            require(b"[redacted]" in bodies, "bundle should retain visible redaction markers")
            text = bodies.decode("utf-8", errors="replace")
            for candidate in secret_variants(secret) | secret_variants(second_secret) | secret_variants(url_secret) | secret_variants("url-password-13579"):
                require(candidate not in text, f"secret variant leaked: {candidate!r}")
            require("ABCDEFGHIJ" not in text, "a secret fragment crossing the tail boundary leaked")
            require(("L" * 64) not in text and "-END" not in text, "long-secret tail fragment leaked")
            require("https://al:" not in text, "short URL username leaked")
            for unknown in (
                "pw12345", "structured-user-secret", "log-only-session-123", "log-only-csrf-456",
                "log-only-passphrase-789", "log-only-credential-012", "log-only-private-key-345",
                "log-only-username-678",
            ):
                require(unknown not in text, f"log-only credential leaked: {unknown}")

        try:
            artifacts.build_support_bundle(
                root / "structured-bomb.zip",
                diagnostics={"message": "A" * (3 * 1024 * 1024)},
            )
        except ValueError as exc:
            require("structured support input exceeds byte limit" in str(exc), exc)
        else:
            raise AssertionError("oversized structured input must be rejected before compression")

        try:
            artifacts.build_support_bundle(root / "short-secret.zip", explicit_secrets=("abc",))
        except ValueError as exc:
            require("shorter than four" in str(exc), exc)
        else:
            raise AssertionError("short exact secrets must fail closed")

        try:
            artifacts.build_support_bundle(
                root / "too-small.zip",
                diagnostics={"safe": "value"},
                final_archive_cap_bytes=32,
            )
        except ValueError as exc:
            require("final archive limit" in str(exc), exc)
        else:
            raise AssertionError("final archive cap must be enforced")

        try:
            artifacts.build_support_bundle(root / "deadline.zip", diagnostics={"safe": "value"}, deadline_seconds=0)
        except ValueError as exc:
            require("deadline exceeded" in str(exc), exc)
        else:
            raise AssertionError("support bundle deadline must be enforced")

        require(not list(root.glob(".inkdrop-support-*.zip")), "temporary support archives must be cleaned")

    print("PASS: backup integrity and redacted support artifact core")


if __name__ == "__main__":
    main()
