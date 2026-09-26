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

After each clip except the last, ffmpeg extracts the last frame (`ffmpeg -sseof -0.08 -i clip.mp4 -frames:v 1`). The next still is an Imagine image edit whose source image is that frame (a base64 data URI, which the edits API accepts). The edit prompt keeps the look bible, then tells the edit to keep the same face, the same body type, the same clothes, and the same color grade and lighting as that source frame. Only pose, blocking, and action may change, and only as far as the still prompt and the locked end state require. That new still is the first frame of the next image-to-video call.

A last-frame seed keeps wardrobe, lighting, and blocking attached to a real pixel frame. Rewriting the next still from prose alone drifts. Image edit is the documented way to condition a still on a local image without hosting a public URL.

**Fallback: `prose_regenerate` when ffmpeg is missing.** Every still is text-to-image. The look bible and the locked `start_state` / `end_state` lines are written into the prompt. `stitched_episode` stays false until ffmpeg concat actually writes `episode.mp4`.

When both are non-empty, shot N `start_state` must equal shot N-1 `end_state`.

### Look bible

A pack carries `look_bible`: `cast`, `wardrobe`, `palette`, `lighting`, and `camera`. Those five lines are the locked look for every shot. An empty bible is valid on a hand-written pack. `POST /api/packs/plan` and `POST /api/packs/fill` always write all five.

The text model is asked for the bible in the same strict JSON schema as the shots. Blank lines fall back to the fill heuristic (same face and body, same clothes, a palette and key light taken from the story, 35mm natural color). The heuristic does not call xAI.

At render time the server turns the bible into one block and puts it at the front of every still prompt and every image-to-video prompt. The motion prompt also says to continue from that exact still and not to change costume, hair, identity, or lighting. Only the described motion is animated.

A moderation retry softens the shot prose only. If a look-bible block is already in the text, it is lifted out, left unchanged, and written back. The pack bible is applied again when the Imagine prompt is built, so a rewrite cannot drop it.

The wizard shows the five lines under **Look bible**. Plan and Fill blanks load them into the draft. They are sent with the pack. They are not media URLs.

### Grade match

Before concat, the server soft-matches every clip after the first toward clip 1. It reads `signalstats` (YAVG, black and white points, saturation) and applies a gentle `eq` (brightness, contrast, saturation, gamma). That is a colorlevels-style nudge. It does not need `lut3d` or a generated LUT. Clip 1 is the reference and is not rewritten. Graded files are `clip.graded.mp4` next to the Imagine clip. The shot's `clip_path` stays the Imagine file.

The pass is on unless `OMARCHY_GRADE_MATCH` is `0`, `false`, `no`, or `off`. If ffmpeg is missing `eq` or `signalstats`, or a measurement fails, the pass is skipped, the job message says why, and concat still runs. The episode does not fail because the grade did not run.

`grade_match` on the job is true only when a graded file was written and used for concat. A stub job leaves it false. The job panel shows that flag, `continuity_mode` (`last_frame_edit` or `prose_regenerate`, or not set on a stub), and each shot's `still_mode` (`text_to_image`, `cast_reference`, or `last_frame_edit`).

When the source clip has an audio stream, the grade pass keeps it (`aac`). A silent source stays silent. The pass does not invent audio.

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

## Director brief

Simple mode in the wizard is one story prompt, an overall length, and aspect and resolution. **Plan** asks `POST /api/packs/plan` for a full pack draft and loads it into the shot editor. **Run** then behaves as it does today: fill any field that is still blank, create the pack, and start the job. Planning is text only. It does not call Imagine, and the honesty gates stay false until a run actually does that work. Advanced mode keeps the full shot editor and **Fill blanks from logline**.

`POST /api/packs/plan` accepts `{prompt, target_duration_sec, aspect_ratio?, resolution?, title?, style_preset?, cast?, staging?, lock_staging?}`. `cast` and `staging` are optional and are returned on the draft so another app can pass them through. The response is a pack draft valid for `POST /api/packs`: title, logline, aspect, resolution, `look_bible`, `style_preset`, `beat_map`, `cast`, `staging`, `lock_staging`, and shots with `id`, `prompt_still`, `prompt_motion`, `duration_sec`, `start_state`, `end_state`, `beat`, `camera`, and `stage`. It does not save the pack, call Imagine, or add media URLs. The same bearer token as the other pack routes is required.

