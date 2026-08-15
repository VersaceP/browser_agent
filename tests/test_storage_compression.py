"""Per-resource zlib compression for the four bulk resource types.

Plan: docs/resource-compression-plan.md. The tests mirror its section 9
one-for-one; the numbering in the comments is that list.

The recurring shape is a paired store: the same content saved through a
compression-enabled store and a compression-disabled one, then every read
surface compared. "None" is the rollback switch whose rows must be
byte-identical to the pre-compression shape, which makes it the correct
reference oracle for every round-trip claim.
"""

import json
import re
import tempfile
import tracemalloc
import unittest
import zlib
from pathlib import Path
from unittest import mock

from harness.local_fs import local_fs_read, local_fs_search
from harness.offload import offload_large_tool_result
from harness.storage import create_storage
from harness.storage.base import StorageCorruptError
from harness.storage.file_store import FileStore
from harness.storage.resource_codec import (
    ENCODING_IDENTITY,
    ENCODING_ZLIB_JSON,
    ENCODING_ZLIB_TEXT,
    encode_json_resource,
    encode_text_resource,
)
from harness.storage.sqlite_store import (
    EVENT_PAYLOAD_OFFLOAD_THRESHOLD,
    SqliteStore,
    build_resource_uri,
)
from harness.storage.virtual_fs import VirtualTaskFs
from harness.utils import RunLogger


HARNESS_VERSION = "test-compression"


