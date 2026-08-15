"""
harness.storage.resource_codec - The logical/physical split for JSON resources.

A JSON resource has two representations and they are deliberately different:

* the **logical file** is what a reader sees - pretty-printed with ``indent=2``,
  exactly the bytes the file backend writes.  Line numbers, read budgets,
  ``local_fs_search`` line hits and the artifact SHA-256 all describe this form.
* the **stored column** is compact.  Indentation is 76% of a semantic tree's
  bytes and buys nothing in a database, where nobody reads the column directly.

Everything that needs either form goes through this module.  The writer sizes
and hashes the logical form here; every reader renders the logical form here.
One function on both sides is what stops them from drifting apart - a mismatch
would show up as "corrupted artifact" long after the write that caused it.

Only ``content_json`` is affected.  ``content_text`` holds a caller's string
verbatim and ``content_blob`` holds bytes; neither has indentation to remove,
and round-tripping them through a JSON parser would corrupt them.
"""

from __future__ import annotations

import hashlib
import json
import zlib
from typing import Any, Mapping, NamedTuple, Optional

from harness.storage.base import StorageCorruptError


class EncodedResource(NamedTuple):
    """Both representations of one JSON resource, plus the logical measures."""

    stored_text: str
    logical_text: str
    logical_byte_size: int
    logical_sha256: str
    stored_byte_size: int


# ---------------------------------------------------------------------------
# Compression (the physical layer for large process resources)
# ---------------------------------------------------------------------------
#
# Four resource types - the harness's own bulk captures, never business
# output - are zlib-compressed into ``content_blob`` once their logical bytes
# reach ``min_bytes``.  "zlib" was chosen over per-row delta chains after
# measuring both on live data: chains save 48% but replay on every read;
# plain zlib saves 92% of the same bytes with a single bounded decompress.
#
# What gets compressed is the *logical* text, not the stored column: the
# decompressed bytes then equal ``byte_size`` exactly, which is the integrity
# check every read performs.  ``byte_size`` / ``sha256`` stay logical and are
# untouched by compression; only ``stored_byte_size`` describes the blob.

ENCODING_IDENTITY = "identity"
ENCODING_ZLIB_JSON = "zlib-json-v1"
ENCODING_ZLIB_TEXT = "zlib-text-v1"

_KNOWN_ENCODINGS = frozenset({ENCODING_IDENTITY, ENCODING_ZLIB_JSON, ENCODING_ZLIB_TEXT})

# Resource types whose representation is harness-internal bulk capture.
# Extraction output and everything operator-facing stays readable in place.
COMPRESSIBLE_RESOURCE_TYPES = frozenset({
    "event_payload",
    "observation",
    "tool_result",
    "context_compaction",
})

DEFAULT_COMPRESSION_MIN_BYTES = 16384
DEFAULT_COMPRESSION_LEVEL = 6

# Hard ceiling on any single decompression, independent of the row's claimed
# size: a damaged or crafted stream must not be able to request unbounded
# memory before the row's own metadata is checked.
MAX_DECOMPRESSED_BYTES = 256 * 1024 * 1024


class StoredResource(NamedTuple):
    """One resource's complete physical shape plus its logical measures."""

    content_json: Optional[str]
    content_text: Optional[str]
    content_blob: Optional[bytes]
    content_encoding: str
    logical_text: str
    logical_byte_size: int
    logical_sha256: str
    stored_byte_size: Optional[int]


def normalize_text_file(text: str) -> str:
    """Translate line endings the way opening a text file for reading does.

    The file backend goes through ``Path.read_text``, whose universal-newline
    mode turns ``\\r\\n`` and ``\\r`` into ``\\n``. A database column keeps
    whatever bytes it was given, so without this a Windows-style CSV reads
    back as a different document from the database than from disk - different
    content, different digest, and a dual-mode mismatch on a file nothing is
    wrong with.

    The row still stores the original bytes; this describes what a *read*
    returns, which is the only thing the two backends have to agree on.
    """

    return text.replace("\r\n", "\n").replace("\r", "\n")


