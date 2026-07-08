import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, mediaUrl } from "../api.js";
import { COLUMNS, columnForStage, isGenerating, stageLabel } from "../stages.js";
import StatusPill from "../components/StatusPill.jsx";

export default function ProjectDetail() {
  const { id } = useParams();
  const { data, isLoading } = useQuery({
    queryKey: ["project", id],
    queryFn: () => api.getProject(id),
    refetchInterval: (q) =>
      q.state.data && isGenerating(q.state.data.project.stage) ? 1500 : 6000,
  });

  if (isLoading || !data) return <div className="empty">Loading…</div>;
  const { project, scenes, metadata, characters } = data;
  const visualStyle = project.visual_style_prompt || "";

  return (
    <>
      <Link to="/" className="back-link">
        ← Board
      </Link>
      <div className="detail-head">
        <div>
          <h1>{project.title}</h1>
          <div className="detail-sub">
            <span>{project.topic_prompt}</span>
            <span>·</span>
            <span>{project.target_duration_seconds}s target</span>
            {visualStyle && (
              <span className="style-chip" title={visualStyle}>
                Style: {project.content_preset_name || "content preset"}
              </span>
            )}
            {characters.length > 0 && (
              <span className="tag">{characters.length} characters</span>
            )}
          </div>
        </div>
        <StatusPill stage={project.stage} />
      </div>

      <StageProgress stage={project.stage} />
      <StageBar project={project} scenes={scenes} />

      {project.error && <div className="banner">Error — {project.error}</div>}

      {project.stage === "CAST_REVIEW" && <CastPanel project={project} visualStyle={visualStyle} />}

      {project.stage === "DONE" && (
        <FinalPanel project={project} metadata={metadata} />
      )}

      <div className="scenes">
        {scenes.map((s) => (
          <SceneCard
            key={s.id}
            projectId={id}
            scene={s}
            stage={project.stage}
            visualStyle={visualStyle}
          />
        ))}
      </div>
    </>
  );
}

function StageProgress({ stage }) {
  const current = columnForStage(stage);
  const idx = COLUMNS.findIndex((c) => c.key === current);
  return (
    <div className="stage-progress" style={{ marginTop: 20 }}>
      {COLUMNS.map((c, i) => (
        <div
          key={c.key}
          className={"stage-step " + (i < idx ? "done" : i === idx ? "current" : "")}
          title={c.label}
        />
      ))}
    </div>
  );
}

