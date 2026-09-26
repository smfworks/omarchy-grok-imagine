import { useEffect, useRef, useState } from "react";
import {
  attachMusic,
  createPack,
  downloadEpisode,
  editShot,
  extendShot,
  fetchHealth,
  fetchJobs,
  fetchReferenceBlob,
  fillPack,
  planPack,
  preflightPack,
  regenerateShot,
  runPack,
  uploadMusic,
  uploadReference,
} from "./api";
import { examplePack } from "./example";
import { DURATION_PRESETS, MAX_PLAN_TARGET_SEC, MIN_PLAN_TARGET_SEC, planEstimate } from "./planMath";
import {
  ASPECTS,
  BEAT_ROLES,
  CAST_ROLES,
  CAMERA_ANGLES,
  CAMERA_MOVES,
  CAMERA_SCALES,
  GATES,
  RESOLUTIONS,
  STYLE_PRESETS,
  VIDEO_MODES,
  emptyCamera,
  emptyLookBible,
  type CameraCard,
  type CastRef,
  type Health,
  type Job,
  type LookBible,
  type PackDraft,
  type PreflightResult,
  type ShotDraft,
} from "./types";

type Mode = "simple" | "advanced";

function emptyShot(id: string, startState = ""): ShotDraft {
  return {
    id,
    prompt_still: "",
    prompt_motion: "",
    duration_sec: 8,
    end_state: "",
    start_state: startState,
    beat: "",
    camera: emptyCamera(),
    stage: null,
    video_mode: "image_to_video",
    dialogue: "",
    voice_id: "",
  };
}

function tokenLabel(value: string): string {
  return value ? value.replaceAll("_", " ") : "not set";
}

function totalsLine(totals: PreflightResult["totals"]): string {
  const stills = totals.stills === 1 ? "1 still" : `${totals.stills} stills`;
  const videos = totals.videos === 1 ? "1 video" : `${totals.videos} videos`;
  return `${stills}, ${videos}, ${totals.video_seconds} s of video`;
}

function nextShotId(shots: ShotDraft[]): string {
  const used = new Set(shots.map((shot) => shot.id));
  for (let index = shots.length + 1; index < 100; index += 1) {
    const candidate = `s${String(index).padStart(2, "0")}`;
    if (!used.has(candidate)) {
      return candidate;
    }
  }
  return `s${shots.length + 1}`;
}

function hasBlanks(pack: PackDraft): boolean {
  return pack.shots.some(
    (shot) =>
      !shot.prompt_still.trim() ||
      !shot.prompt_motion.trim() ||
      !shot.start_state.trim() ||
      !shot.end_state.trim(),
  );
}

function canDescribe(pack: PackDraft): boolean {
  return Boolean(pack.title.trim() || pack.logline.trim());
}

function validate(pack: PackDraft): string | null {
  if (!pack.title.trim()) {
    return "Title is required.";
  }
  if (pack.shots.length === 0) {
    return "Add at least one shot.";
  }
  const ids = new Set<string>();
  for (let index = 0; index < pack.shots.length; index += 1) {
    const shot = pack.shots[index];
    const id = shot.id.trim();
    if (!/^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/.test(id)) {
      return `Shot ${index + 1} needs a filename-safe id.`;
    }
    if (ids.has(id)) {
      return `Duplicate shot id ${id}.`;
    }
    ids.add(id);
    if (!shot.prompt_still.trim() || !shot.prompt_motion.trim()) {
      return `Shot ${id} needs a still prompt and a motion prompt.`;
    }
    if (!Number.isInteger(shot.duration_sec) || shot.duration_sec < 1 || shot.duration_sec > 15) {
      return `Shot ${id} duration must be an integer from 1 to 15.`;
    }
    if (index > 0) {
      const previous = pack.shots[index - 1].end_state.trim();
      const start = shot.start_state.trim();
      if (previous && start && previous !== start) {
        return `Shot ${id} start state must match the previous end state.`;
      }
    }
  }
  return null;
}

function payload(pack: PackDraft): PackDraft {
  return {
    title: pack.title.trim(),
    logline: pack.logline.trim(),
    aspect_ratio: pack.aspect_ratio,
    resolution: pack.resolution,
    look_bible: {
      cast: pack.look_bible.cast.trim(),
      wardrobe: pack.look_bible.wardrobe.trim(),
      palette: pack.look_bible.palette.trim(),
      lighting: pack.look_bible.lighting.trim(),
      camera: pack.look_bible.camera.trim(),
    },
    style_preset: pack.style_preset.trim(),
    beat_map: pack.beat_map
      .map((beat) => ({ role: beat.role.trim(), summary: beat.summary.trim() }))
      .filter((beat) => beat.role),
    cast: pack.cast.map((item) => ({
      id: item.id,
      name: item.name.trim(),
      role: item.role,
      markers: item.markers.trim(),
      image_path: item.image_path,
    })),
    staging: pack.staging,
    lock_staging: pack.lock_staging,
    music_path: pack.music_path.trim(),
    shots: pack.shots.map((shot) => ({
      id: shot.id.trim(),
      prompt_still: shot.prompt_still.trim(),
      prompt_motion: shot.prompt_motion.trim(),
      duration_sec: shot.duration_sec,
      end_state: shot.end_state.trim(),
      start_state: shot.start_state.trim(),
      beat: shot.beat.trim(),
      camera: {
        scale: shot.camera.scale.trim(),
        angle: shot.camera.angle.trim(),
        move: shot.camera.move.trim(),
        exit_frame: shot.camera.exit_frame.trim(),
      },
      stage: shot.stage,
      video_mode: shot.video_mode || "image_to_video",
      dialogue: shot.dialogue.trim(),
      voice_id: shot.video_mode === "reference_to_video" ? shot.voice_id.trim() : "",
    })),
  };
}

