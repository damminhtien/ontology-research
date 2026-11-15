"""Project the canonical event log into PostgreSQL operational indexes."""

from __future__ import annotations

import argparse
import os
import sys
import time
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if REPO_ROOT.as_posix() not in sys.path:
    sys.path.insert(0, REPO_ROOT.as_posix())

from foundry.events import EventLog  # noqa: E402
from foundry.identity import IdentityService  # noqa: E402
from foundry.merge import apply_identity_event  # noqa: E402
from foundry.paths import production_event_log_path  # noqa: E402
from foundry.postgres import PostgresDatabase, PostgresIdentityStore  # noqa: E402
from foundry.postgres_readmodel import PostgresReadModelStore  # noqa: E402
from foundry.projector import Projector  # noqa: E402


def project_once(
    log: EventLog,
    identity: IdentityService,
    model: PostgresReadModelStore,
    *,
    limit: int | None = None,
) -> dict[str, object]:
    """Apply one committed suffix and return stable counters."""
    checkpoint = model.checkpoint_sequence or 0
    projector = Projector(model)
    counts: Counter[str] = Counter()
    applied = skipped = 0
    for event in log.iter_after_sequence(checkpoint):
        if limit is not None and applied + skipped >= limit:
            break
        with model.transaction():
            if model.is_applied(event.event_id):
                counts["duplicates"] += 1
                continue
            apply_identity_event(identity, event)
            if projector.apply(event):
                applied += 1
            else:
                model.mark_skipped(event.event_id, event.sequence)
                skipped += 1
            counts[event.event_type] += 1
    return {
        "applied": applied,
        "skipped": skipped,
        "duplicates": counts["duplicates"],
        "by_type": {key: value for key, value in sorted(counts.items()) if key != "duplicates"},
        "checkpoint_sequence": model.checkpoint_sequence,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--event-log",
        type=Path,
        default=production_event_log_path(),
    )
    parser.add_argument(
        "--database-url",
        default=os.environ.get("FOUNDRY_DATABASE_URL"),
        help="PostgreSQL DSN (defaults to FOUNDRY_DATABASE_URL)",
    )
    parser.add_argument("--rebuild", action="store_true", help="clear projections then replay")
    parser.add_argument("--verify", action="store_true", help="verify checkpoint against the log")
    parser.add_argument("--follow", action="store_true", help="poll for new events continuously")
    parser.add_argument("--limit", type=int, help="maximum events to process in this invocation")
    args = parser.parse_args()
    if not args.database_url:
        parser.error("--database-url or FOUNDRY_DATABASE_URL is required")
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be positive")
    if args.rebuild and args.verify:
        parser.error("--rebuild and --verify are separate operations")
    if args.rebuild and args.limit is not None:
        parser.error("--rebuild cannot be combined with a partial --limit")

    database = PostgresDatabase(args.database_url)
    database.migrate()
    log = EventLog(args.event_log)
    model = PostgresReadModelStore(database)
    identity = IdentityService(PostgresIdentityStore(database))
    if args.rebuild:
        database.reset_projection()
    if args.verify:
        result = database.verify_projection(log.last_sequence)
        print(result)
        if not result["consistent"]:
            raise SystemExit(1)
        return

    while True:
        result = project_once(log, identity, model, limit=args.limit)
        if not args.follow or result["applied"] or result["skipped"] or result["duplicates"]:
            print(result)
        if not args.follow or args.limit is not None:
            break
        time.sleep(1.0)

    verification = database.verify_projection(log.last_sequence)
    if not verification["consistent"] and not args.follow and args.limit is None:
        print({"verification": verification})
        raise SystemExit(1)


if __name__ == "__main__":
    main()