function StageBar({ project, scenes }) {
  const qc = useQueryClient();
  const id = project.id;
  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ["project", id] });
    qc.invalidateQueries({ queryKey: ["projects"] });
  };
  const call = (fn) => useMutation({ mutationFn: fn, onSuccess: invalidate });

  const approveScript = call(() => api.approveScript(id));
  const approveStoryboard = call(() => api.approveStoryboard(id));
  const approveClips = call(() => api.approveClips(id));
  const regenScript = call(() => api.regenScript(id));
  const stepBack = call(() => api.stepBack(id));
  const stepForward = call(() => api.stepForward(id));
  const retryFailedStep = call(() => api.retryFailedStep(id));
  const cancelProject = call(() => api.cancelProject(id));
  const rerender = call(() => api.rerender(id));

  const generating = isGenerating(project.stage);
  const canStepBack = ["SCRIPT_READY", "CAST_REVIEW", "STORYBOARD_READY", "CLIPS_READY", "DONE"].includes(project.stage);
  const castBusy = project.stage === "CAST_REVIEW" && (project.status_message || "").startsWith("Generating");
  const navMode = ["Moved back one step", "Moved forward one step"].includes(project.status_message || "");
  const hasScenes = scenes.length > 0;
  const sceneBusy = scenes.some((scene) => scene.status === "generating");
  const working = generating || castBusy || sceneBusy;
  const audioReady = hasScenes && scenes.every((scene) => scene.audio_path);
  const storyboardReady = hasScenes && scenes.every((scene) => scene.image_path);
  const videoScenes = scenes.filter((scene) => scene.scene_type === "video");
  const clipsReady = hasScenes && videoScenes.every((scene) => scene.clip_path);
  const canStepForward =
    navMode &&
    ((project.stage === "IDEA" && hasScenes) ||
      (project.stage === "SCRIPT_READY" && audioReady) ||
      (project.stage === "CAST_REVIEW" && storyboardReady) ||
      (project.stage === "STORYBOARD_READY" && clipsReady) ||
      (project.stage === "CLIPS_READY" && project.status_message === "Moved back one step"));

  let action = null;
  if (generating) {
    action = (
      <div className="row">
        <span className="spinner" />
        <span className="muted">{project.status_message || stageLabel(project.stage)}</span>
      </div>
    );
  } else if (project.stage === "SCRIPT_READY") {
    action = (
      <div className="row">
        <button className="btn" onClick={() => regenScript.mutate()}>
          Regenerate script
        </button>
        <button className="btn primary" onClick={() => approveScript.mutate()}>
          Approve script → audio & cast review
        </button>
      </div>
    );
  } else if (project.stage === "CAST_REVIEW") {
    const busy = (project.status_message || "").startsWith("Generating");
    action = (
      <div className="row">
        {busy && <span className="spinner" />}
        <span className="muted">Review character sheets below, then approve</span>
      </div>
    );
  } else if (project.stage === "STORYBOARD_READY") {
    action = (
      <button className="btn primary" onClick={() => approveStoryboard.mutate()}>
        Approve storyboard → generate clips
      </button>
    );
  } else if (project.stage === "CLIPS_READY") {
    action = (
      <button className="btn primary" onClick={() => approveClips.mutate()}>
        Approve clips → render final video
      </button>
    );
  } else if (project.stage === "DONE") {
    action = (
      <button className="btn" onClick={() => rerender.mutate()}>
        Re-render
      </button>
    );
  } else if (project.stage === "FAILED") {
    action = (
      <button className="btn" disabled={retryFailedStep.isPending} onClick={() => retryFailedStep.mutate()}>
        Retry failed step
      </button>
    );
  } else if (project.stage === "CANCELED") {
    action = (
      <button className="btn" disabled={retryFailedStep.isPending} onClick={() => retryFailedStep.mutate()}>
        Resume canceled step
      </button>
    );
  } else if (project.stage === "IDEA") {
    action = (
      <button className="btn primary" onClick={() => regenScript.mutate()}>
        Generate script
      </button>
    );
  }

  const approvedCount = scenes.filter((s) => s.approved).length;

  return (
    <div className="stage-bar">
      <div className="grow">
        <strong>{stageLabel(project.stage)}</strong>
        <div className="muted" style={{ fontSize: 12.5, marginTop: 2 }}>
          {scenes.length} scenes · {approvedCount} approved
        </div>
      </div>
      <div className="stage-actions">
        {working && (
          <button className="btn danger" disabled={cancelProject.isPending} onClick={() => cancelProject.mutate()}>
            Cancel
          </button>
        )}
        {canStepBack && !working && (
          <button className="btn" disabled={stepBack.isPending} onClick={() => stepBack.mutate()}>
            Back one step
          </button>
        )}
        {canStepForward && !working && (
          <button className="btn" disabled={stepForward.isPending} onClick={() => stepForward.mutate()}>
            Forward one step
          </button>
        )}
        {action}
      </div>
    </div>
  );
}

