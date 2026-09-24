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
# Edits and extensions in the current REST examples use grok-imagine-video, not 1.5.
VIDEO_EDIT_MODEL = "grok-imagine-video"

# Intersection of the image and video aspect ratios documented on the REST
# reference, so one pack value is valid for stills and for image-to-video.
ASPECT_RATIOS = ("1:1", "16:9", "9:16", "4:3", "3:4", "3:2", "2:3")
VIDEO_RESOLUTIONS = ("480p", "720p", "1080p")

MIN_DURATION_SEC = 1
MAX_DURATION_SEC = 15
DEFAULT_DURATION_SEC = 8

# Director brief. Shot count is the nearest count of ~8s clips, clamped so a
# plan stays cheap. 8 shots × 15s is the long end; two clips is the short end.
MIN_PLAN_TARGET_SEC = 8
MAX_PLAN_TARGET_SEC = 120
MIN_PLAN_SHOTS = 2
MAX_PLAN_SHOTS = 8
PREFERRED_SHOT_SEC = 8

# Chat completions model for director-brief expansion. Imagine stills and video
# stay on IMAGE_MODEL and VIDEO_MODEL. Override with XAI_TEXT_MODEL.
TEXT_MODEL = "grok-4.6"

DEFAULT_VIDEO_POLL_SEC = 5.0
DEFAULT_VIDEO_TIMEOUT_SEC = 600.0
DEFAULT_TEXT_TIMEOUT_SEC = 120.0

# Image edits accept multiple sources. The Imagine landing page says 3. The
# multi-image page says 5. Three satisfies both, and it is also the count in
# the reference-to-video examples.
MAX_REFERENCE_IMAGES = 3
MAX_CAST_ENTRIES = 8
# Video editing keeps the input duration, which the generation page caps at 8.7s.
EDIT_MAX_INPUT_SEC = 8.7
EXTEND_MIN_SEC = 2
EXTEND_MAX_SEC = 10
MAX_UPLOAD_IMAGE_BYTES = 10 * 1024 * 1024
MAX_UPLOAD_AUDIO_BYTES = 20 * 1024 * 1024

VIDEO_MODES = ("image_to_video", "reference_to_video")
CAST_ROLES = ("character", "prop", "location")


def reference_video_resolution(resolution: str) -> tuple[str, str | None]:
    """Reference-to-video is capped at 720p. 1080p is sent as 720p with a note."""
    if resolution == "1080p":
        return (
            "720p",
            "Reference-to-video is capped at 720p on grok-imagine-video-1.5. "
            "This shot was sent at 720p.",
        )
    return resolution, None


def image_resolution_for(video_resolution: str) -> str:
    if video_resolution == "1080p":
        return "2k"
    return "1k"
