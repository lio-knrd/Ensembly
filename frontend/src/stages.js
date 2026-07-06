// Pipeline stage metadata shared across screens.

// Kanban columns (board view) grouping the fine-grained pipeline stages.
export const COLUMNS = [
  { key: "IDEA", label: "Idea", stages: ["IDEA"] },
  {
    key: "SCRIPT",
    label: "Script",
    stages: ["SCRIPT_GENERATING", "SCRIPT_READY", "SCRIPT_APPROVED"],
  },
  { key: "AUDIO", label: "Audio", stages: ["AUDIO_GENERATING"] },
  { key: "CAST", label: "Cast", stages: ["CAST_REVIEW"] },
  {
    key: "STORYBOARD",
    label: "Storyboard",
    stages: ["STORYBOARD_GENERATING", "STORYBOARD_READY", "STORYBOARD_APPROVED"],
  },
  {
    key: "CLIPS",
    label: "Clips",
    stages: ["CLIPS_GENERATING", "CLIPS_READY", "CLIPS_APPROVED"],
  },
  { key: "RENDER", label: "Render", stages: ["RENDERING"] },
  { key: "DONE", label: "Done", stages: ["DONE"] },
];

export function columnForStage(stage) {
  if (stage === "FAILED") return "IDEA";
  const col = COLUMNS.find((c) => c.stages.includes(stage));
  return col ? col.key : "IDEA";
}

const LABELS = {
  IDEA: "Idea",
  SCRIPT_GENERATING: "Writing script…",
  SCRIPT_READY: "Script ready",
  SCRIPT_APPROVED: "Script approved",
  AUDIO_GENERATING: "Generating audio…",
  CAST_REVIEW: "Cast review",
  STORYBOARD_GENERATING: "Generating images…",
  STORYBOARD_READY: "Storyboard ready",
  STORYBOARD_APPROVED: "Storyboard approved",
  CLIPS_GENERATING: "Generating clips…",
  CLIPS_READY: "Clips ready",
  CLIPS_APPROVED: "Clips approved",
  RENDERING: "Rendering…",
  DONE: "Done",
  FAILED: "Failed",
};

export const stageLabel = (stage) => LABELS[stage] || stage;

// Pill visual tone by stage.
export function stageTone(stage) {
  if (stage === "FAILED") return "danger";
  if (stage === "DONE") return "success";
  if (stage.endsWith("_GENERATING") || stage === "RENDERING") return "active";
  if (stage.endsWith("_READY") || stage === "CAST_REVIEW") return "ready";
  return "muted";
}

export const isGenerating = (stage) =>
  stage.endsWith("_GENERATING") || stage === "RENDERING";