const STAGE_X: Record<string, number> = {
  offscreen_left: 8,
  left_edge: 18,
  left_third: 30,
  center: 50,
  right_third: 70,
  right_edge: 82,
  offscreen_right: 92,
};

const STAGE_DEPTH: Record<string, number> = {
  far: 24,
  background: 42,
  mid: 60,
  foreground: 80,
};

function travelMark(travel: string): string {
  if (travel === "screen_left") {
    return "←";
  }
  if (travel === "screen_right") {
    return "→";
  }
  if (travel === "toward_camera") {
    return "◎";
  }
  if (travel === "away_from_camera") {
    return "↑";
  }
  return "•";
}

function StageStrip({ pack, shot }: { pack: PackDraft; shot: ShotDraft }) {
  const stage = shot.stage;
  const blocks = stage?.end.length ? stage.end : (stage?.start ?? []);
  if (!stage || blocks.length === 0) {
    return null;
  }
  const scene =
    pack.staging?.scenes.find(
      (item) => item.id === stage.scene_id || item.shot_ids.includes(shot.id),
    ) ?? pack.staging?.scenes[0];
  const labels = new Map((scene?.entities ?? []).map((entity) => [entity.id, entity.label]));
  const travel =
    scene?.travel ||
    blocks.find((block) => block.travel && block.travel !== "static")?.travel ||
    "static";
  return (
    <div className="stage-strip" data-testid={`stage-strip-${shot.id}`}>
      <div className="stage-axis" />
      <span className="stage-arrow" aria-hidden="true">
        {travelMark(travel)}
      </span>
      {blocks
        .filter((block) => block.visible)
        .map((block) => (
          <span
            key={block.id}
            className="stage-dot"
            style={{
              left: `${STAGE_X[block.x] ?? 50}%`,
              top: `${STAGE_DEPTH[block.depth] ?? 58}%`,
            }}
            title={labels.get(block.id) || block.id}
          >
            <span className="stage-name">{labels.get(block.id) || block.id}</span>
          </span>
        ))}
    </div>
  );
}

function CastThumb({ item, preview }: { item: CastRef; preview?: string }) {
  const [url, setUrl] = useState<string | null>(preview ?? null);
  useEffect(() => {
    if (preview) {
      setUrl(preview);
      return undefined;
    }
    let cancelled = false;
    let objectUrl = "";
    fetchReferenceBlob(item.id)
      .then((blob) => {
        if (cancelled) {
          return;
        }
        objectUrl = URL.createObjectURL(blob);
        setUrl(objectUrl);
      })
      .catch(() => {
        if (!cancelled) {
          setUrl(null);
        }
      });
    return () => {
      cancelled = true;
      if (objectUrl) {
        URL.revokeObjectURL(objectUrl);
      }
    };
  }, [item.id, preview]);
  if (!url) {
    return <span className="note">No preview</span>;
  }
  return <img className="cast-preview" alt={`${item.name} reference`} src={url} />;
}