`style_preset` is optional. `generic`, `action_duel`, `quiet_drama`, `trek`, and `chase` are the presets. Omit it and the server infers one from the prompt, or uses `generic`. A chase is inferred from words such as chase, pursuit, bandits, or gallop. A hand-written pack may leave `style_preset` empty, `beat_map` empty, `staging` empty, and each shot `beat`, `camera`, and `stage` empty. Those packs still load and run. Fill blanks keeps craft fields you already set.

### Director craft

Plan writes a beat map and a camera card so the shots are a story, not equal filler. Roles are `setup`, `turn`, `climax`, and `button`. Two shots are setup then button. Three are setup, climax, button. Longer plans keep one setup and one button and put the extra shots into turn, then climax.

Each shot `camera` card is one scale (`wide`, `medium`, `close`, `extreme_close`), one angle (`eye`, `low`, `high`, `ots`, `dutch`), one move (`static`, `dolly_in`, `dolly_out`, `orbit`, `pan`, `tilt`, `whip_pan`, `handheld`), and an `exit_frame`. Scales alternate on purpose. A four-shot generic plan is wide, then medium, then close, then wide. Dutch is used at most once, and only on an action-duel climax. The planner does not assign `orbit`, `whip_pan`, or a reverse over-the-shoulder. Those cross the line of action unless the shot sets `camera_side` to `cross` and gives a `cross_reason`. The still prompt is a locked frame: who, wardrobe, pose, space, and light, with no camera move. The motion prompt restates each figure's screen side, depth, and facing, then one action and the one camera move. A turn is a torso twist, not a reversal of travel. Look-bible anchors are repeated lightly in both lines. `look_bible.camera` stays the lens and grade. The shot card is the grammar for that clip.

`staging` is the scene map: entities, the axis of action, travel, and default relations such as bandits behind, far. Each shot `stage` is the blocking at the first frame and the last: screen third, depth, facing, look, travel, visibility, `camera_side`, and `cross_reason` when a side changes. Shot N `stage.start` copies shot N-1 `stage.end`. When `lock_staging` is true (the default), the server injects a staging clause and negative locks such as "Nobody rides beside" into every still, edit, and motion prompt. A rider whose look is opposite travel is written as a waist twist in the saddle, with the horse still galloping in side profile, and pursuers stay in side profile too. Pursuers drop farther behind, still riding, in the final shot unless the story says they stop. A rider's own horse is part of that rider, not a separate beside relation. Scene `shot_ids` that do not match the pack are mapped onto the real shot ids. The wizard shows a read-only stage strip per shot and one Lock staging toggle. Users do not fill the enums. The field list is in `docs/staging.schema.json`.

`exit_frame` names the picture the next shot should open on. On a heuristic plan it matches `end_state`, and the next `start_state` copies it, so a last-frame edit starts clean. The text model is asked for the same card. The server keeps the card it requested and keeps a model exit frame when one is present. Violent wording is softened before the draft is returned. Durations are unchanged: a later beat is shorter only when the even split already makes that clip shorter.

In Simple mode, Plan loads the beat map and the camera cards into the review. Edit them, then Run. Advanced mode still accepts a pack that has none of these fields. Planning is text only. Honesty gates stay false until Imagine runs.

`target_duration_sec` is an integer from **8 to 120** seconds. Shorter or longer is HTTP 422, with the message that the target must be in that range. Two shots is the short end (a single clip is not a plan). Eight shots is the long end, so a plan stays within eight Imagine clips. `120` is eight clips at the 15-second maximum.

Shot count is the nearest number of ~8 second clips. Halves round up (`20` seconds is three shots, not two). The count is then clamped to 2–8. Each clip is an integer from 1 to 15 seconds, as even as possible, and the durations sum to the target. From 12 through 80 seconds every clip lands in 6–10 seconds. An 8-second film is two 4-second clips, because two shots cannot both be 6 seconds and still add up to 8. A 100-second film is eight clips of 13 or 12 seconds, because eight clips is the cap.

When `XAI_API_KEY` is set, the server sends the story to the text model `grok-4.6` with `POST /v1/chat/completions` and a strict JSON schema (`response_format.type` of `json_schema`). That is the structured-output shape in the current xAI docs, and Grok 4.6 accepts chat completions. The key stays in the server environment. The JSON is validated into the pack model. Durations, ids, aspect, the start/end chain, and the camera card are applied by the server, not trusted from the model. The model writes the still, the motion, and the exit frame for the card it was given. Violent wording is softened with the same replacements as a moderation retry, so a planned pack is less likely to be rejected later. `XAI_TEXT_MODEL` can pin a different text model. The Imagine still and video models do not change.

