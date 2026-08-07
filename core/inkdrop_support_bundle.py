#!/usr/bin/env python3
"""Build a bounded, shareable InkDrop log bundle with fail-closed redaction."""

from __future__ import annotations

import base64
import configparser
import contextlib
import fnmatch
import io
import json
import os
import re
import sqlite3
import stat as stat_module
import time
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from urllib.parse import parse_qsl, quote, quote_plus, urlsplit

from core import inkdrop_runtime_config
from core import inkdrop_secret_store
from core import inkdrop_version


EXPORT_SCHEMA = "inkdrop.support_bundle.v1"
DEFAULT_PER_FILE_CAP_BYTES = 2 * 1024 * 1024
DEFAULT_TOTAL_CAP_BYTES = 16 * 1024 * 1024
HARD_MAX_FILES = 32
HARD_MAX_SCAN_ENTRIES = 512
HARD_PER_FILE_CAP_BYTES = 2 * 1024 * 1024
HARD_TOTAL_LOG_BYTES = 16 * 1024 * 1024
HARD_FINAL_ARCHIVE_BYTES = 20 * 1024 * 1024
HARD_MANIFEST_BYTES = 256 * 1024
HARD_STRUCTURED_MEMBER_BYTES = 512 * 1024
HARD_STRUCTURED_TOTAL_BYTES = 2 * 1024 * 1024
HARD_SECRET_INVENTORY_BYTES = 1024 * 1024
HARD_SECRET_INVENTORY_VALUES = 128
MIN_EXACT_SECRET_LENGTH = 4
DEFAULT_DEADLINE_SECONDS = 15.0
REDACTION_LOOKBEHIND_BYTES = 64 * 1024
MAX_CONFIG_BYTES = 2 * 1024 * 1024
REDACTED = "[redacted]"
REDACTED_URL = "[redacted-url]"

_KEY_NORMALIZER = re.compile(r"[^a-z0-9]+")
_URL_RE = re.compile(r"(?i)\b(?:https?|ftp|file)://[^\s<>\"']+|\bmagnet:\?[^\s<>\"']+")
_AUTH_RE = re.compile(r"(?i)\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]+")
_HEADER_RE = re.compile(
    r"(?im)\b(?:authorization|proxy-authorization|cookie|set-cookie|"
    r"x-(?:inkdrop-)?api-key|x-auth-token|x-inkdrop-csrf|api-key|apikey)"
    r"\s*[:=]\s*[^\r\n]+"
)
_CREDENTIAL_RE = re.compile(
    r'''(?ix)
    (?P<key>
      api[\s_-]*key|apikey|access[\s_-]*key|private[\s_-]*key|client[\s_-]*secret|
      password|passwd|passphrase|pwd|pass|access[\s_-]*token|refresh[\s_-]*token|
      auth[\s_-]*token|token|secret|credential|authorization|cookie|session|csrf|
      signature|webhook|user[\s_-]*key
    )
    ["']?\s*[:=]\s*
    (?P<value>[^\r\n]+)
    '''
)

_SECRET_KEYS = {
    "apikey", "accesskey", "privatekey", "clientsecret", "password", "passwd",
    "passphrase", "pwd", "pass", "accesstoken", "refreshtoken", "authtoken",
    "token", "secret", "credential", "credentials", "authorization",
    "proxyauthorization", "cookie", "cookies", "setcookie", "session", "csrf",
    "signature", "sig", "webhook", "webhookurl", "userkey",
}
_PRIVATE_IDENTITY_KEYS = {"username", "user", "login"}
_DECLARATION_KEYS = {"secretfields", "secretref", "secretrefs"}
_URL_KEYS = {
    "url", "baseurl", "host", "endpoint", "webhookurl", "downloadurl", "sourceurl",
    "feedurl", "href", "magnet", "magneturl", "uri",
}
_SAFE_STRING_KEYS = {
    "timestamp", "time", "createdat", "updatedat", "level", "levelname", "logger",
    "event", "eventtype", "status", "statuscode", "errortype", "type", "provider",
    "source", "action", "message", "reason", "detail", "method", "schema",
    "state", "health", "container", "service", "filesystem", "mount", "name",
    "builddate", "candidatemanifeststatus", "channel", "commitsha", "displayversion",
    "imagedigest", "imagerepository", "imagerevision", "product", "productname",
    "releasechannel", "shortcommitsha", "shortsha", "version",
    "ioinkdropqabuildnumber", "orgopencontainersimagecreated",
    "orgopencontainersimagerevision", "orgopencontainersimagetitle",
    "orgopencontainersimageversion",
}
_SAFE_HEADER_KEYS = {"contenttype", "contentlength", "accept"}

_SAFE_CONFIG_STRING_KEYS = {
    "adapter", "category", "completionrole", "downloadcategory", "foldercompletionpolicy",
    "implementationstatus", "importconflictpolicy", "label", "pluginname",
    "preferredlanguage", "rootfolderstrategy", "source", "sourcekind", "sourcemode",
    "templateproviderid", "templatevisibility", "torrentcleanuppolicy", "verificationmode",
}
_SAFE_CONFIG_LIST_KEYS = {
    "allowedextensions", "allowedlanguages", "capabilities", "categories", "comiccategories",
    "contentratings", "ebookcategories", "failurecategories", "healthproviderids",
    "libraryvisibilityproviderorder", "protocols", "sourceorder", "translatedlanguages",
}
_PATH_KEYS = {
    "path", "root", "directory", "downloadpath", "savepath", "stagingroot", "workingdirectory",
    "comicroot", "mangaroot", "downloadroot", "incompleteroot", "manualinboxroot",
}


