---
name: omarchy-imagine
description: Drive the local Omarchy Grok Imagine API to create a pack, run Imagine stills and image-to-video, and poll honesty gates. Use when building or running an episode against http://127.0.0.1:8010.
---

# Omarchy Grok Imagine

The API listens on `http://127.0.0.1:8010`. Every pack and job route requires:

```http
Authorization: Bearer local-dev-token
```

`local-dev-token` is the local app token. It is not an xAI key. The xAI key is the server environment variable `XAI_API_KEY`, read from the process environment. Never put `XAI_API_KEY` in a request body, and never commit it.

OpenAPI: `http://127.0.0.1:8010/docs`.

Models the server pins (do not send a different model; the adapter chooses them):

- Stills: `grok-imagine-image-2.0` via `POST https://api.x.ai/v1/images/generations` and, for later shots when ffmpeg is available, `POST https://api.x.ai/v1/images/edits`
- Video: `grok-imagine-video-1.5` via `POST https://api.x.ai/v1/videos/generations`, then poll `GET https://api.x.ai/v1/videos/{request_id}`

You only call the local API. The server calls xAI.

## Health

No bearer token.

```bash
curl -s http://127.0.0.1:8010/api/health
```

`imagine_configured: false` means `XAI_API_KEY` is unset. A run will finish as `stub`. Gates stay false. There will be no media URLs.

`continuity` is `last_frame_edit` when ffmpeg is installed, otherwise `prose_regenerate`.

## 1. Create a pack

`POST /api/packs` returns `201` and the pack, including `id`.

Rules:

- `aspect_ratio`: `1:1`, `16:9`, `9:16`, `4:3`, `3:4`, `3:2`, or `2:3`. Default `16:9`.
- `resolution`: `480p`, `720p`, or `1080p`. Default `720p`. This is the video resolution.
- At least one shot. `duration_sec` is an integer from 1 to 15. Default 8.
- Shot `id` is a filename-safe token (`s01`). Ids are unique.
- `prompt_still` and `prompt_motion` are required.
- When shot N has a non-empty `start_state` and shot N-1 has a non-empty `end_state`, they must be equal. A mismatch is `422`.

```bash
curl -s -X POST http://127.0.0.1:8010/api/packs \
  -H "Authorization: Bearer local-dev-token" \
  -H "Content-Type: application/json" \
  -d '{
    "title": "Harbor dawn",
    "logline": "A fisher leaves the dock as the fog lifts.",
    "aspect_ratio": "16:9",
    "resolution": "720p",
    "shots": [
      {
        "id": "s01",
        "prompt_still": "A wooden fishing boat at a quiet harbor, dawn fog, 35mm film still",
        "prompt_motion": "The boat eases away from the dock, fog sliding past the lens",
        "duration_sec": 8,
        "end_state": "The boat is ten meters off the dock, bow pointed toward open water, fog still thick.",
        "start_state": ""
      },
      {
        "id": "s02",
        "prompt_still": "The same fishing boat in open water, fog thinning, same hull",
        "prompt_motion": "A slow push in as the fog thins and the bow rises on a swell",
        "duration_sec": 8,
        "start_state": "The boat is ten meters off the dock, bow pointed toward open water, fog still thick.",
        "end_state": "The boat is in open water, fog lifted to the horizon, bow unchanged."
      }
    ]
  }'
```

Save `id` as `PACK_ID`.

`GET /api/packs` lists packs. `GET /api/packs/$PACK_ID` fetches one. Both need the bearer token. Unknown ids are `404`.

## 2. Run

```bash
curl -s -X POST "http://127.0.0.1:8010/api/packs/$PACK_ID/run" \
  -H "Authorization: Bearer local-dev-token"
```

Response:

```json
{"job_id": "<uuid>", "pack_id": "<uuid>", "status": "queued"}
```

The pipeline runs in the background. Do not treat `queued` as finished.

`409` means this pack already has a job in `queued` or `running`. Poll that job instead of starting another.

Missing or wrong bearer token is `401`.

## 3. Poll jobs