Without a key, planning uses the fill-blanks heuristic for the look bible, then rewrites each still and motion line from the beat and the camera card. Shot count and durations still come from the math above. That path makes no network call, so CI can plan without a live API.

```bash
curl -s -X POST http://127.0.0.1:8010/api/packs/plan \
  -H "Authorization: Bearer local-dev-token" \
  -H "Content-Type: application/json" \
  -d '{"prompt":"A fisher leaves the dock as the fog lifts.","target_duration_sec":24,"aspect_ratio":"16:9","resolution":"720p"}'
```

The JSON that comes back is a valid `POST /api/packs` body. Shot 2 `start_state` equals shot 1 `end_state`, and the durations sum to 24.

## Preflight

Before a run spends an Imagine call, review the exact prompts and the call count.

`POST /api/packs/preflight` takes the same pack body as `POST /api/packs`. It does not save the pack, does not open an Imagine client, and does not need `XAI_API_KEY`. The same bearer token is required. A stub run is unchanged: with no key, `POST /run` still finishes as `stub` and leaves every honesty gate false.

The response lists each shot's assembled still prompt and motion prompt (the same builders the pipeline uses), the still mode (`text_to_image`, `cast_reference`, or `last_frame_edit`), and the video mode. When ffmpeg is available, every shot after the first is seeded from the previous clip's last frame, and the still prompt includes `seed frame is the previous clip's last frame at run time`. Totals are still calls, video calls, and seconds of video, for example `4 stills, 4 videos, 32 s of video`. Prices are not estimated.

Issues use `block`, `warn`, or `info`:

| Code | Severity | When |
| --- | --- | --- |
| `verb_count` | warn | More than one action verb from the CLIP_BRIDGE verb bank is in `prompt_motion` |
| `camera_conflict` | warn | The motion text and `camera.move` name opposing moves (push against pull, a reversed screen direction, or a move the card does not match) |
| `banned_cut` | warn | Editorial cut language such as `cut to`, `smash cut`, `dissolve`, or `[shot 2]` |
| `handoff_state` | block | Shot N `start_state` is not shot N-1 `end_state` after strip |
| `lock_drift` | warn | A locked name, wardrobe item, or explicitly locked color is missing from a later shot where that character appears |
| `r2v_resolution` | info | A `1080p` pack will send `reference_to_video` at `720p` |
| `side_flip` | block | An entity switches screen side, or `camera_side` is `cross`, without `cross_reason` |
| `travel_flip` | block | Travel reverses (screen-left against screen-right) without `cross_reason` |
| `relation_violation` | block | A `behind` relation is not upstream of travel, or the gap is touching without a contact beat |
| `stage_handoff` | block | Shot N `stage.start` is not shot N-1 `stage.end` |
| `line_risk_camera` | warn | `orbit`, `whip_pan`, or a reverse `ots` in a multi-entity scene that stays on the same side of the line |
| `r2v_no_anchor` | warn | `reference_to_video` in a scene with two or more entities, so the still is not the first frame |
| `stage_missing` | warn | Two or more entities and the shot has no stage |
| `clause_missing` | warn | Lock staging is on and the assembled prompt has no staging clause |
| `vague_position` | info | Pursuit prose says near, next to, alongside, or beside |

`blocking` is true when any issue is `block`. Warnings and the resolution note do not block. A continuity mismatch that is already written on both sides is still `422` from the pack schema, the same as create. A one-sided gap is a preflight block, with a message on that shot.

The wizard button is **Review & run** in Simple and Advanced mode. It opens one panel: collapsible prompts, issues colored by severity, and the totals line. **Confirm run** stays disabled while a block is open. Confirm then fills any blank, creates the pack, and starts the job.

```bash
curl -s -X POST http://127.0.0.1:8010/api/packs/preflight \
  -H "Authorization: Bearer local-dev-token" \
  -H "Content-Type: application/json" \
  -d '{"title":"Harbor dawn","logline":"Fog lifts.","aspect_ratio":"16:9","resolution":"720p","shots":[{"id":"s01","prompt_still":"A quiet harbor at dawn","prompt_motion":"The boat eases off the dock","duration_sec":8,"end_state":"The boat is offshore.","start_state":""}]}'
```

## Moderation retry