def _key(value):
    return _KEY_NORMALIZER.sub("", str(value or "").casefold())


def _secret_key(value, declared=()):
    normalized = _key(value)
    if normalized in {_key(item) for item in declared}:
        return True
    if normalized in _DECLARATION_KEYS:
        return False
    return normalized in _SECRET_KEYS or any(
        marker in normalized
        for marker in ("password", "passwd", "passphrase", "apikey", "authtoken", "accesstoken", "refreshtoken", "clientsecret")
    )


def _env_secret_key(value):
    normalized = _key(value)
    return normalized.startswith("inkdropsourcesecret") or _secret_key(value) or any(
        marker in normalized
        for marker in (
            "apikey", "password", "passwd", "passphrase", "token", "secret",
            "credential", "privatekey", "cookie", "session", "csrf", "username",
            "userkey", "webhook",
        )
    )


def _table_exists(con, name):
    return bool(con.execute("select 1 from sqlite_master where type='table' and name=?", (name,)).fetchone())


def _json_object(value):
    if isinstance(value, str) and len(value.encode("utf-8", errors="replace")) > MAX_CONFIG_BYTES:
        raise ValueError("configuration JSON exceeds support redaction limit")
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


class SecretInventory:
    def __init__(self):
        self.values = set()
        self.basic_pairs = set()
        self.errors = []
        self.total_bytes = 0
        self._variants = None

    def mark_error(self, code):
        if code not in self.errors:
            self.errors.append(code)

    def add(self, value):
        if value in (None, ""):
            return
        text = str(value)
        if not text or text in self.values:
            return
        encoded_size = len(text.encode("utf-8", errors="strict"))
        if encoded_size < MIN_EXACT_SECRET_LENGTH:
            self.mark_error("secret_inventory:ShortValue")
            return
        if (
            len(self.values) >= HARD_SECRET_INVENTORY_VALUES
            or self.total_bytes + encoded_size > HARD_SECRET_INVENTORY_BYTES
        ):
            self.mark_error("secret_inventory:LimitExceeded")
            return
        self.values.add(text)
        self.total_bytes += encoded_size
        self._variants = None

    def add_identity(self, value):
        text = str(value or "")
        if len(text.encode("utf-8", errors="replace")) >= 8:
            self.add(text)

    def add_pair(self, username, password):
        if username not in (None, "") and password not in (None, ""):
            pair = f"{username}:{password}"
            self.add(password)
            self.add_identity(username)
            self.add(pair)
            if pair in self.values:
                self.basic_pairs.add(pair)

    def variants(self):
        if self._variants is not None:
            return self._variants
        variants = set()
        raw_values = set(self.values) | set(self.basic_pairs)
        for value in raw_values:
            newline_values = {value, value.replace("\r\n", "\n").replace("\r", "\n")}
            newline_values.update(item.replace("\n", "\r\n") for item in tuple(newline_values))
            for candidate in newline_values:
                variants.add(candidate)
                variants.add(quote(candidate, safe=""))
                variants.add(quote_plus(candidate, safe=""))
                variants.add(json.dumps(candidate, ensure_ascii=True)[1:-1])
                payload = candidate.encode("utf-8", errors="strict")
                variants.add(base64.b64encode(payload).decode("ascii"))
                variants.add(base64.urlsafe_b64encode(payload).decode("ascii"))
        self._variants = tuple(sorted((item for item in variants if item), key=len, reverse=True))
        return self._variants


def _url_secrets(value, inventory):
    text = str(value or "").strip()
    if not text:
        return
    try:
        parsed = urlsplit(text)
    except ValueError:
        return
    inventory.add_identity(parsed.username)
    inventory.add(parsed.password)
    for name, item in parse_qsl(parsed.query, keep_blank_values=True):
        if _secret_key(name):
            inventory.add(item)


def _collect_mapping(value, inventory, declared=(), depth=0):
    if depth > 12:
        inventory.mark_error("configuration:DepthLimitExceeded")
        return
    if isinstance(value, dict):
        items = list(value.items())
        if len(items) > HARD_MAX_SCAN_ENTRIES:
            inventory.mark_error("configuration:ItemLimitExceeded")
            items = items[:HARD_MAX_SCAN_ENTRIES]
        local_declared = set(declared)
        fields = value.get("secret_fields")
        if isinstance(fields, list):
            local_declared.update(str(item) for item in fields)
        usernames = [item for key, item in items if _key(key) in _PRIVATE_IDENTITY_KEYS and item not in (None, "")]
        passwords = [item for key, item in items if _secret_key(key, local_declared) and item not in (None, "")]
        for username in usernames:
            inventory.add_identity(username)
            for password in passwords:
                inventory.add_pair(username, password)
        for raw_key, item in items:
            normalized = _key(raw_key)
            if normalized in _DECLARATION_KEYS:
                continue
            if normalized in {"secretenv", "commandenv"} and isinstance(item, dict):
                for env_value in item.values():
                    if isinstance(env_value, (str, int, float)):
                        inventory.add(env_value)
                    else:
                        _collect_mapping(env_value, inventory, local_declared, depth + 1)
                continue
            if _secret_key(raw_key, local_declared) or normalized in _PRIVATE_IDENTITY_KEYS:
                if isinstance(item, (str, int, float)):
                    if normalized in _PRIVATE_IDENTITY_KEYS:
                        inventory.add_identity(item)
                    else:
                        inventory.add(item)
                else:
                    _collect_mapping(item, inventory, local_declared, depth + 1)
                continue
            if normalized in _URL_KEYS and isinstance(item, str):
                _url_secrets(item, inventory)
            _collect_mapping(item, inventory, local_declared, depth + 1)
    elif isinstance(value, (list, tuple)):
        if len(value) > HARD_MAX_SCAN_ENTRIES:
            inventory.mark_error("configuration:ItemLimitExceeded")
        for item in value[:HARD_MAX_SCAN_ENTRIES]:
            _collect_mapping(item, inventory, declared, depth + 1)


