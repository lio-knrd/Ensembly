import { useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, mediaUrl } from "../api.js";
import { COLUMNS, columnForStage, isGenerating, stageLabel } from "../stages.js";
import Modal from "../components/Modal.jsx";
import StatusPill from "../components/StatusPill.jsx";

export default function ProjectDetail() {
  const { id } = useParams();
  const navigate = useNavigate();
  const qc = useQueryClient();
  const { data, isLoading } = useQuery({
    queryKey: ["project", id],
    queryFn: () => api.getProject(id),
  });
  const del = useMutation({
    mutationFn: () => api.deleteProject(id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["projects"] });
      qc.removeQueries({ queryKey: ["project", id] });
      navigate("/");
    },
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
            {project.voice_name && <span className="tag">Voice: {project.voice_name}</span>}
          </div>
        </div>
        <div className="detail-head-actions">
          <StatusPill stage={project.stage} />
          <button
            className="btn danger sm"
            disabled={del.isPending}
            onClick={() => {
              const ok = window.confirm(`Delete "${project.title}"? This removes it from the board.`);
              if (ok) del.mutate();
            }}
          >
            {del.isPending ? "Deleting..." : "Delete"}
          </button>
        </div>
      </div>

      <StageProgress stage={project.stage} />
      <StageBar project={project} scenes={scenes} />
      {showSoundtrack(project.stage) && <SoundtrackPanel projectId={id} />}

      {project.error && <div className="banner">Error — {project.error}</div>}

      {project.stage === "CAST_REVIEW" && <CastPanel project={project} visualStyle={visualStyle} />}

      {project.stage === "DONE" && (
        <FinalPanel project={project} metadata={metadata} />
      )}

      <div className="scenes">
        {scenes.map((s, index) => (
          <SceneCard
            key={s.id}
            projectId={id}
            scene={s}
            stage={project.stage}
            visualStyle={visualStyle}
            hasNextScene={index < scenes.length - 1}
          />
        ))}
      </div>
    </>
  );
}