```bash
curl -s "http://127.0.0.1:8010/api/packs/$PACK_ID/jobs" \
  -H "Authorization: Bearer local-dev-token"
```

The newest job is `jobs[0]`. Poll about every 5 seconds until `status` is one of:

| Status | Meaning |
| --- | --- |
| `queued` | Accepted, not started |
| `running` | Imagine or ffmpeg work is in progress |
| `stub` | `XAI_API_KEY` was unset. No Imagine call. No media. |
| `done` | ffmpeg wrote the episode |
| `error` | Stop. Read `error`. Gates show only the work that actually happened. |

Stop conditions are `stub`, `done`, and `error`. A live video can take several minutes. The server stops polling xAI after `OMARCHY_VIDEO_TIMEOUT_SEC` (default 600) and records `error`.

### Gates

These booleans stay false until the corresponding work has happened. Never set them yourself, and never fill in a URL when they are false.

| Field | True only when |
| --- | --- |
| `called_imagine_still` | An Imagine still request was sent (generations or edits) |
| `produced_still` | A still file was written from those bytes |
| `called_imagine_video` | `POST /v1/videos/generations` returned |
| `produced_mp4` | A clip file was written from the downloaded video |
| `stitched_episode` | ffmpeg concat wrote `episode.mp4` |

Each object in `shots` repeats the first four flags plus:

- `still_path`, `clip_path`, `last_frame_path` — relative to the data directory, or `null` if that file is not on disk
- `still_mode` — `text_to_image` or `last_frame_edit`
- `video_request_id` — the xAI request id, not a media URL
- `error` — shot-level failure text

On a stub job every gate is false, `episode_path` is null, `continuity_mode` is null, and the JSON contains no `http://` or `https://` URL.

Example stub check (copy as one script after `PACK_ID` is set):

```bash
curl -s "http://127.0.0.1:8010/api/packs/$PACK_ID/jobs" \
  -H "Authorization: Bearer local-dev-token" \
  -o /tmp/omarchy-jobs.json

python - <<'PY'
import json
job = json.load(open("/tmp/omarchy-jobs.json", encoding="utf-8"))["jobs"][0]
assert job["status"] == "stub"
for gate in (
    "called_imagine_still",
    "produced_still",
    "called_imagine_video",
    "produced_mp4",
    "stitched_episode",
):
    assert job[gate] is False, gate
assert job["episode_path"] is None
blob = json.dumps(job)
assert "http://" not in blob
assert "https://" not in blob
print("stub ok", job["id"])
PY
```

## 4. Episode

Only when the latest job has `stitched_episode: true` and `episode_path` is set:

```bash
curl -sL "http://127.0.0.1:8010/api/packs/$PACK_ID/episode" \
  -H "Authorization: Bearer local-dev-token" \
  -o episode.mp4
```

Otherwise the route is `404` with detail `Episode is not stitched. No media URL is available.` Do not synthesize a substitute URL. Do not copy a clip and call it the episode.

On-disk layout after a successful live run (under the server data dir, default `./data`):

```text
runs/<pack_id>/shots/<shot_id>/still.png
runs/<pack_id>/shots/<shot_id>/clip.mp4
runs/<pack_id>/episode.mp4
```

A later run deletes that pack directory before it starts. Historical gates remain on the old job, but paths disappear once the files are gone, and `/episode` follows the latest job only.

## Continuity the server applies

You do not extract frames yourself.

- ffmpeg present: shot 1 is text-to-image. Each later still is an image edit seeded by the previous clip's last frame (`last_frame_edit`).
- ffmpeg absent: every still is text-to-image with the locked states in the prompt (`prose_regenerate`), and the episode is not stitched.

Write `end_state` as prose that the next shot can repeat as `start_state`.

## What not to do

- Do not call `api.x.ai` from the agent for this pipeline. The local server owns the adapter, polling, download, and gate updates.
- Do not mark a gate true in any client. Read them from `/jobs`.
- Do not invent `still.png`, `clip.mp4`, or `episode.mp4` URLs when the run is `stub` or a gate is false.
- Do not send Comfy, Qwen, or other local-GPU instructions. This app does not use them.