def _read_small(path):
    path = Path(path)
    if not path.is_file():
        return None
    if path.stat().st_size > MAX_CONFIG_BYTES:
        raise ValueError("configured credential file exceeds support redaction limit")
    return path.read_text(encoding="utf-8", errors="strict")


def _collect_secret_refs(value, environ, inventory, depth=0):
    if depth > 12:
        inventory.mark_error("secret_refs:DepthLimitExceeded")
        return
    if isinstance(value, dict):
        items = list(value.items())
        if len(items) > HARD_MAX_SCAN_ENTRIES:
            inventory.mark_error("secret_refs:ItemLimitExceeded")
            items = items[:HARD_MAX_SCAN_ENTRIES]
        for raw_key, item in items:
            if _key(raw_key) in {"secretref", "secretrefs"}:
                refs = list(item.values()) if isinstance(item, dict) else list(item) if isinstance(item, list) else [item]
                if len(refs) > HARD_MAX_SCAN_ENTRIES:
                    inventory.mark_error("secret_refs:ItemLimitExceeded")
                for ref in refs[:HARD_MAX_SCAN_ENTRIES]:
                    ref = str(ref or "").strip()
                    if not ref:
                        continue
                    env_name = ref[4:] if ref.lower().startswith("env:") else ref
                    inventory.add(environ.get(env_name))
                    normalized = _key(ref)
                    prefix = "INKDROP_SOURCE_SECRET_"
                    for name, env_value in environ.items():
                        if str(name).startswith(prefix) and _key(str(name)[len(prefix):]) == normalized:
                            inventory.add(env_value)
            else:
                _collect_secret_refs(item, environ, inventory, depth + 1)
    elif isinstance(value, (list, tuple)):
        if len(value) > HARD_MAX_SCAN_ENTRIES:
            inventory.mark_error("secret_refs:ItemLimitExceeded")
        for item in value[:HARD_MAX_SCAN_ENTRIES]:
            _collect_secret_refs(item, environ, inventory, depth + 1)


def _deadline_check(deadline):
    if deadline is not None and time.monotonic() > deadline:
        raise TimeoutError("support bundle deadline exceeded")


def _contains_secret(payloads, encoded_variants, *, deadline=None):
    for index, secret in enumerate(encoded_variants):
        if index % 8 == 0:
            _deadline_check(deadline)
        if any(secret in payload for payload in payloads):
            return True
    return False


def _collect_fallback_files(environ, inventory, deadline=None):
    specs = (
        ("INKDROP_PROWLARR_CONFIG", "prowlarr"),
        ("INKDROP_QBITTORRENT_CONFIG", "qbittorrent"),
        ("INKDROP_MYLAR_CONFIG", "mylar"),
        ("INKDROP_SLSKD_CONFIG", "slskd"),
    )
    for env_key, kind in specs:
        _deadline_check(deadline)
        configured = str(environ.get(env_key) or "").strip()
        if not configured:
            continue
        try:
            text = _read_small(configured)
            if text is None:
                inventory.errors.append(f"{kind}:Unavailable")
                continue
            if kind == "prowlarr":
                inventory.add(ET.fromstring(text).findtext("ApiKey"))
            elif kind == "mylar":
                parser = configparser.ConfigParser(interpolation=None)
                parser.read_string(text)
                inventory.add(parser.get("SABnzbd", "sab_apikey", fallback=""))
            elif kind == "slskd":
                match = re.search(r"(?m)^\s*key:\s*([^\s#]+)", text)
                inventory.add(match.group(1) if match else "")
            else:
                try:
                    import yaml
                    parsed = yaml.safe_load(text) or {}
                except ImportError:
                    parsed = {}
                    for name, item in re.findall(r"(?m)^\s*(user|pass|api_key):\s*([^\s#]+)", text):
                        parsed[name] = item
                _collect_mapping(parsed, inventory)
        except Exception as exc:
            inventory.errors.append(f"{kind}:{type(exc).__name__}")


def _collect_kavita_db(environ, inventory, deadline=None):
    configured = str(environ.get("INKDROP_KAVITA_DB") or "").strip()
    if not configured:
        return
    if not Path(configured).is_file():
        inventory.errors.append("kavita:Unavailable")
        return
    try:
        _deadline_check(deadline)
        uri = f"file:{Path(configured).resolve().as_posix()}?mode=ro"
        with contextlib.closing(sqlite3.connect(uri, uri=True, timeout=2.0)) as con:
            succeeded = False
            for query in (
                "select \"Key\" from AppUserAuthKey where \"Key\" is not null limit 513",
            ):
                try:
                    for index, row in enumerate(con.execute(query)):
                        if index >= HARD_MAX_SCAN_ENTRIES:
                            inventory.mark_error("kavita:RowLimitExceeded")
                            break
                        inventory.add(row[0])
                        if inventory.errors:
                            break
                    succeeded = True
                    break
                except sqlite3.Error:
                    continue
            if not succeeded:
                inventory.errors.append("kavita:SchemaError")
    except Exception as exc:
        inventory.errors.append(f"kavita:{type(exc).__name__}")


