-- harness.storage migration 0001 - initial schema.
--
-- NOTE: PRAGMA page_size / auto_vacuum are NOT set here.  They only take
-- effect on an empty database and must run before the first CREATE TABLE,
-- so migrations.py issues them on the connection before applying this file.

CREATE TABLE tasks (
    task_id                  TEXT PRIMARY KEY,
    create_time              TEXT NOT NULL,
    last_run_at              TEXT,
    snapshot_json            TEXT NOT NULL DEFAULT '{}',

    is_deleted               INTEGER NOT NULL DEFAULT 0
                                 CHECK (is_deleted IN (0, 1)),
    deleted_at               TEXT,

    -- Hard delete is a two-phase operator action: external files are removed
    -- before the row, so a crash mid-purge must stay recognisable as "purging"
    -- rather than reverting to a runnable task.
    purge_status             TEXT NOT NULL DEFAULT 'none'
                                 CHECK (purge_status IN ('none', 'pending', 'purging', 'failed')),
    purge_requested_at       TEXT,

    created_harness_version  TEXT NOT NULL,
    last_harness_version     TEXT NOT NULL,
    created_schema_version   INTEGER NOT NULL
);

CREATE INDEX idx_tasks_live ON tasks(is_deleted, create_time);


CREATE TABLE task_runs (
    run_id           TEXT PRIMARY KEY,
    task_id          TEXT NOT NULL,
    run_number       INTEGER NOT NULL,
    started_at       TEXT NOT NULL,
    finished_at      TEXT,
    status           TEXT NOT NULL
                         CHECK (status IN ('running', 'completed', 'failed',
                                           'cancelled', 'interrupted')),
    harness_version  TEXT NOT NULL,
    process_id       INTEGER,
    host_name        TEXT,
    error_json       TEXT,

    UNIQUE (task_id, run_number),
    -- Redundant with the PK, but composite child FKs need a UNIQUE parent
    -- index on exactly these columns in this order.
    UNIQUE (task_id, run_id),

    FOREIGN KEY (task_id)
        REFERENCES tasks(task_id)
        ON DELETE CASCADE
);

CREATE INDEX idx_task_runs_task ON task_runs(task_id, run_number);


CREATE TABLE task_resources (
    resource_id       TEXT PRIMARY KEY,
    task_id           TEXT NOT NULL,
    run_id            TEXT NOT NULL,

    resource_type     TEXT NOT NULL,
    logical_path      TEXT NOT NULL,
    media_type        TEXT NOT NULL,
    content_encoding  TEXT NOT NULL DEFAULT 'identity',

    -- Exactly one storage form.  external_path exists because the harness
    -- never holds the bytes of a Download.start file: the ABCP platform
    -- writes it to disk and only a receipt comes back.
    content_json      TEXT,
    content_text      TEXT,
    content_blob      BLOB,
    external_path     TEXT,

    metadata_json     TEXT NOT NULL DEFAULT '{}',
    -- Nullable: an external file may be unreadable when the row is written.
    byte_size         INTEGER,
    sha256            TEXT,
    created_at        TEXT NOT NULL,

    -- Same logical_path may be rewritten within one run (downloads, coding
    -- agent output).  Older versions stay addressable so historical
    -- run_events.payload_resource_id never resolves to newer content.
    resource_version       INTEGER NOT NULL DEFAULT 1,
    is_current             INTEGER NOT NULL DEFAULT 1
                               CHECK (is_current IN (0, 1)),
    supersedes_resource_id TEXT,

    UNIQUE (task_id, resource_id),

    CHECK (
        (content_json  IS NOT NULL) +
        (content_text  IS NOT NULL) +
        (content_blob  IS NOT NULL) +
        (external_path IS NOT NULL) = 1
    ),

    FOREIGN KEY (task_id)
        REFERENCES tasks(task_id)
        ON DELETE CASCADE,

    FOREIGN KEY (task_id, run_id)
        REFERENCES task_runs(task_id, run_id)
        ON DELETE CASCADE
);

CREATE UNIQUE INDEX uq_current_resource_path
    ON task_resources(task_id, run_id, logical_path)
    WHERE is_current = 1;

CREATE INDEX idx_task_resources_type
    ON task_resources(task_id, resource_type, created_at);

CREATE INDEX idx_task_resources_path
    ON task_resources(task_id, logical_path, resource_version);