function CastPanel({ project, visualStyle }) {
  const id = project.id;
  const qc = useQueryClient();
  const { data: cast = [] } = useQuery({
    queryKey: ["cast", id],
    queryFn: () => api.getCast(id),
    refetchInterval: 2000,
  });
  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ["cast", id] });
    qc.invalidateQueries({ queryKey: ["project", id] });
    qc.invalidateQueries({ queryKey: ["projects"] });
  };
  const genMissing = useMutation({
    mutationFn: () => api.generateMissingSheets(id),
    onSuccess: invalidate,
  });
  const approve = useMutation({ mutationFn: () => api.approveCast(id), onSuccess: invalidate });

  const missing = cast.filter((c) => !c.has_sheet).length;
  const busy = (project.status_message || "").startsWith("Generating");

  return (
    <div className="panel">
      <div className="cast-head">
        <div style={{ maxWidth: 560 }}>
          <h2>Character sheets</h2>
          <p className="panel-sub" style={{ margin: 0 }}>
            {cast.length === 0
              ? "The script references no characters — nothing to lock in."
              : missing
              ? `${missing} of ${cast.length} character${cast.length === 1 ? "" : "s"} still need a reference sheet. Sheets keep each character's look consistent across every scene (visual style comes from the content preset).`
              : "All character sheets are ready. Approve to generate the storyboard images."}
          </p>
          {visualStyle && (
            <div className="style-summary" title={visualStyle}>
              Character sheets use style: {visualStyle}
            </div>
          )}
        </div>
        <div className="row">
          {missing > 0 && (
            <button
              className="btn"
              disabled={busy || genMissing.isPending}
              onClick={() => genMissing.mutate()}
            >
              {busy || genMissing.isPending ? (
                <>
                  <span className="spinner" /> Generating…
                </>
              ) : (
                `Generate all missing (${missing})`
              )}
            </button>
          )}
          <button
            className="btn primary"
            disabled={approve.isPending}
            onClick={() => approve.mutate()}
          >
            {missing > 0
              ? "Skip & generate storyboard"
              : "Approve cast → generate storyboard"}
          </button>
        </div>
      </div>

      {cast.length > 0 && (
        <div className="cast-grid">
          {cast.map((c) => (
            <CastCard key={c.name} projectId={id} member={c} onDone={invalidate} busy={busy} />
          ))}
        </div>
      )}
    </div>
  );
}

function CastCard({ projectId, member, onDone, busy }) {
  const [description, setDescription] = useState(member.description || "");
  const gen = useMutation({
    mutationFn: () =>
      api.generateCastSheet(projectId, {
        name: member.name,
        description: description.trim() || null,
        generate_description: !description.trim(),
      }),
    onSuccess: onDone,
  });
  const thumb = mediaUrl(member.reference_image_path);

  return (
    <div className={"cast-card" + (member.has_sheet ? "" : " missing")}>
      <div
        className="cast-thumb"
        style={thumb ? { backgroundImage: `url(${thumb})` } : undefined}
      >
        {!thumb && "🗿"}
      </div>
      <div className="cast-info">
        <div className="row" style={{ justifyContent: "space-between" }}>
          <h4>{member.name}</h4>
          {member.has_sheet ? (
            <span className="tag">ready</span>
          ) : (
            <span className="cast-missing-tag">no sheet</span>
          )}
        </div>
        <textarea
          value={description}
          placeholder="Appearance notes — leave blank to let the AI describe"
          onChange={(e) => setDescription(e.target.value)}
        />
        <button
          className="btn sm"
          disabled={busy || gen.isPending}
          onClick={() => gen.mutate()}
        >
          {gen.isPending
            ? "Generating…"
            : member.has_sheet
            ? "Regenerate sheet"
            : "Generate sheet"}
        </button>
      </div>
    </div>
  );
}

function FinalPanel({ project, metadata }) {
  const folder = project.folder_path;
  const slug = (project.title || "video")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/(^-|-$)/g, "");
  const videoUrl = folder ? mediaUrl(`${folder}/final/${slug}.mp4`) : null;

  return (
    <div className="panel">
      <h2>Final video</h2>
      <div className="row" style={{ alignItems: "flex-start", gap: 24 }}>
        {videoUrl && (
          <div className="final-video">
            <video src={videoUrl} controls />
          </div>
        )}
        <div style={{ flex: 1, minWidth: 0 }}>
          {metadata?.title && (
            <div className="field">
              <label>Title</label>
              <div>{metadata.title}</div>
            </div>
          )}
          {metadata?.description && (
            <div className="field">
              <label>Description</label>
              <div className="muted">{metadata.description}</div>
            </div>
          )}
          {metadata?.hashtags?.length > 0 && (
            <div className="field">
              <label>Hashtags</label>
              <div className="row" style={{ flexWrap: "wrap" }}>
                {metadata.hashtags.map((h) => (
                  <span className="tag" key={h}>
                    {h}
                  </span>
                ))}
              </div>
            </div>
          )}
          {metadata?.suggested_caption && (
            <div className="field">
              <label>Suggested caption</label>
              <textarea readOnly value={metadata.suggested_caption} />
            </div>
          )}
          {videoUrl && (
            <a className="btn" href={videoUrl} download>
              Download MP4
            </a>
          )}
        </div>
      </div>
    </div>
  );
}