def collect_secret_inventory(state_db=None, *, environ=None, secret_root=None, deadline=None):
    environ = dict(os.environ if environ is None else environ)
    inventory = SecretInventory()
    explicit_state_db = state_db is not None
    for name, value in environ.items():
        _deadline_check(deadline)
        if _env_secret_key(name):
            normalized_name = _key(name)
            if not _secret_key(name) and any(
                marker in normalized_name for marker in ("username", "login")
            ):
                inventory.add_identity(value)
            else:
                inventory.add(value)
        if isinstance(value, str) and "://" in value:
            _url_secrets(value, inventory)
        if inventory.errors:
            break
    prefixes = {}
    for name, value in environ.items():
        upper = str(name).upper()
        if upper.endswith("_USERNAME"):
            prefixes.setdefault(upper[:-9], {})["username"] = value
        elif upper.endswith("_PASSWORD"):
            prefixes.setdefault(upper[:-9], {})["password"] = value
    if not inventory.errors:
        for pair in prefixes.values():
            inventory.add_pair(pair.get("username"), pair.get("password"))

    if state_db is None:
        state_db = inkdrop_runtime_config.state_db_path(environ)
    state_db = Path(state_db) if state_db else None
    if state_db and state_db.is_file():
        try:
            _deadline_check(deadline)
            uri = f"file:{state_db.resolve().as_posix()}?mode=ro"
            with contextlib.closing(sqlite3.connect(uri, uri=True, timeout=2.0)) as con:
                con.row_factory = sqlite3.Row
                con.execute("pragma query_only=1")
                con.execute("pragma busy_timeout=1000")
                if _table_exists(con, "provider_configs"):
                    provider_rows = con.execute(
                        "select id,base_url,secret_ref,settings_json from provider_configs limit ?",
                        (HARD_MAX_SCAN_ENTRIES + 1,),
                    )
                    for index, row in enumerate(provider_rows):
                        if index >= HARD_MAX_SCAN_ENTRIES:
                            inventory.mark_error("provider_configs:RowLimitExceeded")
                            break
                        _deadline_check(deadline)
                        settings = _json_object(row["settings_json"])
                        _collect_mapping(settings, inventory)
                        _collect_secret_refs(settings, environ, inventory)
                        _url_secrets(row["base_url"], inventory)
                        ref = str(row["secret_ref"] or "")
                        if ref.lower().startswith("env:"):
                            inventory.add(environ.get(ref[4:]))
                        if inventory.errors:
                            break
                if not inventory.errors and _table_exists(con, "download_client_instances"):
                    client_rows = con.execute(
                        "select username,base_url,settings_json,secret_refs_json "
                        "from download_client_instances limit 257"
                    )
                    for index, row in enumerate(client_rows):
                        if index >= 256:
                            inventory.mark_error("download_client_instances:RowLimitExceeded")
                            break
                        _deadline_check(deadline)
                        settings = _json_object(row["settings_json"])
                        _collect_mapping(settings, inventory)
                        inventory.add_identity(row["username"])
                        _url_secrets(row["base_url"], inventory)
                        resolved = []
                        secret_refs = _json_object(row["secret_refs_json"])
                        if len(secret_refs) > HARD_MAX_SCAN_ENTRIES:
                            inventory.mark_error("download_client_secret_refs:ItemLimitExceeded")
                        for reference in list(secret_refs.values())[:HARD_MAX_SCAN_ENTRIES]:
                            _deadline_check(deadline)
                            try:
                                value = inkdrop_secret_store.read_secret(reference, root=secret_root)
                            except Exception as exc:
                                inventory.errors.append(f"download_client_secret:{type(exc).__name__}")
                                continue
                            inventory.add(value)
                            resolved.append(value)
                        for password in resolved:
                            inventory.add_pair(row["username"], password)
                        if inventory.errors:
                            break
        except Exception as exc:
            inventory.errors.append(f"state_db:{type(exc).__name__}")
    elif explicit_state_db:
        inventory.errors.append("state_db:Unavailable")
    if not inventory.errors:
        _collect_fallback_files(environ, inventory, deadline=deadline)
    if not inventory.errors:
        _collect_kavita_db(environ, inventory, deadline=deadline)
    return inventory