def encode_text_resource(content: str) -> EncodedResource:
    """Prepare string content: stored verbatim, measured as it sits on disk.

    Unlike JSON, a text resource's file *is* the string it was given - the file
    backend writes it unchanged and reports ``st_size``. So size and digest
    describe those raw bytes here too, and the two backends agree without a
    migration. Newline translation happens on read, where it belongs: a CRLF
    file then reads back as LF from either backend while its recorded size
    stays the one number both backends can actually produce.

    The overstatement this leaves - one byte per CRLF line - is the harmless
    direction for a read budget. Understating it would let a read exceed the
    budget it was given.
    """

    raw = content.encode("utf-8")
    return EncodedResource(
        stored_text=content,
        logical_text=normalize_text_file(content),
        logical_byte_size=len(raw),
        logical_sha256=hashlib.sha256(raw).hexdigest(),
        stored_byte_size=len(raw),
    )


def render_json_text(stored_text: str) -> str:
    """Turn a stored JSON column back into the logical file's text.

    Idempotent for rows written before compact storage: re-rendering text that
    is already ``indent=2`` reproduces it byte for byte, so old and new rows
    read identically and no migration is needed to mix them.

    Text that does not parse as JSON is returned unchanged rather than raising.
    A stored column should always be valid JSON, but a reader failing loudly
    over a damaged row would take down a read path whose job is to show the
    operator what is in there.
    """

    try:
        value = json.loads(stored_text)
    except (TypeError, ValueError):
        return stored_text
    return _render_value(value)


def encode_json_resource(content: Any) -> EncodedResource:
    """Prepare a JSON resource for storage.

    ``logical_byte_size`` and ``logical_sha256`` describe the rendered form on
    purpose: they are the size a reader will page through and the digest the
    artifact gate compares against, neither of which has anything to do with
    how the row happens to be encoded.
    """

    logical_text = _render_value(content)
    # Derived from the text that was hashed, never encoded from the object a
    # second time. ``default=str`` runs during the render above; running it
    # again on the same object can produce a different string - anything whose
    # __str__ carries a timestamp, a counter or an address - and the row would
    # then hold content its own sha256 does not describe.
    stored_text = json.dumps(
        json.loads(logical_text), ensure_ascii=False, separators=(",", ":")
    )
    logical_raw = logical_text.encode("utf-8")
    return EncodedResource(
        stored_text=stored_text,
        logical_text=logical_text,
        logical_byte_size=len(logical_raw),
        logical_sha256=hashlib.sha256(logical_raw).hexdigest(),
        stored_byte_size=len(stored_text.encode("utf-8")),
    )


def _render_value(value: Any) -> str:
    """The one definition of a JSON resource's logical bytes.

    Must stay identical to what ``FileStore.save_resource`` writes, including
    ``default=str``: a value the encoder had to stringify has to stringify the
    same way on both sides or the two backends' files differ.
    """

    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


# -- encoding (write path) --------------------------------------------------

def encode_resource(
    content: Any,
    *,
    resource_type: str,
    compression: str = "none",
    min_bytes: int = DEFAULT_COMPRESSION_MIN_BYTES,
    level: int = DEFAULT_COMPRESSION_LEVEL,
) -> StoredResource:
    """Decide how one resource lands in the database. The only such decision.

    Every writer funnels through here, so "is this row compressed" has one
    answer everywhere. The JSON/text split, the logical measurements and the
    compression threshold all live in this function; callers pass their
    configuration and never re-derive any of it.

    Compression applies when all of these hold:

    * ``compression == "zlib"`` (the rollback switch),
    * ``resource_type`` is harness-internal bulk capture (see
      ``COMPRESSIBLE_RESOURCE_TYPES``),
    * the logical bytes reach ``min_bytes`` - small rows keep their readable
      columns, which measured on live data costs 0.7 MB out of 71 MB saved.

    ``bytes`` content never reaches this function: it is already a binary
    blob with no JSON/text duality to preserve, and callers store it as-is.
    """

    if isinstance(content, str):
        encoded = encode_text_resource(content)
        kind = "text"
        logical_raw = content.encode("utf-8")
    else:
        encoded = encode_json_resource(content)
        kind = "json"
        logical_raw = encoded.logical_text.encode("utf-8")
    # For text, the logical bytes are the original string's bytes, which is
    # exactly what encode_text_resource measured; for JSON they are the
    # rendered text. Either way this is what byte_size must describe.
    compress = (
        compression == "zlib"
        and resource_type in COMPRESSIBLE_RESOURCE_TYPES
        and len(logical_raw) >= max(0, int(min_bytes))
    )
    if not compress:
        return StoredResource(
            content_json=encoded.stored_text if kind == "json" else None,
            content_text=encoded.stored_text if kind == "text" else None,
            content_blob=None,
            content_encoding=ENCODING_IDENTITY,
            logical_text=encoded.logical_text,
            logical_byte_size=encoded.logical_byte_size,
            logical_sha256=encoded.logical_sha256,
            # Historical shapes preserved exactly: only JSON rows carried a
            # distinct stored size; a text column holds the same bytes as the
            # file and leaves it NULL. "none" must write byte-identical rows.
            stored_byte_size=(encoded.stored_byte_size if kind == "json" else None),
        )

    blob = zlib.compress(logical_raw, int(level))
    return StoredResource(
        content_json=None,
        content_text=None,
        content_blob=blob,
        content_encoding=(ENCODING_ZLIB_JSON if kind == "json" else ENCODING_ZLIB_TEXT),
        logical_text=encoded.logical_text,
        logical_byte_size=encoded.logical_byte_size,
        logical_sha256=encoded.logical_sha256,
        stored_byte_size=len(blob),
    )


