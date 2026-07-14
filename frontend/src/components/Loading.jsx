import { Flame } from "lucide-react";

// Shared loading state used across every screen. `full` centers it in the
// available page height; `inline` renders a compact row inside panels.
export default function Loading({ label = "Loading", full = false, inline = false }) {
  const className =
    "loading" + (full ? " loading-full" : "") + (inline ? " loading-inline" : "");
  return (
    <div className={className} role="status" aria-live="polite">
      <span className="loading-mark">
        <Flame />
      </span>
      <span className="loading-label">{label}</span>
    </div>
  );
}
