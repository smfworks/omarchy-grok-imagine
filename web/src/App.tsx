import { useEffect, useState } from "react";
import { createPack, downloadEpisode, fetchHealth, fetchJobs, runPack } from "./api";
import { examplePack } from "./example";
import { ASPECTS, GATES, RESOLUTIONS, type Health, type Job, type PackDraft, type ShotDraft } from "./types";

function emptyShot(id: string, startState = ""): ShotDraft {
  return {
    id,
    prompt_still: "",
    prompt_motion: "",
    duration_sec: 8,
    end_state: "",
    start_state: startState,
  };
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
    shots: pack.shots.map((shot) => ({
      id: shot.id.trim(),
      prompt_still: shot.prompt_still.trim(),
      prompt_motion: shot.prompt_motion.trim(),
      duration_sec: shot.duration_sec,
      end_state: shot.end_state.trim(),
      start_state: shot.start_state.trim(),
    })),
  };
}

export function App() {
  const [draft, setDraft] = useState<PackDraft>({
    title: "",
    logline: "",
    aspect_ratio: "16:9",
    resolution: "720p",
    shots: [emptyShot("s01")],
  });
  const [health, setHealth] = useState<Health | null>(null);
  const [healthError, setHealthError] = useState<string | null>(null);
  const [packId, setPackId] = useState<string | null>(null);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

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

  async function onRun() {
    const problem = validate(draft);
    if (problem) {
      setError(problem);
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const created = await createPack(payload(draft));
      setPackId(created.id);
      setJobs([]);
      await runPack(created.id);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Run failed");
    } finally {
      setBusy(false);
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

  return (
    <main className="page">
      <header className="masthead">
        <div>
          <p className="eyebrow">SMF Works</p>
          <h1>Omarchy Grok Imagine</h1>
          <p className="lede">
            Write a short pack. Imagine makes the stills and clips. ffmpeg stitches the episode.
          </p>
        </div>
      </header>

      {healthError ? <p className="banner error">{healthError}</p> : null}
      {health ? (
        <p className="banner">
          {health.imagine_configured ? (
            <>
              <strong>Imagine key is set.</strong> A run will call {health.image_model} and{" "}
              {health.video_model}.
            </>
          ) : (
            <>
              <strong>No XAI_API_KEY.</strong> Run is a dry-run. Status stays <code>stub</code> and
              every gate stays false.
            </>
          )}{" "}
          Continuity: {health.continuity === "last_frame_edit" ? "last-frame edit" : "prose regenerate"}.
        </p>
      ) : null}
      {error ? <p className="banner error">{error}</p> : null}

      <section className="panel">
        <h2>Pack</h2>
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

      <section className="panel">
        <h2>Shots</h2>
        <p className="note">
          When both are set, a shot&apos;s start state must match the previous shot&apos;s end state.
          Duration is 1–15 seconds. Default is 8.
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
          <button type="button" onClick={addShot}>
            Add shot
          </button>
          <button type="button" onClick={() => setDraft(examplePack)}>
            Load example
          </button>
          <button type="button" className="primary" onClick={() => void onRun()} disabled={busy || running}>
            {busy ? "Sending…" : "Run"}
          </button>
        </div>
      </section>

      <section className="panel">
        <h2>Job</h2>
        {!latest ? <p className="note">No run yet. Gates stay blank until a job exists.</p> : null}
        {latest ? (
          <>
            <p className="note">
              Status <span className="status">{latest.status}</span>
              {latest.continuity_mode ? ` · ${latest.continuity_mode}` : ""}
              {packId ? ` · pack ${packId}` : ""}
            </p>
            {latest.message ? <p className="note">{latest.message}</p> : null}
            {latest.error ? <p className="banner error">{latest.error}</p> : null}
            <div className="gates">
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
              <p className="shot-status" key={shot.id}>
                <strong>{shot.id}</strong>
                <span>still {shot.called_imagine_still ? "called" : "not called"}</span>
                <span>file {shot.produced_still ? "yes" : "no"}</span>
                <span>video {shot.called_imagine_video ? "called" : "not called"}</span>
                <span>clip {shot.produced_mp4 ? "yes" : "no"}</span>
                {shot.still_mode ? <span>{shot.still_mode}</span> : null}
                {shot.still_path ? <code>{shot.still_path}</code> : null}
                {shot.clip_path ? <code>{shot.clip_path}</code> : null}
                {shot.error ? <span>{shot.error}</span> : null}
              </p>
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