# -- decoding (read path) ---------------------------------------------------

def _row_value(row: Any, key: str) -> Any:
    """Read a column from a dict or a sqlite3.Row without preferring either.

    sqlite3.Row supports subscripting but not ``.get``, and read paths hand
    both shapes to the codec depending on where the row came from.
    """

    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return None


def _corrupt(row: Mapping[str, Any], reason: str) -> StorageCorruptError:
    """A decode failure with the row's own identifiers attached."""

    return StorageCorruptError(
        f"resource row cannot be decoded ({reason}):"
        f" task_id={_row_value(row, 'task_id')!r} resource_id={_row_value(row, 'resource_id')!r}"
        f" logical_path={_row_value(row, 'logical_path')!r}"
        f" content_encoding={_row_value(row, 'content_encoding')!r}"
    )


def _bounded_decompress(
    blob: bytes, *, byte_size: Optional[int], row: Mapping[str, Any]
) -> bytes:
    """zlib.decompress with every guard the storage layer needs.

    Never an unbounded call: the output cap is taken from the row's own
    ``byte_size`` (compressed rows decompress to exactly that many bytes, so
    anything beyond the small slack allowance is damage), capped by a global
    ceiling for rows whose metadata is missing. A stream that ends early,
    trails garbage, or disagrees with ``byte_size`` is corruption, reported as
    ``StorageCorruptError`` rather than a raw ``zlib.error`` so callers can
    distinguish "this row is damaged" from "the codec is broken".
    """

    try:
        raw_size = int(byte_size) if byte_size is not None else None
    except (TypeError, ValueError):
        raw_size = None
    limit = (
        min(int(raw_size * 1.05) + 4096, MAX_DECOMPRESSED_BYTES)
        if raw_size is not None
        else MAX_DECOMPRESSED_BYTES
    )
    decompressor = zlib.decompressobj()
    out = bytearray()
    data = bytes(blob)
    try:
        while not decompressor.eof:
            remaining = limit - len(out)
            if remaining <= 0:
                raise _corrupt(
                    row,
                    f"decompressed output exceeds {limit} bytes"
                    f" (byte_size={byte_size})",
                )
            piece = decompressor.decompress(data, remaining)
            out += piece
            data = decompressor.unconsumed_tail
            if not piece and not data and not decompressor.eof:
                # All input consumed without reaching the end of the stream.
                raise _corrupt(row, "compressed stream is truncated")
    except zlib.error as exc:
        raise _corrupt(row, f"zlib stream error: {exc}") from exc
    if not decompressor.eof:
        raise _corrupt(row, "compressed stream is truncated")
    if decompressor.unused_data:
        raise _corrupt(row, "trailing bytes after the compressed stream")
    if raw_size is not None and len(out) != raw_size:
        raise _corrupt(
            row,
            f"decompressed to {len(out)} bytes but byte_size says {raw_size}",
        )
    return bytes(out)