Imagine can reject a still or a clip for content moderation. A live video poll does this as HTTP 400 with `Generated video rejected by content moderation.` A finished video can also come back with `respect_moderation: false` and no URL. Image generation and image edit use the same moderation wording on HTTP 400, and a 200 image body can set `respect_moderation` to false. The docs describe that image case as filtered by moderation.

When a shot hits one of those responses, the job rewrites that shot's still prompt and motion prompt and submits the shot again. The rewrite is deterministic and does not call a text model: gore and death become exhaustion and victory, and a fight becomes a choreographed clash with no blood. Each shot gets at most two rewrites. The next rejection fails the job. The error is stored, and the gates stay at whatever work actually happened. Shots that already produced a clip are not rendered again.

The job panel shows the original prompt, the softened prompt, and the retry count for that shot. The saved pack keeps the text you typed.

## Phase 2

The plan is [docs/phase2.md](docs/phase2.md). Three additions sit on the director brief, director craft, and continuity lock. A pack that omits `cast`, `music_path`, `video_mode`, `dialogue`, and `voice_id` still validates and runs.

### Cast / reference lock

`cast` is a list of named local images. Each entry has `id`, `name`, `role` (`character`, `prop`, or `location`), `markers`, and `image_path` under `data/references/`. The wizard Cast panel (Simple and Advanced) uploads, previews, and removes them.

```bash
curl -s -X POST http://127.0.0.1:8010/api/references \
  -H "Authorization: Bearer local-dev-token" \
  -F "file=@mara.png" \
  -F "name=Mara" \
  -F "role=character" \
  -F "markers=Grey coat, scar on the left brow"
```

PNG, JPEG, or WebP by magic bytes, max 10 MB. The response is a cast entry with a relative `image_path`. `GET /api/references/{id}` returns the file. `DELETE` removes it. Saving a pack rejects a missing image. The server does not invent a URL.

Imagine image generations do not take reference images. Stills use image edits, at most 3 sources (the landing page allows 3; the multi-image page allows 5; this app stays at 3):

- No cast and no previous frame: text-to-image (`still_mode: text_to_image`).
- Cast and no previous frame: edits with those images (`still_mode: cast_reference`).
- Previous frame and no cast: the Phase 1 single-image edit (`still_mode: last_frame_edit`).
- Previous frame and cast: the last frame is `<IMAGE_0>`, plus up to 2 cast refs (`still_mode: last_frame_edit`).

Plan and fill keep a cast you already sent and name those people, props, and places in the still and motion lines.

### Reference-to-video

Each shot has `video_mode`: `image_to_video` (default) or `reference_to_video`.

