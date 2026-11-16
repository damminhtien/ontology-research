# PostgreSQL operational projections

PostgreSQL is the persistent operational store for canonical identity indexes
and the indexed assertion, relation, entity, and track read model. The
append-only event log remains authoritative. Parquet and DuckDB remain the
analytical lane; Oxigraph remains an evaluation backend.

## External data root

By default, runtime data is stored under `~/Data/ontology-research`:

| Data | Default path |
|---|---|
| PostgreSQL cluster | `~/Data/ontology-research/postgresql` |
| Production event log | `~/Data/ontology-research/data/production.jsonl` |
| Demo event log | `~/Data/ontology-research/data/events.jsonl` |
| Parquet lake | `~/Data/ontology-research/data/lake` |
| Reference snapshots | `~/Data/ontology-research/data/reference` |
| Raw crawl runs | `~/Data/ontology-research/data/crawl-runs` |

The repository's `data` path is a local symlink to the external data directory
for older scripts. New defaults use the external path directly. Nothing under
this root is committed to Git. Set `FOUNDRY_DATA_ROOT` to move the full root; use
`FOUNDRY_ADMIN_LOG`, `FOUNDRY_EVENT_LOG`, `FOUNDRY_LAKE_ROOT`,
`FOUNDRY_REFERENCE_ROOT`, or `FOUNDRY_CRAWL_ROOT` to override individual
locations. `FOUNDRY_POSTGRES_DATA` names the intended location when initializing
a PostgreSQL cluster. A PostgreSQL service must also be configured to use that
directory; the Homebrew service on this machine uses a symlink from its standard
cluster path into `~/Data/ontology-research/postgresql`.

## PostgreSQL setup

The local development setup uses PostgreSQL 18 and the built-in `pg_trgm`
extension. The schema migration creates `pg_trgm` and its alias-search index;
JSONB, B-tree temporal indexes, and partial indexes are built into PostgreSQL.

```bash
brew services start postgresql@18
createdb ontology_research
export FOUNDRY_DATABASE_URL=postgresql:///ontology_research
make project-events
```

The migration runner needs permission to create the extension. Run the first
migration as the database owner, then grant the application role only the
table permissions it needs. The PostgreSQL database should be local to the
deployment; do not put a production DSN in source control.

## Start and verify the projector

Run a one-time catch-up:

```bash
FOUNDRY_DATABASE_URL=postgresql:///ontology_research \
  .venv/bin/python tools/project_events.py
FOUNDRY_DATABASE_URL=postgresql:///ontology_research \
  .venv/bin/python tools/project_events.py --verify
```

Run the standalone worker continuously in its own process:

```bash
FOUNDRY_DATABASE_URL=postgresql:///ontology_research \
  .venv/bin/python tools/project_events.py --follow
```

The projector serializes writers with a PostgreSQL advisory transaction lock.
Each event's identity changes, read-model writes, idempotency record, last
event time, and checkpoint commit in one database transaction. A failed
projection rolls back all of them; the worker retries from the previous
checkpoint. Complete segments at or below the checkpoint are skipped using
the event-log segment index. Only the checkpoint segment and newer segments
are read.

The console reads PostgreSQL directly when `FOUNDRY_DATABASE_URL` is set. Start
the worker before the API so queries do not serve an unprojected tail. The
`make console` target uses the local `ontology_research` DSN unless an explicit
`FOUNDRY_DATABASE_URL` is set.

## Rebuild and consistency check

The projection is disposable and can be recreated from the event log without
loading the full stream into memory:

```bash
FOUNDRY_DATABASE_URL=postgresql:///ontology_research \
  .venv/bin/python tools/project_events.py --rebuild
FOUNDRY_DATABASE_URL=postgresql:///ontology_research \
  .venv/bin/python tools/project_events.py --verify
```

`--rebuild` clears only PostgreSQL identity/read projections and checkpoints;
it does not alter the event log or the lake. Verification compares the stored
checkpoint with the event log's committed last sequence. A rebuild uses the
same event applier as incremental projection, so merge/split state and
assertion provenance are replayed identically.

PostgreSQL integration tests use a dedicated database:

```bash
createdb ontology_research_test
make postgres-test
```

The default `make check` stays self-contained and skips those integration tests
unless `FOUNDRY_TEST_DATABASE_URL` is set.
