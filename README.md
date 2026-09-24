# Omarchy Grok Imagine

SMF Works desktop app for Omarchy. A short wizard (or Grok Bot / Cursor) writes a pack. [Grok Imagine](https://docs.x.ai/developers/model-capabilities/imagine) generates each still and each clip. **ffmpeg** concatenates the clips into one episode. This repo is not a fork of `aigc-production-flow` and it does not live inside that tree.

Imagine is an xAI product. API keys come from the [xAI console](https://console.x.ai). This repository never stores a key.

## What Phase 1 does

1. Validate a pack (title, logline, aspect, resolution, shots).
2. For each shot, call Imagine for a still, then image-to-video using that still as the first frame.
3. Stitch the clips with the ffmpeg concat demuxer. Imagine is not used to concatenate.

Ports, chosen so they do not collide with AIGC Studio (`5174` / `8000`) or Overwatch (`4173`):

| Service | URL |
| --- | --- |
| API | `http://127.0.0.1:8010` |
| Wizard | `http://127.0.0.1:5180` |

Local requests use `Authorization: Bearer local-dev-token`. That token is a fixed local development value, not an xAI key.

## Models

Pinned in `src/omarchy_imagine/config.py` from the current docs:

| Step | Model | Endpoint |
| --- | --- | --- |
| Text-to-image | `grok-imagine-image-2.0` | `POST https://api.x.ai/v1/images/generations` |
| Still seeded from the previous frame | `grok-imagine-image-2.0` | `POST https://api.x.ai/v1/images/edits` |
| Image-to-video | `grok-imagine-video-1.5` | `POST https://api.x.ai/v1/videos/generations` then `GET https://api.x.ai/v1/videos/{request_id}` |

References:

- [Image generation](https://docs.x.ai/developers/model-capabilities/images/generation)
- [Image-to-video](https://docs.x.ai/developers/model-capabilities/video/image-to-video)
- [Videos REST reference](https://docs.x.ai/developers/rest-api-reference/inference/videos)

`grok-imagine-image` still appears on the models list. The generation and edit examples in the current docs use `grok-imagine-image-2.0`, so Phase 1 pins that id.

Pack `resolution` is the video resolution (`480p`, `720p`, `1080p`). The image API uses `1k` / `1.5k` / `2k`. A `1080p` pack requests `2k` stills. `480p` and `720p` request `1k`.

Aspect ratios are the values both APIs document: `1:1`, `16:9`, `9:16`, `4:3`, `3:4`, `3:2`, `2:3`. Shot duration is 1–15 seconds. The default is 8.

## Continuity

**Choice: `last_frame_edit` when ffmpeg is on `PATH`.**

After each clip except the last, ffmpeg extracts the last frame (`ffmpeg -sseof -0.08 -i clip.mp4 -frames:v 1`). The next still is an Imagine image edit whose source image is that frame (a base64 data URI, which the edits API accepts). The edit prompt is the shot's `prompt_still` plus the locked start and end state. That new still is the first frame of the next image-to-video call.

A last-frame seed keeps wardrobe, lighting, and blocking attached to a real pixel frame. Rewriting the next still from prose alone drifts. Image edit is the documented way to condition a still on a local image without hosting a public URL.

**Fallback: `prose_regenerate` when ffmpeg is missing.** Every still is text-to-image, and the locked `start_state` / `end_state` lines are written into the prompt. `stitched_episode` stays false until ffmpeg concat actually writes `episode.mp4`.

When both are non-empty, shot N `start_state` must equal shot N-1 `end_state`.

## Fill blanks

A pack needs still and motion prompts before it can run. If you only have a title and/or a logline, the wizard and the API can fill the empty shot fields from that description.

`POST /api/packs/fill` accepts a partial pack, or just `{title, logline, shot_count?}`. It returns a pack draft. It does not save the pack, call Imagine, or add media URLs. The same bearer token as the other pack routes is required.

The fill is a deterministic heuristic. It does not need `XAI_API_KEY`. Text you already typed is kept. An empty still prompt becomes a visual line from the logline (or the title, when the logline is blank). An empty motion prompt is camera and action direction built from that shot's still prompt, such as a tracking shot alongside a running figure. Empty start and end states describe the picture at that boundary, using the adjacent still prompts, and they stay chained. Aspect, resolution, and `duration_sec` stay as you set them; omitted values stay at `16:9`, `720p`, and 8 seconds. Omitted `shot_count` with no shots creates two shots. A `shot_count` smaller than the shots you sent does not drop those shots.

Continuity is a locked chain: shot N `start_state` equals shot N-1 `end_state`. If you already set one side of that boundary, the blank side copies it. If both sides are set and they differ, the route is `422` and neither side is rewritten.

In the wizard, **Fill blanks from logline** does that fill and leaves the draft on screen. **Run** does the same fill first when a title or logline is present and any of those shot fields is still blank, then creates the pack and starts the job.

```bash
curl -s -X POST http://127.0.0.1:8010/api/packs/fill \
  -H "Authorization: Bearer local-dev-token" \
  -H "Content-Type: application/json" \
  -d '{"title":"Harbor dawn","logline":"A fisher leaves the dock as the fog lifts.","shot_count":2}'
```

The JSON that comes back is a valid `POST /api/packs` body.

## Moderation retry

Imagine can reject a still or a clip for content moderation. A live video poll does this as HTTP 400 with `Generated video rejected by content moderation.` A finished video can also come back with `respect_moderation: false` and no URL. Image generation and image edit use the same moderation wording on HTTP 400, and a 200 image body can set `respect_moderation` to false. The docs describe that image case as filtered by moderation.

When a shot hits one of those responses, the job rewrites that shot's still prompt and motion prompt and submits the shot again. The rewrite is deterministic and does not call a text model: gore and death become exhaustion and victory, and a fight becomes a choreographed clash with no blood. Each shot gets at most two rewrites. The next rejection fails the job. The error is stored, and the gates stay at whatever work actually happened. Shots that already produced a clip are not rendered again.

The job panel shows the original prompt, the softened prompt, and the retry count for that shot. The saved pack keeps the text you typed.

## Honesty gates

Each job exposes booleans that stay false until that work has happened:

- `called_imagine_still`
- `produced_still`
- `called_imagine_video`
- `produced_mp4`
- `stitched_episode`

The same four Imagine flags are stored on each shot. A job-level Imagine flag becomes true once that work has happened for at least one shot. `stitched_episode` is true only after ffmpeg writes the episode file.

**Stub mode.** If `XAI_API_KEY` is unset, `POST /api/packs/{id}/run` accepts the pack, records a job, and finishes as `status: stub`. No Imagine client is created. Every gate stays false. The response contains no media URLs. Paths are included only when a real file is on disk.

`GET /api/health` reports `imagine_configured` without revealing the key.

## Install on Omarchy

Omarchy is Arch-based. From a checkout of this repo:

```bash
sudo pacman -S python python-pip nodejs npm ffmpeg
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cd web
npm install
npm run build
cd ..
cp .env.example .env
# Put a key from https://console.x.ai in .env as XAI_API_KEY=...
# Leave it empty for the stub dry-run.
set -a && source .env && set +a
```

API (repo root, venv active):

```bash
omarchy-imagine
# or: uvicorn omarchy_imagine.app:app --host 127.0.0.1 --port 8010
```

Wizard, in another terminal:

```bash
cd web
npm run dev          # http://127.0.0.1:5180
# or, after npm run build:
npm run preview
```

`scripts/run.sh` starts the API and then `npm run preview` for the built wizard. It sources `.env` when that file exists.

Desktop launcher. This opens a chrome-free window (an Omarchy web app, or Chromium `--app=`) instead of a full browser. The script sources `.env` and `.venv` when they exist, starts uvicorn on `:8010` and `npm run preview` on `:5180` if those ports are down, then focuses the window.

```bash
chmod +x scripts/omarchy-grok-imagine.sh
mkdir -p ~/.local/bin ~/.local/share/applications
ln -sfn "$(pwd)/scripts/omarchy-grok-imagine.sh" ~/.local/bin/omarchy-grok-imagine
cp packaging/omarchy-grok-imagine.desktop ~/.local/share/applications/
update-desktop-database ~/.local/share/applications/ || true
```

`Exec=omarchy-grok-imagine` expects that symlink on `PATH`. If the app menu does not see `~/.local/bin`, set `Exec` in the copied desktop file to the absolute script path. `StartupWMClass` is `OmarchyGrokImagine`, the same class the launcher passes to Chromium.

Launch order, matching Overwatch: `omarchy-launch-or-focus-webapp`, then `omarchy-launch-webapp`, then Chromium or Chrome `--app=` with `--class=OmarchyGrokImagine`, then `xdg-open`. You can also run `scripts/omarchy-grok-imagine.sh` by absolute path. `scripts/run.sh` is still the foreground terminal helper.

Artifacts, when a live run produces them:

```text
data/runs/<pack_id>/shots/<shot_id>/still.png
data/runs/<pack_id>/shots/<shot_id>/clip.mp4
data/runs/<pack_id>/episode.mp4
```

`data/` is gitignored. A new run clears that pack's artifact directory first, so a stub run cannot keep serving an older episode.

## Dry-run (no key)

```bash
curl -s http://127.0.0.1:8010/api/health

curl -s -X POST http://127.0.0.1:8010/api/packs \
  -H "Authorization: Bearer local-dev-token" \
  -H "Content-Type: application/json" \
  -d '{"title":"Harbor dawn","logline":"Fog lifts.","aspect_ratio":"16:9","resolution":"720p","shots":[{"id":"s01","prompt_still":"A quiet harbor at dawn","prompt_motion":"The boat eases off the dock","duration_sec":8,"end_state":"The boat is offshore.","start_state":""}]}'

# Use the id from the create response.
curl -s -X POST http://127.0.0.1:8010/api/packs/$PACK_ID/run \
  -H "Authorization: Bearer local-dev-token"

curl -s http://127.0.0.1:8010/api/packs/$PACK_ID/jobs \
  -H "Authorization: Bearer local-dev-token"
```

`POST /run` returns quickly with `status: queued` (the pipeline runs in the background). Poll `/jobs` until `status` is `stub`, `done`, or `error`. Without a key the job settles on `stub`, every gate is false, and `GET /api/packs/$PACK_ID/episode` is 404.

With a key, the same flow calls Imagine and, when ffmpeg is installed, writes `episode.mp4`. Download it with the bearer token. Do not invent a media URL if the gates are false.

The full agent flow is in [`.cursor/skills/omarchy-imagine/SKILL.md`](.cursor/skills/omarchy-imagine/SKILL.md) and the shorter [docs/agent-skill.md](docs/agent-skill.md). OpenAPI lives at `http://127.0.0.1:8010/docs`.

## Drive from Grok Bot

Point Grok Bot, Grok Build, or Cursor at the skill above. It documents the exact HTTP calls against `http://127.0.0.1:8010` with `Bearer local-dev-token`: create a pack, enqueue a run, poll jobs, and download the episode only after `stitched_episode` is true.

There is also a symlink at `skills/omarchy-imagine/SKILL.md`.

## Development

```bash
source .venv/bin/activate
ruff check src tests
pytest
cd web && npm run build
```

GitHub Actions installs ffmpeg, runs ruff and pytest, then typechecks and builds the wizard. Tests do not call the live Imagine API. `OMARCHY_SYNC_JOBS=1` runs a job inside the request so tests can read the terminal status immediately. Leave that unset for the wizard; the UI polls `/jobs`.

Useful environment variables:

| Variable | Default | Role |
| --- | --- | --- |
| `XAI_API_KEY` | unset | Live Imagine. Unset means stub. |
| `OMARCHY_DATA_DIR` | `./data` | SQLite file and `runs/` |
| `XAI_API_BASE` | `https://api.x.ai/v1` | Override only for a compatible proxy |
| `OMARCHY_VIDEO_POLL_SEC` | `5` | Video status poll interval |
| `OMARCHY_VIDEO_TIMEOUT_SEC` | `600` | Give up on a still-pending video |
| `VITE_API_BASE` | `http://127.0.0.1:8010` | Wizard API origin |
| `VITE_API_TOKEN` | `local-dev-token` | Wizard bearer token |

## Later

Not in Phase 1:

- Hermes Desktop pane
- Cost estimator UI
- Multi-episode seasons
- Imagine video editing, extension, and reference-to-video APIs

Also out of scope: ComfyUI, Qwen, MiniMax, and local GPU lanes.

## License

[MIT](LICENSE). Copyright 2026 SMF Works.