class SupportRedactor:
    def __init__(self, inventory, *, deadline=None):
        self.inventory = inventory
        self._variants = inventory.variants()
        self.deadline = deadline
        self.redactions = 0
        self.decode_replacements = 0

    def _replace(self, text, pattern, replacement):
        text, count = pattern.subn(replacement, text)
        self.redactions += count
        return text

    def exact_text(self, value):
        text = str(value or "")
        for index, secret in enumerate(self._variants):
            if index % 8 == 0:
                _deadline_check(self.deadline)
            count = text.count(secret)
            if count:
                text = text.replace(secret, REDACTED)
                self.redactions += count
        return text

    def text(self, value):
        text = self.exact_text(value)
        text = self._replace(text, _URL_RE, REDACTED_URL)
        text = self._replace(text, _AUTH_RE, REDACTED)
        text = self._replace(text, _HEADER_RE, REDACTED)
        text, count = _CREDENTIAL_RE.subn(lambda match: f"{match.group('key')}={REDACTED}", text)
        self.redactions += count
        return text

    def value(self, value, *, key_name="", declared=(), depth=0):
        if depth > 10:
            self.redactions += 1
            return "[bounded]"
        normalized = _key(key_name)
        if _secret_key(key_name, declared) or normalized in _PRIVATE_IDENTITY_KEYS:
            self.redactions += 1
            return REDACTED
        if isinstance(value, dict):
            local_declared = set(declared)
            fields = value.get("secret_fields")
            if isinstance(fields, list):
                local_declared.update(str(item) for item in fields)
            if normalized in {"headers", "requestheaders", "responseheaders"}:
                out = {}
                for raw_key, item in list(value.items())[:128]:
                    if _key(raw_key) in _SAFE_HEADER_KEYS:
                        out[str(raw_key)[:128]] = self.text(item)
                    else:
                        out[str(raw_key)[:128]] = REDACTED
                        self.redactions += 1
                return out
            out = {}
            for raw_key, item in list(value.items())[:128]:
                if _key(raw_key) in _DECLARATION_KEYS:
                    out[str(raw_key)[:128]] = REDACTED
                    self.redactions += 1
                else:
                    out[str(raw_key)[:128]] = self.value(
                        item, key_name=str(raw_key), declared=local_declared, depth=depth + 1
                    )
            return out
        if isinstance(value, (list, tuple)):
            return [self.value(item, key_name=key_name, declared=declared, depth=depth + 1) for item in list(value)[:128]]
        if value is None or isinstance(value, (bool, int, float)):
            return value
        text = str(value)
        if normalized in _URL_KEYS or _URL_RE.search(text):
            self.redactions += 1
            return self.text(text)
        safe = self.text(text)
        if normalized not in _SAFE_STRING_KEYS:
            self.redactions += 1
            return REDACTED
        return safe[:4096]

    def log_bytes(self, payload):
        decoded = payload.decode("utf-8", errors="replace")
        self.decode_replacements += decoded.count("\ufffd")
        decoded = self.exact_text(decoded)
        lines = []
        for line in decoded.splitlines(keepends=True):
            ending = "\n" if line.endswith(("\n", "\r")) else ""
            body = line.rstrip("\r\n")
            try:
                parsed = json.loads(body)
            except (TypeError, ValueError):
                safe = self.text(body)
            else:
                safe = json.dumps(self.value(parsed), ensure_ascii=False, sort_keys=True)
            lines.append(safe + ending)
        return "".join(lines).encode("utf-8")


def _safe_endpoint(value):
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        parsed = urlsplit(text if "://" in text else "http://" + text)
        scheme = parsed.scheme.lower()
        host = parsed.hostname or ""
        port = parsed.port
    except (TypeError, ValueError):
        return ""
    if scheme not in {"http", "https"} or not host:
        return ""
    rendered_host = f"[{host}]" if ":" in host else host
    return f"{scheme}://{rendered_host}{f':{port}' if port else ''}"


def _configured(value):
    return value not in (None, "", [], {})


def _public_config_value(value, redactor, *, key_name="", declared=(), depth=0):
    if depth > 8:
        return "[bounded]"
    normalized = _key(key_name)
    if _secret_key(key_name, declared) or normalized in _PRIVATE_IDENTITY_KEYS:
        return bool(_configured(value))
    if normalized in _DECLARATION_KEYS:
        return None
    if isinstance(value, dict):
        local_declared = set(declared)
        fields = value.get("secret_fields")
        if isinstance(fields, list):
            local_declared.update(str(item) for item in fields)
        out = {}
        secret_configured = {}
        for raw_key, item in list(value.items())[:128]:
            child = str(raw_key)[:128]
            child_normalized = _key(child)
            if child_normalized in _DECLARATION_KEYS:
                continue
            if _secret_key(child, local_declared):
                secret_configured[child] = bool(_configured(item))
                continue
            if child_normalized in _PRIVATE_IDENTITY_KEYS:
                out[f"{child}_configured"] = bool(_configured(item))
                continue
            public = _public_config_value(
                item, redactor, key_name=child, declared=local_declared, depth=depth + 1
            )
            if public is not None:
                out[child] = public
        if secret_configured:
            out["secret_configured"] = secret_configured
        return out
    if isinstance(value, (list, tuple)):
        if normalized in _SAFE_CONFIG_LIST_KEYS:
            return [redactor.text(item)[:256] for item in list(value)[:64] if item not in (None, "")]
        return {"configured": bool(value), "item_count": min(len(value), 1000000)}
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if normalized in _URL_KEYS or normalized.endswith("url") or normalized.endswith("endpoint"):
        endpoint = _safe_endpoint(value)
        return {"configured": bool(str(value or "").strip()), "endpoint": endpoint}
    if normalized in _PATH_KEYS or normalized.endswith("path") or normalized.endswith("root") or normalized.endswith("directory"):
        return {"configured": bool(str(value or "").strip())}
    if normalized in _SAFE_CONFIG_STRING_KEYS:
        return redactor.text(value)[:256]
    return {"configured": bool(str(value or "").strip())}


def _table_columns(con, table):
    return {str(row[1]) for row in con.execute(f'pragma table_info("{table}")')}


