# snappedcut

Python SDK for the [Snapped Cut](https://snappedcut.com) cloud render API.
**The standard library and nothing else** — no dependencies, so it installs into
a locked-down CI image or a Lambda without pulling a tree behind it.

```sh
pip install snappedcut
```

```python
from pathlib import Path
from snappedcut import SnappedCut

snapcut = SnappedCut()                      # reads SNAPCUT_KEY

clip = snapcut.upload("clip.webm", "video/webm")   # a path streams, chunk by chunk
design = {"width": 1080, "height": 1920, "clips": [{"type": "video", "asset": clip["id"]}]}
outputs = [{"format": "mp4", "preset": "vertical"}, {"format": "webm", "preset": "hd720"}]
created = snapcut.render_design(design, {"outputs": outputs}, idempotency_key="order-42")

job = snapcut.wait_for_job(created["job"]["id"])
for output in job["outputs"]:
    data = snapcut.download_output(job["id"], output["index"])
    Path(f"out-{output['index']}.{output['format']}").write_bytes(data)
```

The same `idempotency_key` sent twice returns the first job (`created["idempotent"]`)
instead of charging again, so a retry after a dropped connection is safe. Presets:
`source`, `vertical`, `square`, `portrait`, `landscape`, `hd720`, `uhd2160` —
`snapcut.operations()` lists them live.

Get a key from [your account page](https://snappedcut.com/account) — it looks
like `sk_snapcut_…`. Keep it on a server; it spends credits.

## Verifying webhooks

```python
from snappedcut import verify_webhook

if not verify_webhook(secret, raw_body, request.headers["X-Snappedcut-Signature"]):
    return 400
```

Pass the body exactly as it arrived, not re-serialised JSON. A signature more
than five minutes from now is refused, so a captured delivery cannot be replayed.

## API

| | |
| --- | --- |
| `SnappedCut(key=None, base=..., timeout=120.0)` | `key` falls back to `$SNAPCUT_KEY`. `base` defaults to `https://snappedcut.com`. |
| `account()` | `AccountSummary` — who the key belongs to, credits left, the price list. |
| `render(bundle, kind="cloud-render")` | Queue a render of a project bundle. `bundle` is `bytes` or any file object. |
| `job(id)` / `jobs()` | One `Job`, or every job on the account. |
| `output(id)` | The rendered bytes. Raises unless the job is done. |
| `wait(id, timeout=1800, on_state=None)` | Polls to completion, returns the bytes. |
| `create_job(request, idempotency_key=None)` | A JSON job: a design, an uploaded bundle, or a tool over uploaded media. |
| `create_batch(jobs, defaults=None)` | Many jobs in one request; each succeeds or is refused on its own. |
| `get_job(id)` / `list_jobs()` / `wait_for_job(id, timeout=1800)` | The job as a dict, with every output. |
| `download_output(id, index=0)` | One output's bytes. |
| `compile_design(design, vars=None)` / `render_design(design, options=None, idempotency_key=None)` | Check a design, or render it. |
| `upload(data, content_type, name=None)` / `get_asset(id)` / `delete_asset(id)` | Upload once, use by id. `data` is bytes, a path or a binary file. |
| `operations()` | Every operation and output preset. |
| `usage(from_=None, to=None, end_user_id=None, group_by=None)` / `delete_end_user(id)` | Spend, grouped; and erasing one end user. |
| `create_embed_token(pk, end_user_id, ...)` | A session token for the embedded editor. |
| `list_destinations()` / `create_destination` / `get_destination` / `update_destination` / `delete_destination` / `test_destination` / `presign_destination` | Your own S3-compatible buckets as render targets. A failed `test_destination` is returned (`ok: False`), not raised. |
| `list_webhooks()` / `create_webhook(url, events=None)` / `get_webhook` / `update_webhook` / `delete_webhook` / `rotate_webhook` / `webhook_deliveries` / `test_webhook` | Webhook endpoints. The secret comes back once, from create and rotate. |
| `verify_webhook(secret, raw_body, header, now_ms=None)` | Also `SnappedCut.verify_webhook`. |

`account()`, `render()`, `job()` and `wait()` return frozen dataclasses, and
`py.typed` ships, so an editor and `mypy --strict` both see real types. The
calls added in 0.2 return the JSON as plain dicts, so a field the API adds
within a version simply appears rather than breaking anything.

## Large bundles resume themselves

`render()` sends a bundle under 64 MiB in one request. Above that it uploads in
chunks, and if the connection drops it carries on from **the server's** count of
what it holds, not its own — a `409` carries the expected offset and the client
believes it. Three attempts per chunk; a `4xx` that is not `409` is a real
refusal (no credits, bad bundle, bad key) and stops immediately rather than
retrying its way through your balance.

## You cannot set the price

There is no `seconds` argument, deliberately. The server reads the timeline
duration out of the bundle's own `project.json` and prices on that, because a
duration supplied by the caller is a price supplied by the caller. What you were
actually charged comes back as `charged`, with the server's reasoning in
`priced`.

## Errors

Every failure raises `SnappedCutError` with the HTTP `status`, the server's
`message` and `code`, and `retry_after_ms` on it, and the two failures you
actually branch on have properties so you need not parse a message
(`is_out_of_credits` / `is_rate_limited` are the same, under the names every
Snapped Cut SDK shares):

```python
from snappedcut import SnappedCutError

try:
    snapcut.render(bundle)
except SnappedCutError as error:
    if error.out_of_credits:        # 402
        top_up()
    elif error.rate_limited:        # 429
        time.sleep(error.retry_after_ms / 1000)
    else:
        raise
```

A server that cannot be reached at all is `status == 0`, which is the one case
worth sending again. A job that fails server-side has **already been refunded**
by the time `wait()` raises, so there is nothing to reconcile.

`repr()` never contains the key — a repr ends up in logs and tracebacks.

## What this is not

Snapped Cut edits video **on the device**: decode, cut, filter and encode all
run in a browser on WebCodecs. There is no API for them and there cannot be —
no SDK can make Python do that. What this covers is the one thing that *does*
happen on our machines: a cloud render, which is opt-in and costs credits.

To build the bundles this takes, see
[`@snappedcut/mcp`](https://github.com/snappedcut-dev/mcp) or the editor's
**Share → Export bundle**. A bundle whose media is missing is refused at submit,
before any charge — it would otherwise render successfully as the right number
of blank frames, and nothing downstream could tell.

## Releases

Published from CI through [PyPI trusted publishing](https://docs.pypi.org/trusted-publishers/),
which signs an attestation linking each wheel back to the commit and workflow
run that built it. Nothing is published from anyone's laptop.

MIT