function showSoundtrack(stage) {
  return !["IDEA", "SCRIPT_GENERATING"].includes(stage);
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
            <CastCard key={c.key || `${c.name}-${c.state || "default"}`} projectId={id} member={c} onDone={invalidate} busy={busy} />
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
        state: member.state || "",
        description: description.trim() || null,
        generate_description: !description.trim(),
      }),
    onSuccess: onDone,
  });
  const selectSheet = useMutation({
    mutationFn: (path) =>
      api.selectReference(member.character_id, {
        path,
        form_id: member.form_id || null,
      }),
    onSuccess: onDone,
  });
  const thumb = mediaUrl(member.reference_image_path, member.reference_version);
  const variants = member.reference_variants || [];

  return (
    <div className={"cast-card" + (member.has_sheet ? "" : " missing")}>
      <div className="cast-sheet-column">
      <div
        className="cast-thumb"
        style={thumb ? { backgroundImage: `url(${thumb})` } : undefined}
      >
        {!thumb && "🗿"}
      </div>
        {variants.length > 1 && (
          <div className="sheet-variant-list">
            {variants.map((path, index) => (
              <button
                type="button"
                key={path}
                className={"sheet-variant" + (path === member.reference_image_path ? " active" : "")}
                disabled={busy || selectSheet.isPending || path === member.reference_image_path}
                onClick={() => selectSheet.mutate(path)}
                title={`Sheet version ${index + 1}`}
              >
                <img src={mediaUrl(path)} alt="" />
              </button>
            ))}
          </div>
        )}
      </div>
      <div className="cast-info">
        <div className="row" style={{ justifyContent: "space-between" }}>
          <h4>{member.state ? `${member.name} / ${member.state}` : member.name}</h4>
          {member.has_sheet ? (
            <span className="tag">ready</span>
          ) : (
            <span className="cast-missing-tag">no sheet</span>
          )}
        </div>
        {member.state && (
          <div className="form-note">
            {member.missing_form
              ? "Special form requested by the script"
              : `Form: ${member.form_name || member.state}`}
          </div>
        )}
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

function SoundtrackPanel({ projectId }) {
  const qc = useQueryClient();
  const [open, setOpen] = useState(false);
  const { data, isLoading } = useQuery({
    queryKey: ["project-music", projectId],
    queryFn: () => api.getProjectMusic(projectId),
  });
  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ["project-music", projectId] });
    qc.invalidateQueries({ queryKey: ["project", projectId] });
  };
  const update = useMutation({
    mutationFn: (body) => api.updateProjectMusic(projectId, body),
    onSuccess: invalidate,
  });
  const clear = useMutation({
    mutationFn: () => api.clearProjectMusic(projectId),
    onSuccess: invalidate,
  });

  const track = data?.track;
  const volume = data?.volume ?? 0.075;
  const enabled = data?.enabled ?? true;

  return (
    <div className="panel soundtrack-panel">
      <div className="soundtrack-head">
        <div>
          <h2>Soundtrack</h2>
          <div className="panel-sub">
            {track ? `${track.title} - ${track.artist_name || "Jamendo artist"}` : "No track selected"}
          </div>
        </div>
        <div className="row">
          <button className="btn" onClick={() => setOpen(true)}>
            {track ? "Change track" : "Choose track"}
          </button>
          {track && (
            <button className="btn ghost" disabled={clear.isPending} onClick={() => clear.mutate()}>
              Remove
            </button>
          )}
        </div>
      </div>
      {isLoading ? (
        <div className="muted">Loading...</div>
      ) : track ? (
        <div className="soundtrack-current">
          {track.image_url && <img src={track.image_url} alt="" />}
          <div className="soundtrack-meta">
            <div className="row soundtrack-title-row">
              <strong>{track.title}</strong>
              <span className="tag">{licenseName(track.license_url)}</span>
              {track.downloaded && <span className="tag">downloaded</span>}
            </div>
            <div className="muted">
              {track.artist_name} {track.duration_seconds ? `- ${formatDuration(track.duration_seconds)}` : ""}
            </div>
            {track.audio_url && <audio src={track.audio_url} controls preload="metadata" />}
          </div>
          <label className="check-row soundtrack-enabled">
            <input
              type="checkbox"
              checked={enabled}
              disabled={update.isPending}
              onChange={(e) => update.mutate({ enabled: e.target.checked })}
            />
            Use in render
          </label>
          <div className="soundtrack-volume">
            <label>Volume</label>
            <input
              type="range"
              min="0"
              max="0.3"
              step="0.005"
              value={volume}
              disabled={update.isPending}
              onChange={(e) => update.mutate({ volume: Number(e.target.value) })}
            />
            <span>{Math.round(volume * 100)}%</span>
          </div>
        </div>
      ) : (
        <div className="soundtrack-empty">
          <button className="btn primary" onClick={() => setOpen(true)}>
            Search Jamendo
          </button>
        </div>
      )}
      {open && <MusicSearchModal projectId={projectId} onClose={() => setOpen(false)} onSelected={invalidate} />}
    </div>
  );
}

