"""Tests run on stdlib unittest, so CI needs nothing the package does not."""

from __future__ import annotations

import io
import json
import unittest
import unittest.mock
import urllib.error
from typing import Any

from snappedcut import SnappedCut, SnappedCutError, verify_webhook
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


class TestWebhookVector(unittest.TestCase):
    # The fixed vector every Snapped Cut SDK asserts.
    secret = "whsec_snapcut_test"
    body = '{"id":"evt_1","type":"job.succeeded"}'
    header = "t=1760000000000,v1=54c2adf2a351242de19b156794d65c56bebf9fc56cadac990c56b979a7a61ab2"

    def test_the_vector(self) -> None:
        self.assertTrue(verify_webhook(self.secret, self.body, self.header, 1760000001000))
        self.assertTrue(
            SnappedCut.verify_webhook(self.secret, self.body.encode(), self.header, 1760000001000)
        )
        self.assertFalse(verify_webhook(self.secret, self.body, self.header, 1760000400000))
        self.assertFalse(verify_webhook(self.secret, self.body + " ", self.header, 1760000001000))

    def test_malformed_headers_are_false_not_errors(self) -> None:
        now = 1760000001000
        self.assertFalse(verify_webhook(self.secret, self.body, None, now))
        self.assertFalse(verify_webhook(self.secret, self.body, "v1=" + "0" * 64, now))
        self.assertFalse(verify_webhook(self.secret, self.body, "t=1760000000000,v1=ABC", now))


class TestJsonJobs(unittest.TestCase):
    def test_create_job_sends_the_key_the_idempotency_key_and_json(self) -> None:
        created = {
            "job": job("queued"),
            "charged": 3,
            "price": 3,
            "credits": 7,
            "idempotent": False,
        }
        open_, calls = opener([{**created, "unknown": 1}])
        got = SnappedCut(key="sk_snapcut_t", opener=open_).create_job(
            {"design": {"clips": []}}, idempotency_key="order-42"
        )
        call = calls[0]
        self.assertEqual((call["method"], call["url"]), ("POST", "https://snappedcut.com/api/jobs"))
        self.assertEqual(call["headers"]["idempotency-key"], "order-42")
        self.assertEqual(call["headers"]["authorization"], "Bearer sk_snapcut_t")
        self.assertEqual(call["headers"]["user-agent"], "snappedcut-python/0.2.0")
        self.assertEqual(call["headers"]["content-type"], "application/json")
        self.assertEqual(json.loads(call["body"]), {"design": {"clips": []}})
        self.assertEqual(got["job"]["id"], "j1")

    def test_a_402_carries_the_shared_error_fields(self) -> None:
        open_, _ = opener([http_error(402, {"error": "Not enough credits.", "retryAfterMs": 0})])
        with self.assertRaises(SnappedCutError) as caught:
            SnappedCut(key="k", opener=open_).create_job({"design": {}})
        error = caught.exception
        self.assertEqual((error.status, error.message), (402, "Not enough credits."))
        self.assertEqual(error.retry_after_ms, 0)
        self.assertIsNone(error.code)
        self.assertTrue(error.is_out_of_credits)
        self.assertFalse(error.is_rate_limited)

    def test_an_error_code_comes_through(self) -> None:
        open_, _ = opener([http_error(429, {"error": "Slow down.", "code": "rate_limited"})])
        with self.assertRaises(SnappedCutError) as caught:
            SnappedCut(key="k", opener=open_).get_job("j1")
        self.assertEqual(caught.exception.code, "rate_limited")
        self.assertTrue(caught.exception.is_rate_limited)

    def test_wait_for_job_returns_the_job_when_done(self) -> None:
        open_, _ = opener([job("queued"), job("running"), job("done")])
        slept: list[float] = []
        done = SnappedCut(key="k", opener=open_).wait_for_job("j1", sleep=slept.append)
        self.assertEqual(done["state"], "done")
        self.assertEqual(slept, [1.0, 1.5])

    def test_wait_for_job_raises_on_failed(self) -> None:
        open_, _ = opener([job("running"), job("failed", "Decode failed.")])
        with self.assertRaises(SnappedCutError) as caught:
            SnappedCut(key="k", opener=open_).wait_for_job("j1", sleep=lambda _: None)
        self.assertEqual(caught.exception.message, "Decode failed.")

    def test_download_output_adds_the_index_only_past_zero(self) -> None:
        open_, calls = opener([b"one", b"two"])
        sc = SnappedCut(key="k", opener=open_)
        self.assertEqual(sc.download_output("j1"), b"one")
        self.assertEqual(sc.download_output("j1", 1), b"two")
        self.assertTrue(calls[0]["url"].endswith("/api/jobs/j1/output"))
        self.assertTrue(calls[1]["url"].endswith("/api/jobs/j1/output?index=1"))

    def test_a_failed_destination_test_is_an_answer(self) -> None:
        open_, _ = opener([http_error(422, {"ok": False, "key": "k", "error": "Denied."})])
        self.assertFalse(SnappedCut(key="k", opener=open_).test_destination("d1")["ok"])


class TestUpload(unittest.TestCase):
    def test_chunks_skip_the_api_key_then_confirm(self) -> None:
        made = {
            "assetId": "a1",
            "upload": {"url": "https://up.test/api/uploads/a1?token=tok", "maxChunkBytes": 4},
        }
        open_, calls = opener(
            [made, {"received": 4}, {"received": 6}, {"asset": {"id": "a1", "confirmed": True}}]
        )
        asset = SnappedCut(key="sk_snapcut_t", opener=open_).upload(
            b"abcdef", "video/webm", "clip.webm"
        )
        self.assertEqual(asset["id"], "a1")
        self.assertEqual([c["method"] for c in calls], ["POST", "PUT", "PUT", "POST"])
        asked = {"contentType": "video/webm", "name": "clip.webm"}
        self.assertEqual(json.loads(calls[0]["body"]), asked)
        put1, put2 = calls[1], calls[2]
        self.assertEqual(put1["url"], "https://up.test/api/uploads/a1?token=tok&offset=0")
        self.assertEqual(put2["url"], "https://up.test/api/uploads/a1?token=tok&offset=4")
        self.assertEqual((put1["body"], put2["body"]), (b"abcd", b"ef"))
        for put in (put1, put2):
            self.assertNotIn("authorization", put["headers"])
            self.assertNotIn("sk_snapcut_t", json.dumps(put["headers"]))
        self.assertEqual(calls[3]["url"], "https://snappedcut.com/api/uploads/a1/confirm")
        self.assertEqual(calls[3]["headers"]["authorization"], "Bearer sk_snapcut_t")


if __name__ == "__main__":
    unittest.main()
