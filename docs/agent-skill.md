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
