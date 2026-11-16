# Event schema v6: assertion and source-record lineage

Schema v6 separates assertion record time from world valid time and makes
source-record identity explicit.

## New document and entity writes

Document registration requires the exact source bytes (`content`) or a
lowercase SHA-256 supplied by the trusted source. When both are present, the
writer verifies that they match. A source record keeps one stable key across
versions; each distinct content hash identifies a version. Retrying the same
record version returns the original `DocumentRegistered` event.

Structured entity events carry the same stable source-record key and a hash of
the normalized source fields stored in that event. Replaying entity, document,
and assertion events reconstructs identity and assertion status from the log.

## Assertion fields

`recorded_at` is the event's record time. `valid_from` and `valid_until` remain
the half-open world-validity interval. Extractor `model_score` is separate from
categorical `evidence_quality`; neither means that a claim has a given
probability of being true. Character evidence spans are checked against source
text and preserve the exact matched text.

When a new assertion supersedes an older one, replay marks the predecessor as
`superseded`. A retraction with no replacement marks it `retracted`. A
predecessor can have only one replacement or retraction, and retrying that
operation returns the original event. Uncorrected overlapping claims remain
visible as conflicts.

## Historical records

The v5-to-v6 upcaster maps historical `confidence` to `model_score`, uses the
stored event time for missing `recorded_at`, and supplies `unknown` evidence
quality and `asserted` status. Historical documents and entities do not
contain source bytes. Their generated SHA-256 values are deterministic
fingerprints of stored metadata or fields, not hashes of the unavailable
original bytes. Consumers that require byte-level proof must fetch and hash
the original source independently.

Downstream consumers should read `model_score` in place of the former
assertion `confidence` field. The old event payload remains readable through
the versioned upcaster chain.