function SceneCard({ projectId, scene, stage, visualStyle }) {
  const qc = useQueryClient();
  const invalidate = () => qc.invalidateQueries({ queryKey: ["project", projectId] });

  const [narration, setNarration] = useState(scene.narration_text);
  const [prompt, setPrompt] = useState(scene.image_prompt);

  const save = useMutation({
    mutationFn: (body) => api.updateScene(projectId, scene.id, body),
    onSuccess: invalidate,
  });
  const regenImage = useMutation({
    mutationFn: () => api.regenSceneImage(projectId, scene.id),
    onSuccess: invalidate,
  });
  const regenAudio = useMutation({
    mutationFn: () => api.regenSceneAudio(projectId, scene.id),
    onSuccess: invalidate,
  });
  const regenClip = useMutation({
    mutationFn: () => api.regenSceneClip(projectId, scene.id),
    onSuccess: invalidate,
  });

  const img = mediaUrl(scene.image_path);
  const clip = mediaUrl(scene.clip_path);
  const audio = mediaUrl(scene.audio_path);
  const showClip = clip && scene.scene_type === "video";
  const busy = scene.status === "generating";

  return (
    <div className={"scene" + (scene.approved ? " approved" : "")}>
      <div className="scene-visual">
        <span className="scene-index">#{scene.order_index + 1}</span>
        {busy ? (
          <span className="spinner" />
        ) : showClip ? (
          <video src={clip} muted loop playsInline
            onMouseOver={(e) => e.target.play()} onMouseOut={(e) => e.target.pause()} />
        ) : img ? (
          <img src={img} alt="" />
        ) : (
          "no image"
        )}
      </div>

      <div className="scene-main">
        <textarea
          className="scene-narration"
          value={narration}
          onChange={(e) => setNarration(e.target.value)}
          onBlur={() =>
            narration !== scene.narration_text &&
            save.mutate({ narration_text: narration })
          }
        />
        <textarea
          className="scene-prompt"
          value={prompt}
          onChange={(e) => setPrompt(e.target.value)}
          onBlur={() =>
            prompt !== scene.image_prompt && save.mutate({ image_prompt: prompt })
          }
        />
        {visualStyle && (
          <div className="scene-style" title={visualStyle}>
            <span>Generation style</span>
            <strong>{visualStyle}</strong>
          </div>
        )}
        <div className="scene-meta-row">
          <div className="seg">
            <button
              className={scene.scene_type === "still" ? "on" : ""}
              onClick={() => save.mutate({ scene_type: "still" })}
            >
              Still
            </button>
            <button
              className={scene.scene_type === "video" ? "on" : ""}
              onClick={() => save.mutate({ scene_type: "video" })}
            >
              Video
            </button>
          </div>
          {scene.duration_seconds != null && (
            <span>{scene.duration_seconds.toFixed(1)}s</span>
          )}
          {scene.suggested_characters?.length > 0 && (
            <span className="tag">new character: {scene.suggested_characters.join(", ")}</span>
          )}
        </div>
        {audio && <Waveform src={audio} />}
      </div>

      <div className="scene-actions">
        <button
          className={"btn sm " + (scene.approved ? "" : "primary")}
          onClick={() => save.mutate({ approved: !scene.approved })}
        >
          {scene.approved ? "Approved ✓" : "Approve"}
        </button>
        {img && (
          <button className="btn sm" disabled={regenImage.isPending} onClick={() => regenImage.mutate()}>
            Regen image
          </button>
        )}
        {audio && (
          <button className="btn sm" disabled={regenAudio.isPending} onClick={() => regenAudio.mutate()}>
            Regen audio
          </button>
        )}
        {scene.scene_type === "video" && img && (
          <button className="btn sm" disabled={regenClip.isPending} onClick={() => regenClip.mutate()}>
            Regen clip
          </button>
        )}
      </div>
    </div>
  );
}

// Lightweight synthetic waveform + native audio playback (duration visible).
function Waveform({ src }) {
  const bars = Array.from({ length: 40 }, (_, i) =>
    6 + Math.abs(Math.sin(i * 1.3) * 16) + (i % 3) * 2
  );
  return (
    <div>
      <div className="waveform">
        {bars.map((h, i) => (
          <span key={i} style={{ height: `${h}px` }} />
        ))}
      </div>
      <audio src={src} controls preload="none" />
    </div>
  );
}