function MusicSearchModal({ projectId, onClose, onSelected }) {
  const [query, setQuery] = useState("mythic ambient");
  const [submitted, setSubmitted] = useState("mythic ambient");
  const [instrumental, setInstrumental] = useState(true);
  const { data, isFetching, error } = useQuery({
    queryKey: ["music-search", submitted, instrumental],
    queryFn: () => api.searchMusic(submitted, { instrumental, limit: 20 }),
    enabled: Boolean(submitted),
  });
  const select = useMutation({
    mutationFn: (track) => api.selectProjectMusic(projectId, track),
    onSuccess: () => {
      onSelected();
      onClose();
    },
  });
  const results = data?.results || [];

  return (
    <Modal title="Jamendo music" onClose={onClose}>
      <form
        className="music-search-form"
        onSubmit={(e) => {
          e.preventDefault();
          setSubmitted(query.trim() || "ambient");
        }}
      >
        <input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="mythic, calm, epic..." />
        <button className="btn primary" type="submit">
          Search
        </button>
      </form>
      <label className="check-row music-filter">
        <input
          type="checkbox"
          checked={instrumental}
          onChange={(e) => setInstrumental(e.target.checked)}
        />
        Instrumental
      </label>
      {data && data.configured === false && <div className="banner compact">{data.message}</div>}
      {error && <div className="banner compact">{error.message}</div>}
      {select.error && <div className="banner compact">{select.error.message}</div>}
      {isFetching ? (
        <div className="empty">Searching...</div>
      ) : (
        <div className="music-results">
          {results.map((track) => (
            <div className="music-result" key={track.provider_track_id}>
              {track.image_url && <img src={track.image_url} alt="" />}
              <div className="music-result-main">
                <div className="row soundtrack-title-row">
                  <strong>{track.title}</strong>
                  <span className="tag">{licenseName(track.license_url)}</span>
                </div>
                <div className="muted">
                  {track.artist_name} {track.duration_seconds ? `- ${formatDuration(track.duration_seconds)}` : ""}
                </div>
                {track.audio_url && <audio src={track.audio_url} controls preload="none" />}
              </div>
              <button
                className="btn sm"
                disabled={!track.download_allowed || select.isPending}
                onClick={() => select.mutate(track)}
                title={track.download_allowed ? "Use track" : "Download not allowed by Jamendo"}
              >
                Use
              </button>
            </div>
          ))}
          {!results.length && data?.configured !== false && <div className="empty">No tracks found</div>}
        </div>
      )}
    </Modal>
  );
}

function formatDuration(seconds) {
  const mins = Math.floor((seconds || 0) / 60);
  const secs = Math.floor((seconds || 0) % 60);
  return `${mins}:${String(secs).padStart(2, "0")}`;
}