def _select_rows(con, table, wanted, limit):
    columns = _table_columns(con, table)
    selected = [name for name in wanted if name in columns]
    if not selected:
        return []
    fields = ",".join(f'"{name}"' for name in selected)
    return (
        dict(row)
        for row in con.execute(f'select {fields} from "{table}" limit ?', (int(limit),))
    )


def collect_public_configuration(state_db, redactor, *, deadline=None):
    providers = []
    clients = []
    app_settings = []
    if not state_db or not Path(state_db).is_file():
        return {"providers": providers, "app_settings": app_settings}, {"download_clients": clients}
    uri = f"file:{Path(state_db).resolve().as_posix()}?mode=ro"
    with contextlib.closing(sqlite3.connect(uri, uri=True, timeout=2.0)) as con:
        con.row_factory = sqlite3.Row
        con.execute("pragma query_only=1")
        con.execute("pragma busy_timeout=1000")
        if _table_exists(con, "provider_configs"):
            rows = _select_rows(
                con,
                "provider_configs",
                ("id", "provider_type", "display_name", "enabled", "base_url", "source", "settings_json"),
                128,
            )
            for row in rows:
                _deadline_check(deadline)
                try:
                    settings = _json_object(row.pop("settings_json", "{}"))
                    safe_settings = _public_config_value(settings, redactor)
                except Exception:
                    safe_settings = {"available": False}
                base_url = row.pop("base_url", "")
                public = {
                    key: row.get(key)
                    for key in ("id", "provider_type", "display_name", "source")
                    if row.get(key) not in (None, "")
                }
                public["enabled"] = bool(row.get("enabled"))
                public["base_url_configured"] = bool(str(base_url or "").strip())
                public["base_url"] = _safe_endpoint(base_url)
                public["settings"] = safe_settings
                for key in ("id", "provider_type", "display_name", "source"):
                    if key in public:
                        public[key] = redactor.text(public[key])[:256]
                providers.append(public)
        if _table_exists(con, "app_settings"):
            allowed_settings = {
                "automation.source_order": "source_order",
                "media_management.library_visibility_provider_order":
                    "library_visibility_provider_order",
            }
            setting_keys = tuple(allowed_settings)
            placeholders = ",".join("?" for _key_name in setting_keys)
            query = (
                "select key,value_json,source from app_settings "
                f"where key in ({placeholders}) order by key"
            )
            for row in (dict(item) for item in con.execute(query, setting_keys)):
                _deadline_check(deadline)
                setting_key = str(row.get("key") or "")
                value_key = allowed_settings.get(setting_key)
                if not value_key:
                    continue
                raw_value = row.get("value_json")
                try:
                    if len(str(raw_value or "").encode("utf-8", errors="replace")) > MAX_CONFIG_BYTES:
                        raise ValueError("configuration JSON exceeds support redaction limit")
                    parsed_value = json.loads(raw_value or "null")
                    safe_value = _public_config_value(
                        parsed_value, redactor, key_name=value_key
                    )
                except (TypeError, ValueError):
                    safe_value = {"available": False}
                app_settings.append(
                    {
                        "key": setting_key,
                        "value": safe_value,
                        "source": redactor.text(row.get("source"))[:64],
                    }
                )
        mappings = {}
        if _table_exists(con, "download_client_provider_mappings"):
            for row in _select_rows(
                con,
                "download_client_provider_mappings",
                ("provider_id", "protocol", "media_type", "client_instance_id"),
                256,
            ):
                mappings.setdefault(str(row.get("client_instance_id") or ""), []).append(
                    {
                        "provider_id": redactor.text(row.get("provider_id"))[:128],
                        "protocol": redactor.text(row.get("protocol"))[:32],
                        "media_type": redactor.text(row.get("media_type"))[:32],
                    }
                )
        if _table_exists(con, "download_client_instances"):
            rows = _select_rows(
                con,
                "download_client_instances",
                (
                    "id", "name", "client_type", "enabled", "priority", "base_url", "username",
                    "category", "download_path", "categories_json", "download_paths_json",
                    "path_mappings_json", "settings_json", "secret_refs_json", "source",
                ),
                128,
            )
            for row in rows:
                _deadline_check(deadline)
                base_url = row.get("base_url")
                refs = _json_object(row.get("secret_refs_json"))
                try:
                    settings = _public_config_value(_json_object(row.get("settings_json")), redactor)
                except Exception:
                    settings = {"available": False}
                try:
                    categories = _public_config_value(
                        _json_object(row.get("categories_json")), redactor, key_name="categories"
                    )
                except Exception:
                    categories = {"available": False}
                try:
                    download_paths = _public_config_value(
                        _json_object(row.get("download_paths_json")), redactor,
                        key_name="download_paths",
                    )
                except Exception:
                    download_paths = {"available": False}
                try:
                    path_mappings_raw = row.get("path_mappings_json") or "[]"
                    if len(str(path_mappings_raw).encode("utf-8", errors="replace")) > MAX_CONFIG_BYTES:
                        raise ValueError("configuration JSON exceeds support redaction limit")
                    path_mappings_parsed = json.loads(path_mappings_raw)
                    path_mapping_count = min(
                        len(path_mappings_parsed) if isinstance(path_mappings_parsed, list) else 0,
                        1000000,
                    )
                except (TypeError, ValueError):
                    path_mapping_count = 0
                public = {
                    "id": redactor.text(row.get("id"))[:128],
                    "name": redactor.text(row.get("name"))[:128],
                    "client_type": redactor.text(row.get("client_type"))[:64],
                    "enabled": bool(row.get("enabled")),
                    "priority": int(row.get("priority") or 100),
                    "base_url_configured": bool(str(base_url or "").strip()),
                    "base_url": _safe_endpoint(base_url),
                    "username_configured": bool(str(row.get("username") or "").strip()),
                    "category": redactor.text(row.get("category"))[:128],
                    "download_path_configured": bool(str(row.get("download_path") or "").strip()),
                    "categories": categories,
                    "download_paths": download_paths,
                    "path_mapping_count": path_mapping_count,
                    "settings": settings,
                    "secret_configured": {str(key)[:128]: bool(value) for key, value in refs.items()},
                    "provider_mappings": mappings.get(str(row.get("id") or ""), [])[:64],
                    "source": redactor.text(row.get("source"))[:64],
                }
                clients.append(public)
    return {"providers": providers, "app_settings": app_settings}, {"download_clients": clients}


