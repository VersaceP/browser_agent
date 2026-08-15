"""Schema, migration and pragma behaviour for harness.storage."""

import sqlite3
import tempfile
import unittest
from pathlib import Path

from harness.storage.factory import open_database
from harness.storage.migrations import (
    MIGRATIONS,
    SCHEMA_VERSION,
    apply_migrations,
    current_schema_version,
    split_sql_statements,
)
from harness.storage.sqlite_connection import (
    ConnectionRegistry,
    StorageClosedError,
    connect,
    database_is_empty,
    freelist_count,
    maybe_incremental_vacuum,
    write_transaction,
)


class StorageSchemaTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "harness.db"
        self.connection = open_database(self.db_path)
        self.addCleanup(self.connection.close)
        self.addCleanup(self._tmp.cleanup)

    def _pragma(self, name):
        row = self.connection.execute(f"PRAGMA {name}").fetchone()
        return row[0] if row else None

    # -- migrations --------------------------------------------------------
    def test_migrations_apply_and_record_version(self):
        self.assertEqual(current_schema_version(self.connection), SCHEMA_VERSION)
        rows = self.connection.execute(
            "SELECT version, migration_name FROM schema_migrations ORDER BY version"
        ).fetchall()
        self.assertEqual([int(row[0]) for row in rows], [m.version for m in MIGRATIONS])

    def test_migrations_are_idempotent(self):
        self.assertEqual(apply_migrations(self.connection), [])
        self.assertEqual(current_schema_version(self.connection), SCHEMA_VERSION)

    def test_reopening_an_existing_database_applies_nothing(self):
        self.connection.close()
        reopened = open_database(self.db_path)
        self.addCleanup(reopened.close)
        self.assertEqual(apply_migrations(reopened), [])

    def test_split_sql_statements_keeps_comments_and_literals_intact(self):
        script = (
            "CREATE TABLE demo (a TEXT); -- trailing comment\n"
            "INSERT INTO demo(a) VALUES ('semi;colon');\n"
        )
        statements = split_sql_statements(script)
        self.assertEqual(len(statements), 2)
        self.assertIn("semi;colon", statements[1])

    def test_all_expected_tables_exist(self):
        rows = self.connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        names = {row[0] for row in rows}
        for expected in (
            "schema_migrations",
            "tasks",
            "task_runs",
            "task_resources",
            "run_events",
            "task_snapshots",
            "task_plan_versions",
            "task_plan_reviews",
            "worker_trace_events",
            "strategy_attempts",
        ):
            self.assertIn(expected, names)

    # -- pragmas -----------------------------------------------------------
    def test_wal_and_concurrency_pragmas_are_active(self):
        self.assertEqual(str(self._pragma("journal_mode")).lower(), "wal")
        self.assertEqual(int(self._pragma("foreign_keys")), 1)
        self.assertEqual(int(self._pragma("synchronous")), 1)  # NORMAL
        self.assertEqual(int(self._pragma("busy_timeout")), 5000)

    def test_auto_vacuum_is_incremental(self):
        # 2 == INCREMENTAL. It only binds while the database is empty, so a
        # regression here is unrecoverable without a full VACUUM.
        self.assertEqual(int(self._pragma("auto_vacuum")), 2)

    def test_auto_vacuum_survives_reconnect(self):
        self.connection.close()
        reopened = open_database(self.db_path)
        self.addCleanup(reopened.close)
        self.assertEqual(int(reopened.execute("PRAGMA auto_vacuum").fetchone()[0]), 2)

    def test_database_is_empty_detects_a_fresh_file(self):
        fresh_path = Path(self._tmp.name) / "fresh.db"
        fresh = connect(fresh_path)
        self.addCleanup(fresh.close)
        self.assertTrue(database_is_empty(fresh))
        apply_migrations(fresh)
        self.assertFalse(database_is_empty(fresh))

    def test_incremental_vacuum_skips_below_threshold(self):
        self.assertLess(freelist_count(self.connection), 1000)
        self.assertEqual(maybe_incremental_vacuum(self.connection), 0)

    def test_registry_hands_out_one_connection_and_stays_closed(self):
        registry = ConnectionRegistry(Path(self._tmp.name) / "registry.db")
        first = registry.connection()
        self.assertIs(registry.connection(), first)
        registry.close_all()
        # Reopening lazily after close would turn close() into a no-op and
        # hide a write-after-shutdown bug instead of surfacing it.
        with self.assertRaises(StorageClosedError):
            registry.connection()

    # -- transactions ------------------------------------------------------
    def test_write_transaction_rolls_back_on_error(self):
        with self.assertRaises(ValueError):
            with write_transaction(self.connection):
                self.connection.execute(
                    "INSERT INTO schema_migrations(version, migration_name, applied_at)"
                    " VALUES (999, 'bogus', 'now')"
                )
                raise ValueError("boom")
        row = self.connection.execute(
            "SELECT count(*) FROM schema_migrations WHERE version = 999"
        ).fetchone()
        self.assertEqual(int(row[0]), 0)

    # -- constraints -------------------------------------------------------
    def _seed_task_and_run(self):
        with write_transaction(self.connection):
            self.connection.execute(
                "INSERT INTO tasks(task_id, create_time, snapshot_json,"
                " created_harness_version, last_harness_version, created_schema_version)"
                " VALUES ('t1', '2026-08-14T00:00:00+00:00', '{}', 'v', 'v', 1)"
            )
            self.connection.execute(
                "INSERT INTO task_runs(run_id, task_id, run_number, started_at,"
                " status, harness_version)"
                " VALUES ('r1', 't1', 1, '2026-08-14T00:00:00+00:00', 'running', 'v')"
            )

    def test_foreign_keys_are_enforced(self):
        with self.assertRaises(sqlite3.IntegrityError):
            with write_transaction(self.connection):
                self.connection.execute(
                    "INSERT INTO task_runs(run_id, task_id, run_number, started_at,"
                    " status, harness_version)"
                    " VALUES ('r0', 'missing-task', 1, 'now', 'running', 'v')"
                )

    def test_cascade_delete_removes_children(self):
        self._seed_task_and_run()
        with write_transaction(self.connection):
            self.connection.execute(
                "INSERT INTO run_events(task_id, run_id, event_time, event_type,"
                " payload_json, payload_byte_size)"
                " VALUES ('t1', 'r1', 'now', 'demo', '{}', 2)"
            )
        with write_transaction(self.connection):
            self.connection.execute("DELETE FROM tasks WHERE task_id = 't1'")
        row = self.connection.execute("SELECT count(*) FROM run_events").fetchone()
        self.assertEqual(int(row[0]), 0)

    def test_run_event_payload_is_exclusive(self):
        self._seed_task_and_run()
        with self.assertRaises(sqlite3.IntegrityError):
            with write_transaction(self.connection):
                self.connection.execute(
                    "INSERT INTO run_events(task_id, run_id, event_time, event_type,"
                    " payload_json, payload_resource_id, payload_byte_size)"
                    " VALUES ('t1', 'r1', 'now', 'demo', '{}', 'res-1', 2)"
                )
        with self.assertRaises(sqlite3.IntegrityError):
            with write_transaction(self.connection):
                self.connection.execute(
                    "INSERT INTO run_events(task_id, run_id, event_time, event_type,"
                    " payload_byte_size) VALUES ('t1', 'r1', 'now', 'demo', 0)"
                )

    def _insert_resource(self, resource_id, columns, values, logical_path="a.json",
                         is_current=1):
        self.connection.execute(
            "INSERT INTO task_resources(resource_id, task_id, run_id, resource_type,"
            f" logical_path, media_type, created_at, is_current, {columns})"
            " VALUES (?, 't1', 'r1', 'observation', ?, 'application/json',"
            f" '2026-08-14T00:00:00+00:00', ?, {values})",
            (resource_id, logical_path, is_current),
        )

    def test_resource_requires_exactly_one_storage_form(self):
        self._seed_task_and_run()
        with write_transaction(self.connection):
            self._insert_resource("ok-json", "content_json", "'{}'")
            # An external download: the harness never held these bytes, so
            # byte_size and sha256 stay unknown.
            self._insert_resource(
                "ok-external", "external_path", "'downloads/x.csv'",
                logical_path="downloads/x.csv",
            )
        with self.assertRaises(sqlite3.IntegrityError):
            with write_transaction(self.connection):
                self._insert_resource(
                    "two-forms", "content_json, content_text", "'{}', 'text'",
                    logical_path="b.json",
                )
        with self.assertRaises(sqlite3.IntegrityError):
            with write_transaction(self.connection):
                self._insert_resource(
                    "no-form", "metadata_json", "'{}'", logical_path="c.json",
                )

    def test_only_one_current_resource_per_logical_path(self):
        self._seed_task_and_run()
        with write_transaction(self.connection):
            self._insert_resource("v1", "content_json", "'{}'")
        with self.assertRaises(sqlite3.IntegrityError):
            with write_transaction(self.connection):
                self._insert_resource("v2", "content_json", "'{}'")
        # Superseding the old version frees the path for a new current row.
        with write_transaction(self.connection):
            self.connection.execute(
                "UPDATE task_resources SET is_current = 0 WHERE resource_id = 'v1'"
            )
            self._insert_resource("v2", "content_json", "'{}'")
        row = self.connection.execute(
            "SELECT count(*) FROM task_resources WHERE logical_path = 'a.json'"
        ).fetchone()
        self.assertEqual(int(row[0]), 2)

    def test_task_run_status_is_constrained(self):
        self._seed_task_and_run()
        with self.assertRaises(sqlite3.IntegrityError):
            with write_transaction(self.connection):
                self.connection.execute(
                    "UPDATE task_runs SET status = 'exploded' WHERE run_id = 'r1'"
                )

    def test_worker_trace_sequence_is_unique(self):
        self._seed_task_and_run()
        with write_transaction(self.connection):
            self.connection.execute(
                "INSERT INTO worker_trace_events(task_id, run_id, worker_id,"
                " sequence_no, trace_json, created_at)"
                " VALUES ('t1', 'r1', 'browser-001', 1, '{}', 'now')"
            )
        with self.assertRaises(sqlite3.IntegrityError):
            with write_transaction(self.connection):
                self.connection.execute(
                    "INSERT INTO worker_trace_events(task_id, run_id, worker_id,"
                    " sequence_no, trace_json, created_at)"
                    " VALUES ('t1', 'r1', 'browser-001', 1, '{}', 'now')"
                )


if __name__ == "__main__":
    unittest.main()
