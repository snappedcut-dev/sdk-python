from __future__ import annotations

import contextlib
import hashlib
import hmac
import io
import json
import os
import re
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, BinaryIO, cast
from urllib.parse import quote, urlencode

VERSION = "0.2.0"
USER_AGENT = f"snappedcut-python/{VERSION}"
DEFAULT_BASE = "https://snappedcut.com"
_HEX64 = re.compile(r"[0-9a-f]{64}")

# How much of a bundle goes in one request.
#
# Cloudflare's free plan refuses a proxied request body over 100 MB and the site
# is behind it, so a 512 MB bundle cannot be one POST however well it streams.
# 64 MiB leaves room for headers and for a tighter cap on some other proxy
# without making the chunk count silly.
CHUNK = 64 * 1024 * 1024

_RETRIES = 3


class SnappedCutError(Exception):
    """A refusal from the server, with its status.

    A distinct class because the two failures callers actually branch on --
    "no credits" (402) and "slow down" (429) -- are not exceptional and should
    be catchable without parsing a message.
    """

    def __init__(
        self,
        message: str,
        status: int,
        retry_after_ms: int = 0,
        expected: int = 0,
        code: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.retry_after_ms = retry_after_ms
        # On a 409 from a chunked upload: the byte the server's copy ends at.
        self.expected = expected
        # The body's machine-readable `code`, when the server sends one.
        self.code = code

    @property
    def out_of_credits(self) -> bool:
        return self.status == 402

    @property
    def rate_limited(self) -> bool:
        return self.status == 429

    # The names every Snapped Cut SDK shares; the two above stay for 0.1 callers.
    @property
    def is_out_of_credits(self) -> bool:
        return self.status == 402

    @property
    def is_rate_limited(self) -> bool:
        return self.status == 429


def verify_webhook(
    secret: str,
    raw_body: str | bytes,
    signature_header: str | None,
    now_ms: int | None = None,
) -> bool:
    """Check an ``X-Snappedcut-Signature`` header (``t=<epoch ms>,v1=<hex>``).

    Pass the body exactly as it arrived -- re-serialised JSON will not match.
    False when ``t`` is missing or more than five minutes from now, so a captured
    delivery cannot be replayed later.
    """
    parts: dict[str, str] = {}
    for piece in (signature_header or "").split(","):
        name, _, value = piece.partition("=")
        parts[name.strip()] = value.strip()
    try:
        t = int(parts.get("t", ""))
    except ValueError:
        return False
    now = int(time.time() * 1000) if now_ms is None else now_ms
    v1 = parts.get("v1", "")
    if abs(now - t) > 300_000 or not _HEX64.fullmatch(v1):
        return False
    body = raw_body if isinstance(raw_body, bytes) else raw_body.encode()
    expected = hmac.new(secret.encode(), f"{t}.".encode() + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, v1)


def _q(value: str) -> str:
    return quote(value, safe="")


@dataclass(frozen=True)
class Priced:
    """What the server decided the job was, and therefore what it cost."""

    seconds: float
    project: str
    layers: int

    @classmethod
    def _from(cls, raw: dict[str, Any]) -> Priced:
        return cls(
            seconds=raw.get("seconds", 0),
            project=raw.get("project", ""),
            layers=raw.get("layers", 0),
        )


@dataclass(frozen=True)
class Job:
    id: str
    kind: str
    state: str
    seconds: float
    error: str | None = None
    created_at: int = 0
    updated_at: int = 0

    @classmethod
    def _from(cls, raw: dict[str, Any]) -> Job:
        return cls(
            id=raw["id"],
            kind=raw.get("kind", "cloud-render"),
            state=raw.get("state", "queued"),
            seconds=raw.get("seconds", 0),
            error=raw.get("error"),
            created_at=raw.get("createdAt", 0),
            updated_at=raw.get("updatedAt", 0),
        )


@dataclass(frozen=True)
class QueuedJob(Job):
    charged: int = 0
    credits: int = 0
    priced: Priced = field(default_factory=lambda: Priced(0, "", 0))


@dataclass(frozen=True)
class AccountSummary:
    id: str
    email: str
    credits: int
    prices: dict[str, int]


class SnappedCut:
    """A client for the Snapped Cut cloud render API.

    `key` falls back to the ``SNAPCUT_KEY`` environment variable. `opener` is
    injectable so the tests need no server.

    The calls added in 0.2 return the JSON as plain dicts, so a field the API
    adds within a version simply appears rather than breaking anything.
    """

    verify_webhook = staticmethod(verify_webhook)

    def __init__(
        self,
        key: str | None = None,
        base: str = DEFAULT_BASE,
        timeout: float = 120.0,
        opener: Callable[[urllib.request.Request, float], Any] | None = None,
    ) -> None:
        self.key = key if key is not None else os.environ.get("SNAPCUT_KEY", "")
        self.base = base.rstrip("/")
        self.timeout = timeout
        self._open = opener or (lambda req, t: urllib.request.urlopen(req, timeout=t))

    # -- plumbing ---------------------------------------------------------

    def _call(
        self,
        path: str,
        method: str = "GET",
        body: bytes | None = None,
        content_type: str | None = None,
        headers: Mapping[str, str] | None = None,
        ok: Sequence[int] = (),
        auth: bool = True,
    ) -> tuple[int, bytes]:
        sent: dict[str, str] = {"user-agent": USER_AGENT}
        if self.key and auth:
            sent["authorization"] = f"Bearer {self.key}"
        if content_type:
            sent["content-type"] = content_type
        sent.update(headers or {})

        # An absolute URL is the upload's own, which carries its own token.
        url = path if path.startswith(("http://", "https://")) else f"{self.base}{path}"
        request = urllib.request.Request(url, data=body, headers=sent, method=method)

        try:
            with self._open(request, self.timeout) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as error:
            raw = error.read()
            if error.code in ok:
                return error.code, raw
            detail: dict[str, Any] = {}
            # An error body is not guaranteed to be JSON -- a proxy may answer
            # with HTML -- and the status is the part that matters either way.
            with contextlib.suppress(ValueError, TypeError):
                detail = json.loads(raw)
            raise SnappedCutError(
                detail.get("error") or f"Snapped Cut said {error.code}.",
                error.code,
                detail.get("retryAfterMs", 0),
                detail.get("expected", 0),
                detail.get("code"),
            ) from error
        except urllib.error.URLError as error:
            # Status 0 means "never reached the server", which is the one case
            # worth sending again.
            raise SnappedCutError(f"Could not reach Snapped Cut: {error.reason}", 0) from error

    def _json(self, path: str, **kwargs: Any) -> dict[str, Any]:
        _, raw = self._call(path, **kwargs)
        parsed: dict[str, Any] = json.loads(raw) if raw else {}
        return parsed

    def _post(
        self,
        path: str,
        payload: Any,
        method: str = "POST",
        headers: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        return self._json(
            path,
            method=method,
            body=json.dumps(payload).encode(),
            content_type="application/json",
            headers=headers,
        )

    # -- the API ----------------------------------------------------------

    def account(self) -> AccountSummary:
        """The account behind the key: who, and how many credits are left."""
        raw = self._json("/api/account")
        return AccountSummary(
            id=raw.get("id", ""),
            email=raw.get("email", ""),
            credits=raw.get("credits", 0),
            prices=raw.get("prices", {}),
        )

    def render(self, bundle: bytes | BinaryIO, kind: str = "cloud-render") -> QueuedJob:
        """Queue a render.

        There is no ``seconds`` parameter and there should not be: the server
        reads the timeline duration out of the bundle's own ``project.json`` and
        prices on that. A duration supplied by the caller is a price supplied by
        the caller. What it was charged for comes back in ``priced``.

        A bundle over 64 MiB is uploaded in chunks and resumes itself from the
        *server's* count of what it holds, not ours.
        """
        data = bundle if isinstance(bundle, bytes) else bundle.read()

        if len(data) <= CHUNK:
            return self._queued(
                self._json(
                    f"/api/jobs?{urlencode({'kind': kind})}",
                    method="POST",
                    body=data,
                    content_type="application/zip",
                )
            )
        return self._render_chunked(data, kind)

    def _render_chunked(self, data: bytes, kind: str) -> QueuedJob:
        upload = ""
        offset = 0
        attempts = 0

        while offset < len(data):
            to = min(len(data), offset + CHUNK)
            last = to == len(data)

            query: dict[str, Any] = {"kind": kind, "offset": offset}
            if upload:
                query["upload"] = upload
            if last:
                query["final"] = "1"

            try:
                body = self._json(
                    f"/api/jobs?{urlencode(query)}",
                    method="POST",
                    body=data[offset:to],
                    content_type="application/zip",
                )
                if last:
                    return self._queued(body)
                # The server's count, not ours: it is the one with the file.
                upload = body["upload"]
                offset = body["received"]
                attempts = 0
            except SnappedCutError as error:
                attempts += 1
                if attempts > _RETRIES:
                    raise
                # Where the server says its copy ends. Believe it over our count.
                if error.status == 409:
                    offset = error.expected
                    continue
                # A refusal is a refusal -- no credits, bad bundle, bad key.
                # Only a lost connection (0) or a server fault is worth resending.
                if error.status != 0 and error.status < 500:
                    raise

        raise SnappedCutError("That upload ended without queueing a job.", 0)

    @staticmethod
    def _queued(body: dict[str, Any]) -> QueuedJob:
        job = Job._from(body["job"])
        return QueuedJob(
            id=job.id,
            kind=job.kind,
            state=job.state,
            seconds=job.seconds,
            error=job.error,
            created_at=job.created_at,
            updated_at=job.updated_at,
            charged=body.get("charged", 0),
            credits=body.get("credits", 0),
            priced=Priced._from(body.get("priced", {})),
        )

    def job(self, job_id: str) -> Job:
        return Job._from(self._json(f"/api/jobs/{quote(job_id, safe='')}"))

    def jobs(self) -> list[Job]:
        return [Job._from(raw) for raw in self._json("/api/jobs").get("jobs", [])]

    def output(self, job_id: str) -> bytes:
        """The rendered file. Raises unless the job is done."""
        _, raw = self._call(f"/api/jobs/{quote(job_id, safe='')}/output")
        return raw

    def wait(
        self,
        job_id: str,
        timeout: float = 30 * 60,
        on_state: Callable[[Job], None] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> bytes:
        """Wait for a job to finish, then hand back the file.

        Polls with a backoff rather than at a fixed interval: a render is
        minutes, so a one-second poll spends most of its requests learning
        nothing, and the server has a rate limit a busy loop will find.
        """
        deadline = time.monotonic() + timeout
        delay = 1.0

        while True:
            job = self.job(job_id)
            if on_state:
                on_state(job)

            if job.state == "done":
                return self.output(job_id)
            # The credits are already back -- the server refunds a failure -- so
            # this is a plain error, not something the caller has to reconcile.
            if job.state == "failed":
                raise SnappedCutError(job.error or "The render failed.", 500)
            if time.monotonic() > deadline:
                raise SnappedCutError(f"Job {job_id} is still {job.state}.", 504)

            sleep(delay)
            delay = min(delay * 1.5, 15.0)

    # -- JSON jobs, batches, designs (0.2) ---------------------------------

    def create_job(
        self, request: Mapping[str, Any], idempotency_key: str | None = None
    ) -> dict[str, Any]:
        """POST /api/jobs as JSON. Returns ``{job, charged, price, credits, idempotent}``.

        An Idempotency-Key makes a retry return the first job instead of a
        second charge.
        """
        key = idempotency_key or request.get("idempotencyKey")
        headers = {"idempotency-key": key} if key else None
        return self._post("/api/jobs", dict(request), headers=headers)

    def create_batch(
        self, jobs: Sequence[Mapping[str, Any]], defaults: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"jobs": [dict(j) for j in jobs]}
        if defaults:
            body["defaults"] = dict(defaults)
        return self._post("/api/jobs/batch", body)

    def get_job(self, job_id: str) -> dict[str, Any]:
        return self._json(f"/api/jobs/{_q(job_id)}")

    def list_jobs(self) -> list[dict[str, Any]]:
        return cast(list[dict[str, Any]], self._json("/api/jobs").get("jobs", []))

    def download_output(self, job_id: str, index: int = 0) -> bytes:
        """One output's bytes. A job can render several; ``index`` picks one."""
        suffix = f"?index={index}" if index > 0 else ""
        return self._call(f"/api/jobs/{_q(job_id)}/output{suffix}")[1]

    def wait_for_job(
        self,
        job_id: str,
        timeout: float = 30 * 60,
        sleep: Callable[[float], None] = time.sleep,
    ) -> dict[str, Any]:
        """Poll from 1 s, x1.5 each time, capped at 15 s. Raises on failed or timeout."""
        deadline = time.monotonic() + timeout
        delay = 1.0
        while True:
            job = self.get_job(job_id)
            if job.get("state") == "done":
                return job
            if job.get("state") == "failed":
                raise SnappedCutError(job.get("error") or "The render failed.", 500)
            if time.monotonic() > deadline:
                raise SnappedCutError(f"Job {job_id} is still {job.get('state')}.", 504)
            sleep(delay)
            delay = min(delay * 1.5, 15.0)

    def compile_design(
        self, design: Mapping[str, Any], vars: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"design": dict(design)}
        if vars:
            body["vars"] = dict(vars)
        return self._post("/api/designs", body)

    def render_design(
        self,
        design: Mapping[str, Any],
        options: Mapping[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return self.create_job({**(options or {}), "design": dict(design)}, idempotency_key)

    # -- uploads -----------------------------------------------------------

    def upload(
        self,
        data: bytes | str | os.PathLike[str] | BinaryIO,
        content_type: str,
        name: str | None = None,
    ) -> dict[str, Any]:
        """Upload bytes, a file path or a binary file object, then confirm. Returns the asset.

        A path or file is streamed: one chunk in memory at a time.
        """
        if isinstance(data, str | os.PathLike):
            with open(data, "rb") as handle:
                return self.upload(handle, content_type, name or os.path.basename(data))
        stream: BinaryIO = io.BytesIO(data) if isinstance(data, bytes) else data

        request: dict[str, Any] = {"contentType": content_type}
        if name:
            request["name"] = name
        made = self._post("/api/uploads", request)
        url, step = made["upload"]["url"], int(made["upload"]["maxChunkBytes"])

        offset = 0
        while True:
            chunk = stream.read(step)
            if not chunk and offset > 0:
                break
            # The upload URL carries its own token; our key is not sent to it.
            self._call(
                f"{url}&offset={offset}",
                method="PUT",
                body=chunk,
                content_type=content_type,
                auth=False,
            )
            offset += len(chunk)
            if len(chunk) < step:
                break
        confirmed = self._post(f"/api/uploads/{_q(made['assetId'])}/confirm", {})
        return cast(dict[str, Any], confirmed["asset"])

    def get_asset(self, asset_id: str) -> dict[str, Any]:
        return cast(dict[str, Any], self._json(f"/api/uploads/{_q(asset_id)}")["asset"])

    def delete_asset(self, asset_id: str) -> None:
        self._call(f"/api/uploads/{_q(asset_id)}", method="DELETE")

    # -- meta, usage, embed ------------------------------------------------

    def operations(self) -> dict[str, Any]:
        """Every operation, its params, and the output presets."""
        return self._json("/api/operations")

    def usage(
        self,
        from_: int | None = None,
        to: int | None = None,
        end_user_id: str | None = None,
        group_by: str | None = None,
    ) -> dict[str, Any]:
        pairs = (("from", from_), ("to", to), ("endUserId", end_user_id), ("groupBy", group_by))
        params = {k: v for k, v in pairs if v is not None}
        return self._json("/api/usage" + (f"?{urlencode(params)}" if params else ""))

    def delete_end_user(self, end_user_id: str) -> dict[str, Any]:
        """Delete one end user's renders and uploads, and their id from everything kept."""
        return self._json(f"/api/usage?endUserId={_q(end_user_id)}", method="DELETE")

    def create_embed_token(
        self,
        pk: str,
        end_user_id: str,
        max_credits: int | None = None,
        ttl_seconds: int | None = None,
        origin: str | None = None,
        features: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"pk": pk, "endUserId": end_user_id}
        optional = (
            ("maxCredits", max_credits),
            ("ttlSeconds", ttl_seconds),
            ("origin", origin),
            ("features", dict(features) if features is not None else None),
        )
        body.update({k: v for k, v in optional if v is not None})
        return self._post("/api/embed/tokens", body)

    # -- storage destinations ----------------------------------------------

    def list_destinations(self) -> list[dict[str, Any]]:
        return cast(list[dict[str, Any]], self._json("/api/destinations")["destinations"])

    def create_destination(self, destination: Mapping[str, Any]) -> dict[str, Any]:
        made = self._post("/api/destinations", dict(destination))
        return cast(dict[str, Any], made["destination"])

    def get_destination(self, destination_id: str) -> dict[str, Any]:
        found = self._json(f"/api/destinations/{_q(destination_id)}")
        return cast(dict[str, Any], found["destination"])

    def update_destination(self, destination_id: str, patch: Mapping[str, Any]) -> dict[str, Any]:
        done = self._post(f"/api/destinations/{_q(destination_id)}", dict(patch), method="PATCH")
        return cast(dict[str, Any], done["destination"])

    def delete_destination(self, destination_id: str) -> None:
        self._call(f"/api/destinations/{_q(destination_id)}", method="DELETE")

    def test_destination(self, destination_id: str) -> dict[str, Any]:
        """A failed test write (422) is an answer ``{ok: False, ...}``, returned, not raised."""
        path = f"/api/destinations/{_q(destination_id)}/test"
        _, raw = self._call(path, method="POST", ok=(422,))
        return cast(dict[str, Any], json.loads(raw))

    def presign_destination(
        self, destination_id: str, key: str, expires_in: int | None = None
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"key": key}
        if expires_in:
            body["expiresIn"] = expires_in
        return self._post(f"/api/destinations/{_q(destination_id)}/presign", body)

    # -- webhook endpoints -------------------------------------------------

    def list_webhooks(self) -> list[dict[str, Any]]:
        return cast(list[dict[str, Any]], self._json("/api/webhooks")["webhooks"])

    def create_webhook(self, url: str, events: Sequence[str] | None = None) -> dict[str, Any]:
        """Returns ``{webhook, secret}``. The secret comes back this once."""
        body: dict[str, Any] = {"url": url}
        if events is not None:
            body["events"] = list(events)
        return self._post("/api/webhooks", body)

    def get_webhook(self, webhook_id: str) -> dict[str, Any]:
        return cast(dict[str, Any], self._json(f"/api/webhooks/{_q(webhook_id)}")["webhook"])

    def update_webhook(
        self,
        webhook_id: str,
        url: str | None = None,
        events: Sequence[str] | None = None,
        enabled: bool | None = None,
    ) -> dict[str, Any]:
        optional = (("url", url), ("events", list(events) if events is not None else None))
        patch: dict[str, Any] = {k: v for k, v in optional if v is not None}
        if enabled is not None:
            patch["enabled"] = enabled
        done = self._post(f"/api/webhooks/{_q(webhook_id)}", patch, method="PATCH")
        return cast(dict[str, Any], done["webhook"])

    def delete_webhook(self, webhook_id: str) -> None:
        self._call(f"/api/webhooks/{_q(webhook_id)}", method="DELETE")

    def rotate_webhook(self, webhook_id: str) -> str:
        """A new signing secret, returned this once."""
        return cast(str, self._post(f"/api/webhooks/{_q(webhook_id)}/rotate", {})["secret"])

    def webhook_deliveries(self, webhook_id: str, limit: int = 50) -> list[dict[str, Any]]:
        found = self._json(f"/api/webhooks/{_q(webhook_id)}/deliveries?limit={limit}")
        return cast(list[dict[str, Any]], found["deliveries"])

    def test_webhook(self, webhook_id: str) -> dict[str, Any]:
        return self._post(f"/api/webhooks/{_q(webhook_id)}/test", {})

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        # Never the key: a repr ends up in logs and tracebacks.
        return f"SnappedCut(base={self.base!r}, key={'set' if self.key else 'unset'})"
