"""xAI Imagine REST adapters for stills and image-to-video.

Shapes follow the public docs (retrieved for this implementation):

- POST https://api.x.ai/v1/images/generations
- POST https://api.x.ai/v1/images/edits
- POST https://api.x.ai/v1/videos/generations
- GET  https://api.x.ai/v1/videos/{request_id}

Video generation is asynchronous. The submit call returns ``request_id``.
Polling continues while ``status`` is ``pending``. ``done`` yields
``video.url``. ``failed`` and ``expired`` are errors. This module never
invents a media URL.
"""

from __future__ import annotations

import base64
import json
import os
import time
from urllib.parse import urlparse

import httpx

from omarchy_imagine.config import (
    DEFAULT_VIDEO_POLL_SEC,
    DEFAULT_VIDEO_TIMEOUT_SEC,
    IMAGE_MODEL,
    VIDEO_MODEL,
    XAI_API_BASE,
)


class ImagineError(Exception):
    def __init__(
        self,
        message: str,
        *,
        request_sent: bool,
        request_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.request_sent = request_sent
        self.request_id = request_id


def data_uri(image: bytes) -> str:
    if image.startswith(b"\x89PNG\r\n\x1a\n"):
        mime = "image/png"
    elif image.startswith(b"\xff\xd8\xff"):
        mime = "image/jpeg"
    elif image.startswith(b"RIFF") and image[8:12] == b"WEBP":
        mime = "image/webp"
    else:
        mime = "image/png"
    encoded = base64.b64encode(image).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def _error_text(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except json.JSONDecodeError:
        return response.text[:500]
    if isinstance(payload, dict):
        err = payload.get("error")
        if isinstance(err, dict) and err.get("message"):
            return str(err["message"])[:500]
        if isinstance(err, str):
            return err[:500]
        if payload.get("message"):
            return str(payload["message"])[:500]
        detail = payload.get("detail")
        if isinstance(detail, str):
            return detail[:500]
    return response.text[:500]


class ImagineClient:
    def __init__(
        self,
        api_key: str,
        *,
        http_client: httpx.Client | None = None,
        base_url: str | None = None,
        poll_interval: float | None = None,
        poll_timeout: float | None = None,
    ) -> None:
        if not api_key.strip():
            raise ValueError("ImagineClient requires an API key")
        self.api_key = api_key.strip()
        if base_url is not None:
            configured = base_url
        else:
            configured = os.environ.get("XAI_API_BASE", XAI_API_BASE)
        self.base_url = configured.rstrip("/")
        self._http = http_client or httpx.Client(
            timeout=httpx.Timeout(180.0, connect=30.0),
            follow_redirects=True,
        )
        self.poll_interval = (
            poll_interval
            if poll_interval is not None
            else float(os.environ.get("OMARCHY_VIDEO_POLL_SEC", DEFAULT_VIDEO_POLL_SEC))
        )
        self.poll_timeout = (
            poll_timeout
            if poll_timeout is not None
            else float(os.environ.get("OMARCHY_VIDEO_TIMEOUT_SEC", DEFAULT_VIDEO_TIMEOUT_SEC))
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> ImagineClient:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    def generate_still(self, prompt: str, aspect_ratio: str, resolution: str) -> bytes:
        body = {
            "model": IMAGE_MODEL,
            "prompt": prompt,
            "aspect_ratio": aspect_ratio,
            "resolution": resolution,
            "n": 1,
            "response_format": "b64_json",
        }
        response = self._post("/images/generations", body)
        return self._image_bytes(response)

    def edit_still(self, prompt: str, image: bytes, aspect_ratio: str, resolution: str) -> bytes:
        body = {
            "model": IMAGE_MODEL,
            "prompt": prompt,
            "aspect_ratio": aspect_ratio,
            "resolution": resolution,
            "n": 1,
            "response_format": "b64_json",
            "image": {"url": data_uri(image), "type": "image_url"},
        }
        response = self._post("/images/edits", body)
        return self._image_bytes(response)

    def image_to_video(
        self,
        *,
        prompt: str,
        image: bytes,
        duration_sec: int,
        aspect_ratio: str,
        resolution: str,
    ) -> tuple[bytes, str]:
        body = {
            "model": VIDEO_MODEL,
            "prompt": prompt,
            "image": {"url": data_uri(image)},
            "duration": duration_sec,
            "aspect_ratio": aspect_ratio,
            "resolution": resolution,
        }
        response = self._post("/videos/generations", body)
        try:
            payload = response.json()
        except json.JSONDecodeError as exc:
            raise ImagineError(
                "Imagine video submit response was not JSON",
                request_sent=True,
            ) from exc
        request_id = payload.get("request_id") if isinstance(payload, dict) else None
        if not isinstance(request_id, str) or not request_id:
            raise ImagineError(
                "Imagine video submit did not return request_id",
                request_sent=True,
            )
        video_url = self._poll_video(request_id)
        return self._download(video_url, request_id=request_id), request_id

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def _post(self, path: str, body: dict) -> httpx.Response:
        url = f"{self.base_url}{path}"
        try:
            response = self._http.post(url, headers=self._headers(), json=body)
        except httpx.HTTPError as exc:
            raise ImagineError(
                f"Imagine request failed before a response: {exc.__class__.__name__}",
                request_sent=False,
            ) from exc
        if response.status_code >= 400:
            raise ImagineError(
                f"Imagine {path} failed ({response.status_code}): {_error_text(response)}",
                request_sent=True,
            )
        return response

    def _image_bytes(self, response: httpx.Response) -> bytes:
        try:
            payload = response.json()
        except json.JSONDecodeError as exc:
            raise ImagineError("Imagine image response was not JSON", request_sent=True) from exc
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list) or not data or not isinstance(data[0], dict):
            raise ImagineError("Imagine image response did not include data[0]", request_sent=True)
        item = data[0]
        encoded = item.get("b64_json")
        if isinstance(encoded, str) and encoded:
            try:
                raw = base64.b64decode(encoded, validate=False)
            except ValueError as exc:
                raise ImagineError(
                    "Imagine b64_json could not be decoded",
                    request_sent=True,
                ) from exc
            if not raw:
                raise ImagineError("Imagine returned empty image bytes", request_sent=True)
            return raw
        url = item.get("url")
        if isinstance(url, str) and url:
            return self._download(url)
        raise ImagineError("Imagine returned no image bytes and no URL", request_sent=True)

    def _poll_video(self, request_id: str) -> str:
        deadline = time.monotonic() + self.poll_timeout
        url = f"{self.base_url}/videos/{request_id}"
        while True:
            try:
                response = self._http.get(
                    url,
                    headers={"Authorization": f"Bearer {self.api_key}"},
                )
            except httpx.HTTPError as exc:
                raise ImagineError(
                    f"Imagine video poll failed before a response: {exc.__class__.__name__}",
                    request_sent=True,
                    request_id=request_id,
                ) from exc
            if response.status_code >= 400:
                raise ImagineError(
                    f"Imagine video poll failed ({response.status_code}): {_error_text(response)}",
                    request_sent=True,
                    request_id=request_id,
                )
            try:
                payload = response.json()
            except json.JSONDecodeError as exc:
                raise ImagineError(
                    "Imagine video poll was not JSON",
                    request_sent=True,
                    request_id=request_id,
                ) from exc
            if not isinstance(payload, dict):
                raise ImagineError(
                    "Imagine video poll was not an object",
                    request_sent=True,
                    request_id=request_id,
                )
            status = payload.get("status")
            if status == "pending":
                if time.monotonic() >= deadline:
                    raise ImagineError(
                        f"Imagine video timed out after {self.poll_timeout:.0f}s "
                        f"(request_id={request_id})",
                        request_sent=True,
                        request_id=request_id,
                    )
                time.sleep(self.poll_interval)
                continue
            if status == "done":
                video = payload.get("video") if isinstance(payload.get("video"), dict) else {}
                if video.get("respect_moderation") is False:
                    raise ImagineError(
                        "Imagine video was filtered by moderation and returned no usable URL",
                        request_sent=True,
                        request_id=request_id,
                    )
                media = video.get("url")
                if not isinstance(media, str) or not media:
                    raise ImagineError(
                        "Imagine video completed without a URL",
                        request_sent=True,
                        request_id=request_id,
                    )
                return media
            if status in {"failed", "expired"}:
                message = _video_failure_message(payload) or f"Imagine video {status}"
                raise ImagineError(message, request_sent=True, request_id=request_id)
            raise ImagineError(
                f"Imagine video returned unexpected status {status!r}",
                request_sent=True,
                request_id=request_id,
            )

    def _download(self, url: str, *, request_id: str | None = None) -> bytes:
        parsed = urlparse(url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ImagineError(
                "Refusing a non-https media URL from Imagine",
                request_sent=True,
                request_id=request_id,
            )
        headers: dict[str, str] = {}
        if parsed.hostname == "api.x.ai":
            headers["Authorization"] = f"Bearer {self.api_key}"
        try:
            response = self._http.get(url, headers=headers)
        except httpx.HTTPError as exc:
            raise ImagineError(
                f"Failed to download Imagine media: {exc.__class__.__name__}",
                request_sent=True,
                request_id=request_id,
            ) from exc
        if response.status_code >= 400:
            raise ImagineError(
                f"Failed to download Imagine media ({response.status_code})",
                request_sent=True,
                request_id=request_id,
            )
        if not response.content:
            raise ImagineError(
                "Imagine media download was empty",
                request_sent=True,
                request_id=request_id,
            )
        return bytes(response.content)


def _video_failure_message(payload: dict) -> str:
    err = payload.get("error")
    if isinstance(err, dict):
        code = err.get("code") or ""
        message = err.get("message") or ""
        text = f"{code}: {message}".strip(": ").strip()
        return text[:500]
    if isinstance(err, str):
        return err[:500]
    return ""
