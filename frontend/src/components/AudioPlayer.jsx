import { useEffect, useMemo, useRef, useState } from "react";
import { Pause, Play } from "lucide-react";

// Deterministic pseudo-waveform seeded by the source URL so a given
// track always renders the same shape.
function barsFor(seed, count) {
  let h = 2166136261;
  for (let i = 0; i < seed.length; i++) {
    h ^= seed.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  const bars = [];
  for (let i = 0; i < count; i++) {
    h ^= h << 13;
    h ^= h >>> 17;
    h ^= h << 5;
    const r = (h >>> 0) / 4294967295;
    const envelope = 0.5 + 0.5 * Math.sin((i / (count - 1)) * Math.PI);
    bars.push(0.2 + 0.8 * (0.35 + 0.65 * r) * envelope);
  }
  return bars;
}

function fmt(seconds) {
  if (!Number.isFinite(seconds)) return "0:00";
  const m = Math.floor(seconds / 60);
  const s = Math.floor(seconds % 60);
  return `${m}:${String(s).padStart(2, "0")}`;
}

// Waveform, transport and time readout as one visual unit, replacing the
// mismatched "decorative waveform above a native <audio>" combo.
export default function AudioPlayer({ src, compact = false }) {
  const audioRef = useRef(null);
  const waveRef = useRef(null);
  const [playing, setPlaying] = useState(false);
  const [progress, setProgress] = useState(0);
  const [duration, setDuration] = useState(0);
  const bars = useMemo(() => barsFor(src || "", compact ? 32 : 48), [src, compact]);

  useEffect(() => {
    setPlaying(false);
    setProgress(0);
    setDuration(0);
  }, [src]);

  const toggle = () => {
    const el = audioRef.current;
    if (!el) return;
    if (el.paused) el.play();
    else el.pause();
  };

  const seek = (event) => {
    const el = audioRef.current;
    if (!el || !el.duration) return;
    const rect = waveRef.current.getBoundingClientRect();
    const frac = Math.min(Math.max((event.clientX - rect.left) / rect.width, 0), 1);
    el.currentTime = frac * el.duration;
    setProgress(frac);
  };

  return (
    <div className={"audio-player" + (compact ? " compact" : "")}>
      <audio
        ref={audioRef}
        src={src}
        preload="metadata"
        onPlay={() => setPlaying(true)}
        onPause={() => setPlaying(false)}
        onEnded={() => {
          setPlaying(false);
          setProgress(0);
        }}
        onTimeUpdate={(e) => {
          const el = e.target;
          if (el.duration) setProgress(el.currentTime / el.duration);
        }}
        onLoadedMetadata={(e) => setDuration(e.target.duration)}
      />
      <button
        type="button"
        className="audio-play"
        onClick={toggle}
        aria-label={playing ? "Pause" : "Play"}
      >
        {playing ? <Pause /> : <Play />}
      </button>
      <div className="audio-wave" ref={waveRef} onClick={seek} role="presentation">
        {bars.map((b, i) => (
          <span
            key={i}
            className={progress > 0 && i / bars.length <= progress ? "played" : ""}
            style={{ height: `${Math.round(b * 100)}%` }}
          />
        ))}
      </div>
      <span className="audio-time">
        {playing || progress > 0 ? `${fmt(duration * progress)} / ${fmt(duration)}` : fmt(duration)}
      </span>
    </div>
  );
}