def _json_member(value):
    payload = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
    if len(payload) > HARD_STRUCTURED_MEMBER_BYTES:
        raise RuntimeError("support bundle structured member exceeds hard limit")
    return payload


def collect_log_files(log_dir=None, *, deadline=None):
    root = Path(log_dir or inkdrop_runtime_config.log_dir())
    rows = []
    skipped = []
    if root.is_symlink():
        return rows, [{"name": "unsafe-log-root", "reason": "symlink_rejected"}]
    if not root.is_dir():
        return rows, skipped
    resolved_root = root.resolve(strict=True)
    scanned = 0
    with os.scandir(root) as entries:
        for entry in entries:
            _deadline_check(deadline)
            scanned += 1
            if scanned > HARD_MAX_SCAN_ENTRIES:
                skipped.append({"name": "additional-entries", "reason": "scan_limit_reached"})
                break
            if not fnmatch.fnmatch(entry.name, "*.log*"):
                continue
            if entry.is_symlink():
                skipped.append({"name": "unsafe-log", "reason": "symlink_rejected"})
                continue
            try:
                if not entry.is_file(follow_symlinks=False):
                    continue
                path = Path(entry.path)
                resolved = path.resolve(strict=True)
                if os.path.commonpath((str(resolved_root), str(resolved))) != str(resolved_root):
                    skipped.append({"name": "unsafe-log", "reason": "root_escape_rejected"})
                    continue
                stat = resolved.stat()
            except (OSError, ValueError):
                skipped.append({"name": "unreadable-log", "reason": "path_validation_failed"})
                continue
            if not stat_module.S_ISREG(stat.st_mode) or int(getattr(stat, "st_nlink", 1)) != 1:
                skipped.append({"name": "unsafe-log", "reason": "hardlink_rejected"})
                continue
            rows.append({
                "path": resolved,
                "size_bytes": int(stat.st_size),
                "modified_at": int(stat.st_mtime),
                "device": int(stat.st_dev),
                "inode": int(stat.st_ino),
            })
    rows.sort(key=lambda row: (row["modified_at"], str(row["path"])), reverse=True)
    if len(rows) > HARD_MAX_FILES:
        skipped.extend(
            {"name": f"log-{index:03d}", "reason": "file_limit_reached"}
            for index in range(HARD_MAX_FILES + 1, len(rows) + 1)
        )
    return rows[:HARD_MAX_FILES], skipped[:HARD_MAX_FILES * 2]


def _tail_with_context(row, cap_bytes):
    path = Path(row["path"])
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if (
            not stat_module.S_ISREG(opened.st_mode)
            or int(getattr(opened, "st_nlink", 1)) != 1
            or int(opened.st_dev) != int(row["device"])
            or int(opened.st_ino) != int(row["inode"])
        ):
            raise OSError("log file changed after validation")
        size = int(opened.st_size)
        read_cap = cap_bytes + REDACTION_LOOKBEHIND_BYTES
        with os.fdopen(descriptor, "rb", closefd=True) as handle:
            descriptor = -1
            if size > read_cap:
                handle.seek(size - read_cap)
            return handle.read(read_cap), size > cap_bytes
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def support_bundle_filename(now=None):
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now))
    return f"inkdrop-support-{stamp}.zip"


