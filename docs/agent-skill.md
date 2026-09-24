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

Director brief (`POST /api/packs/plan`) when you have one story prompt and a target length in seconds. The response is a full draft (title, logline, shots, durations, chained start/end states), not a saved pack. `target_duration_sec` must be from 8 to 120 or the route is `422`. Shot count is about one clip per 8 seconds, at least 2 and at most 8, and the durations sum to the target. With `XAI_API_KEY` set, a text model writes the prose. Without a key, the fill heuristic does, and no Imagine call is made. Post that JSON to `POST /api/packs` when you want to save it. Gates stay false until you run.

```bash
curl -s -X POST http://127.0.0.1:8010/api/packs/plan \
  -H "Authorization: Bearer local-dev-token" \
  -H "Content-Type: application/json" \
  -d '{"prompt":"A fisher leaves the dock as the fog lifts.","target_duration_sec":24}'
```

Run (`POST /api/packs/$PACK_ID/run`). The body comes back with `status: queued`. Poll until the newest job is `stub`, `done`, or `error`:

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

Without `XAI_API_KEY`, status is `stub`, every gate is false, and the payload has no media URL.

Download only when `stitched_episode` is true:

```bash
curl -sL "http://127.0.0.1:8010/api/packs/$PACK_ID/episode" \
  -H "Authorization: Bearer local-dev-token" \
  -o episode.mp4
```

A stub or unfinished job returns `404` from `/episode`. Do not invent a URL.

List and fetch packs with `GET /api/packs` and `GET /api/packs/$PACK_ID`. A second run while a job is `queued` or `running` is `409`. A continuity mismatch (`start_state` must equal the previous `end_state` when both are set) is `422`. A missing bearer token is `401`.
