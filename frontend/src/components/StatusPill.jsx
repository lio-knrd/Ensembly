import { stageLabel, stageTone } from "../stages.js";

export default function StatusPill({ stage }) {
  return (
    <span className={`pill ${stageTone(stage)}`}>
      <span className="dot" />
      {stageLabel(stage)}
    </span>
  );
}
