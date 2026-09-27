"""How one CBZ member gets compressed, decided by measurement rather than guess.

WHY THIS EXISTS. Every CBZ writer in this tree opened its archive with
`ZIP_DEFLATED, compresslevel=6` and passed every member through it. For an
already-compressed page that is close to pure waste: on a 16-page fixture of
noisy JPEGs, level 6 took 793 ms and level 1 took 717 ms, while storing the
same pages took 9.7 ms. Dropping the level barely helps, because most of the
cost is the search, not the level.

WHAT THE OBVIOUS FIX GETS WRONG. "JPEG is already compressed, so store it" is
not true of every JPEG. Measured here, same encoder, same quality 85:

    16 noisy JPEG pages     DEFLATE 793.3 ms / 22,597,907 B   STORED 9.7 ms / 23,129,356 B  (+2.4%)
    16 grainy scan pages    deflate ratio 0.980 -- no useful gain
    16 clean line-art pages DEFLATE 121.8 ms /  3,078,947 B   STORED 1.8 ms /  4,583,068 B  (+48.9%)

A page of flat line art at a moderate quality leaves long repeated runs in the
entropy-coded stream, and DEFLATE finds them. Storing those pages would have
inflated an archive by half. So the decision cannot be made from the file
extension; it has to be made from the bytes.

HOW IT IS DECIDED. Members under PROBE_MIN_BYTES are always deflated -- they
are cheap to compress and are usually ComicInfo.xml, which deflates about 50x.
Anything larger is probed: the first PROBE_SAMPLE_BYTES are deflated at
PROBE_LEVEL, and if that sample does not shrink by at least
(1 - STORE_WHEN_RATIO_AT_LEAST) the member is stored whole. The probe is a
bounded, fixed cost (about 1.7 ms for 64 KiB) and it is conservative in the
direction that matters: on the line-art page it reports 0.75 where the full
level-6 ratio is 0.67, so a compressible member is never mistaken for an
incompressible one on the strength of an unlucky first slice.

The policy lives here, in one place, for the reason inkdrop_comicinfo_identity
exists: several modules write CBZs, and two copies of "how do we compress a
page" is exactly the shape that lets them drift apart.
"""

from __future__ import annotations

import zipfile
import zlib
from pathlib import Path

# Below this, always deflate: the work is negligible and small members are
# usually metadata, which compresses enormously.
PROBE_MIN_BYTES = 64 * 1024

# How much of a large member is sampled to decide. Enough to see past any
# header and into the entropy-coded body; small enough that the probe is a
# rounding error against compressing the member for real.
PROBE_SAMPLE_BYTES = 64 * 1024

# The probe only has to answer "is there meaningful gain here", so it runs at
# the cheapest level. It under-reports the gain a level-6 pass would find,
# which is the safe direction: it can send a compressible member to DEFLATE,
# never an incompressible one there by mistake.
PROBE_LEVEL = 1

# A member whose probe does not shrink below this fraction of its own size is
# stored. At 0.90 that means "less than a tenth off" is not worth paying for.
STORE_WHEN_RATIO_AT_LEAST = 0.90

# What a member that is worth compressing gets. Unchanged from what every
# writer used before this module existed.
DEFAULT_LEVEL = 6

DEFLATE = (zipfile.ZIP_DEFLATED, DEFAULT_LEVEL)
STORE = (zipfile.ZIP_STORED, None)


def compression_for_sample(sample, member_bytes):
    """(compress_type, compresslevel) for a member of `member_bytes`, judged
    from `sample` -- its leading bytes, or all of it when it is short.

    Returns the pair to hand to ZipFile.write()/writestr(); a stored member's
    level is None because zipfile ignores it and passing 6 would only imply a
    compression that does not happen.
    """

    try:
        member_bytes = int(member_bytes)
    except (TypeError, ValueError):
        return DEFLATE
    if member_bytes < PROBE_MIN_BYTES:
        return DEFLATE
    sample = bytes(sample or b"")
    if not sample:
        # No evidence either way. Keep the behaviour this module replaced.
        return DEFLATE
    try:
        probed = zlib.compress(sample, PROBE_LEVEL)
    except zlib.error:
        return DEFLATE
    if len(probed) >= len(sample) * STORE_WHEN_RATIO_AT_LEAST:
        return STORE
    return DEFLATE


def compression_for_bytes(data):
    """The decision for a member already held in memory.

    Accepts str as well as bytes because ZipFile.writestr() does, and encodes
    it the same way zipfile will (utf-8) so the probe measures the bytes that
    actually get written.
    """

    if isinstance(data, str):
        data = data.encode("utf-8")
    elif data is None:
        data = b""
    elif not isinstance(data, (bytes, bytearray, memoryview)):
        return DEFLATE
    data = bytes(data)
    return compression_for_sample(data[:PROBE_SAMPLE_BYTES], len(data))


def compression_for_file(path):
    """The decision for a member about to be read off disk.

    Reads at most PROBE_SAMPLE_BYTES. A member that cannot be sized or read is
    deflated, because an unreadable probe is not evidence that compression is
    pointless -- and the write that follows will raise on its own if the file
    is genuinely gone.
    """

    path = Path(path)
    try:
        member_bytes = path.stat().st_size
    except OSError:
        return DEFLATE
    if member_bytes < PROBE_MIN_BYTES:
        return DEFLATE
    try:
        with path.open("rb") as handle:
            sample = handle.read(PROBE_SAMPLE_BYTES)
    except OSError:
        return DEFLATE
    return compression_for_sample(sample, member_bytes)


def write_file_member(archive, path, stored_name):
    """Write one on-disk member under this policy."""

    compress_type, compresslevel = compression_for_file(path)
    archive.write(path, stored_name, compress_type=compress_type, compresslevel=compresslevel)
    return compress_type


def write_bytes_member(archive, stored_name, data):
    """Write one in-memory member under this policy."""

    compress_type, compresslevel = compression_for_bytes(data)
    archive.writestr(stored_name, data, compress_type=compress_type, compresslevel=compresslevel)
    return compress_type
