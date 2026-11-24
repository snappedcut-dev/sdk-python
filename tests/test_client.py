"""Tests run on stdlib unittest, so CI needs nothing the package does not."""

from __future__ import annotations

import io
import json
import unittest
import unittest.mock
import urllib.error
from typing import Any

from snappedcut import SnappedCut, SnappedCutError
from snappedcut.client import CHUNK


class FakeResponse(io.BytesIO):
    status = 200

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()


def opener(replies: list[Any]) -> tuple[Any, list[dict[str, Any]]]:
    """An opener that records what it was asked and answers from a script."""
    calls: list[dict[str, Any]] = []
    queue = list(replies)

    def open_(request: Any, _timeout: float) -> Any:
        calls.append(
            {
                "url": request.full_url,
                "method": request.method,
                "body": request.data,
                "headers": {k.lower(): v for k, v in request.headers.items()},
            }
        )
        if not queue:
            raise AssertionError(f"unexpected request: {request.full_url}")
        reply = queue.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return FakeResponse(reply if isinstance(reply, bytes) else json.dumps(reply).encode())

    return open_, calls


def http_error(status: int, payload: dict[str, Any]) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "https://snappedcut.com/x", status, "err", {}, io.BytesIO(json.dumps(payload).encode())
    )


ACCOUNT = {"id": "a1", "email": "e@x.com", "credits": 50, "prices": {"cloud-render": 2}}
QUEUED = {
    "job": {
        "id": "j1",
        "kind": "cloud-render",
        "state": "queued",
        "error": None,
        "seconds": 4,
        "createdAt": 1,
        "updatedAt": 1,
    },
    "charged": 8,
    "credits": 92,
    "priced": {"seconds": 4, "project": "p", "layers": 2},
}


def job(state: str, error: str | None = None) -> dict[str, Any]:
    return {
        "id": "j1",
        "kind": "cloud-render",
        "state": state,
        "error": error,
        "seconds": 4,
        "createdAt": 1,
        "updatedAt": 1,
    }


class TestAuth(unittest.TestCase):
    def test_key_is_sent_as_a_bearer_token(self) -> None:
        open_, calls = opener([ACCOUNT])
        SnappedCut(key="sk_snapcut_t", opener=open_).account()
        self.assertEqual(calls[0]["headers"]["authorization"], "Bearer sk_snapcut_t")
        self.assertEqual(calls[0]["url"], "https://snappedcut.com/api/account")

    def test_key_falls_back_to_the_environment(self) -> None:
        open_, calls = opener([ACCOUNT])
        with unittest.mock.patch.dict("os.environ", {"SNAPCUT_KEY": "sk_snapcut_env"}):
            SnappedCut(opener=open_).account()
        self.assertEqual(calls[0]["headers"]["authorization"], "Bearer sk_snapcut_env")

    def test_repr_never_contains_the_key(self) -> None:
        # A repr ends up in logs and tracebacks.
        self.assertNotIn("sk_snapcut_secret", repr(SnappedCut(key="sk_snapcut_secret")))

    def test_a_trailing_slash_on_the_base_does_not_double_up(self) -> None:
        open_, calls = opener([ACCOUNT])
        SnappedCut(key="k", base="http://localhost:3000/", opener=open_).account()
        self.assertEqual(calls[0]["url"], "http://localhost:3000/api/account")


class TestErrors(unittest.TestCase):
    def test_a_402_is_catchable_without_parsing_a_message(self) -> None:
        open_, _ = opener([http_error(402, {"error": "Not enough credits."})])
        with self.assertRaises(SnappedCutError) as caught:
            SnappedCut(key="k", opener=open_).account()
        self.assertTrue(caught.exception.out_of_credits)
        self.assertEqual(caught.exception.status, 402)

    def test_a_429_reports_itself_as_rate_limited(self) -> None:
        open_, _ = opener([http_error(429, {"error": "Slow down.", "retryAfterMs": 2000})])
        with self.assertRaises(SnappedCutError) as caught:
            SnappedCut(key="k", opener=open_).account()
        self.assertTrue(caught.exception.rate_limited)
        self.assertEqual(caught.exception.retry_after_ms, 2000)

    def test_an_unreachable_server_is_status_zero_not_a_crash(self) -> None:
        open_, _ = opener([urllib.error.URLError("no route")])
        with self.assertRaises(SnappedCutError) as caught:
            SnappedCut(key="k", opener=open_).account()
        self.assertEqual(caught.exception.status, 0)

    def test_a_non_json_error_body_still_raises_cleanly(self) -> None:
        err = urllib.error.HTTPError("u", 500, "err", {}, io.BytesIO(b"<html>oops"))
        open_, _ = opener([err])
        with self.assertRaises(SnappedCutError) as caught:
            SnappedCut(key="k", opener=open_).account()
        self.assertEqual(caught.exception.status, 500)


