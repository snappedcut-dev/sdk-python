from __future__ import annotations

import contextlib
import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, BinaryIO, Callable
from urllib.parse import quote, urlencode

DEFAULT_BASE = "https://snappedcut.com"

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
    ) -> None:
        super().__init__(message)
        self.status = status
        self.retry_after_ms = retry_after_ms
        # On a 409 from a chunked upload: the byte the server's copy ends at.
        self.expected = expected

    @property
    def out_of_credits(self) -> bool:
        return self.status == 402

    @property
    def rate_limited(self) -> bool:
        return self.status == 429


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
    """

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
    ) -> tuple[int, bytes]:
        headers: dict[str, str] = {"user-agent": "snappedcut-python/0.1.0"}
        if self.key:
            headers["authorization"] = f"Bearer {self.key}"
        if content_type:
            headers["content-type"] = content_type

        request = urllib.request.Request(
            f"{self.base}{path}", data=body, headers=headers, method=method
        )

        try:
            with self._open(request, self.timeout) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as error:
            raw = error.read()
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
            ) from error
        except urllib.error.URLError as error:
            # Status 0 means "never reached the server", which is the one case
            # worth sending again.
            raise SnappedCutError(f"Could not reach Snapped Cut: {error.reason}", 0) from error

    def _json(self, path: str, **kwargs: Any) -> dict[str, Any]:
        _, raw = self._call(path, **kwargs)
        parsed: dict[str, Any] = json.loads(raw)
        return parsed

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

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        # Never the key: a repr ends up in logs and tracebacks.
        return f"SnappedCut(base={self.base!r}, key={'set' if self.key else 'unset'})"
