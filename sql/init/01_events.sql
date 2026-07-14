-- Append-only event log. Never UPDATE/DELETE — corrections are new events (arch §4).
-- Only the events table this phase; memory/kb DDL lands in the phases that use them.
CREATE TABLE events (
    id          BIGSERIAL   PRIMARY KEY,             -- global monotonic order
    run_id      TEXT        NOT NULL,                -- which episode
    ts          TIMESTAMPTZ NOT NULL DEFAULT now(),  -- emit time
    agent       TEXT        NOT NULL,                -- emitter ("kernel" for system events)
    type        TEXT        NOT NULL,                -- dotted namespace, e.g. "claim.made"
    payload     JSONB       NOT NULL,                -- structured payload (queryable)
    reply_to    BIGINT,                              -- optional: id of event this responds to
    correlation TEXT                                 -- optional: thread/task id to group related events
);

CREATE INDEX idx_events_run     ON events(run_id, id);
CREATE INDEX idx_events_type    ON events(run_id, type);
CREATE INDEX idx_events_corr    ON events(run_id, correlation);
CREATE INDEX idx_events_payload ON events USING gin (payload);  -- query into JSON
