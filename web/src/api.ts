import type { Health, Job, PackDraft } from "./types";

const API_BASE = import.meta.env.VITE_API_BASE || "http://127.0.0.1:8010";
const TOKEN = import.meta.env.VITE_API_TOKEN || "local-dev-token";

type ErrorBody = {
  detail?: unknown;
};

function formatDetail(detail: unknown): string {
  if (typeof detail === "string") {
    return detail;
  }
  if (Array.isArray(detail)) {
    return detail
      .map((item) => {
        if (item && typeof item === "object" && "msg" in item) {
          const record = item as { msg?: unknown; loc?: unknown };
          const loc = Array.isArray(record.loc)
            ? record.loc.filter((part) => part !== "body").join(".")
            : "";
          const msg = typeof record.msg === "string" ? record.msg : "Invalid value";
          return loc ? `${loc}: ${msg}` : msg;
        }
        return JSON.stringify(item);
      })
      .join("\n");
  }
  return JSON.stringify(detail);
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers);
  headers.set("Authorization", `Bearer ${TOKEN}`);
  if (init?.body) {
    headers.set("Content-Type", "application/json");
  }
  const response = await fetch(`${API_BASE}${path}`, { ...init, headers });
  if (!response.ok) {
    let message = response.statusText || `HTTP ${response.status}`;
    try {
      const body = (await response.json()) as ErrorBody;
      if (body.detail !== undefined) {
        message = formatDetail(body.detail);
      }
    } catch {
      // The body was not JSON.
    }
    throw new Error(message);
  }
  return (await response.json()) as T;
}

export async function fetchHealth(): Promise<Health> {
  const response = await fetch(`${API_BASE}/api/health`);
  if (!response.ok) {
    throw new Error(`Health check failed (${response.status})`);
  }
  return (await response.json()) as Health;
}

export async function fillPack(pack: PackDraft): Promise<PackDraft> {
  return request("/api/packs/fill", { method: "POST", body: JSON.stringify(pack) });
}

export async function createPack(pack: PackDraft): Promise<{ id: string }> {
  return request("/api/packs", { method: "POST", body: JSON.stringify(pack) });
}

export async function runPack(packId: string): Promise<{ job_id: string; status: string }> {
  return request(`/api/packs/${packId}/run`, { method: "POST" });
}

export async function fetchJobs(packId: string): Promise<Job[]> {
  const payload = await request<{ jobs: Job[] }>(`/api/packs/${packId}/jobs`);
  return payload.jobs;
}

export async function downloadEpisode(packId: string): Promise<void> {
  const response = await fetch(`${API_BASE}/api/packs/${packId}/episode`, {
    headers: { Authorization: `Bearer ${TOKEN}` },
  });
  if (!response.ok) {
    let message = "Episode is not stitched.";
    try {
      const body = (await response.json()) as ErrorBody;
      if (typeof body.detail === "string") {
        message = body.detail;
      }
    } catch {
      // Keep the default message.
    }
    throw new Error(message);
  }
  const blob = await response.blob();
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = "episode.mp4";
  link.click();
  URL.revokeObjectURL(url);
}