class TestRender(unittest.TestCase):
    def test_render_never_sends_a_duration(self) -> None:
        open_, calls = opener([QUEUED])
        queued = SnappedCut(key="k", opener=open_).render(b"x" * 32)
        self.assertEqual(queued.charged, 8)
        self.assertEqual(queued.priced.seconds, 4)
        # A caller-supplied duration would be a caller-supplied price.
        self.assertNotIn("seconds", calls[0]["url"])

    def test_render_accepts_a_file_object(self) -> None:
        open_, calls = opener([QUEUED])
        SnappedCut(key="k", opener=open_).render(io.BytesIO(b"zipbytes"))
        self.assertEqual(calls[0]["body"], b"zipbytes")

    def test_a_small_bundle_is_one_request(self) -> None:
        open_, calls = opener([QUEUED])
        SnappedCut(key="k", opener=open_).render(b"x" * 1024)
        self.assertEqual(len(calls), 1)
        self.assertNotIn("offset=", calls[0]["url"])

    def test_a_large_bundle_is_chunked_and_the_last_chunk_is_final(self) -> None:
        open_, calls = opener([{"upload": "u1", "received": CHUNK}, QUEUED])
        queued = SnappedCut(key="k", opener=open_).render(b"\0" * (CHUNK + 16))
        self.assertEqual(len(calls), 2)
        self.assertIn("offset=0", calls[0]["url"])
        self.assertIn("upload=u1", calls[1]["url"])
        self.assertIn("final=1", calls[1]["url"])
        self.assertEqual(queued.id, "j1")

    def test_a_409_resumes_from_the_offset_the_server_reports(self) -> None:
        # Our count and the server's disagreed. The server has the file.
        open_, calls = opener(
            [
                {"upload": "u1", "received": 10},
                http_error(409, {"error": "offset", "expected": CHUNK}),
                QUEUED,
            ]
        )
        SnappedCut(key="k", opener=open_).render(b"\0" * (CHUNK + 16))
        self.assertIn(f"offset={CHUNK}", calls[2]["url"])

    def test_a_refusal_mid_upload_stops_rather_than_retrying(self) -> None:
        open_, calls = opener(
            [{"upload": "u1", "received": 10}, http_error(402, {"error": "Not enough credits."})]
        )
        with self.assertRaises(SnappedCutError) as caught:
            SnappedCut(key="k", opener=open_).render(b"\0" * (CHUNK + 16))
        self.assertTrue(caught.exception.out_of_credits)
        self.assertEqual(len(calls), 2)


class TestWait(unittest.TestCase):
    def test_wait_polls_until_done_then_returns_the_bytes(self) -> None:
        open_, _ = opener([job("running"), job("done"), b"MOVIE"])
        seen: list[str] = []
        data = SnappedCut(key="k", opener=open_).wait(
            "j1", on_state=lambda j: seen.append(j.state), sleep=lambda _: None
        )
        self.assertEqual(seen, ["running", "done"])
        self.assertEqual(data, b"MOVIE")

    def test_a_failed_job_raises_rather_than_returning_an_empty_file(self) -> None:
        open_, _ = opener([job("failed", "decode failed")])
        with self.assertRaises(SnappedCutError) as caught:
            SnappedCut(key="k", opener=open_).wait("j1", sleep=lambda _: None)
        self.assertIn("decode failed", str(caught.exception))

    def test_wait_gives_up_at_the_timeout_with_a_504(self) -> None:
        open_, _ = opener([job("queued")] * 40)
        with self.assertRaises(SnappedCutError) as caught:
            SnappedCut(key="k", opener=open_).wait("j1", timeout=-1, sleep=lambda _: None)
        self.assertEqual(caught.exception.status, 504)

    def test_a_job_id_is_escaped_into_the_path(self) -> None:
        open_, calls = opener([job("done")])
        SnappedCut(key="k", opener=open_).job("a/../b")
        self.assertIn("a%2F..%2Fb", calls[0]["url"])


class TestJobs(unittest.TestCase):
    def test_jobs_unwraps_the_envelope(self) -> None:
        open_, _ = opener([{"jobs": [job("done"), job("queued")]}])
        found = SnappedCut(key="k", opener=open_).jobs()
        self.assertEqual([j.state for j in found], ["done", "queued"])

    def test_jobs_tolerates_an_empty_account(self) -> None:
        open_, _ = opener([{}])
        self.assertEqual(SnappedCut(key="k", opener=open_).jobs(), [])


if __name__ == "__main__":
    unittest.main()
