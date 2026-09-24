export type LookBible = {
  cast: string;
  wardrobe: string;
  palette: string;
  lighting: string;
  camera: string;
};

export type CameraCard = {
  scale: string;
  angle: string;
  move: string;
  exit_frame: string;
};

export type StoryBeat = {
  role: string;
  summary: string;
};

export const emptyLookBible = (): LookBible => ({
  cast: "",
  wardrobe: "",
  palette: "",
  lighting: "",
  camera: "",
});

export const emptyCamera = (): CameraCard => ({
  scale: "",
  angle: "",
  move: "",
  exit_frame: "",
});

export type CastRef = {
  id: string;
  name: string;
  role: string;
  markers: string;
  image_path: string;
};

export type ShotDraft = {
  id: string;
  prompt_still: string;
  prompt_motion: string;
  duration_sec: number;
  end_state: string;
  start_state: string;
  beat: string;
  camera: CameraCard;
  video_mode: string;
  dialogue: string;
  voice_id: string;
};

export type PackDraft = {
  title: string;
  logline: string;
  aspect_ratio: string;
  resolution: string;
  look_bible: LookBible;
  style_preset: string;
  beat_map: StoryBeat[];
  cast: CastRef[];
  music_path: string;
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

export type ShotRevision = {
  version: number;
  action: string;
  clip_path: string | null;
  prompt: string;
  created_at: string;
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
  video_mode: string | null;
  video_request_id: string | null;
  note: string | null;
  error: string | null;
  moderation: ModerationNote | null;
  revisions: ShotRevision[];
};

export type Job = {
  id: string;
  pack_id: string;
  status: string;
  message: string | null;
  error: string | null;
  continuity_mode: string | null;
  grade_match: boolean;
  has_audio: boolean;
  music_bed_applied: boolean;
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
export const STYLE_PRESETS = ["generic", "action_duel", "quiet_drama", "trek"] as const;
export const BEAT_ROLES = ["setup", "turn", "climax", "button"] as const;
export const CAMERA_SCALES = ["wide", "medium", "close", "extreme_close"] as const;
export const CAMERA_ANGLES = ["eye", "low", "high", "ots", "dutch"] as const;
export const CAMERA_MOVES = [
  "static",
  "dolly_in",
  "dolly_out",
  "orbit",
  "pan",
  "tilt",
  "whip_pan",
  "handheld",
] as const;

export const CAST_ROLES = ["character", "prop", "location"] as const;
export const VIDEO_MODES = ["image_to_video", "reference_to_video"] as const;

export type PreflightIssue = {
  code: string;
  severity: "block" | "warn" | "info";
  message: string;
};

export type PreflightShot = {
  id: string;
  still_prompt: string;
  motion_prompt: string;
  still_mode_expected: string;
  video_mode: string;
  issues: PreflightIssue[];
};

export type PreflightResult = {
  shots: PreflightShot[];
  totals: {
    stills: number;
    videos: number;
    video_seconds: number;
  };
  blocking: boolean;
};

export const GATES = [
  ["called_imagine_still", "Called Imagine still"],
  ["produced_still", "Produced still"],
  ["called_imagine_video", "Called Imagine video"],
  ["produced_mp4", "Produced clip"],
  ["stitched_episode", "Stitched episode"],
] as const;