def build_support_bundle_bytes(
    log_dir=None,
    *,
    state_db=None,
    environ=None,
    secret_root=None,
    status_summary=None,
    system_health=None,
    per_file_cap_bytes=DEFAULT_PER_FILE_CAP_BYTES,
    total_cap_bytes=DEFAULT_TOTAL_CAP_BYTES,
    redactor_factory=SupportRedactor,
    deadline_seconds=DEFAULT_DEADLINE_SECONDS,
):
    started = time.monotonic()
    deadline_seconds = max(1.0, min(float(deadline_seconds or DEFAULT_DEADLINE_SECONDS), 30.0))
    deadline = started + deadline_seconds
    per_file_cap_bytes = min(HARD_PER_FILE_CAP_BYTES, max(4096, int(per_file_cap_bytes)))
    total_cap_bytes = min(HARD_TOTAL_LOG_BYTES, max(per_file_cap_bytes, int(total_cap_bytes)))
    inventory = collect_secret_inventory(
        state_db, environ=environ, secret_root=secret_root, deadline=deadline
    )
    structured_redactor = SupportRedactor(inventory, deadline=deadline)
    encoded_variants = tuple(item.encode("utf-8") for item in inventory.variants())
    version = structured_redactor.value(
        inkdrop_version.build_metadata(environ=environ), key_name="version"
    )
    effective_environment = dict(os.environ if environ is None else environ)
    effective_state_db = state_db or inkdrop_runtime_config.state_db_path(effective_environment)
    if inventory.errors:
        providers = {"available": False, "providers": []}
        clients = {"available": False, "download_clients": []}
    else:
        try:
            providers, clients = collect_public_configuration(
                effective_state_db, structured_redactor, deadline=deadline
            )
        except Exception:
            providers = {"available": False, "providers": []}
            clients = {"available": False, "download_clients": []}
    if inventory.errors:
        status = {"available": False, "reason": "secret_inventory_unavailable"}
        health = {"available": False, "reason": "secret_inventory_unavailable"}
    else:
        status = structured_redactor.value(
            status_summary if isinstance(status_summary, (dict, list)) else {},
            key_name="status",
        )
        health = structured_redactor.value(
            system_health if isinstance(system_health, (dict, list)) else {},
            key_name="system_health",
        )
    structured_members = [
        ("diagnostics/version.json", _json_member(version)),
        ("configuration/providers.json", _json_member(providers)),
        ("configuration/download-clients.json", _json_member(clients)),
        ("diagnostics/status.json", _json_member(status)),
        ("diagnostics/system-health.json", _json_member(health)),
    ]
    structured_bytes = sum(len(payload) for _name, payload in structured_members)
    if structured_bytes > HARD_STRUCTURED_TOTAL_BYTES:
        raise RuntimeError("support bundle structured diagnostics exceed hard limit")
    manifest = {
        "schema": EXPORT_SCHEMA,
        "generated_at": int(time.time()),
        "log_dir": "<runtime-log-dir>",
        "per_file_cap_bytes": per_file_cap_bytes,
        "total_cap_bytes": total_cap_bytes,
        "files": [],
        "skipped": [],
        "redactions": structured_redactor.redactions,
        "decode_replacements": 0,
        "structured_members": [name for name, _payload in structured_members],
        "structured_uncompressed_bytes": structured_bytes,
        "limits": {
            "max_files": HARD_MAX_FILES,
            "max_final_archive_bytes": HARD_FINAL_ARCHIVE_BYTES,
            "max_structured_member_bytes": HARD_STRUCTURED_MEMBER_BYTES,
            "max_structured_total_bytes": HARD_STRUCTURED_TOTAL_BYTES,
        },
    }
    rows, path_skips = collect_log_files(log_dir, deadline=deadline)
    manifest["skipped"].extend(path_skips)
    if inventory.errors:
        manifest["skipped"] = [
            {"name": f"log-{index:03d}", "reason": "secret_inventory_unavailable"}
            for index, _row in enumerate(rows, 1)
        ]
        rows = []
    budget = total_cap_bytes
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for name, member_payload in structured_members:
            archive.writestr(name, member_payload)
        for index, row in enumerate(rows, 1):
            _deadline_check(deadline)
            safe_name = f"log-{index:03d}.log"
            if budget <= 0:
                manifest["skipped"].append({"name": safe_name, "reason": "total_cap_reached"})
                continue
            redactor = redactor_factory(inventory)
            if hasattr(redactor, "deadline"):
                redactor.deadline = deadline
            try:
                raw, truncated = _tail_with_context(row, min(per_file_cap_bytes, budget))
                payload = redactor.log_bytes(raw)
                cap = min(per_file_cap_bytes, budget)
                if len(payload) > cap:
                    payload = payload[-cap:].decode("utf-8", errors="ignore").encode("utf-8")
                    truncated = True
                if _contains_secret((payload,), encoded_variants, deadline=deadline):
                    raise ValueError("post-redaction secret verification failed")
            except Exception:
                manifest["skipped"].append({"name": safe_name, "reason": "redaction_failed"})
                continue
            budget -= len(payload)
            archive.writestr(f"logs/{safe_name}", payload)
            manifest["redactions"] += redactor.redactions
            manifest["decode_replacements"] += redactor.decode_replacements
            manifest["files"].append(
                {
                    "name": safe_name,
                    "size_bytes": row["size_bytes"],
                    "included_bytes": len(payload),
                    "truncated": bool(truncated or len(payload) < row["size_bytes"]),
                    "modified_at": row["modified_at"],
                }
            )
        manifest_payload = json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8")
        if len(manifest_payload) > HARD_MANIFEST_BYTES:
            raise RuntimeError("support bundle manifest exceeds hard limit")
        archive.writestr("manifest.json", manifest_payload)
    payload = buffer.getvalue()
    if len(payload) > HARD_FINAL_ARCHIVE_BYTES:
        raise RuntimeError("support bundle archive exceeds hard limit")
    with zipfile.ZipFile(io.BytesIO(payload), "r") as archive:
        if archive.testzip() is not None:
            raise RuntimeError("support bundle ZIP validation failed")
        if sum(item.file_size for item in archive.infolist()) > (
            HARD_TOTAL_LOG_BYTES + HARD_STRUCTURED_TOTAL_BYTES + HARD_MANIFEST_BYTES
        ):
            raise RuntimeError("support bundle uncompressed size exceeds hard limit")
        for item in archive.infolist():
            metadata = item.filename.encode("utf-8") + item.comment + item.extra
            member = archive.read(item)
            if _contains_secret((metadata, member), encoded_variants, deadline=deadline):
                raise RuntimeError("support bundle post-build secret verification failed")
    return payload, manifest