export function App() {
  const [draft, setDraft] = useState<PackDraft>({
    title: "",
    logline: "",
    aspect_ratio: "16:9",
    resolution: "720p",
    look_bible: emptyLookBible(),
    style_preset: "",
    beat_map: [],
    cast: [],
    staging: null,
    lock_staging: true,
    music_path: "",
    shots: [emptyShot("s01")],
  });
  const [health, setHealth] = useState<Health | null>(null);
  const [healthError, setHealthError] = useState<string | null>(null);
  const [packId, setPackId] = useState<string | null>(null);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [activity, setActivity] = useState<"plan" | "fill" | "run" | "review" | null>(null);
  const [review, setReview] = useState<PreflightResult | null>(null);
  const reviewRef = useRef<HTMLElement | null>(null);
  const busy = activity !== null;
  const [mode, setMode] = useState<Mode>("simple");
  const [planned, setPlanned] = useState(false);
  const [briefPrompt, setBriefPrompt] = useState("");
  const [briefTitle, setBriefTitle] = useState("");
  const [briefStyle, setBriefStyle] = useState("");
  const [targetSec, setTargetSec] = useState(32);

  useEffect(() => {
    setReview(null);
  }, [draft]);

  useEffect(() => {
    if (review) {
      reviewRef.current?.scrollIntoView({ behavior: "smooth", block: "start" });
    }
  }, [review]);
  const [castName, setCastName] = useState("");
  const [castRole, setCastRole] = useState("character");
  const [castMarkers, setCastMarkers] = useState("");
  const [castFile, setCastFile] = useState<File | null>(null);
  const castFileInput = useRef<HTMLInputElement>(null);
  const [castPreviews, setCastPreviews] = useState<Record<string, string>>({});
  const [musicLabel, setMusicLabel] = useState("");
  const [revisePrompt, setRevisePrompt] = useState<Record<string, string>>({});
  const [extendSec, setExtendSec] = useState<Record<string, number>>({});
  const [reviseStatus, setReviseStatus] = useState<Record<string, string>>({});

  useEffect(() => {
    let cancelled = false;
    fetchHealth()
      .then((body) => {
        if (!cancelled) {
          setHealth(body);
        }
      })
      .catch(() => {
        if (!cancelled) {
          setHealthError("API is not reachable at http://127.0.0.1:8010.");
        }
      });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (!packId) {
      return undefined;
    }
    let cancelled = false;
    const tick = () => {
      fetchJobs(packId)
        .then((next) => {
          if (!cancelled) {
            setJobs(next);
          }
        })
        .catch((err: unknown) => {
          if (!cancelled) {
            setError(err instanceof Error ? err.message : "Could not load jobs");
          }
        });
    };
    tick();
    const timer = window.setInterval(tick, 1500);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [packId]);

  const latest = jobs[0] ?? null;
  const running = latest?.status === "queued" || latest?.status === "running";
  const estimate = planEstimate(targetSec);
  const showEditor = mode === "advanced" || planned;

  function updateBible(patch: Partial<LookBible>) {
    setDraft((current) => ({
      ...current,
      look_bible: { ...current.look_bible, ...patch },
    }));
  }

  function updateCamera(index: number, patch: Partial<CameraCard>) {
    setDraft((current) => ({
      ...current,
      shots: current.shots.map((shot, shotIndex) =>
        shotIndex === index ? { ...shot, camera: { ...shot.camera, ...patch } } : shot,
      ),
    }));
  }

  function updateBeatSummary(index: number, summary: string) {
    setDraft((current) => ({
      ...current,
      beat_map: current.beat_map.map((beat, beatIndex) =>
        beatIndex === index ? { ...beat, summary } : beat,
      ),
    }));
  }

  function updateShot(index: number, patch: Partial<ShotDraft>) {
    setDraft((current) => ({
      ...current,
      shots: current.shots.map((shot, shotIndex) =>
        shotIndex === index ? { ...shot, ...patch } : shot,
      ),
    }));
  }

  function addShot() {
    setDraft((current) => {
      const previous = current.shots[current.shots.length - 1];
      return {
        ...current,
        shots: [...current.shots, emptyShot(nextShotId(current.shots), previous?.end_state ?? "")],
      };
    });
  }

  function removeShot(index: number) {
    setDraft((current) => ({
      ...current,
      shots: current.shots.filter((_, shotIndex) => shotIndex !== index),
    }));
  }

  function lockContinuity(index: number) {
    setDraft((current) => {
      const shots = current.shots.map((shot) => ({ ...shot }));
      const next = shots[index + 1];
      if (next) {
        next.start_state = shots[index].end_state;
      }
      return { ...current, shots };
    });
  }

  function adoptPack(pack: PackDraft): PackDraft {
    return {
      ...pack,
      style_preset: pack.style_preset ?? "",
      beat_map: (pack.beat_map ?? []).map((beat) => ({
        role: beat.role ?? "",
        summary: beat.summary ?? "",
      })),
      look_bible: { ...emptyLookBible(), ...(pack.look_bible ?? {}) },
      cast: pack.cast ?? [],
      staging: pack.staging ?? null,
      lock_staging: pack.lock_staging !== false,
      music_path: pack.music_path ?? "",
      shots: pack.shots.map((shot) => ({
        ...emptyShot(shot.id),
        ...shot,
        camera: { ...emptyCamera(), ...(shot.camera ?? {}) },
        stage: shot.stage ?? null,
      })),
    };
  }

  async function fillFromDescription(pack: PackDraft): Promise<PackDraft> {
    const filled = adoptPack(await fillPack(payload(pack)));
    setDraft(filled);
    return filled;
  }

  function selectMode(next: Mode) {
    setMode(next);
    if (next === "simple" && draft.shots.some((shot) => shot.prompt_still.trim())) {
      setPlanned(true);
    }
  }

  async function onPlan() {
    const prompt = briefPrompt.trim();
    if (!prompt) {
      setError("Add a story prompt before planning.");
      return;
    }
    if (!planEstimate(targetSec)) {
      setError(`Length must be between ${MIN_PLAN_TARGET_SEC} and ${MAX_PLAN_TARGET_SEC} seconds.`);
      return;
    }
    setActivity("plan");
    setError(null);
    try {
      const title = briefTitle.trim();
      const filled = await planPack({
        prompt,
        target_duration_sec: targetSec,
        aspect_ratio: draft.aspect_ratio,
        resolution: draft.resolution,
        ...(title ? { title } : {}),
        ...(briefStyle ? { style_preset: briefStyle } : {}),
        ...(draft.cast.length ? { cast: draft.cast } : {}),
        ...(draft.staging ? { staging: draft.staging } : {}),
        lock_staging: draft.lock_staging,
        ...(draft.music_path ? { music_path: draft.music_path } : {}),
      });
      setDraft(adoptPack(filled));
      setPlanned(true);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Could not plan the pack");
    } finally {
      setActivity(null);
    }
  }

  async function onFill() {
    if (!canDescribe(draft)) {
      setError("Add a title or logline before filling blanks.");
      return;
    }
    setActivity("fill");
    setError(null);
    try {
      await fillFromDescription(draft);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Could not fill blanks");
    } finally {
      setActivity(null);
    }
  }

  async function onReview() {
    setActivity("review");
    setError(null);
    const problem = validate(draft);
    if (problem) {
      setError(problem);
      setReview(null);
      setActivity(null);
      return;
    }
    try {
      setReview(await preflightPack(payload(draft)));
    } catch (err: unknown) {
      setReview(null);
      setError(err instanceof Error ? err.message : "Could not review the pack");
    } finally {
      setActivity(null);
    }
  }

  async function onRun() {
    setActivity("run");
    setError(null);
    try {
      let current = draft;
      if (canDescribe(current) && hasBlanks(current)) {
        current = await fillFromDescription(current);
      }
      const problem = validate(current);
      if (problem) {
        setError(problem);
        return;
      }
      const created = await createPack(payload(current));
      setPackId(created.id);
      setJobs([]);
      await runPack(created.id);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Run failed");
    } finally {
      setActivity(null);
    }
  }

  async function onDownload() {
    if (!packId) {
      return;
    }
    setError(null);
    try {
      await downloadEpisode(packId);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Download failed");
    }
  }

  async function onAddCast() {
    const name = castName.trim();
    if (!name) {
      setError("Cast needs a name.");
      return;
    }
    if (!castFile) {
      setError("Cast needs a PNG, JPEG, or WebP image.");
      return;
    }
    setError(null);
    try {
      const entry = await uploadReference(castFile, name, castRole, castMarkers.trim());
      const preview = URL.createObjectURL(castFile);
      setCastPreviews((current) => ({ ...current, [entry.id]: preview }));
      setDraft((current) => ({ ...current, cast: [...current.cast, entry] }));
      setCastName("");
      setCastMarkers("");
      setCastFile(null);
      if (castFileInput.current) {
        castFileInput.current.value = "";
      }
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Could not upload the reference");
    }
  }

  function removeCast(id: string) {
    setDraft((current) => ({ ...current, cast: current.cast.filter((item) => item.id !== id) }));
    setCastPreviews((current) => {
      const next = { ...current };
      const url = next[id];
      if (url) {
        URL.revokeObjectURL(url);
      }
      delete next[id];
      return next;
    });
  }

  async function onMusicFile(file: File | null) {
    if (!file) {
      return;
    }
    setError(null);
    try {
      if (packId && latest && latest.status === "done" && latest.stitched_episode) {
        const saved = await attachMusic(packId, file);
        setDraft((current) => ({ ...current, music_path: saved.music_path }));
        setMusicLabel(file.name);
        const jobs = await fetchJobs(packId);
        setJobs(jobs);
        return;
      }
      const saved = await uploadMusic(file);
      setDraft((current) => ({ ...current, music_path: saved.music_path }));
      setMusicLabel(file.name);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Could not upload the music bed");
    }
  }

  async function onRevise(shotId: string, action: "regenerate" | "edit" | "extend") {
    if (!packId || !latest) {
      return;
    }
    const prompt = (revisePrompt[shotId] ?? "").trim();
    if (action !== "regenerate" && !prompt) {
      setReviseStatus((current) => ({ ...current, [shotId]: "Add a prompt first." }));
      return;
    }
    setReviseStatus((current) => ({ ...current, [shotId]: `${action}…` }));
    setError(null);
    try {
      let job: Job;
      if (action === "regenerate") {
        job = await regenerateShot(packId, latest.id, shotId, "", prompt);
      } else if (action === "edit") {
        job = await editShot(packId, latest.id, shotId, prompt);
      } else {
        job = await extendShot(packId, latest.id, shotId, prompt, extendSec[shotId] ?? 4);
      }
      setJobs((current) => [job, ...current.filter((item) => item.id !== job.id)]);
      setReviseStatus((current) => ({
        ...current,
        [shotId]: job.status === "error" ? job.error || job.message || "Failed" : job.status,
      }));
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : "Revise failed";
      setReviseStatus((current) => ({ ...current, [shotId]: message }));
      setError(message);
    }
  }

  return (
    <main className="page">
      <header className="masthead">
        <div>
          <p className="eyebrow">SMF Works</p>
          <h1>Omarchy Grok Imagine</h1>
          <p className="lede">
            Describe the story and how long it should run. Plan writes the shots. Imagine makes the
            stills and clips. ffmpeg stitches the episode.
          </p>
        </div>
      </header>

      <div className="modes" role="tablist" aria-label="Pack editor mode">
        <button
          type="button"
          className={mode === "simple" ? "active" : ""}
          onClick={() => selectMode("simple")}
        >
          Simple
        </button>
        <button
          type="button"
          className={mode === "advanced" ? "active" : ""}
          onClick={() => selectMode("advanced")}
        >
          Advanced
        </button>
      </div>

      {healthError ? <p className="banner error">{healthError}</p> : null}
      {health ? (
        <p className="banner">
          {health.imagine_configured ? (
            <>
              <strong>Imagine key is set.</strong> Plan calls the text model. A run calls{" "}
              {health.image_model} and {health.video_model}.
            </>
          ) : (
            <>
              <strong>No XAI_API_KEY.</strong> Plan stays on this machine. Run is a dry-run. Status
              stays <code>stub</code> and every gate stays false.
            </>
          )}{" "}
          Continuity: {health.continuity === "last_frame_edit" ? "last-frame edit" : "prose regenerate"}.
        </p>
      ) : null}
      {error ? <p className="banner error">{error}</p> : null}

      {mode === "simple" ? (
        <section className="panel">
          <h2>Director brief</h2>
          <p className="note">
            One story and an overall length. Plan expands that into shots you can edit. Planning is
            text only. Honesty gates stay false until Imagine runs.
          </p>
          <label className="field">
            <span>Story</span>
            <textarea
              id="story-prompt"
              className="story"
              value={briefPrompt}
              onChange={(event) => setBriefPrompt(event.target.value)}
              placeholder="A fisher leaves the dock as the fog lifts, and the morning opens onto calm water."
            />
          </label>
          <label className="field">
            <span>Title (optional)</span>
            <input
              value={briefTitle}
              onChange={(event) => setBriefTitle(event.target.value)}
              placeholder="Harbor dawn"
            />
          </label>
          <label className="field">
            <span>Style (optional)</span>
            <select value={briefStyle} onChange={(event) => setBriefStyle(event.target.value)}>
              <option value="">Infer from the story</option>
              {STYLE_PRESETS.map((preset) => (
                <option key={preset} value={preset}>
                  {tokenLabel(preset)}
                </option>
              ))}
            </select>
          </label>
          <label className="field">
            <span>Length (seconds)</span>
            <input
              type="number"
              min={MIN_PLAN_TARGET_SEC}
              max={MAX_PLAN_TARGET_SEC}
              value={targetSec}
              onChange={(event) => setTargetSec(Number(event.target.value))}
            />
          </label>
          <div className="presets" aria-label="Length presets">
            {DURATION_PRESETS.map((seconds) => (
              <button
                key={seconds}
                type="button"
                className={targetSec === seconds ? "active" : ""}
                onClick={() => setTargetSec(seconds)}
              >
                {seconds}s
              </button>
            ))}
          </div>
          <div className="grid">
            <label className="field">
              <span>Aspect</span>
              <select
                value={draft.aspect_ratio}
                onChange={(event) => setDraft({ ...draft, aspect_ratio: event.target.value })}
              >
                {ASPECTS.map((aspect) => (
                  <option key={aspect}>{aspect}</option>
                ))}
              </select>
            </label>
            <label className="field">
              <span>Resolution</span>
              <select
                value={draft.resolution}
                onChange={(event) => setDraft({ ...draft, resolution: event.target.value })}
              >
                {RESOLUTIONS.map((resolution) => (
                  <option key={resolution}>{resolution}</option>
                ))}
              </select>
            </label>
          </div>
          {estimate ? (
            <p className="note">
              About {estimate.count} shots ({estimate.durations.join("s + ")}s). Planning is text
              only. Honesty gates stay false until Imagine runs.
            </p>
          ) : (
            <p className="note">
              Length must be between {MIN_PLAN_TARGET_SEC} and {MAX_PLAN_TARGET_SEC} seconds.
            </p>
          )}
          <div className="actions">
            <button
              type="button"
              className="primary"
              onClick={() => void onPlan()}
              disabled={busy || running || !estimate}
            >
              {activity === "plan" ? "Planning…" : "Plan"}
            </button>
          </div>
        </section>
      ) : null}

      {showEditor ? (
      <section className="panel">
        <h2>{mode === "simple" ? "Review" : "Pack"}</h2>
        {mode === "simple" ? (
          <p className="note">
            {draft.shots.length} shots, {draft.shots.reduce((sum, shot) => sum + shot.duration_sec, 0)}{" "}
            seconds. Edit anything, then Review & run. This draft has no media yet. Honesty gates stay false
            until Imagine runs.
          </p>
        ) : null}
        <label className="field">
          <span>Title</span>
          <input
            value={draft.title}
            onChange={(event) => setDraft({ ...draft, title: event.target.value })}
          />
        </label>
        <label className="field">
          <span>Logline</span>
          <input
            value={draft.logline}
            onChange={(event) => setDraft({ ...draft, logline: event.target.value })}
          />
        </label>
        <div className="grid">
          <label className="field">
            <span>Aspect</span>
            <select
              value={draft.aspect_ratio}
              onChange={(event) => setDraft({ ...draft, aspect_ratio: event.target.value })}
            >
              {ASPECTS.map((aspect) => (
                <option key={aspect}>{aspect}</option>
              ))}
            </select>
          </label>
          <label className="field">
            <span>Resolution</span>
            <select
              value={draft.resolution}
              onChange={(event) => setDraft({ ...draft, resolution: event.target.value })}
            >
              {RESOLUTIONS.map((resolution) => (
                <option key={resolution}>{resolution}</option>
              ))}
            </select>
          </label>
        </div>
      </section>
      ) : null}

      <section className="panel" data-testid="cast-panel">
        <h2>Cast</h2>
        <p className="note">
          Upload a local reference for a character, prop, or location. The file stays in the
          data directory. Stills use up to three of these images. Reference-to-video can use
          them too, at 720p.
        </p>
        <div className="grid">
          <label className="field">
            <span>Name</span>
            <input value={castName} onChange={(event) => setCastName(event.target.value)} />
          </label>
          <label className="field">
            <span>Role</span>
            <select value={castRole} onChange={(event) => setCastRole(event.target.value)}>
              {CAST_ROLES.map((role) => (
                <option key={role} value={role}>
                  {tokenLabel(role)}
                </option>
              ))}
            </select>
          </label>
        </div>
        <label className="field">
          <span>Identity markers</span>
          <input
            value={castMarkers}
            onChange={(event) => setCastMarkers(event.target.value)}
            placeholder="Same face, grey coat, scar on the left brow"
          />
        </label>
        <label className="field">
          <span>Reference image</span>
          <input
            ref={castFileInput}
            type="file"
            accept="image/png,image/jpeg,image/webp"
            onChange={(event) => setCastFile(event.target.files?.[0] ?? null)}
          />
        </label>
        <div className="actions">
          <button type="button" onClick={() => void onAddCast()} disabled={busy}>
            Add reference
          </button>
        </div>
        <div className="cast-list">
          {draft.cast.map((item) => (
            <article className="cast-card" key={item.id}>
              <CastThumb item={item} preview={castPreviews[item.id]} />
              <div>
                <strong>{item.name}</strong>
                <p className="note">
                  {tokenLabel(item.role)}
                  {item.markers ? ` · ${item.markers}` : ""}
                </p>
                <button type="button" onClick={() => removeCast(item.id)}>
                  Remove
                </button>
              </div>
            </article>
          ))}
        </div>
        <label className="field">
          <span>Music bed</span>
          <input
            type="file"
            accept="audio/mpeg,audio/wav,audio/mp4,audio/ogg,.mp3,.wav,.m4a,.ogg"
            onChange={(event) => void onMusicFile(event.target.files?.[0] ?? null)}
          />
        </label>
        {draft.music_path ? (
          <p className="note">Music file stored{musicLabel ? `: ${musicLabel}` : ""}. It is mixed under the episode. Nothing is generated or downloaded.</p>
        ) : (
          <p className="note">Optional. A local audio file is mixed under the episode at a low level with a fade out.</p>
        )}
      </section>

      {showEditor ? (
      <section className="panel">
        <h2>Look bible</h2>
        <p className="note">
          Locked for the whole pack. Every still and every motion prompt repeats these lines.
          Plan and Fill blanks write them. A moderation retry softens the shot text and keeps
          this block.
        </p>
        <label className="field">
          <span>Cast</span>
          <textarea
            value={draft.look_bible.cast}
            onChange={(event) => updateBible({ cast: event.target.value })}
          />
        </label>
        <label className="field">
          <span>Wardrobe</span>
          <textarea
            value={draft.look_bible.wardrobe}
            onChange={(event) => updateBible({ wardrobe: event.target.value })}
          />
        </label>
        <label className="field">
          <span>Palette</span>
          <textarea
            value={draft.look_bible.palette}
            onChange={(event) => updateBible({ palette: event.target.value })}
          />
        </label>
        <label className="field">
          <span>Lighting</span>
          <textarea
            value={draft.look_bible.lighting}
            onChange={(event) => updateBible({ lighting: event.target.value })}
          />
        </label>
        <label className="field">
          <span>Camera</span>
          <textarea
            value={draft.look_bible.camera}
            onChange={(event) => updateBible({ camera: event.target.value })}
          />
        </label>
      </section>
      ) : null}

      {showEditor && (draft.beat_map.length > 0 || draft.style_preset) ? (
      <section className="panel">
        <h2>Story beats</h2>
        <p className="note">
          Setup, turn, climax, and button. The camera card on each shot is the scale, the angle,
          and one move. Exit frame is the picture the next shot should open on. Edit the cards
          before Run. Planning does not create media, and honesty gates stay false until Imagine
          runs.
        </p>
        <label className="field">
          <span>Style preset</span>
          <select
            value={draft.style_preset}
            onChange={(event) => setDraft({ ...draft, style_preset: event.target.value })}
          >
            <option value="">Not set</option>
            {STYLE_PRESETS.map((preset) => (
              <option key={preset} value={preset}>
                {tokenLabel(preset)}
              </option>
            ))}
          </select>
        </label>
        {draft.beat_map.map((beat, index) => (
          <div className="beat-row" key={`${beat.role}-${index}`}>
            <span className="chip">{tokenLabel(beat.role)}</span>
            <label className="field">
              <span className="sr-only">{tokenLabel(beat.role)} summary</span>
              <textarea
                value={beat.summary}
                onChange={(event) => updateBeatSummary(index, event.target.value)}
              />
            </label>
          </div>
        ))}
      </section>
      ) : null}

      {showEditor ? (
      <section className="panel">
        <h2>Shots</h2>
        <label className="lock-staging">
          <input
            type="checkbox"
            data-testid="lock-staging"
            checked={draft.lock_staging}
            onChange={(event) => setDraft({ ...draft, lock_staging: event.target.checked })}
          />
          Lock staging
        </label>
        <p className="note">
          When both are set, a shot&apos;s start state must match the previous shot&apos;s end state.
          Duration is 1–15 seconds. Default is 8.
          {mode === "advanced"
            ? " Fill blanks writes empty still prompts, motion prompts, and a locked start/end chain from the title and logline. Text you already typed stays. Review & run shows the prompts and the call count. Confirm fills any field that is still blank, then creates the pack and starts the job."
            : " Review & run shows the prompts and the call count. Confirm fills any field that is still blank, then creates the pack and starts the job."}
        </p>
        {draft.shots.map((shot, index) => (
          <article className="shot" key={`${shot.id}-${index}`}>
            <div className="shot-head">
              <strong>Shot {index + 1}</strong>
              <div className="row-actions">
                {index < draft.shots.length - 1 ? (
                  <button type="button" onClick={() => lockContinuity(index)}>
                    Copy end state forward
                  </button>
                ) : null}
                <button
                  type="button"
                  onClick={() => removeShot(index)}
                  disabled={draft.shots.length === 1}
                >
                  Remove
                </button>
              </div>
            </div>
            <p className="craft-line">
              <span className="chip">{tokenLabel(shot.beat)}</span>
              <span className="chip">{tokenLabel(shot.camera.scale)}</span>
              <span className="chip">{tokenLabel(shot.camera.angle)}</span>
              <span className="chip">{tokenLabel(shot.camera.move)}</span>
            </p>
            <StageStrip pack={draft} shot={shot} />
            <div className="grid">
              <label className="field">
                <span>Beat</span>
                <select
                  value={shot.beat}
                  onChange={(event) => updateShot(index, { beat: event.target.value })}
                >
                  <option value="">Not set</option>
                  {BEAT_ROLES.map((role) => (
                    <option key={role} value={role}>
                      {tokenLabel(role)}
                    </option>
                  ))}
                </select>
              </label>
              <label className="field">
                <span>Scale</span>
                <select
                  value={shot.camera.scale}
                  onChange={(event) => updateCamera(index, { scale: event.target.value })}
                >
                  <option value="">Not set</option>
                  {CAMERA_SCALES.map((scale) => (
                    <option key={scale} value={scale}>
                      {tokenLabel(scale)}
                    </option>
                  ))}
                </select>
              </label>
            </div>
            <div className="grid">
              <label className="field">
                <span>Angle</span>
                <select
                  value={shot.camera.angle}
                  onChange={(event) => updateCamera(index, { angle: event.target.value })}
                >
                  <option value="">Not set</option>
                  {CAMERA_ANGLES.map((angle) => (
                    <option key={angle} value={angle}>
                      {tokenLabel(angle)}
                    </option>
                  ))}
                </select>
              </label>
              <label className="field">
                <span>Move</span>
                <select
                  value={shot.camera.move}
                  onChange={(event) => updateCamera(index, { move: event.target.value })}
                >
                  <option value="">Not set</option>
                  {CAMERA_MOVES.map((move) => (
                    <option key={move} value={move}>
                      {tokenLabel(move)}
                    </option>
                  ))}
                </select>
              </label>
            </div>
            <label className="field">
              <span>Exit frame</span>
              <textarea
                value={shot.camera.exit_frame}
                onChange={(event) => updateCamera(index, { exit_frame: event.target.value })}
              />
            </label>
            <div className="grid">
              <label className="field">
                <span>Id</span>
                <input value={shot.id} onChange={(event) => updateShot(index, { id: event.target.value })} />
              </label>
              <label className="field">
                <span>Duration (seconds)</span>
                <input
                  type="number"
                  min={1}
                  max={15}
                  value={shot.duration_sec}
                  onChange={(event) =>
                    updateShot(index, { duration_sec: Number(event.target.value) })
                  }
                />
              </label>
            </div>
            <div className="grid">
              <label className="field">
                <span>Video mode</span>
                <select
                  value={shot.video_mode}
                  onChange={(event) => updateShot(index, { video_mode: event.target.value })}
                >
                  {VIDEO_MODES.map((mode) => (
                    <option key={mode} value={mode}>
                      {tokenLabel(mode)}
                    </option>
                  ))}
                </select>
              </label>
              <label className="field">
                <span>Voice id</span>
                <input
                  value={shot.voice_id}
                  disabled={shot.video_mode !== "reference_to_video"}
                  placeholder={shot.video_mode === "reference_to_video" ? "eve" : "Reference-to-video only"}
                  onChange={(event) => updateShot(index, { voice_id: event.target.value })}
                />
              </label>
            </div>
            <label className="field">
              <span>Dialogue</span>
              <input
                value={shot.dialogue}
                onChange={(event) => updateShot(index, { dialogue: event.target.value })}
                placeholder="Optional spoken line, written into the motion prompt"
              />
            </label>
            <label className="field">
              <span>Still prompt</span>
              <textarea
                value={shot.prompt_still}
                onChange={(event) => updateShot(index, { prompt_still: event.target.value })}
              />
            </label>
            <label className="field">
              <span>Motion prompt</span>
              <textarea
                value={shot.prompt_motion}
                onChange={(event) => updateShot(index, { prompt_motion: event.target.value })}
              />
            </label>
            <label className="field">
              <span>Start state</span>
              <textarea
                value={shot.start_state}
                onChange={(event) => updateShot(index, { start_state: event.target.value })}
              />
            </label>
            <label className="field">
              <span>End state</span>
              <textarea
                value={shot.end_state}
                onChange={(event) => updateShot(index, { end_state: event.target.value })}
              />
            </label>
          </article>
        ))}
        <div className="actions">
          <button type="button" onClick={addShot} disabled={busy}>
            Add shot
          </button>
          {mode === "advanced" ? (
            <>
              <button type="button" onClick={() => setDraft(examplePack)} disabled={busy}>
                Load example
              </button>
              <button type="button" onClick={() => void onFill()} disabled={busy || running}>
                Fill blanks from logline
              </button>
            </>
          ) : null}
          <button
            type="button"
            className="primary"
            onClick={() => void onReview()}
            disabled={busy || running}
          >
            {activity === "review" ? "Reviewing…" : "Review & run"}
          </button>
        </div>
      </section>
      ) : null}

      {showEditor && review ? (
        <section className="panel" id="preflight-panel" ref={reviewRef}>
          <h2>Review & run</h2>
          <p className="totals">{totalsLine(review.totals)}</p>
          <p className="note">
            These are the prompts a run will send, and how many Imagine calls that takes. No Imagine
            call has been made.
          </p>
          {review.shots.map((shot) => (
            <article className="shot" key={shot.id}>
              <div className="shot-head">
                <strong>{shot.id}</strong>
                <span>
                  still {shot.still_mode_expected} · video {shot.video_mode}
                </span>
              </div>
              {shot.issues.length === 0 ? <p className="note">No issues.</p> : null}
              {shot.issues.map((issue, issueIndex) => (
                <p className={`issue ${issue.severity}`} key={`${shot.id}-${issue.code}-${issueIndex}`}>
                  <strong>{issue.severity}</strong> {issue.code}: {issue.message}
                </p>
              ))}
              <details>
                <summary>Still prompt</summary>
                <pre className="prompt-block">{shot.still_prompt}</pre>
              </details>
              <details>
                <summary>Motion prompt</summary>
                <pre className="prompt-block">{shot.motion_prompt}</pre>
              </details>
            </article>
          ))}
          <div className="actions">
            <button
              type="button"
              className="primary"
              onClick={() => void onRun()}
              disabled={busy || running || review.blocking}
            >
              {activity === "run" ? "Sending…" : "Confirm run"}
            </button>
            <button type="button" onClick={() => setReview(null)} disabled={busy}>
              Close
            </button>
          </div>
          {review.blocking ? (
            <p className="note">A blocking issue has to be fixed before this run.</p>
          ) : null}
        </section>
      ) : null}

      <section className="panel">
        <h2>Job</h2>
        {!latest ? <p className="note">No run yet. Gates stay blank until a job exists.</p> : null}
        {latest ? (
          <>
            <p className="note">
              Status <span className="status">{latest.status}</span>
              {` · continuity ${latest.continuity_mode ?? "not set"}`}
              {` · grade match ${latest.grade_match ? "ran" : "did not run"}`}
              {` · audio ${latest.has_audio ? "yes" : "no"}`}
              {` · music bed ${latest.music_bed_applied ? "applied" : "not applied"}`}
              {packId ? ` · pack ${packId}` : ""}
            </p>
            {latest.message ? <p className="note">{latest.message}</p> : null}
            {latest.error ? <p className="banner error">{latest.error}</p> : null}
            <div className="gates">
              <div className="gate">
                <span>Grade match</span>
                <b className={latest.grade_match ? "yes" : "no"}>
                  {latest.grade_match ? "ran" : "did not run"}
                </b>
              </div>
              {GATES.map(([key, label]) => {
                const value = latest[key];
                return (
                  <div className="gate" key={key}>
                    <span>{label}</span>
                    <b className={value ? "yes" : "no"}>{value ? "true" : "false"}</b>
                  </div>
                );
              })}
            </div>
            {latest.shots.map((shot) => (
              <div className="shot-status" key={shot.id}>
                <strong>{shot.id}</strong>
                <span>still {shot.called_imagine_still ? "called" : "not called"}</span>
                <span>file {shot.produced_still ? "yes" : "no"}</span>
                <span>video {shot.called_imagine_video ? "called" : "not called"}</span>
                <span>clip {shot.produced_mp4 ? "yes" : "no"}</span>
                <span>still mode {shot.still_mode ?? "not set"}</span>
                <span>video mode {shot.video_mode ?? "not set"}</span>
                {shot.note ? <span>{shot.note}</span> : null}
                {shot.still_path ? <code>{shot.still_path}</code> : null}
                {shot.clip_path ? <code>{shot.clip_path}</code> : null}
                {shot.error ? <span>{shot.error}</span> : null}
                {shot.moderation && shot.moderation.retry_count > 0 ? (
                  <div className="rewrite">
                    <strong>Moderation retry {shot.moderation.retry_count}</strong>
                    <p>Still was: {shot.moderation.original_prompt_still}</p>
                    <p>Still sent: {shot.moderation.softened_prompt_still}</p>
                    <p>Motion was: {shot.moderation.original_prompt_motion}</p>
                    <p>Motion sent: {shot.moderation.softened_prompt_motion}</p>
                  </div>
                ) : null}
                {latest.status === "done" || latest.status === "error" ? (
                  <div className="revise" data-testid={`revise-${shot.id}`}>
                    <label className="field">
                      <span>Revise prompt</span>
                      <textarea
                        value={revisePrompt[shot.id] ?? ""}
                        onChange={(event) =>
                          setRevisePrompt((current) => ({ ...current, [shot.id]: event.target.value }))
                        }
                      />
                    </label>
                    <label className="field">
                      <span>Extend seconds (2–10)</span>
                      <input
                        type="number"
                        min={2}
                        max={10}
                        value={extendSec[shot.id] ?? 4}
                        onChange={(event) =>
                          setExtendSec((current) => ({
                            ...current,
                            [shot.id]: Number(event.target.value),
                          }))
                        }
                      />
                    </label>
                    <div className="actions">
                      <button
                        type="button"
                        data-testid={`revise-regenerate-${shot.id}`}
                        onClick={() => void onRevise(shot.id, "regenerate")}
                      >
                        Regenerate
                      </button>
                      <button
                        type="button"
                        data-testid={`revise-edit-${shot.id}`}
                        onClick={() => void onRevise(shot.id, "edit")}
                      >
                        Edit
                      </button>
                      <button
                        type="button"
                        data-testid={`revise-extend-${shot.id}`}
                        onClick={() => void onRevise(shot.id, "extend")}
                      >
                        Extend
                      </button>
                    </div>
                    {reviseStatus[shot.id] ? <p className="note">{reviseStatus[shot.id]}</p> : null}
                    {shot.revisions?.length ? (
                      <ul className="versions">
                        {shot.revisions.map((revision) => (
                          <li key={`${shot.id}-${revision.version}`}>
                            v{revision.version} {revision.action}
                            {revision.clip_path ? ` · ${revision.clip_path}` : ""}
                          </li>
                        ))}
                      </ul>
                    ) : null}
                  </div>
                ) : null}
              </div>
            ))}
            <div className="actions" style={{ marginTop: 12 }}>
              <button type="button" onClick={() => void onDownload()} disabled={!latest.stitched_episode}>
                Download episode.mp4
              </button>
            </div>
          </>
        ) : null}
      </section>
      <footer>Local API on port 8010. Wizard on port 5180. Imagine is an xAI product.</footer>
    </main>
  );
}
