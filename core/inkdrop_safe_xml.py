"""XML parsing that cannot be made to expand entities.

Every XML document InkDrop parses arrives from outside it: ComicInfo.xml out of
a downloaded archive, an RSS feed, an indexer or OPDS response. All of them went
through `xml.etree.ElementTree.fromstring()`, which is the stdlib default and
which happily expands internal entities to whatever depth they nest.

Measured, not assumed: a five-level nested internal entity in a ComicInfo.xml
parsed successfully and expanded a single `<Series>` element to 100,000
characters in 0.002s. Each further nesting level multiplies by ten. Nothing in
the path bounded it -- no size ceiling on the expansion, no entity-depth limit,
and no hardened parser anywhere: `defusedxml` appears in no file and in no
requirements file, while `xml.etree` appears in eighteen.

The refusal is on the entity DECLARATION rather than on the expanded size. A
size bound has to be picked and can be picked wrong; a document that cannot
declare an entity cannot expand one, so the property holds by construction and
not by choosing a number.

WHY A TEXT SCAN AND NOT PARSER HANDLERS. The obvious implementation is expat's
EntityDeclHandler, which is what defusedxml uses. It is not reachable here:
ElementTree's C accelerator -- what CPython uses by default -- does not expose
the underlying expat parser at all (`XMLParser` has no `.parser` attribute
since the accelerator became the default), and rebuilding the tree from raw
expat callbacks would mean reimplementing ElementTree's namespace handling to
match. The internal subset is instead read directly, which is exact rather than
approximate: an entity can only be DECLARED in the DTD's internal subset, and
the internal subset can only appear between `<!DOCTYPE` and the root element,
so there is no CDATA, comment or element content for a literal `<!ENTITY` to
hide in and produce a false refusal.

An EXTERNAL DTD is not refused. expat does not resolve external entities unless
a handler is installed, and none is, so an external subset cannot expand
anything -- an entity it would have declared simply comes back as an undefined
entity and the parse fails the way any malformed document does. Refusing it
would cost otherwise-valid feeds for no gain.

A DOCTYPE with no entity declarations still parses, for the same reason.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ElementTree


class UnsafeXmlError(ValueError):
    """The document declared an entity in its DTD's internal subset.

    A subclass of ValueError on purpose. Every call site already treats a
    malformed document as a ValueError or an ElementTree.ParseError -- itself a
    SyntaxError -- and refuses it, so a hostile document should take the path a
    corrupt one already takes rather than escaping as a new exception type
    nobody catches. Call sites that want to tell the two apart can still catch
    UnsafeXmlError specifically.
    """


# Only the first 64 KiB is scanned for the DOCTYPE. The internal subset must
# precede the root element, so a DOCTYPE further in than this is not a DOCTYPE.
# The bound exists so the scan cannot itself become the expensive step on a
# large document.
_DOCTYPE_SCAN_LIMIT = 64 * 1024

_DOCTYPE_RE = re.compile(r"<!DOCTYPE", re.IGNORECASE)
_ENTITY_DECL_RE = re.compile(r"<!ENTITY\b", re.IGNORECASE)


def _as_text(data):
    """The document as str for scanning purposes, or None if undecodable.

    Decoding for the SCAN only -- the parse itself still receives the original
    object, so encoding behaviour is exactly ElementTree's. `errors="replace"`
    because a byte that will not decode cannot be part of an ASCII `<!ENTITY`
    token anyway, and refusing to scan would be the wrong answer.
    """
    if isinstance(data, str):
        return data
    if isinstance(data, (bytes, bytearray, memoryview)):
        return bytes(data).decode("utf-8", errors="replace")
    return None


def _internal_subset(text):
    """The text between the DOCTYPE's `[` and its matching `]`, or "".

    Returns "" when there is no DOCTYPE or no internal subset. Brackets are
    matched by depth rather than by the first `]`, so a subset containing a
    conditional section or a nested bracket is read whole.
    """
    head = text[:_DOCTYPE_SCAN_LIMIT]
    match = _DOCTYPE_RE.search(head)
    if not match:
        return ""
    open_at = head.find("[", match.end())
    if open_at == -1:
        return ""
    # A `>` before the `[` means that DOCTYPE ended without an internal subset.
    end_at = head.find(">", match.end())
    if end_at != -1 and end_at < open_at:
        return ""
    depth = 0
    for index in range(open_at, len(head)):
        char = head[index]
        if char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth == 0:
                return head[open_at + 1:index]
    # Unterminated internal subset: return what there is rather than "" -- an
    # unterminated subset is malformed and must not read as "no subset".
    return head[open_at + 1:]


def assert_no_entity_declarations(data):
    """Raise UnsafeXmlError if the document declares an entity. Else return None."""
    text = _as_text(data)
    if text is None:
        return None
    subset = _internal_subset(text)
    if not subset:
        return None
    found = _ENTITY_DECL_RE.search(subset)
    if found:
        raise UnsafeXmlError(
            "XML entity declaration is not allowed. Entity expansion has no bound, "
            "so a nested declaration can expand a single element to an arbitrary "
            "size at parse time."
        )
    return None


def fromstring(data):
    """Drop-in for ElementTree.fromstring() that cannot expand an entity.

    Identical return value and identical exceptions for every document that
    declares none, so no call site has to change how it handles bad input.
    """
    assert_no_entity_declarations(data)
    return ElementTree.fromstring(data)


__all__ = ["UnsafeXmlError", "assert_no_entity_declarations", "fromstring"]
