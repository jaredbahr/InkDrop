#!/usr/bin/env python3
import re
import unicodedata


LANGUAGE_ALIASES = {
    "en": "en",
    "eng": "en",
    "english": "en",
    "us": "en",
    "uk": "en",
    "es": "es",
    "spa": "es",
    "spanish": "es",
    "espanol": "es",
    "castellano": "es",
    "latino": "es",
    "latam": "es",
    "es-la": "es",
    "es_es": "es",
    "pt": "pt",
    "pt-br": "pt",
    "portuguese": "pt",
    "portugues": "pt",
    "brasileiro": "pt",
    "fr": "fr",
    "fre": "fr",
    "fra": "fr",
    "french": "fr",
    "francais": "fr",
    "de": "de",
    "ger": "de",
    "deu": "de",
    "german": "de",
    "deutsch": "de",
    "it": "it",
    "ita": "it",
    "italian": "it",
    "jp": "ja",
    "jpn": "ja",
    "ja": "ja",
    "japanese": "ja",
    "raw": "raw",
    "raws": "raw",
}

NON_ENGLISH_MARKERS = {
    "spanish",
    "espanol",
    "spa",
    "castellano",
    "latino",
    "latam",
    "es-la",
    "es-es",
    "pt-br",
    "portuguese",
    "portugues",
    "brasileiro",
    "francais",
    "french",
    "italian",
    "deutsch",
    "german",
    "japanese",
    "jpn",
    "raw",
    "raws",
}

EXPLICIT_ENGLISH_MARKERS = {"english", "eng"}


def normalize_text(value):
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return text.lower()


def language_code(value):
    text = normalize_text(value).strip().replace("_", "-")
    if not text:
        return ""
    primary = re.split(r"[-\s/]+", text, 1)[0]
    return LANGUAGE_ALIASES.get(text) or LANGUAGE_ALIASES.get(primary) or primary


def text_tokens(value):
    return {token for token in re.split(r"[^a-z0-9]+", normalize_text(value)) if token}


def non_english_script_labels(value):
    labels = []
    for char in str(value or ""):
        code = ord(char)
        if 0x3040 <= code <= 0x30FF or 0x31F0 <= code <= 0x31FF or 0xFF66 <= code <= 0xFF9F:
            labels.append("japanese_script")
        elif 0x4E00 <= code <= 0x9FFF or 0x3400 <= code <= 0x4DBF:
            labels.append("cjk_ideograph")
        elif 0xAC00 <= code <= 0xD7AF or 0x1100 <= code <= 0x11FF or 0x3130 <= code <= 0x318F:
            labels.append("korean_script")
        elif 0x0400 <= code <= 0x04FF:
            labels.append("cyrillic_script")
    return list(dict.fromkeys(labels))


def marker_matches(value):
    text = normalize_text(value).replace("_", "-")
    padded = f" {text} "
    hits = []
    for marker in sorted(NON_ENGLISH_MARKERS, key=len, reverse=True):
        marker_text = marker.replace("_", "-")
        pattern = r"(?<![a-z0-9])" + re.escape(marker_text).replace(r"\-", r"[-\s_.]+") + r"(?![a-z0-9])"
        if re.search(pattern, padded):
            hits.append(marker)
    return list(dict.fromkeys(hits))


def language_from_metadata(comicinfo=None, metadata=None):
    values = []
    for source in (comicinfo, metadata):
        if not isinstance(source, dict):
            continue
        for key in (
            "LanguageISO",
            "language",
            "language_iso",
            "translatedLanguage",
            "translated_language",
            "locale",
        ):
            if source.get(key):
                values.append(source.get(key))
    for value in values:
        code = language_code(value)
        if code:
            return code, str(value)
    return "", ""


def classify_release_language(
    *,
    title,
    path=None,
    comicinfo=None,
    metadata=None,
    preferred_languages=("en",),
    blocked_languages=None,
    unknown_policy="allow_if_exact",
):
    preferred = {language_code(value) for value in (preferred_languages or ("en",)) if language_code(value)}
    if "any" in preferred:
        return {
            "ok": True,
            "blocked": False,
            "status": "allowed_any_language",
            "reason": "",
            "markers": [],
            "language": "",
            "confidence": "policy",
        }
    preferred = preferred or {"en"}
    blocked = {language_code(value) for value in (blocked_languages or []) if language_code(value)}
    blocked.update({"es", "pt", "fr", "de", "it", "ja", "ko", "zh", "ru", "raw"} - preferred)

    haystacks = [title, path]
    markers = []
    scripts = []
    for value in haystacks:
        markers.extend(marker_matches(value))
        scripts.extend(non_english_script_labels(value))
    markers = list(dict.fromkeys(markers))
    scripts = list(dict.fromkeys(scripts))
    explicit_english = bool(text_tokens(" ".join(str(v or "") for v in haystacks)) & EXPLICIT_ENGLISH_MARKERS)
    metadata_language, metadata_raw = language_from_metadata(comicinfo=comicinfo, metadata=metadata)

    if metadata_language and metadata_language not in preferred:
        return {
            "ok": False,
            "blocked": True,
            "status": "blocked",
            "reason": "wrong_language_source",
            "detail": f"metadata language {metadata_raw or metadata_language} is not preferred",
            "markers": markers,
            "language": metadata_language,
            "confidence": "metadata",
        }
    if markers and not explicit_english:
        marker_codes = {language_code(marker) for marker in markers}
        if marker_codes & blocked:
            return {
                "ok": False,
                "blocked": True,
                "status": "blocked",
                "reason": "wrong_language_source",
                "detail": "non-English language marker: " + ", ".join(markers[:4]),
                "markers": markers,
                "language": sorted(marker_codes & blocked)[0],
                "confidence": "marker",
            }
    if scripts and not explicit_english:
        return {
            "ok": False,
            "blocked": True,
            "status": "blocked",
            "reason": "wrong_language_source",
            "detail": "non-English script marker: " + ", ".join(scripts[:3]),
            "markers": markers + scripts,
            "language": "script",
            "confidence": "script",
        }
    if metadata_language in preferred:
        return {
            "ok": True,
            "blocked": False,
            "status": "preferred_language",
            "reason": "",
            "markers": markers,
            "language": metadata_language,
            "confidence": "metadata",
        }
    if explicit_english:
        return {
            "ok": True,
            "blocked": False,
            "status": "likely_preferred_language",
            "reason": "",
            "markers": markers,
            "language": "en",
            "confidence": "marker",
        }
    if unknown_policy == "block":
        return {
            "ok": False,
            "blocked": True,
            "status": "blocked",
            "reason": "unknown_language_low_confidence",
            "detail": "release language is unknown",
            "markers": markers,
            "language": "",
            "confidence": "unknown",
        }
    return {
        "ok": True,
        "blocked": False,
        "status": "unknown_language_allowed",
        "reason": "",
        "markers": markers,
        "language": metadata_language,
        "confidence": "unknown",
    }
