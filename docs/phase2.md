# Phase 2 plan

Three gaps versus Kling, Runway, and MiniMax Hailuo: real reference images, a per-shot revise loop, and audio on the episode. This sits on the existing director brief, director craft, and continuity lock. Old packs that omit the new fields stay valid.

Docs checked on 2026-09-24:

- [Reference-to-video](https://docs.x.ai/developers/model-capabilities/video/reference-to-video)
- [Video generation](https://docs.x.ai/developers/model-capabilities/video/generation)
- [Video editing](https://docs.x.ai/developers/model-capabilities/video/editing)
- [Video extension](https://docs.x.ai/developers/model-capabilities/video/extension)
- [Videos REST](https://docs.x.ai/developers/rest-api-reference/inference/videos)
- [Image edits REST](https://docs.x.ai/developers/rest-api-reference/inference/images)
- [Imagine landing](https://docs.x.ai/developers/model-capabilities/imagine)

## Doc conflicts, and the choice

Reference-to-video is usable on `grok-imagine-video-1.5`. The reference-to-video page, the video generation mode table, and the files-input examples all send `reference_images` to that model. The Imagine landing page still says reference-to-video requires `grok-imagine-video` and that 1.5 does not support it. That landing sentence is stale against the dedicated pages. This app offers the mode.

Reference-to-video is capped at 720p (generation page). A `1080p` pack that picks the mode is sent at 720p, and the shot records that.

The generation page says `image` plus `reference_images` is HTTP 400. The reference-to-video page says `grok-imagine-video-1.5` can pin `image` together with references, and that classic `grok-imagine-video` rejects the combination. This app does not combine them. `image_to_video` sends the still as `image` and no references. `reference_to_video` sends `reference_images` and no `image`, so the first frame is not locked.

Video edits and extensions in the REST examples use `grok-imagine-video`, not 1.5. Edits inherit the input duration, capped at 8.7 seconds, and the output is capped at 720p. Extensions add 2–10 seconds (default 6 in the REST reference). This app uses `grok-imagine-video` for those two calls and refuses an edit when ffprobe says the clip is longer than 8.7 seconds.

Generated videos include an audio track unless `generate_audio` is false. This app does not send that flag, so a returned track is kept. There is no dialogue request field. A shot `dialogue` line is written into the prompt. `voice_id` is `reference_audios[].voice_id` and is documented only for reference-to-video on `grok-imagine-video-1.5` (preset voices, max 3, case-insensitive). A `voice_id` on `image_to_video` is HTTP 422. Custom audio voice files are partner-only and are not accepted.

Image generations do not take reference images. Multi-image editing does: the landing page says up to 3 sources, the multi-image page says up to 5, and the edits REST body uses an `images` array tagged `<IMAGE_0>`, `<IMAGE_1>`, … This app sends at most 3, which both pages allow. A single last-frame edit with no cast still uses the `image` object, as Phase 1 does.

## 1. Cast / reference lock

### Schema

`cast` on the pack, default `[]`. Each entry:

| Field | Rule |
| --- | --- |
| `id` | Filename-safe token, unique in the pack |
| `name` | Required |
| `role` | `character`, `prop`, or `location` |
| `markers` | Identity text, may be empty |
| `image_path` | Relative path under `data/references/`. Required when the pack is saved |

`music_path` on the pack, default `""`. Relative path under `data/music/` when set.

Shot fields, all optional:

| Field | Default | Rule |
| --- | --- | --- |
| `video_mode` | `image_to_video` | `image_to_video` or `reference_to_video` |
| `dialogue` | `""` | Folded into the video prompt. Not a separate xAI field |
| `voice_id` | `""` | Only with `reference_to_video` |

`reference_to_video` requires at least one cast image or a `voice_id`.

### API

`POST /api/references` multipart (`file`, `name`, `role`, `markers`), bearer auth. PNG, JPEG, or WebP by magic bytes, max 10 MB. Writes `data/references/{id}.{ext}` and returns the cast entry. No invented URL.

`GET /api/references/{id}` returns that file. `DELETE` removes it.

`POST /api/packs` rejects a cast entry whose file is missing, and a `music_path` that is missing.

`POST /api/music` stores a local audio file (wav, mp3, m4a/mp4, ogg; max 20 MB) under `data/music/`. The server never generates or downloads music.

### Generation

Characters, then props, then locations, at most 3 images.

- No cast and no previous frame: `POST /v1/images/generations` (`still_mode: text_to_image`).
- Cast and no previous frame: `POST /v1/images/edits` with `images` (`still_mode: cast_reference`).
- Previous frame and no cast: existing `image` edit (`still_mode: last_frame_edit`).
- Previous frame and cast: `images` is the last frame plus up to 2 cast refs (`still_mode: last_frame_edit`). `<IMAGE_0>` is the frame to keep.

`image_to_video` is unchanged: the still is the first frame.

`reference_to_video` posts `reference_images` (and `reference_audios` when `voice_id` is set) to `grok-imagine-video-1.5`. Resolution above 720p is lowered and noted on the shot.

Plan and fill keep a cast you already sent. Generated still and motion lines name any cast entry they do not already mention. The render prompts repeat the same lock.

## 2. Per-shot revise

Only a job in `done` or `error`. `queued` and `running` are 409. A stub job, or a live job with `XAI_API_KEY` unset, is 422 and does not open a client.

| Route | Body | xAI |
| --- | --- | --- |
| `POST /api/packs/{pack}/jobs/{job}/shots/{shot}/regenerate` | optional `prompt_still`, `prompt_motion` | Still plus video for that shot only |
| `.../edit` | `prompt` | `POST /v1/videos/edits` |
| `.../extend` | `prompt`, `duration_sec` 2–10 | `POST /v1/videos/extensions` |

The current clip stays `clip.mp4`. Before it is replaced, it is copied to `clip.vN.mp4` and the previous revision's path is updated to that copy. `shots[].revisions` records `version`, `action` (`generate`, `regenerate`, `edit`, `extend`), `clip_path`, `prompt`, and `created_at`. Gates flip only for work that happened.

A failed edit or extend that did not replace the clip leaves a `done` job `done`. The shot `error` explains it. If the clip was replaced but concat did not write a new episode, the old episode file is removed and `stitched_episode` stays false.

### Grade and continuity after a revise

Grade match runs again on the current clips before concat. Clip 1 is the reference. A revised later shot is matched toward the current clip 1. A revised clip 1 becomes the new reference and the other current clips are matched again. Archived `clip.vN.mp4` files stay as Imagine wrote them.

Other shots are not re-rendered. A regenerate uses the previous shot's last frame when that file is on disk (extracting it from the previous clip if the png is missing). Edit and extend re-extract this shot's last frame so a later regenerate of the next shot can see it. They do not rebuild the next shot.

## 3. Audio

Concat probes each clip. If every clip is silent, the episode stays silent. If any clip has audio, silent clips get a matching silent track first so the concat does not drop the tracks that exist. Grade match keeps an audio stream when the source has one (it no longer forces `-an` in that case).

`has_audio` is true only when ffprobe sees an audio stream on `episode.mp4`.

When `music_path` is set, stitch writes `episode.base.mp4`, then mixes the bed under the episode: sidechain ducking when `sidechaincompress` exists, otherwise a fixed low level, always with a fade out. `music_bed_applied` is true only after that mix writes a file. `POST /api/packs/{id}/music` stores a bed and remixes from `episode.base.mp4` when an episode is already stitched, without calling Imagine.
