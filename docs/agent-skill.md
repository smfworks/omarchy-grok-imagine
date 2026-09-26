# Drive Omarchy Grok Imagine

Canonical skill: [`.cursor/skills/omarchy-imagine/SKILL.md`](../.cursor/skills/omarchy-imagine/SKILL.md). The server is `http://127.0.0.1:8010`. Send `Authorization: Bearer local-dev-token` on every pack and job route. Put the xAI key in the server environment as `XAI_API_KEY`, never in the JSON body.

`GET /api/health` needs no token. `imagine_configured: false` means a run finishes as `stub`.

## HTTP flow

Create (`POST /api/packs`, `201`):

```bash
curl -s -X POST http://127.0.0.1:8010/api/packs \
  -H "Authorization: Bearer local-dev-token" \
  -H "Content-Type: application/json" \
  -d '{"title":"Harbor dawn","logline":"Fog lifts.","aspect_ratio":"16:9","resolution":"720p","shots":[{"id":"s01","prompt_still":"A quiet harbor at dawn","prompt_motion":"The boat eases off the dock","duration_sec":8,"end_state":"The boat is offshore.","start_state":""}]}'
```

Fill blanks (`POST /api/packs/fill`) when the pack only has a title and/or logline. The response is a draft, not a saved pack. Empty still prompts, motion prompts, and continuity states are filled. Text that is already set is kept. No Imagine call, and no media URLs. Post that JSON to `POST /api/packs` when you want to save it.

```bash
curl -s -X POST http://127.0.0.1:8010/api/packs/fill \
  -H "Authorization: Bearer local-dev-token" \
  -H "Content-Type: application/json" \
  -d '{"title":"Harbor dawn","logline":"Fog lifts.","shot_count":2}'
```

Director brief (`POST /api/packs/plan`) when you have one story prompt and a target length in seconds. The response is a full draft (title, logline, `look_bible`, `style_preset`, `beat_map`, `cast`, `staging`, `lock_staging`, shots, durations, chained start/end states), not a saved pack. The request accepts optional `cast`, `staging`, and `lock_staging` and returns them so another app can pass a stage map through. `target_duration_sec` must be from 8 to 120 or the route is `422`. Shot count is about one clip per 8 seconds, at least 2 and at most 8, and the durations sum to the target. With `XAI_API_KEY` set, a text model writes the prose, including the look bible and the stage. Without a key, the fill heuristic does, and no Imagine call is made. Either path also writes director craft: `style_preset` (`generic`, `action_duel`, `quiet_drama`, `trek`, or `chase`), a `beat_map` of setup / turn / climax / button, and on each shot a `beat` plus a `camera` card (`scale`, `angle`, `move`, `exit_frame`). A chase also writes `staging` and a per-shot `stage`. Send `style_preset` to force one; omit it and the server infers it. Those fields are optional on a hand-written pack. An empty camera card and a missing `stage` are valid. The still stays a locked frame. The motion line restates positions, then one action and one camera move. A turn is a torso twist. `exit_frame` is the picture the next shot opens on. Post that JSON to `POST /api/packs` when you want to save it. Gates stay false until you run. The draft has no media URLs.

```bash
curl -s -X POST http://127.0.0.1:8010/api/packs/plan \
  -H "Authorization: Bearer local-dev-token" \
  -H "Content-Type: application/json" \
  -d '{"prompt":"A fisher leaves the dock as the fog lifts.","target_duration_sec":24}'
```

Preflight (`POST /api/packs/preflight`) before you create or run. Send the pack body. The response is the assembled still and motion prompts, `still_mode_expected` (`text_to_image`, `cast_reference`, or `last_frame_edit`), `video_mode`, per-shot issues, and `totals` (`stills`, `videos`, `video_seconds`). `blocking` is true when any issue is `handoff_state`, `side_flip`, `travel_flip`, `relation_violation`, or `stage_handoff`. Warnings (`verb_count`, `camera_conflict`, `banned_cut`, `lock_drift`, `line_risk_camera`, `r2v_no_anchor`, `stage_missing`, `clause_missing`) and the `r2v_resolution` and `vague_position` notes do not block. This route does not save the pack and does not construct an Imagine client, with or without `XAI_API_KEY`. Read it before `POST /run`.