def big_json(min_bytes=40000):
    """A payload whose *logical* rendering exceeds the default threshold."""
    filler = "x" * 400
    return {"rows": [{"i": i, "text": filler} for i in range(min_bytes // 420 + 1)]}


def big_text(min_bytes=40000):
    return "\n".join(f"line {i:05d} " + "y" * 60 for i in range(min_bytes // 70 + 1))


class CompressionTestBase(unittest.TestCase):
    """Two stores over one temp dir: compression on, compression off."""

    pair_counter = 0

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def _store(self, label, **kwargs):
        worktree = self.root / label
        store = SqliteStore(worktree / "harness.db", worktree_dir=str(worktree), **kwargs)
        self.addCleanup(store.close)
        store.create_task(task_id="t1", harness_version=HARNESS_VERSION)
        store.start_run(task_id="t1", harness_version=HARNESS_VERSION, run_id="run-1")
        return store

    def _pair(self, **kwargs):
        """(plain, compressed) stores; kwargs reach both identically."""
        CompressionTestBase.pair_counter += 1
        suffix = f"-{CompressionTestBase.pair_counter}"
        plain = self._store("plain" + suffix, resource_compression="none", **kwargs)
        zipped = self._store("zipped" + suffix, resource_compression="zlib", **kwargs)
        return plain, zipped

    def _ids(self, store):
        return [
            row["resource_id"] for row in store.connection.execute(
                "SELECT resource_id FROM task_resources ORDER BY created_at"
            ).fetchall()
        ]

    def _logger(self, store):
        worktree = Path(store.worktree_dir)
        logger = RunLogger(str(worktree), task_id="t1", run_id="run-1")
        logger.attach_storage(store)
        return logger

    def _row(self, store, resource_id):
        return store.connection.execute(
            "SELECT * FROM task_resources WHERE resource_id = ?",
            (resource_id,),
        ).fetchone()


# -- 1..6: round trips -------------------------------------------------------

class RoundTripTest(CompressionTestBase):
    def _read_via_fs(self, store, path, **kwargs):
        logger = self._logger(store)
        result = local_fs_read(logger, path=path, **kwargs)
        self.assertEqual(result["status"], "done", result)
        return result

    def test_1_json_local_fs_read_is_byte_identical(self):
        payload = big_json()
        plain, zipped = self._pair()
        for store, label in ((plain, "plain"), (zipped, "zipped")):
            store.save_resource(
                task_id="t1", run_id="run-1", resource_type="observation",
                logical_path="obs/semantic.json", content=payload,
            )
            self.assertEqual(
                self._row(store, store.read_resource(
                    current_task_id="t1",
                    resource_uri=build_resource_uri("t1", self._ids(store)[0]),
                )["resource_id"])["content_encoding"],
                ENCODING_IDENTITY if label == "plain" else ENCODING_ZLIB_JSON,
            )
        kwargs = dict(max_bytes=200000, line_limit=5000)
        full = self._read_via_fs(plain, "obs/semantic.json", **kwargs)
        zfull = self._read_via_fs(zipped, "obs/semantic.json", **kwargs)
        self.assertEqual(full["content"], zfull["content"])
        self.assertEqual(full["linesRead"], zfull["linesRead"])
        self.assertEqual(full["truncated"], zfull["truncated"])
        self.assertEqual(full["totalLines"], zfull["totalLines"])
        self.assertEqual(full["byteSize"], zfull["byteSize"])
        # And paged, not just whole-file.
        page = self._read_via_fs(plain, "obs/semantic.json", line_offset=3, line_limit=5)
        zpage = self._read_via_fs(zipped, "obs/semantic.json", line_offset=3, line_limit=5)
        self.assertEqual(page["content"], zpage["content"])
        self.assertEqual(page["nextLineOffset"], zpage["nextLineOffset"])

    def test_2_text_local_fs_read_is_byte_identical_for_every_line_ending(self):
        variants = {
            "lf": "alpha\nbeta\n" + big_text(),
            "crlf": "alpha\r\nbeta\r\n" + big_text().replace("\n", "\r\n"),
            "cr": "alpha\rbeta\r" + big_text().replace("\n", "\r"),
            "no-final-newline": big_text() + "\ntail without newline",
        }
        for name, text in variants.items():
            with self.subTest(variant=name):
                plain, zipped = self._pair()
                for store in (plain, zipped):
                    store.save_resource(
                        task_id="t1", run_id="run-1", resource_type="observation",
                        logical_path="obs/ax.txt", content=text, media_type="text/plain",
                    )
                kwargs = dict(max_bytes=200000, line_limit=5000)
                self.assertEqual(
                    self._read_via_fs(plain, "obs/ax.txt", **kwargs)["content"],
                    self._read_via_fs(zipped, "obs/ax.txt", **kwargs)["content"],
                )

    def test_3_read_resource_columns_match_the_uncompressed_store(self):
        payload = big_json()
        text = big_text()
        plain, zipped = self._pair()
        for store in (plain, zipped):
            store.save_resource(
                task_id="t1", run_id="run-1", resource_type="observation",
                logical_path="obs/json.json", content=payload,
            )
            store.save_resource(
                task_id="t1", run_id="run-1", resource_type="observation",
                logical_path="obs/text.txt", content=text, media_type="text/plain",
            )
        for path in ("obs/json.json", "obs/text.txt"):
            def read(store):
                row = store.connection.execute(
                    "SELECT resource_id FROM task_resources WHERE logical_path = ?",
                    (path,),
                ).fetchone()
                return store.read_resource(
                    current_task_id="t1",
                    resource_uri=build_resource_uri("t1", row["resource_id"]),
                )
            a, b = read(plain), read(zipped)
            # Byte-exact, not merely parse-equal. The restored column is the
            # logical text on both sides, so a caller may compare or measure
            # it without first knowing how the row happened to be stored -
            # parse-equality would let the two forms drift and still pass.
            key = "content_json" if path.endswith(".json") else "content_text"
            self.assertEqual(a[key], b[key])
            expected = (
                encode_json_resource(payload).logical_text if key == "content_json"
                else encode_text_resource(text).logical_text
            )
            self.assertEqual(a[key], expected)
            # The column that is not in use is empty, on both sides.
            other = "content_text" if key == "content_json" else "content_json"
            self.assertIsNone(a[other])
            self.assertIsNone(b[other])
            self.assertEqual(a["byte_size"], b["byte_size"])
            self.assertEqual(a["sha256"], b["sha256"])
            # The physical blob never leaks to read_resource callers.
            self.assertNotIn("content_blob", b)

    def test_4_search_regex_hits_are_identical_including_line_anchors(self):
        payload = big_json()
        plain, zipped = self._pair()
        for store in (plain, zipped):
            store.save_resource(
                task_id="t1", run_id="run-1", resource_type="tool_result",
                logical_path="obs/tree.json", content=payload,
            )
        for pattern in (r'"i": 17', r'^\s+"text":', r'"rowCount"', r'^\{$'):
            with self.subTest(pattern=pattern):
                kwargs = dict(task_id="t1", pattern=pattern, max_results=10)
                self.assertEqual(
                    [r["logical_path"] for r in plain.search_resources(**kwargs)],
                    [r["logical_path"] for r in zipped.search_resources(**kwargs)],
                )

    def test_5_extraction_digest_is_unchanged_by_the_compression_setting(self):
        # Extraction is business output: it must not be compressed (that is
        # test 8) and its recorded digest must not depend on the setting.
        payload = big_json()
        plain, zipped = self._pair()
        digests = {}
        for label, store in (("plain", plain), ("zipped", zipped)):
            saved = store.save_resource(
                task_id="t1", run_id="run-1", resource_type="extraction",
                logical_path="artifacts/extractions/x.json", content=payload,
            )
            digests[label] = (saved["byte_size"], saved["sha256"])
        self.assertEqual(digests["plain"], digests["zipped"])

    def test_6_oversized_event_run_jsonl_view_is_identical(self):
        payload = {"blob": "x" * (EVENT_PAYLOAD_OFFLOAD_THRESHOLD + 500), "n": 1}
        plain, zipped = self._pair()
        for store in (plain, zipped):
            store.append_event(
                task_id="t1", run_id="run-1", event_type="storage.big", payload=payload
            )
        def lines(store):
            logger = self._logger(store)
            result = local_fs_read(logger, path="run.jsonl", max_bytes=500000, line_limit=50)
            self.assertEqual(result["status"], "done", result)
            return result["content"]
        self.assertEqual(lines(plain), lines(zipped))
        self.assertEqual(json.loads(lines(zipped))["payload"], payload)


# -- 7..10: selection and the rollback switch --------------------------------

class SelectionTest(CompressionTestBase):
    def test_7_resources_below_the_threshold_stay_identity(self):
        zipped = self._store("zipped", resource_compression="zlib")
        zipped.save_resource(
            task_id="t1", run_id="run-1", resource_type="observation",
            logical_path="obs/small.json", content={"tiny": True},
        )
        row = self._row(zipped, self._ids(zipped)[0])
        self.assertEqual(row["content_encoding"], ENCODING_IDENTITY)
        self.assertIsNotNone(row["content_json"])

    def test_8_resource_types_outside_the_whitelist_are_never_compressed(self):
        zipped = self._store("zipped", resource_compression="zlib")
        for resource_type in ("extraction", "download", "plan", "anything_else"):
            zipped.save_resource(
                task_id="t1", run_id="run-1", resource_type=resource_type,
                logical_path=f"p/{resource_type}.json", content=big_json(),
            )
        for row in zipped.connection.execute(
            "SELECT content_encoding, logical_path FROM task_resources"
        ).fetchall():
            self.assertEqual(row["content_encoding"], ENCODING_IDENTITY, row["logical_path"])

    def test_9_native_bytes_content_stays_an_identity_blob(self):
        zipped = self._store("zipped", resource_compression="zlib")
        blob = bytes(range(256)) * 200  # ~51 KB of poorly compressable binary
        zipped.save_resource(
            task_id="t1", run_id="run-1", resource_type="observation",
            logical_path="obs/screen.bin", content=blob, media_type="application/octet-stream",
        )
        row = self._row(zipped, self._ids(zipped)[0])
        self.assertEqual(row["content_encoding"], ENCODING_IDENTITY)
        self.assertEqual(bytes(row["content_blob"]), blob)
        self.assertIsNone(row["content_json"])
        self.assertIsNone(row["content_text"])

    def test_10_none_writes_the_pre_compression_row_shape_byte_for_byte(self):
        payload = big_json()
        text = big_text()
        plain = self._store("plain", resource_compression="none")
        plain.save_resource(
            task_id="t1", run_id="run-1", resource_type="observation",
            logical_path="obs/json.json", content=payload,
        )
        plain.save_resource(
            task_id="t1", run_id="run-1", resource_type="observation",
            logical_path="obs/text.txt", content=text, media_type="text/plain",
        )
        rows = {
            row["logical_path"]: row for row in plain.connection.execute(
                "SELECT * FROM task_resources"
            ).fetchall()
        }
        legacy_json = encode_json_resource(payload)
        self.assertEqual(rows["obs/json.json"]["content_json"], legacy_json.stored_text)
        self.assertEqual(rows["obs/json.json"]["stored_byte_size"], legacy_json.stored_byte_size)
        self.assertEqual(rows["obs/json.json"]["content_encoding"], ENCODING_IDENTITY)
        self.assertIsNone(rows["obs/json.json"]["content_blob"])
        legacy_text = encode_text_resource(text)
        self.assertEqual(rows["obs/text.txt"]["content_text"], legacy_text.stored_text)
        self.assertIsNone(rows["obs/text.txt"]["stored_byte_size"])
        self.assertEqual(rows["obs/text.txt"]["content_encoding"], ENCODING_IDENTITY)


# -- 11..12: metadata --------------------------------------------------------

class MetadataTest(CompressionTestBase):
    def test_11_byte_size_and_sha256_are_logical_and_unchanged(self):
        payload = big_json()
        text = big_text()
        plain, zipped = self._pair()
        for store in (plain, zipped):
            store.save_resource(
                task_id="t1", run_id="run-1", resource_type="observation",
                logical_path="obs/json.json", content=payload,
            )
            store.save_resource(
                task_id="t1", run_id="run-1", resource_type="observation",
                logical_path="obs/text.txt", content=text, media_type="text/plain",
            )
        def metadata(store, path):
            row = store.connection.execute(
                "SELECT byte_size, sha256 FROM task_resources WHERE logical_path = ?",
                (path,),
            ).fetchone()
            return int(row["byte_size"]), str(row["sha256"])
        for path in ("obs/json.json", "obs/text.txt"):
            self.assertEqual(metadata(plain, path), metadata(zipped, path))

    def test_12_stored_byte_size_is_the_compressed_column_length(self):
        zipped = self._store("zipped", resource_compression="zlib")
        zipped.save_resource(
            task_id="t1", run_id="run-1", resource_type="observation",
            logical_path="obs/json.json", content=big_json(),
        )
        zipped.save_resource(
            task_id="t1", run_id="run-1", resource_type="observation",
            logical_path="obs/text.txt", content=big_text(), media_type="text/plain",
        )
        for row in zipped.connection.execute(
            "SELECT byte_size, stored_byte_size, length(content_blob) AS blob_len,"
            " content_encoding FROM task_resources"
        ).fetchall():
            self.assertIn(row["content_encoding"], (ENCODING_ZLIB_JSON, ENCODING_ZLIB_TEXT))
            self.assertEqual(int(row["stored_byte_size"]), int(row["blob_len"]))
            self.assertLess(int(row["stored_byte_size"]), int(row["byte_size"]))


# -- 13..15: listing visibility ---------------------------------------------

class ListingVisibilityTest(CompressionTestBase):
    def setUp(self):
        super().setUp()
        self.zipped = self._store("zipped", resource_compression="zlib")
        self.saved = self.zipped.save_resource(
            task_id="t1", run_id="run-1", resource_type="observation",
            logical_path="obs/tree.json", content=big_json(),
        )
        self.view = VirtualTaskFs(self.zipped, "t1")

    def test_13_compressed_resources_stay_in_the_file_listing(self):
        paths = [path for path, _size, _approx in self.view.list_files()]
        self.assertIn("obs/tree.json", paths)
        self.assertEqual(self.view.match_files("obs/*.json")[0][0], "obs/tree.json")
        self.assertEqual(self.view.size_of("obs/tree.json"), (self.saved["byte_size"], False))

    def test_14_local_fs_search_finds_compressed_resources(self):
        logger = self._logger(self.zipped)
        result = local_fs_search(logger, pattern=r'"i": 3', glob_pattern="**/*.json")
        self.assertEqual(result["status"], "done", result)
        self.assertIn(
            "obs/tree.json",
            [hit["relativePath"] for hit in result["results"]],
            result,
        )

    def test_15_byte_size_is_whole_file_and_bytes_read_is_this_slice(self):
        logger = self._logger(self.zipped)
        whole = local_fs_read(logger, path="obs/tree.json", max_bytes=200000, line_limit=5000)
        self.assertEqual(whole["status"], "done", whole)
        self.assertEqual(whole["byteSize"], self.saved["byte_size"])
        # Whole-file read under a budget that fits: every logical byte was
        # charged, terminators included, and nothing was truncated away.
        self.assertFalse(whole["truncated"])
        self.assertEqual(whole["bytesRead"], whole["byteSize"])
        head = local_fs_read(logger, path="obs/tree.json", line_limit=2, max_bytes=200000)
        self.assertEqual(head["byteSize"], self.saved["byte_size"])
        self.assertLess(head["bytesRead"], head["byteSize"])

    def test_15b_a_glob_only_search_never_decompresses(self):
        """Listing file names must not pay for their contents.

        The path and size a glob-only query returns both come from the row's
        columns, so decompressing to produce them is pure waste - and it is
        waste proportional to the largest thing the harness stores. Counted
        rather than timed: a timing assertion would be the flaky way to state
        the same property.
        """

        logger = self._logger(self.zipped)
        real = zlib.decompressobj
        calls = []

        def counting_decompressobj(*args, **kwargs):
            calls.append(1)
            return real(*args, **kwargs)

        with mock.patch(
            "harness.storage.resource_codec.zlib.decompressobj", counting_decompressobj
        ):
            listing = local_fs_search(logger, glob_pattern="obs/*.json")
            self.assertEqual(listing["status"], "done", listing)
            self.assertIn(
                "obs/tree.json", [hit["relativePath"] for hit in listing["results"]]
            )
            self.assertEqual(calls, [], "a glob-only search decompressed a resource")
            # And the content search still reads what it has to.
            hits = local_fs_search(logger, glob_pattern="obs/*.json", pattern=r'"i": 3')
            self.assertEqual(hits["status"], "done", hits)
            self.assertTrue(calls, "a regex search never read the resource")


# -- 16..21: corruption guards ----------------------------------------------

class CorruptionGuardTest(CompressionTestBase):
    def setUp(self):
        super().setUp()
        self.store = self._store("zipped", resource_compression="zlib")
        self.saved = self.store.save_resource(
            task_id="t1", run_id="run-1", resource_type="observation",
            logical_path="obs/tree.json", content=big_json(),
        )
        self.resource_id = self.saved["resource_id"]

    def _corrupt(self, blob=None, encoding=None, byte_size=None):
        self.store.connection.execute(
            "UPDATE task_resources SET"
            " content_blob = COALESCE(?, content_blob),"
            " content_encoding = COALESCE(?, content_encoding),"
            " byte_size = COALESCE(?, byte_size)"
            " WHERE resource_id = ?",
            (blob, encoding, byte_size, self.resource_id),
        )
        self.store.connection.commit()

    def _read(self):
        return self.store.read_resource(
            current_task_id="t1",
            resource_uri=build_resource_uri("t1", self.resource_id),
        )

    def _read_via_local_fs(self):
        return local_fs_read(
            self._logger(self.store), path="obs/tree.json",
            max_bytes=200000, line_limit=5000,
        )

    def _assert_damage_is_reported(self):
        """Assert on both read paths, because they do not share a SELECT.

        ``read_resource`` takes the whole row; the virtual view names its
        columns. A guard that needs a column the view does not fetch protects
        only the operator's path and silently lets the model's path through -
        which is exactly what happened to the ``byte_size`` cross-check, and
        pinning one path is what let it happen unnoticed.
        """

        for label, read in (
            ("read_resource", self._read),
            ("local_fs_read", self._read_via_local_fs),
        ):
            with self.subTest(path=label):
                with self.assertRaises(StorageCorruptError):
                    read()

    def test_16_truncated_blob_raises_storage_corrupt(self):
        row = self._row(self.store, self.resource_id)
        self._corrupt(blob=bytes(row["content_blob"])[:-8])
        self._assert_damage_is_reported()

    def test_17_trailing_garbage_raises_storage_corrupt(self):
        row = self._row(self.store, self.resource_id)
        self._corrupt(blob=bytes(row["content_blob"]) + b"\x00\x01\x02")
        self._assert_damage_is_reported()

    def test_18_unknown_encoding_raises_storage_corrupt(self):
        self._corrupt(encoding="gzip-v9")
        self._assert_damage_is_reported()

    def test_19_length_disagreement_with_byte_size_raises(self):
        self._corrupt(byte_size=17)
        self._assert_damage_is_reported()

    def test_19b_a_valid_stream_of_the_wrong_content_is_still_damage(self):
        # The only guard that catches this one is the length cross-check: the
        # blob decompresses cleanly, ends where it should and carries no
        # trailing bytes - it is simply not the document byte_size describes.
        # Without it a model is handed a short, complete-looking file.
        self._corrupt(blob=zlib.compress(b'{"rows": []}'))
        self._assert_damage_is_reported()

    def test_20_a_decompression_bomb_is_capped_not_allocated(self):
        # ~96 KB of zlib that would expand past 500 MB. byte_size claims a
        # small logical size, so the guard must stop the allocation on the
        # row's own budget, not the stream's ambitions.
        bomb = zlib.compress(b"\0" * (500 * 1024 * 1024))
        self.assertLess(len(bomb), 1024 * 1024)
        self._corrupt(blob=bomb, byte_size=65536)
        self._assert_damage_is_reported()

    def test_20b_the_bomb_cap_comes_from_the_row_on_every_read_path(self):
        # Both paths refuse the bomb, but refusing after materialising the
        # global 256 MB ceiling is not the same guarantee as refusing at the
        # row's 64 KB. Measure the peak instead of trusting the exception.
        bomb = zlib.compress(b"\0" * (400 * 1024 * 1024))
        self._corrupt(blob=bomb, byte_size=65536)
        for label, read in (
            ("read_resource", self._read),
            ("local_fs_read", self._read_via_local_fs),
        ):
            with self.subTest(path=label):
                tracemalloc.start()
                try:
                    with self.assertRaises(StorageCorruptError):
                        read()
                    peak = tracemalloc.get_traced_memory()[1]
                finally:
                    tracemalloc.stop()
                self.assertLess(peak, 32 * 1024 * 1024, f"{label} allocated {peak} bytes")

    def test_21_db_authoritative_read_does_not_fall_back_to_a_stale_file(self):
        # The same logical path exists on disk with different (readable)
        # content; a db-mode task's read must come from the database row or
        # fail loudly - never silently serve the stale file.
        task_dir = Path(self.store.worktree_dir) / "t1"
        (task_dir / "obs").mkdir(parents=True, exist_ok=True)
        (task_dir / "obs" / "tree.json").write_text('{"stale": true}', encoding="utf-8")
        row = self._row(self.store, self.resource_id)
        self._corrupt(blob=bytes(row["content_blob"])[:-4])
        with self.assertRaises(StorageCorruptError):
            self._read()

    def test_21b_the_run_jsonl_view_reports_a_damaged_offloaded_payload(self):
        # run.jsonl resolves an oversized payload out of task_resources through
        # its own SELECT. A row that decompresses to the wrong document must
        # not be rendered into the log as if it were the payload the event
        # referred to - the view has no other way to tell.
        store = self._store("events", resource_compression="zlib")
        store.append_event(
            task_id="t1", run_id="run-1", event_type="observation",
            payload={"rows": [{"i": i, "text": "q" * 400} for i in range(200)]},
        )
        resource_id = store.connection.execute(
            "SELECT payload_resource_id FROM run_events"
            " WHERE payload_resource_id IS NOT NULL"
        ).fetchone()[0]
        self.assertEqual(
            self._row(store, resource_id)["content_encoding"], ENCODING_ZLIB_JSON
        )
        store.connection.execute(
            "UPDATE task_resources SET content_blob = ? WHERE resource_id = ?",
            (zlib.compress(b'{"rows": []}'), resource_id),
        )
        store.connection.commit()
        with self.assertRaises(StorageCorruptError):
            list(VirtualTaskFs(store, "t1").iter_lines("run.jsonl") or [])


# -- 22..23: concurrency and transactions ------------------------------------

class TransactionTest(CompressionTestBase):
    def test_22_compression_happens_outside_the_write_transaction(self):
        store = self._store("zipped", resource_compression="zlib")
        seen = []
        real_compress = zlib.compress

        def spying_compress(data, level=-1):
            # Called on the writer's thread, so this is the writer's
            # connection; in_transaction is exactly what it says.
            try:
                in_txn = bool(store.connection.in_transaction)
            except Exception:
                in_txn = None
            seen.append(in_txn)
            return real_compress(data, level)

        with mock.patch("harness.storage.resource_codec.zlib.compress", spying_compress):
            store.save_resource(
                task_id="t1", run_id="run-1", resource_type="observation",
                logical_path="obs/tree.json", content=big_json(),
            )
        self.assertTrue(seen, "compression never ran")
        self.assertFalse(any(seen), "compression ran inside a write transaction")

    def test_23_compressed_event_resource_and_event_row_commit_together(self):
        from harness.storage.sqlite_connection import ConnectionRegistry

        worktree = self.root / "atomicity"
        real_registry = ConnectionRegistry(worktree / "harness.db")
        self.addCleanup(real_registry.close_all)

        class _FailingEventInserts:
            """Forwards everything; refuses only the run_events INSERT."""

            def __init__(self, connection):
                self._connection = connection

            def __getattr__(self, name):
                return getattr(self._connection, name)

            def execute(self, sql, *args, **kwargs):
                if "INSERT INTO run_events" in sql:
                    raise RuntimeError("simulated crash before the event insert")
                return self._connection.execute(sql, *args, **kwargs)

        class _WrappedRegistry:
            def connection(self):
                return _FailingEventInserts(real_registry.connection())

            def close_all(self):
                real_registry.close_all()

        store = SqliteStore(
            worktree / "harness.db", worktree_dir=str(worktree),
            registry=_WrappedRegistry(), resource_compression="zlib",
        )
        self.addCleanup(store.close)
        store.create_task(task_id="t1", harness_version=HARNESS_VERSION)
        store.start_run(task_id="t1", harness_version=HARNESS_VERSION, run_id="run-1")
        with self.assertRaises(RuntimeError):
            store.append_event(
                task_id="t1", run_id="run-1", event_type="huge",
                payload={"blob": "x" * (EVENT_PAYLOAD_OFFLOAD_THRESHOLD + 500)},
            )
        # The same connection the store used, un-proxied, to inspect the
        # outcome of the rolled-back transaction.
        connection = real_registry.connection()
        resources = connection.execute(
            "SELECT COUNT(*) FROM task_resources WHERE resource_type = 'event_payload'"
        ).fetchone()
        events = connection.execute(
            "SELECT COUNT(*) FROM run_events WHERE payload_resource_id IS NOT NULL"
        ).fetchone()
        # No orphan resource: one implies the other, compression or not.
        self.assertEqual(int(events[0]), 0)
        self.assertEqual(int(resources[0]), 0)


# -- 24: three-way transparency ----------------------------------------------

class BackendTransparencyCompressionTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def _fresh(self, backend):
        worktree = self.root / f"wt-{backend}"
        logger = RunLogger(str(worktree), task_id="t1", run_id="run-1")
        store = create_storage(backend=backend, worktree_dir=str(worktree))
        self.addCleanup(store.close)
        logger.attach_storage(store)
        store.create_task(task_id="t1", harness_version=HARNESS_VERSION)
        store.start_run(task_id="t1", harness_version=HARNESS_VERSION, run_id="run-1")
        return logger

    def test_24_large_offloaded_payload_reads_identically_from_every_backend(self):
        payload = {"rows": [{"i": i, "text": "z" * 300} for i in range(300)]}
        seen = {}
        for backend in ("file", "dual", "db"):
            logger = self._fresh(backend)
            stub = offload_large_tool_result(
                logger=logger, tool_name="Page.getSemanticTree",
                result=payload, step=1,
            )
            self.assertTrue(stub["_offloaded"])
            result = local_fs_read(
                logger, path=stub["savedPath"], max_bytes=200000, line_limit=5000
            )
            self.assertEqual(result["status"], "done", result)
            seen[backend] = {
                key: result[key] for key in (
                    "content", "linesRead", "totalLines", "byteSize",
                    "bytesRead", "truncated", "lineOffset", "lineLimit",
                )
            }
        self.assertEqual(len({json.dumps(v, sort_keys=True) for v in seen.values()}), 1, seen)


if __name__ == "__main__":
    unittest.main()
