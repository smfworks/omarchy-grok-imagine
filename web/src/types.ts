export type ShotDraft = {
  id: string;
  prompt_still: string;
  prompt_motion: string;
  duration_sec: number;
  end_state: string;
  start_state: string;
};

export type PackDraft = {
  title: string;
  logline: string;
  aspect_ratio: string;
  resolution: string;
  shots: ShotDraft[];
};

export type Health = {
  ok: boolean;
  imagine_configured: boolean;
  ffmpeg: boolean;
  image_model: string;
  video_model: string;
  continuity: "last_frame_edit" | "prose_regenerate";
};

export type ModerationNote = {
  retry_count: number;
  original_prompt_still: string;
  original_prompt_motion: string;
  softened_prompt_still: string;
  softened_prompt_motion: string;
};

export type ShotStatus = {
  id: string;
  called_imagine_still: boolean;
  produced_still: boolean;
  called_imagine_video: boolean;
  produced_mp4: boolean;
  still_path: string | null;
  clip_path: string | null;
  last_frame_path: string | null;
  still_mode: string | null;
  video_request_id: string | null;
  error: string | null;
  moderation: ModerationNote | null;
};

export type Job = {
  id: string;
  pack_id: string;
  status: string;
  message: string | null;
  error: string | null;
  continuity_mode: string | null;
  called_imagine_still: boolean;
  produced_still: boolean;
  called_imagine_video: boolean;
  produced_mp4: boolean;
  stitched_episode: boolean;
  episode_path: string | null;
  shots: ShotStatus[];
  created_at: string;
  updated_at: string;
};

export const ASPECTS = ["16:9", "9:16", "1:1", "4:3", "3:4", "3:2", "2:3"] as const;
export const RESOLUTIONS = ["480p", "720p", "1080p"] as const;

export const GATES = [
  ["called_imagine_still", "Called Imagine still"],
  ["produced_still", "Produced still"],
  ["called_imagine_video", "Called Imagine video"],
  ["produced_mp4", "Produced clip"],
  ["stitched_episode", "Stitched episode"],
] as const;