```bash
curl -s -X POST http://127.0.0.1:8010/api/packs/preflight \
  -H "Authorization: Bearer local-dev-token" \
  -H "Content-Type: application/json" \
  -d '{"title":"Harbor dawn","logline":"Fog lifts.","aspect_ratio":"16:9","resolution":"720p","shots":[{"id":"s01","prompt_still":"A quiet harbor at dawn","prompt_motion":"The boat eases off the dock","duration_sec":8,"end_state":"The boat is offshore.","start_state":""}]}'
```

Run (`POST /api/packs/$PACK_ID/run`) only after that preflight. The body comes back with `status: queued`. Poll until the newest job is `stub`, `done`, or `error`:

```bash
curl -s -X POST "http://127.0.0.1:8010/api/packs/$PACK_ID/run" \
  -H "Authorization: Bearer local-dev-token"

curl -s "http://127.0.0.1:8010/api/packs/$PACK_ID/jobs" \
  -H "Authorization: Bearer local-dev-token"
```

Read the gates on `jobs[0]`. They stay false until the work happened:

- `called_imagine_still`
- `produced_still`
- `called_imagine_video`
- `produced_mp4`
- `stitched_episode`

Without `XAI_API_KEY`, status is `stub`, every gate is false, `continuity_mode` is null, `grade_match` is false, `has_audio` is false, `music_bed_applied` is false, and the payload has no media URL.

`has_audio` is true only when ffprobe sees an audio stream on `episode.mp4`. `music_bed_applied` is true only after an uploaded bed was mixed into that file.

## Cast, revise, and audio

Upload a reference image before you put it on a pack. `POST /api/references` is multipart (`file`, `name`, `role`, `markers`) and returns `{id, name, role, markers, image_path}` under `data/references/`. `role` is `character`, `prop`, or `location`. PNG, JPEG, or WebP, max 10 MB. `GET /api/references/{id}` returns the file. `DELETE` removes it. `POST /api/packs` rejects a missing image. Do not invent a URL.

Optional shot fields: `video_mode` (`image_to_video` or `reference_to_video`), `dialogue` (folded into the video prompt; there is no dialogue API field), and `voice_id` (preset voices, only on `reference_to_video`). Reference-to-video uses `grok-imagine-video-1.5` with `reference_images` and no first-frame `image`, capped at 720p. The Imagine landing page still says 1.5 does not support that mode. The dedicated reference-to-video page does. This app follows the dedicated page.

On a `done` or `error` job, revise one shot and restitch:

- `POST /api/packs/{pack}/jobs/{job}/shots/{shot}/regenerate` with optional `prompt_still` and `prompt_motion`
- `.../edit` with `prompt` (`POST /v1/videos/edits`, model `grok-imagine-video`, input duration kept, refused above 8.7 seconds)
- `.../extend` with `prompt` and `duration_sec` from 2 to 10

`queued` or `running` is `409`. A stub job is `422` and does not call xAI. Prior clips are kept as `clip.vN.mp4`. Read `shots[].revisions`. Grade match runs again on the current clips. Other shots are not re-rendered.

`POST /api/music` stores a local wav, mp3, m4a, or ogg (max 20 MB). `POST /api/packs/{id}/music` attaches it and remixes a stitched episode from `episode.base.mp4` without calling Imagine. The server does not generate or download music.

## Continuity lock

`look_bible` is `cast`, `wardrobe`, `palette`, `lighting`, and `camera`. Plan and fill always return it. The server puts that block on every still prompt and every image-to-video prompt. A seeded still (`still_mode: last_frame_edit`) must keep the source frame's face, body, clothes, and grade. Motion prompts say to continue from that exact still and only animate the described motion. A moderation rewrite softens the shot prose and keeps the bible.

Before concat, ffmpeg can soft-match later clips toward clip 1 (`signalstats` + `eq`). `grade_match` is true only when that pass wrote a file. If the filters are missing, or `OMARCHY_GRADE_MATCH` is off, the job message says the pass was skipped and the episode still stitches. Read `continuity_mode`, each shot's `still_mode`, and `grade_match` from the job. Do not invent them.

Download only when `stitched_episode` is true:

```bash
curl -sL "http://127.0.0.1:8010/api/packs/$PACK_ID/episode" \
  -H "Authorization: Bearer local-dev-token" \
  -o episode.mp4
```

A stub or unfinished job returns `404` from `/episode`. Do not invent a URL.

List and fetch packs with `GET /api/packs` and `GET /api/packs/$PACK_ID`. A second run while a job is `queued` or `running` is `409`. A continuity mismatch (`start_state` must equal the previous `end_state` when both are set) is `422`. A missing bearer token is `401`.