Checked against the docs on 2026-09-24. The [reference-to-video](https://docs.x.ai/developers/model-capabilities/video/reference-to-video) page, the generation mode table, and the files-input examples all send `reference_images` to `grok-imagine-video-1.5`. The [Imagine landing](https://docs.x.ai/developers/model-capabilities/imagine) page still says reference-to-video requires `grok-imagine-video` and that 1.5 does not support it. That landing sentence is stale against the dedicated pages, so this app offers the mode on 1.5.

Reference-to-video is capped at 720p. A `1080p` pack that picks the mode is sent at 720p, and the shot `note` says so. This app does not send `image` together with `reference_images`. The generation page treats that combination as HTTP 400, and the reference-to-video page disagrees about whether 1.5 may pin a first frame. `image_to_video` sends the still as `image` and no references. `reference_to_video` sends `reference_images` and no `image`, so the first frame is not locked. The mode needs at least one cast image or a `voice_id`.

`dialogue` is optional prose. There is no dialogue field on the video API, so the line is written into the motion prompt. `voice_id` is `reference_audios[].voice_id` (preset voices such as `eve`, `leo`, `ara`) and is valid only on `reference_to_video`. A `voice_id` on `image_to_video` is HTTP 422. Custom voice audio files are partner-only and are not accepted.

### Per-shot revise

On a job in `done` or `error`, one shot can be revised without redoing the others. `queued` and `running` are 409. A stub job, or a live job with `XAI_API_KEY` unset, is 422 and does not open a client.

| Route | Body | What it calls |
| --- | --- | --- |
| `POST /api/packs/{pack}/jobs/{job}/shots/{shot}/regenerate` | optional `prompt_still`, `prompt_motion` | Still plus video for that shot |
| `.../edit` | `prompt` | `POST /v1/videos/edits` |
| `.../extend` | `prompt`, `duration_sec` 2–10 | `POST /v1/videos/extensions` |

Edits and extensions use `grok-imagine-video`, the model in the REST examples, not 1.5. An edit keeps the input duration and is capped at about 8.7 seconds and 720p. If ffprobe says the clip is longer than 8.7 seconds, the route is 422 and xAI is not called. An extension adds 2–10 seconds.

The current file stays `clip.mp4`. Before it is replaced, the previous file is copied to `clip.vN.mp4`. `shots[].revisions` records `version`, `action` (`generate`, `regenerate`, `edit`, `extend`), `clip_path`, `prompt`, and `created_at`. Gates flip only for work that happened. A failed edit or extend that did not replace the clip leaves a `done` job `done`. If the clip was replaced and concat did not write a new episode, the old episode is removed and `stitched_episode` stays false.

After a revise, grade match runs again on the current clips. Clip 1 is the reference. A revised later shot is matched toward the current clip 1. A revised clip 1 becomes the new reference. Archived `clip.vN.mp4` files stay ungraded. Other shots are not re-rendered. A regenerate of shot N uses the previous shot's last frame when that file is on disk. Edit and extend re-extract this shot's last frame so a later regenerate of the next shot can see it. They do not rebuild the next shot.

The job panel shows Regenerate, Edit, and Extend, a prompt field, a status line, and the version list.

### Audio

Generated video includes audio unless `generate_audio` is false. This app does not send that flag, so a returned track is kept through the stitch. If every clip is silent, the episode stays silent. If any clip has audio, silent clips get a matching silent track first so concat does not drop the tracks that exist.

`has_audio` is true only when ffprobe sees an audio stream on `episode.mp4`.

Optional music is a file you upload. The server never generates or downloads music. `POST /api/music` stores wav, mp3, m4a, or ogg (max 20 MB) under `data/music/`. `POST /api/packs/{id}/music` stores it on the pack and, when the latest job is `done` and already stitched, remixes from `episode.base.mp4` without calling Imagine. The mix ducks under speech when `sidechaincompress` is available, otherwise it sits at a fixed low level, and it fades out. `music_bed_applied` is true only after that mix writes a file ffprobe says has audio.

## Honesty gates

Each job exposes booleans that stay false until that work has happened:

- `called_imagine_still`
- `produced_still`
- `called_imagine_video`
- `produced_mp4`
- `stitched_episode`

Two more flags live on the job and stay false until the file proves them:

- `has_audio` — ffprobe sees an audio stream on `episode.mp4`
- `music_bed_applied` — the uploaded bed was mixed into that file

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
data/references/<id>.png
data/music/<id>.wav
data/runs/<pack_id>/shots/<shot_id>/still.png
data/runs/<pack_id>/shots/<shot_id>/clip.mp4
data/runs/<pack_id>/shots/<shot_id>/clip.v1.mp4
data/runs/<pack_id>/episode.base.mp4
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

Point Grok Bot, Grok Build, or Cursor at the skill above. It documents the exact HTTP calls against `http://127.0.0.1:8010` with `Bearer local-dev-token`: preflight a pack, create it, enqueue a run, poll jobs, and download the episode only after `stitched_episode` is true. Call preflight before run.

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
| `XAI_API_KEY` | unset | Live Imagine, and the director-brief text model. Unset means stub runs and heuristic plans. |
| `XAI_TEXT_MODEL` | `grok-4.6` | Chat model for `POST /api/packs/plan` when a key is set. |
| `OMARCHY_DATA_DIR` | `./data` | SQLite file and `runs/` |
| `XAI_API_BASE` | `https://api.x.ai/v1` | Override only for a compatible proxy |
| `OMARCHY_VIDEO_POLL_SEC` | `5` | Video status poll interval |
| `OMARCHY_VIDEO_TIMEOUT_SEC` | `600` | Give up on a still-pending video |
| `VITE_API_BASE` | `http://127.0.0.1:8010` | Wizard API origin |
| `VITE_API_TOKEN` | `local-dev-token` | Wizard bearer token |

## Later

Not in this build:

- Hermes Desktop pane
- Cost estimator UI
- Multi-episode seasons

Director brief planning is included (`POST /api/packs/plan`, and Simple mode in the wizard). Phase 2 is included: cast reference images, optional reference-to-video, per-shot regenerate / edit / extend, and an uploaded music bed. It does not estimate cost or plan a season.

Also out of scope: ComfyUI, Qwen, MiniMax, and local GPU lanes.

## License

[MIT](LICENSE). Copyright 2026 SMF Works.