CREATE TABLE run_events (
    event_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id             TEXT NOT NULL,
    run_id              TEXT NOT NULL,

    event_time          TEXT NOT NULL,
    event_type          TEXT NOT NULL,
    actor_type          TEXT,
    worker_id           TEXT,

    -- Small payloads inline; oversized ones move to task_resources and only
    -- the reference stays here.
    payload_json        TEXT,
    payload_resource_id TEXT,
    payload_byte_size   INTEGER NOT NULL,

    CHECK (
        (payload_json IS NOT NULL) !=
        (payload_resource_id IS NOT NULL)
    ),

    FOREIGN KEY (task_id)
        REFERENCES tasks(task_id)
        ON DELETE CASCADE,

    FOREIGN KEY (task_id, run_id)
        REFERENCES task_runs(task_id, run_id)
        ON DELETE CASCADE,

    FOREIGN KEY (task_id, payload_resource_id)
        REFERENCES task_resources(task_id, resource_id)
);

CREATE INDEX idx_run_events_task_event  ON run_events(task_id, event_id);
CREATE INDEX idx_run_events_run_event   ON run_events(run_id, event_id);
CREATE INDEX idx_run_events_task_type   ON run_events(task_id, event_type, event_id);
CREATE INDEX idx_run_events_worker      ON run_events(task_id, worker_id, event_id);


CREATE TABLE task_snapshots (
    task_id          TEXT NOT NULL,
    snapshot_key     TEXT NOT NULL,
    value_json       TEXT NOT NULL,
    -- Concurrency control version, unrelated to harness or schema versions.
    revision         INTEGER NOT NULL,
    updated_at       TEXT NOT NULL,
    updated_run_id   TEXT NOT NULL,

    PRIMARY KEY (task_id, snapshot_key),

    FOREIGN KEY (task_id)
        REFERENCES tasks(task_id)
        ON DELETE CASCADE,

    FOREIGN KEY (task_id, updated_run_id)
        REFERENCES task_runs(task_id, run_id)
);


CREATE TABLE task_plan_versions (
    task_id               TEXT NOT NULL,
    plan_version          INTEGER NOT NULL,
    run_id                TEXT NOT NULL,
    accepted_at           TEXT NOT NULL,

    plan_hash             TEXT NOT NULL,
    previous_plan_version INTEGER,
    replan_reason         TEXT,

    plan_json             TEXT NOT NULL,
    diff_json             TEXT NOT NULL DEFAULT '{}',
    validator_review_json TEXT,

    PRIMARY KEY (task_id, plan_version),

    FOREIGN KEY (task_id)
        REFERENCES tasks(task_id)
        ON DELETE CASCADE,

    FOREIGN KEY (task_id, run_id)
        REFERENCES task_runs(task_id, run_id)
);


CREATE TABLE task_plan_reviews (
    review_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id             TEXT NOT NULL,
    run_id              TEXT NOT NULL,
    review_sequence     INTEGER NOT NULL,
    reviewed_at         TEXT NOT NULL,

    candidate_hash      TEXT NOT NULL,
    replan_reason       TEXT,
    decision            TEXT NOT NULL,
    candidate_plan_json TEXT NOT NULL,
    review_json         TEXT NOT NULL,

    UNIQUE (task_id, review_sequence),

    FOREIGN KEY (task_id)
        REFERENCES tasks(task_id)
        ON DELETE CASCADE,

    FOREIGN KEY (task_id, run_id)
        REFERENCES task_runs(task_id, run_id)
);


CREATE TABLE worker_trace_events (
    trace_event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id        TEXT NOT NULL,
    run_id         TEXT NOT NULL,
    worker_id      TEXT NOT NULL,
    sequence_no    INTEGER NOT NULL,
    trace_type     TEXT,
    trace_json     TEXT NOT NULL,
    created_at     TEXT NOT NULL,

    UNIQUE (task_id, run_id, worker_id, sequence_no),

    FOREIGN KEY (task_id)
        REFERENCES tasks(task_id)
        ON DELETE CASCADE,

    FOREIGN KEY (task_id, run_id)
        REFERENCES task_runs(task_id, run_id)
        ON DELETE CASCADE
);

CREATE INDEX idx_worker_trace_lookup
    ON worker_trace_events(task_id, run_id, worker_id, sequence_no);


CREATE TABLE strategy_attempts (
    attempt_id             INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id                TEXT NOT NULL,
    run_id                 TEXT NOT NULL,
    phase_id               TEXT,
    worker_id              TEXT,

    strategy_ids_json      TEXT NOT NULL DEFAULT '[]',
    status                 TEXT,
    status_category        TEXT,
    validated_status       TEXT,
    failure_classification TEXT,
    row_count              INTEGER,
    artifact_count         INTEGER NOT NULL DEFAULT 0,
    created_at             TEXT NOT NULL,

    FOREIGN KEY (task_id)
        REFERENCES tasks(task_id)
        ON DELETE CASCADE,

    FOREIGN KEY (task_id, run_id)
        REFERENCES task_runs(task_id, run_id)
        ON DELETE CASCADE
);

CREATE INDEX idx_strategy_attempts_task
    ON strategy_attempts(task_id, created_at);

CREATE INDEX idx_strategy_attempts_phase
    ON strategy_attempts(task_id, phase_id, created_at);
