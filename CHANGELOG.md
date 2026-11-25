# Changelog

## 0.2.0

Added, alongside everything in 0.1.0, which keeps working unchanged:

- JSON jobs: `create_job` (with an `Idempotency-Key`), `create_batch`, `get_job`,
  `list_jobs`, `download_output(id, index)` for multi-output jobs, `wait_for_job`.
- Designs: `compile_design`, `render_design`.
- Direct uploads: `upload` (bytes, a path or a file, streamed in chunks; the API
  key is never sent to the upload URL), `get_asset`, `delete_asset`.
- `operations()`, `usage(...)` with `group_by`, `delete_end_user`, `create_embed_token`.
- Storage destinations: list, create, get, update, delete, test, presign.
- Webhook endpoints: list, create, get, update, delete, rotate, deliveries, test.
- `verify_webhook` (also `SnappedCut.verify_webhook`): constant-time check of
  `X-Snappedcut-Signature`, refusing anything over five minutes old.
- `SnappedCutError` gains `message`, `code`, `is_out_of_credits` and `is_rate_limited`.
- User-Agent is `snappedcut-python/0.2.0`.

## 0.1.0 — first release
