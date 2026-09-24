import type { PackDraft } from "./types";

export const examplePack: PackDraft = {
  title: "Harbor dawn",
  logline: "A fisher leaves the dock as the fog lifts.",
  aspect_ratio: "16:9",
  resolution: "720p",
  shots: [
    {
      id: "s01",
      prompt_still: "A wooden fishing boat at a quiet harbor, dawn fog, 35mm film still",
      prompt_motion: "The boat eases away from the dock, fog sliding past the lens",
      duration_sec: 8,
      end_state: "The boat is ten meters off the dock, bow pointed toward open water, fog still thick.",
      start_state: "",
    },
    {
      id: "s02",
      prompt_still: "The same fishing boat in open water, fog thinning, same hull",
      prompt_motion: "A slow push in as the fog thins and the bow rises on a swell",
      duration_sec: 8,
      start_state: "The boat is ten meters off the dock, bow pointed toward open water, fog still thick.",
      end_state: "The boat is in open water, fog lifted to the horizon, bow unchanged.",
    },
  ],
};
