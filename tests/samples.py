from __future__ import annotations

import base64

PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)

AUTH = {"Authorization": "Bearer local-dev-token"}


def pack_body() -> dict:
    return {
        "title": "Harbor dawn",
        "logline": "A fisher leaves the dock as the fog lifts.",
        "aspect_ratio": "16:9",
        "resolution": "720p",
        "shots": [
            {
                "id": "s01",
                "prompt_still": "A wooden fishing boat at a quiet harbor, dawn fog",
                "prompt_motion": "The boat eases away from the dock",
                "duration_sec": 8,
                "end_state": "The boat is ten meters off the dock, fog still thick.",
                "start_state": "",
            },
            {
                "id": "s02",
                "prompt_still": "The same fishing boat, fog thinning",
                "prompt_motion": "A slow push in as the fog thins",
                "duration_sec": 8,
                "start_state": "The boat is ten meters off the dock, fog still thick.",
                "end_state": "The boat is in open water and the fog has lifted.",
            },
        ],
    }


def one_shot_body() -> dict:
    body = pack_body()
    body["shots"] = body["shots"][:1]
    return body
