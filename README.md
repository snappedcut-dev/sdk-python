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

job = snapcut.render(Path("project.zip").read_bytes())
print(f"charged {job.charged} credits for {job.priced.seconds}s")

Path("out.webm").write_bytes(snapcut.wait(job.id, on_state=lambda j: print(j.state)))
```

Get a key from [your account page](https://snappedcut.com/account) — it looks
like `sk_snapcut_…`. Keep it on a server; it spends credits.

## API

| | |
| --- | --- |
| `SnappedCut(key=None, base=..., timeout=120.0)` | `key` falls back to `$SNAPCUT_KEY`. `base` defaults to `https://snappedcut.com`. |
| `account()` | `AccountSummary` — who the key belongs to, credits left, the price list. |
| `render(bundle, kind="cloud-render")` | Queue a render. `bundle` is `bytes` or any file object. |
| `job(id)` / `jobs()` | One `Job`, or every job on the account. |
| `output(id)` | The rendered bytes. Raises unless the job is done. |
| `wait(id, timeout=1800, on_state=None)` | Polls to completion, returns the bytes. |

Everything is a frozen dataclass, and `py.typed` ships, so an editor and
`mypy --strict` both see real types.

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

Every failure raises `SnappedCutError` with the HTTP `status` on it, and the two
failures you actually branch on have properties so you need not parse a message:

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