def decode_resource_row(row: Mapping[str, Any]) -> Optional[str]:
    """Restore any row's content to the logical text a reader expects.

    ``identity`` JSON renders back to ``indent=2`` (idempotent for rows from
    before either storage split), ``identity`` text is newline-normalised,
    compressed rows are bounded-decompressed first. Returns None for rows
    with no textual content: native ``bytes`` blobs and ``external_path``
    receipts. Unknown encodings and damaged streams raise
    ``StorageCorruptError`` - guessing a fallback is how a damaged row would
    be read as the wrong content instead of as damaged.
    """

    encoding = str(_row_value(row, "content_encoding") or ENCODING_IDENTITY)
    if encoding not in _KNOWN_ENCODINGS:
        raise _corrupt(row, f"unknown content_encoding {encoding!r}")

    if encoding in (ENCODING_ZLIB_JSON, ENCODING_ZLIB_TEXT):
        blob = _row_value(row, "content_blob")
        if blob is None:
            # A row that already went through restore_row_content keeps its
            # provenance encoding but carries the restored text in the usual
            # column; decoding such a row again must be a no-op, not damage.
            restored_json = _row_value(row, "content_json")
            restored_text = _row_value(row, "content_text")
            if restored_json is None and restored_text is None:
                raise _corrupt(row, f"{encoding} row has no content_blob")
            if restored_text is not None:
                return normalize_text_file(str(restored_text))
            return str(restored_json)
        try:
            # The compressed bytes are the logical text itself, so what comes
            # out is already the reader's document - no re-render needed.
            text = _bounded_decompress(
                bytes(blob), byte_size=_row_value(row, "byte_size"), row=row
            ).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise _corrupt(row, f"not valid UTF-8: {exc}") from exc
        if encoding == ENCODING_ZLIB_TEXT:
            text = normalize_text_file(text)
        return text

    # identity: exactly one of the four content columns is populated.
    text = _row_value(row, "content_text")
    if text is not None:
        return normalize_text_file(str(text))
    stored_json = _row_value(row, "content_json")
    if stored_json is not None:
        return render_json_text(str(stored_json))
    # A binary or external row has no textual logical form.
    return None


def logical_text_from_row(row: Mapping[str, Any]) -> Optional[str]:
    """decode_resource_row, spelled the way read paths name it.

    None means "this row has no text to read" (binary blob or external
    receipt), which callers treat as absent - never as an error.
    """

    return decode_resource_row(row)


def restore_row_content(row: Mapping[str, Any]) -> dict:
    """A row dict whose logical content sits back in its original column.

    For ``read_resource``, whose callers historically consume
    ``content_json`` / ``content_text`` directly. The postcondition is one
    sentence with no exceptions: **whichever textual column the row uses
    holds the logical text** - the same bytes the file backend has on disk,
    the same bytes ``local_fs_read`` pages through. How the row was stored
    does not survive into what a caller reads.

    Storage-shaped exceptions are how this key ends up meaning two things.
    Returning the compact column for an identity row and the rendered text
    for a compressed one makes ``content_json`` a value whose form depends on
    a config flag, and a caller that compares or measures it is right in one
    deployment and wrong in the other. Parsing hides the difference, which is
    the reason it would stay hidden until something stopped parsing.

    ``content_blob`` is removed - the physical bytes are the codec's business,
    and a caller reaching for them by accident is exactly the two-answers
    problem this module exists to prevent. ``content_encoding`` stays for
    provenance: it says how the row was *stored*, never what was returned.
    """

    record = dict(row)
    text = decode_resource_row(record)
    encoding = str(record.get("content_encoding") or ENCODING_IDENTITY)
    if encoding == ENCODING_ZLIB_JSON:
        key = "content_json"
    elif encoding == ENCODING_ZLIB_TEXT:
        key = "content_text"
    elif record.get("content_text") is not None:
        key = "content_text"
    elif record.get("content_json") is not None:
        key = "content_json"
    else:
        # A native blob or an external receipt: no textual form to restore,
        # and inventing one would claim content this row never had.
        key = ""
    if key:
        record[key] = text
        record["content_text" if key == "content_json" else "content_json"] = None
    record.pop("content_blob", None)
    return record