function licenseName(url) {
  const value = (url || "").toLowerCase();
  if (value.includes("zero")) return "CC0";
  if (value.includes("by-sa")) return "CC BY-SA";
  if (value.includes("by/")) return "CC BY";
  return "CC";
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

function SceneCard({ projectId, scene, stage, visualStyle, hasNextScene }) {
  const qc = useQueryClient();
  const invalidate = () => qc.invalidateQueries({ queryKey: ["project", projectId] });

  const [narration, setNarration] = useState(scene.narration_text);
  const [prompt, setPrompt] = useState(scene.image_prompt);

  useEffect(() => {
    setNarration(scene.narration_text);
  }, [scene.id, scene.narration_text]);

  useEffect(() => {
    setPrompt(scene.image_prompt);
  }, [scene.id, scene.image_prompt]);

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
  const selectAsset = useMutation({
    mutationFn: ({ kind, path }) =>
      api.selectSceneAsset(projectId, scene.id, { kind, path }),
    onSuccess: invalidate,
  });

  const mediaVersion = scene.asset_version || 0;
  const img = mediaUrl(scene.image_path, mediaVersion);
  const clip = mediaUrl(scene.clip_path, mediaVersion);
  const audio = mediaUrl(scene.audio_path, mediaVersion);
  const showClip = clip && scene.scene_type === "video";
  const busy = scene.status === "generating";
  const contextRefs = scene.context_refs || [];
  const continuityContext = scene.continuity_context || [];
  const excludedContextIds = scene.excluded_context_scene_ids || [];
  const imageVariants = scene.image_variants || [];
  const clipVariants = scene.clip_variants || [];
  const audioVariants = scene.audio_variants || [];

  const setContextExcluded = (sceneId, excluded) => {
    const next = excluded
      ? Array.from(new Set([...excludedContextIds, sceneId]))
      : excludedContextIds.filter((id) => id !== sceneId);
    save.mutate({ excluded_context_scene_ids: next });
  };

  return (
    <div className={"scene" + (scene.approved ? " approved" : "")}>
      <div className="scene-preview-column">
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
        {imageVariants.length > 1 && (
          <AssetVariants
            label="Images"
            kind="image"
            paths={imageVariants}
            activePath={scene.image_path}
            pending={selectAsset.isPending || busy}
            onSelect={(path) => selectAsset.mutate({ kind: "image", path })}
          />
        )}
        {scene.scene_type === "video" && clipVariants.length > 1 && (
          <AssetVariants
            label="Clips"
            kind="clip"
            paths={clipVariants}
            activePath={scene.clip_path}
            pending={selectAsset.isPending || busy}
            onSelect={(path) => selectAsset.mutate({ kind: "clip", path })}
          />
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
        {(contextRefs.length > 0 || continuityContext.length > 0) && (
          <div className="context-refs">
            <div className="context-refs-head">
              <span>Continuity context</span>
              <strong>{contextRefs.filter((ref) => !ref.excluded).length} sent</strong>
            </div>
            <div className="context-ref-list">
              {contextRefs.map((ref) => {
                const refImg = mediaUrl(ref.image_path, ref.asset_version);
                return (
                  <div
                    key={ref.scene_id}
                    className={"context-ref" + (ref.excluded ? " excluded" : "")}
                    title={ref.reason || ref.prompt}
                  >
                    <img src={refImg} alt="" />
                    <div>
                      <span>Scene {ref.scene_number}</span>
                      {ref.visual_anchor && <small>{ref.visual_anchor}</small>}
                      {ref.reason && <small>{ref.reason}</small>}
                    </div>
                    <button
                      type="button"
                      className="context-ref-toggle"
                      disabled={save.isPending || busy}
                      title={ref.excluded ? "Use this context image" : "Exclude this context image"}
                      onClick={() => setContextExcluded(ref.scene_id, !ref.excluded)}
                    >
                      {ref.excluded ? "Undo" : "X"}
                    </button>
                  </div>
                );
              })}
              {continuityContext
                .filter((item) => !contextRefs.some((ref) => ref.scene_number === item.source_scene))
                .map((item, idx) => (
                  <div key={`${item.source_scene}-${idx}`} className="context-ref missing">
                    <div className="context-ref-placeholder">?</div>
                    <div>
                      <span>Scene {item.source_scene}</span>
                      {item.visual_anchor && <small>{item.visual_anchor}</small>}
                      {item.reason && <small>{item.reason}</small>}
                    </div>
                  </div>
                ))}
            </div>
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
          {scene.scene_type === "video" && hasNextScene && (
            <label
              className="scene-end-frame-toggle"
              title="Use the following scene's picture as the final frame of this generated clip"
            >
              <input
                type="checkbox"
                checked={scene.use_next_scene_as_end_frame !== false}
                disabled={save.isPending || busy}
                onChange={(e) =>
                  save.mutate({ use_next_scene_as_end_frame: e.target.checked })
                }
              />
              End on next scene picture
            </label>
          )}
          {scene.duration_seconds != null && (
            <span>{scene.duration_seconds.toFixed(1)}s</span>
          )}
          {scene.suggested_characters?.length > 0 && (
            <span className="tag">new character: {scene.suggested_characters.join(", ")}</span>
          )}
        </div>
        {audio && <Waveform src={audio} />}
        {audioVariants.length > 1 && (
          <label className="audio-variant-select">
            <span>Narration version</span>
            <select
              value={scene.audio_path || ""}
              disabled={selectAsset.isPending || busy}
              onChange={(e) =>
                selectAsset.mutate({ kind: "audio", path: e.target.value })
              }
            >
              {audioVariants.map((option, index) => (
                <option key={option.path} value={option.path}>
                  Version {index + 1}
                  {option.path === scene.audio_path ? " (selected)" : ""}
                </option>
              ))}
            </select>
          </label>
        )}
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

function AssetVariants({ label, kind, paths, activePath, pending, onSelect }) {
  return (
    <div className="asset-variants">
      <div className="asset-variants-head">
        <span>{label}</span>
        <small>{paths.length} versions</small>
      </div>
      <div className="asset-variant-list">
        {paths.map((path, index) => {
          const src = mediaUrl(path);
          const active = path === activePath;
          return (
            <button
              key={path}
              type="button"
              className={"asset-variant" + (active ? " active" : "")}
              disabled={pending || active}
              title={`${label} version ${index + 1}${active ? " (selected)" : ""}`}
              onClick={() => onSelect(path)}
            >
              {kind === "clip" ? (
                <video src={src} muted preload="metadata" />
              ) : (
                <img src={src} alt="" />
              )}
              <span>{index + 1}</span>
            </button>
          );
        })}
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
      <audio key={src} src={src} controls preload="metadata" />
    </div>
  );
}
