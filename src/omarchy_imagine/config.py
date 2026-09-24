"""Pinned xAI Imagine models and local service settings.

Image model ``grok-imagine-image-2.0`` is the model in the current image
generation and edit examples:

- https://docs.x.ai/developers/model-capabilities/images/generation
- https://docs.x.ai/developers/rest-api-reference/inference/images

``POST /v1/images/generations`` and ``POST /v1/images/edits``.

Video model ``grok-imagine-video-1.5`` is the model in the current
image-to-video and video generation examples:

- https://docs.x.ai/developers/model-capabilities/video/image-to-video
- https://docs.x.ai/developers/model-capabilities/video/generation
- https://docs.x.ai/developers/rest-api-reference/inference/videos

``POST /v1/videos/generations`` returns ``request_id``. Poll
``GET /v1/videos/{request_id}`` until ``status`` is ``done``, ``failed``,
or ``expired``.

The older image id ``grok-imagine-image`` still appears on the models list.
Current generation docs demonstrate ``grok-imagine-image-2.0``, so Phase 1
pins that id.

Pack ``resolution`` is a video resolution (``480p``, ``720p``, ``1080p``).
The image API uses ``1k``, ``1.5k``, and ``2k``. ``1080p`` packs request
``2k`` stills; ``480p`` and ``720p`` request ``1k``.
"""

from __future__ import annotations

API_HOST = "127.0.0.1"
API_PORT = 8010
WEB_PORT = 5180

LOCAL_DEV_TOKEN = "local-dev-token"

XAI_API_BASE = "https://api.x.ai/v1"
IMAGE_MODEL = "grok-imagine-image-2.0"
VIDEO_MODEL = "grok-imagine-video-1.5"

# Intersection of the image and video aspect ratios documented on the REST
# reference, so one pack value is valid for stills and for image-to-video.
ASPECT_RATIOS = ("1:1", "16:9", "9:16", "4:3", "3:4", "3:2", "2:3")
VIDEO_RESOLUTIONS = ("480p", "720p", "1080p")

MIN_DURATION_SEC = 1
MAX_DURATION_SEC = 15
DEFAULT_DURATION_SEC = 8

DEFAULT_VIDEO_POLL_SEC = 5.0
DEFAULT_VIDEO_TIMEOUT_SEC = 600.0


def image_resolution_for(video_resolution: str) -> str:
    if video_resolution == "1080p":
        return "2k"
    return "1k"
