from __future__ import annotations

import base64
import json

import httpx
import pytest

from omarchy_imagine.config import IMAGE_MODEL, VIDEO_MODEL
from omarchy_imagine.imagine import ImagineClient, ImagineError
from tests.samples import PNG_BYTES


def _client(handler) -> ImagineClient:
    return ImagineClient(
        "test-key",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        poll_interval=0,
        poll_timeout=5,
    )


def test_generate_still_posts_documented_endpoint() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        encoded = base64.b64encode(PNG_BYTES).decode("ascii")
        return httpx.Response(200, json={"data": [{"b64_json": encoded}]})

    with _client(handler) as client:
        image = client.generate_still("a harbor at dawn", "16:9", "1k")

    assert image == PNG_BYTES
    assert seen["url"] == "https://api.x.ai/v1/images/generations"
    assert seen["auth"] == "Bearer test-key"
    assert seen["body"]["model"] == IMAGE_MODEL
    assert seen["body"]["prompt"] == "a harbor at dawn"
    assert seen["body"]["aspect_ratio"] == "16:9"
    assert seen["body"]["resolution"] == "1k"
    assert seen["body"]["response_format"] == "b64_json"
    assert seen["body"]["n"] == 1
    assert "test-key" not in json.dumps(seen["body"])


def test_generate_still_downloads_url_when_b64_missing() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/images/generations"):
            return httpx.Response(
                200,
                json={"data": [{"url": "https://imgen.x.ai/example/still.png"}]},
            )
        if request.url.host == "imgen.x.ai":
            assert "authorization" not in {k.lower() for k in request.headers}
            return httpx.Response(200, content=PNG_BYTES)
        return httpx.Response(500, json={"error": {"message": "unexpected"}})

    with _client(handler) as client:
        assert client.generate_still("a harbor", "16:9", "1k") == PNG_BYTES


def test_generate_still_without_bytes_or_url_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, json={"data": [{"url": None, "b64_json": None}]})

    with _client(handler) as client, pytest.raises(ImagineError, match="no image bytes") as raised:
        client.generate_still("a harbor", "16:9", "1k")
    assert raised.value.request_sent is True


def test_edit_still_posts_data_uri() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={"data": [{"b64_json": base64.b64encode(PNG_BYTES).decode("ascii")}]},
        )

    with _client(handler) as client:
        client.edit_still("keep the boat", PNG_BYTES, "16:9", "1k")

    assert seen["url"] == "https://api.x.ai/v1/images/edits"
    assert seen["body"]["model"] == IMAGE_MODEL
    assert seen["body"]["image"]["type"] == "image_url"
    assert seen["body"]["image"]["url"].startswith("data:image/png;base64,")


def test_image_to_video_submits_and_polls() -> None:
    calls: list[str] = []
    polls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.method} {request.url}")
        if request.url.path == "/v1/videos/generations":
            body = json.loads(request.content)
            assert body["model"] == VIDEO_MODEL
            assert body["duration"] == 8
            assert body["aspect_ratio"] == "16:9"
            assert body["resolution"] == "720p"
            assert body["prompt"] == "the boat eases off"
            assert body["image"]["url"].startswith("data:image/png;base64,")
            return httpx.Response(200, json={"request_id": "req-123"})
        if request.url.path == "/v1/videos/req-123":
            polls["n"] += 1
            if polls["n"] == 1:
                return httpx.Response(200, json={"status": "pending", "progress": 10})
            return httpx.Response(
                200,
                json={
                    "status": "done",
                    "model": VIDEO_MODEL,
                    "video": {
                        "url": "https://vidgen.x.ai/example/clip.mp4",
                        "duration": 8,
                        "respect_moderation": True,
                    },
                },
            )
        if request.url.host == "vidgen.x.ai":
            assert "authorization" not in {k.lower() for k in request.headers}
            return httpx.Response(200, content=b"clip-bytes")
        return httpx.Response(500, json={"error": {"message": f"unexpected {request.url}"}})

    with _client(handler) as client:
        clip, request_id = client.image_to_video(
            prompt="the boat eases off",
            image=PNG_BYTES,
            duration_sec=8,
            aspect_ratio="16:9",
            resolution="720p",
        )

    assert clip == b"clip-bytes"
    assert request_id == "req-123"
    assert any(call.startswith("POST https://api.x.ai/v1/videos/generations") for call in calls)
    assert "GET https://api.x.ai/v1/videos/req-123" in calls


def test_video_failure_and_moderation_do_not_invent_urls() -> None:
    def failed(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json={"request_id": "req-bad"})
        return httpx.Response(
            200,
            json={
                "status": "failed",
                "error": {"code": "internal_error", "message": "render failed"},
            },
        )

    with _client(failed) as client, pytest.raises(ImagineError, match="render failed") as raised:
        client.image_to_video(
            prompt="move",
            image=PNG_BYTES,
            duration_sec=4,
            aspect_ratio="16:9",
            resolution="720p",
        )
    assert raised.value.request_sent is True
    assert raised.value.request_id == "req-bad"

    def moderated(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json={"request_id": "req-mod"})
        return httpx.Response(
            200,
            json={
                "status": "done",
                "video": {"url": "", "duration": 4, "respect_moderation": False},
            },
        )

    with _client(moderated) as client, pytest.raises(ImagineError, match="moderation"):
        client.image_to_video(
            prompt="move",
            image=PNG_BYTES,
            duration_sec=4,
            aspect_ratio="16:9",
            resolution="720p",
        )


def test_video_poll_timeout() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json={"request_id": "req-slow"})
        return httpx.Response(200, json={"status": "pending"})

    client = ImagineClient(
        "test-key",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        poll_interval=0,
        poll_timeout=0,
    )
    with client, pytest.raises(ImagineError, match="timed out") as raised:
        client.image_to_video(
            prompt="move",
            image=PNG_BYTES,
            duration_sec=4,
            aspect_ratio="16:9",
            resolution="720p",
        )
    assert raised.value.request_id == "req-slow"


def test_connection_error_is_not_marked_sent() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        raise httpx.ConnectError("refused")

    with (
        _client(handler) as client,
        pytest.raises(ImagineError, match="before a response") as raised,
    ):
        client.generate_still("a harbor", "16:9", "1k")
    assert raised.value.request_sent is False


def test_refuses_non_https_media_url() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, json={"data": [{"url": "http://example.test/still.png"}]})

    with _client(handler) as client, pytest.raises(ImagineError, match="non-https"):
        client.generate_still("a harbor", "16:9", "1k")
