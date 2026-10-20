# Changelog — `asas-sync`

Versions follow semver, and the git tag matches this file: `asas-sync/v0.1.0`.
Pre-1.0, a breaking change bumps the **minor**.

Release procedure: [`RELEASING.md`](../../RELEASING.md).

## 0.1.0 — 2026-09-30

- First release, extracted from a production mirror of an HR system's
  collections and made independent of that system: a `RemoteCollection` protocol, a
  `SyncSpec`, and two package tables (`asas_sync_cursor`, `asas_sync_seen`).
- `run_pass`: key-ordered and stamp-ordered walks, the offset-ceiling
  re-anchor, stuck detection, a watermark capped by the walk's start and the
  greatest stamp read, per-page commits, resumable capped passes.
- `reconcile`: two-miss deletion, nothing marked by an incomplete walk,
  verify-by-key for inexact walks, a `purpose="reconcile"` hint so the host
  can fetch only the key and stamp.
- A compare-and-set lease per collection and organisation.
- `PassResult.changed` / `changed_keys`: the keys met with a stamp after the
  pass's starting watermark, so followers refresh only those; `None` (meaning
  everything) for a walk from nothing, a full or resumed walk, or past
  `SyncSpec.changed_keys_cap`.
- `refresh_keys`: records named by a notification read by key through the
  collection's optional `fetch_keys`, upserted with no walk and no lease.
- `upsert_newer`: an upsert that never replaces a newer row with an older one
  (equal stamps still rewrite), on Postgres and SQLite.
- `run_pass(wait_s=, retry_s=)` and `reconcile(wait_s=)`: wait for a held
  lease between short transactions, holding no connection.
- `saved_copy_is_current`: whether the mirror vouches for a copy of a record
  saved beside it.
