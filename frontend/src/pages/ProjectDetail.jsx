import { useEffect, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import {
  ArrowLeft,
  ArrowRight,
  Captions,
  Check,
  ChevronLeft,
  ChevronRight,
  Download,
  ImageOff,
  Music,
  RefreshCw,
  RotateCcw,
  Undo2,
  Upload,
  UserRound,
  X,
} from "lucide-react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, mediaUrl } from "../api.js";
import { COLUMNS, columnForStage, isGenerating, stageLabel } from "../stages.js";
import Modal from "../components/Modal.jsx";
import StatusPill from "../components/StatusPill.jsx";
import AudioPlayer from "../components/AudioPlayer.jsx";
import Loading from "../components/Loading.jsx";

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

  if (isLoading || !data) return <Loading full />;
  const { project, scenes, metadata, characters } = data;
  const visualStyle = project.visual_style_prompt || "";

  return (
    <>
      <Link to="/" className="back-link">
        <ArrowLeft />
        Board
      </Link>
      <div className="detail-head">
        <div>
          <h1>{project.title}</h1>
          <div className="detail-sub">
            <span>{project.topic_prompt}</span>
            <span className="sep" />
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
      {showRenderPanels(project.stage) && <SoundtrackPanel projectId={id} />}
      {showRenderPanels(project.stage) && <SubtitlesPanel project={project} scenes={scenes} />}
      {scenes.some((scene) => scene.image_path) && (
        <TitleCardPanel project={project} scenes={scenes} />
      )}

      {project.error && <div className="banner">Error: {project.error}</div>}

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

function showRenderPanels(stage) {
  return !["IDEA", "SCRIPT_GENERATING"].includes(stage);
}

function TitleCardPanel({ project, scenes }) {
  const qc = useQueryClient();
  const projectQueryKey = ["project", project.id];
  const usableScenes = scenes.filter((scene) => scene.image_path);
  const [sceneId, setSceneId] = useState(usableScenes[0]?.id || "");
  const [kicker, setKicker] = useState(project.title_card_kicker || "");
  const [text, setText] = useState(project.title_card_text || project.title || "");
  const [partLabel, setPartLabel] = useState(project.title_card_part_label || "");
  const [prompt, setPrompt] = useState(project.title_card_prompt || "");
  const hydrated = useRef(false);
  const busy = project.title_card_status === "generating";
  const cover = mediaUrl(project.title_card_path, project.title_card_version);
  const titleVariants = (project.title_card_variants || [])
    .map((item) => (typeof item === "string" ? { path: item } : item))
    .filter((item) => item?.path);
  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ["project", project.id] });
    qc.invalidateQueries({ queryKey: ["projects"] });
  };
  const updateCopy = useMutation({
    mutationFn: (body) => api.updateTitleCard(project.id, body),
    onSuccess: invalidate,
  });
  const create = useMutation({
    mutationFn: (mode) =>
      api.generateTitleCard(project.id, {
        mode,
        scene_id: mode === "reuse" ? sceneId : null,
        kicker,
        text,
        part_label: partLabel,
        prompt,
      }),
    onSuccess: invalidate,
  });
  const selectCover = useMutation({
    mutationFn: (path) => api.selectTitleCard(project.id, { path }),
    onMutate: async (path) => {
      await qc.cancelQueries({ queryKey: projectQueryKey });
      const previous = qc.getQueryData(projectQueryKey);
      qc.setQueryData(projectQueryKey, (current) =>
        current
          ? {
              ...current,
              project: {
                ...current.project,
                title_card_path: path,
                title_card_status: "ready",
                title_card_version: (current.project.title_card_version || 0) + 1,
              },
            }
          : current
      );
      return { previous };
    },
    onError: (_error, _path, context) => {
      if (context?.previous) qc.setQueryData(projectQueryKey, context.previous);
    },
    onSuccess: (data) => {
      if (data?.project) {
        qc.setQueryData(projectQueryKey, (current) =>
          current
            ? {
                ...current,
                project: { ...current.project, ...data.project },
              }
            : current
        );
      }
      qc.invalidateQueries({ queryKey: ["projects"] });
    },
  });

  useEffect(() => {
    setKicker(project.title_card_kicker || "");
    setText(project.title_card_text || project.title || "");
    setPartLabel(project.title_card_part_label || "");
    setPrompt(project.title_card_prompt || "");
    hydrated.current = true;
  }, [
    project.title,
    project.title_card_kicker,
    project.title_card_text,
    project.title_card_part_label,
    project.title_card_prompt,
  ]);

  useEffect(() => {
    if (!hydrated.current || busy || !text.trim()) return undefined;
    const next = { kicker, text, part_label: partLabel, prompt };
    const unchanged =
      next.kicker === (project.title_card_kicker || "") &&
      next.text === (project.title_card_text || project.title || "") &&
      next.part_label === (project.title_card_part_label || "") &&
      next.prompt === (project.title_card_prompt || "");
    if (unchanged) return undefined;
    const timer = window.setTimeout(() => updateCopy.mutate(next), 650);
    return () => window.clearTimeout(timer);
  }, [
    busy,
    kicker,
    text,
    partLabel,
    prompt,
    project.title,
    project.title_card_kicker,
    project.title_card_text,
    project.title_card_part_label,
    project.title_card_prompt,
  ]);

  return (
    <div className="panel title-card-panel">
      <div className="title-card-media">
        <div className="title-card-preview">
          {cover ? (
            <img src={cover} alt={`Title image: ${project.title_card_text || project.title}`} />
          ) : (
            <div className="title-card-placeholder">No title image yet</div>
          )}
          {busy && (
            <div className="title-card-busy">
              <span className="spinner" /> Creating title image...
            </div>
          )}
        </div>
        {titleVariants.length > 1 && (
          <AssetVariants
            label="Covers"
            kind="image"
            paths={titleVariants.map((item) => item.path)}
            activePath={project.title_card_path}
            pending={busy || selectCover.isPending || updateCopy.isPending}
            onSelect={(path) => selectCover.mutate(path)}
          />
        )}
      </div>
      <div className="title-card-controls">
        <div>
          <h2>Title image</h2>
          <p className="panel-sub">
            A short hook, one dominant subject and an optional series label create the
            thumbnail hierarchy. Part numbering is detected automatically for split projects.
          </p>
        </div>
        <div className="title-copy-grid">
          <div className="field">
            <label>Small hook / kicker</label>
            <input
              value={kicker}
              maxLength={50}
              placeholder="ZEUS' BIGGEST CHALLENGE"
              onChange={(event) => setKicker(event.target.value)}
            />
          </div>
          <div className="field">
            <label>Series label (optional)</label>
            <input
              value={partLabel}
              maxLength={24}
              placeholder="PART 1 or FINAL PART"
              onChange={(event) => setPartLabel(event.target.value)}
            />
          </div>
        </div>
        <div className="field">
          <label>Dominant cover title</label>
          <input
            value={text}
            maxLength={60}
            placeholder="TYPHON"
            onChange={(event) => setText(event.target.value)}
          />
        </div>
        <div className="field">
          <label>Storyboard image</label>
          <div className="row">
            <select value={sceneId} onChange={(event) => setSceneId(event.target.value)}>
              {usableScenes.map((scene) => (
                <option key={scene.id} value={scene.id}>
                  Scene {scene.order_index + 1}
                </option>
              ))}
            </select>
            <button
              className="btn"
              disabled={busy || create.isPending || !sceneId || !text.trim()}
              onClick={() => create.mutate("reuse")}
            >
              Use scene
            </button>
          </div>
        </div>
        <div className="field">
          <label>Optional prompt for a dedicated cover</label>
          <textarea
            value={prompt}
            placeholder="Leave blank to derive it from the project topic and visual style"
            rows={2}
            onChange={(event) => setPrompt(event.target.value)}
          />
        </div>
        <div className="row">
          <button
            className="btn primary"
            disabled={busy || create.isPending || !text.trim()}
            onClick={() => create.mutate("generate")}
          >
            Generate new image version
          </button>
          {updateCopy.isPending && <span className="muted">Updating title image...</span>}
          {project.title_card_status === "failed" && (
            <span className="banner compact">Title image generation failed.</span>
          )}
          {create.error && <span className="banner compact">{create.error.message}</span>}
          {updateCopy.error && <span className="banner compact">{updateCopy.error.message}</span>}
          {selectCover.error && <span className="banner compact">{selectCover.error.message}</span>}
        </div>
      </div>
    </div>
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
          <RefreshCw />
          Regenerate script
        </button>
        <button
          className="btn primary"
          title="Generates narration audio and opens cast review"
          onClick={() => approveScript.mutate()}
        >
          Approve script
          <ArrowRight />
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
      <button
        className="btn primary"
        title="Generates video clips from the approved storyboard"
        onClick={() => approveStoryboard.mutate()}
      >
        Approve storyboard
        <ArrowRight />
      </button>
    );
  } else if (project.stage === "CLIPS_READY") {
    action = (
      <button
        className="btn primary"
        title="Renders the final video from the approved clips"
        onClick={() => approveClips.mutate()}
      >
        Approve clips
        <ArrowRight />
      </button>
    );
  } else if (project.stage === "DONE") {
    action = (
      <button className="btn" onClick={() => rerender.mutate()}>
        <RefreshCw />
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
          {scenes.length} scenes / {approvedCount} approved
        </div>
      </div>
      <div className="stage-actions">
        {working && (
          <button className="btn danger" disabled={cancelProject.isPending} onClick={() => cancelProject.mutate()}>
            <X />
            Cancel
          </button>
        )}
        {canStepBack && !working && (
          <button className="btn" disabled={stepBack.isPending} onClick={() => stepBack.mutate()}>
            <ChevronLeft />
            Back one step
          </button>
        )}
        {canStepForward && !working && (
          <button className="btn" disabled={stepForward.isPending} onClick={() => stepForward.mutate()}>
            Forward one step
            <ChevronRight />
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
              ? "The script references no characters, so there is nothing to lock in."
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
                  <span className="spinner" /> Generating...
                </>
              ) : (
                `Generate all missing (${missing})`
              )}
            </button>
          )}
          <button
            className="btn primary"
            title="Generates the storyboard images"
            disabled={approve.isPending}
            onClick={() => approve.mutate()}
          >
            {missing > 0 ? "Skip & generate storyboard" : "Approve cast"}
            <ArrowRight />
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
        {!thumb && <UserRound />}
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
          placeholder="Appearance notes; leave blank to let the AI describe"
          onChange={(e) => setDescription(e.target.value)}
        />
        <button
          className="btn sm"
          disabled={busy || gen.isPending}
          onClick={() => gen.mutate()}
        >
          {gen.isPending
            ? "Generating..."
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
            {track ? `${track.title} - ${track.artist_name || "Unknown artist"}` : "No music"}
          </div>
        </div>
        <div className="row">
          <button className="btn" onClick={() => setOpen(true)}>
            {track ? "Change track" : "Choose track"}
          </button>
          {track && (
            <button className="btn ghost" disabled={clear.isPending} onClick={() => clear.mutate()}>
              No music
            </button>
          )}
        </div>
      </div>
      {isLoading ? (
        <Loading inline />
      ) : track ? (
        <div className="soundtrack-current">
          {track.image_url ? <img src={track.image_url} alt="" /> : <div className="music-note" aria-hidden="true"><Music /></div>}
          <div className="soundtrack-meta">
            <div className="row soundtrack-title-row">
              <strong>{track.title}</strong>
              <span className="tag">{licenseName(track.license_url)}</span>
              <span className="tag">{track.provider === "local" ? "local library" : "Jamendo"}</span>
            </div>
            <div className="muted">
              {track.artist_name} {track.duration_seconds ? `- ${formatDuration(track.duration_seconds)}` : ""}
            </div>
            {track.audio_url && <AudioPlayer src={track.audio_url} />}
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
  const { data: libraryData, isLoading: libraryLoading } = useQuery({
    queryKey: ["music-library"],
    queryFn: api.listMusicLibrary,
  });
  const select = useMutation({
    mutationFn: (track) => api.selectProjectMusic(projectId, track),
    onSuccess: () => {
      onSelected();
      onClose();
    },
  });
  const results = data?.results || [];
  const library = libraryData?.results || [];

  return (
    <Modal title="Choose soundtrack" onClose={onClose}>
      <div className="music-section-head">
        <strong>Local library</strong>
        <span className="muted">Always available</span>
      </div>
      {libraryLoading ? (
        <Loading inline label="Loading library" />
      ) : (
        <div className="music-results local-music-results">
          {library.map((track) => (
            <MusicResult
              key={`${track.provider}:${track.provider_track_id}`}
              track={track}
              pending={select.isPending}
              onSelect={() => select.mutate(track)}
            />
          ))}
          {!library.length && <div className="empty compact">No local tracks found</div>}
        </div>
      )}
      <div className="music-section-head music-search-head">
        <strong>Find another track</strong>
        <span className="muted">Jamendo</span>
      </div>
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
            <MusicResult
              key={`${track.provider}:${track.provider_track_id}`}
              track={track}
              pending={select.isPending}
              onSelect={() => select.mutate(track)}
            />
          ))}
          {!results.length && data?.configured !== false && <div className="empty">No tracks found</div>}
        </div>
      )}
    </Modal>
  );
}

function MusicResult({ track, pending, onSelect }) {
  return (
    <div className="music-result">
      {track.image_url ? <img src={track.image_url} alt="" /> : <div className="music-note" aria-hidden="true"><Music /></div>}
      <div className="music-result-main">
        <div className="row soundtrack-title-row">
          <strong>{track.title}</strong>
          <span className="tag">{licenseName(track.license_url)}</span>
        </div>
        <div className="muted">
          {track.artist_name} {track.duration_seconds ? `- ${formatDuration(track.duration_seconds)}` : ""}
        </div>
        {track.audio_url && <AudioPlayer src={track.audio_url} compact />}
      </div>
      <button
        className="btn sm"
        disabled={!track.download_allowed || pending}
        onClick={onSelect}
        title={track.download_allowed ? "Use track" : "This track cannot be downloaded"}
      >
        Use
      </button>
    </div>
  );
}

function formatDuration(seconds) {
  const mins = Math.floor((seconds || 0) / 60);
  const secs = Math.floor((seconds || 0) % 60);
  return `${mins}:${String(secs).padStart(2, "0")}`;
}

function licenseName(url) {
  const value = (url || "").toLowerCase();
  if (value === "royalty-free") return "Royalty-free";
  if (value.includes("zero")) return "CC0";
  if (value.includes("by-sa")) return "CC BY-SA";
  if (value.includes("by/")) return "CC BY";
  return "CC";
}

// Mirrors the renderer's caption wrapping (24 chars, at most two lines) so the
// preview block matches the size of what actually gets burned in.
const SAMPLE_LINE_CHARS = 24;

function sampleCaptionLines(scenes) {
  const source =
    scenes.find((scene) => (scene.narration_text || "").trim())?.narration_text ||
    "Your narration appears here";
  const lines = [];
  let current = "";
  for (const word of source.trim().split(/\s+/)) {
    const candidate = current ? `${current} ${word}` : word;
    if (current && candidate.length > SAMPLE_LINE_CHARS) {
      lines.push(current);
      if (lines.length === 2) return lines;
      current = word;
    } else {
      current = candidate;
    }
  }
  if (current) lines.push(current);
  return lines;
}

function SubtitlesPanel({ project, scenes }) {
  const qc = useQueryClient();
  const projectQueryKey = ["project", project.id];
  const defaultPosition = project.subtitle_position_default ?? 0.128;
  const minPosition = project.subtitle_position_min ?? 0.02;
  const maxPosition = project.subtitle_position_max ?? 0.85;
  const clamp = (value) => Math.min(Math.max(value, minPosition), maxPosition);

  const enabled = project.subtitles_enabled !== false;
  const saved = clamp(project.subtitle_position ?? defaultPosition);
  const [position, setPosition] = useState(saved);
  // Mirrored in a ref because a pointerup can land in the same task as the last
  // pointermove, before a re-render hands the handler a fresh closure.
  const livePosition = useRef(saved);
  const frameRef = useRef(null);
  const grabOffset = useRef(null);

  const update = useMutation({
    mutationFn: (body) => api.updateProjectSubtitles(project.id, body),
    onSuccess: (data) =>
      qc.setQueryData(projectQueryKey, (current) =>
        current
          ? {
              ...current,
              project: {
                ...current.project,
                subtitles_enabled: data.enabled,
                subtitle_position: data.position,
              },
            }
          : current
      ),
  });

  // Follow the server value unless the user is mid-drag.
  useEffect(() => {
    if (grabOffset.current !== null) return;
    livePosition.current = saved;
    setPosition(saved);
  }, [saved]);

  const move = (value) => {
    livePosition.current = value;
    setPosition(value);
  };
  const commit = () => {
    const value = livePosition.current;
    if (Math.abs(value - saved) < 0.0005) return;
    update.mutate({ position: Number(value.toFixed(3)) });
  };

  const startDrag = (event) => {
    if (!enabled) return;
    const box = event.currentTarget.getBoundingClientRect();
    grabOffset.current = event.clientY - box.bottom;
    event.currentTarget.setPointerCapture(event.pointerId);
  };
  const onDrag = (event) => {
    const frame = frameRef.current;
    if (grabOffset.current === null || !frame) return;
    const rect = frame.getBoundingClientRect();
    if (!rect.height) return;
    const bottomY = event.clientY - grabOffset.current;
    move(clamp((rect.bottom - bottomY) / rect.height));
  };
  const endDrag = (event) => {
    if (grabOffset.current === null) return;
    grabOffset.current = null;
    event.currentTarget.releasePointerCapture(event.pointerId);
    commit();
  };

  const backdropScene = scenes.find((scene) => scene.image_path);
  const backdrop =
    mediaUrl(backdropScene?.image_path, backdropScene?.asset_version) ||
    mediaUrl(project.title_card_path, project.title_card_version);
  const lines = sampleCaptionLines(scenes);
  const isDefault = Math.abs(saved - defaultPosition) < 0.0005;

  return (
    <div className="panel subtitles-panel">
      <div
        className="subtitle-preview"
        ref={frameRef}
        aria-label="Subtitle placement preview"
      >
        {backdrop ? (
          <img src={backdrop} alt="" />
        ) : (
          <div className="subtitle-preview-empty">No scene image yet</div>
        )}
        <div
          className={"subtitle-sample" + (enabled ? "" : " muted-off")}
          style={{ bottom: `${position * 100}%` }}
          onPointerDown={startDrag}
          onPointerMove={onDrag}
          onPointerUp={endDrag}
          onPointerCancel={endDrag}
        >
          {lines.map((line, index) => (
            <span key={index}>{line}</span>
          ))}
        </div>
      </div>

      <div className="subtitle-controls">
        <div className="subtitle-head">
          <div>
            <h2>Subtitles</h2>
            <div className="panel-sub">
              {enabled
                ? "Burned in from the narration timestamps"
                : "Not burned into this render"}
            </div>
          </div>
          <label className="check-row">
            <input
              type="checkbox"
              checked={enabled}
              disabled={update.isPending}
              onChange={(event) => update.mutate({ enabled: event.target.checked })}
            />
            Burn into render
          </label>
        </div>

        <div className="subtitle-position">
          <label htmlFor="subtitle-position">
            <Captions />
            Position
          </label>
          <input
            id="subtitle-position"
            type="range"
            min={minPosition}
            max={maxPosition}
            step="0.005"
            value={position}
            disabled={!enabled}
            onChange={(event) => move(Number(event.target.value))}
            onPointerUp={commit}
            onKeyUp={commit}
            onBlur={commit}
          />
          <span>{Math.round(position * 100)}% from bottom</span>
        </div>

        <div className="row">
          <button
            className="btn sm"
            disabled={!enabled || isDefault || update.isPending}
            onClick={() => {
              move(defaultPosition);
              commit();
            }}
          >
            <RotateCcw />
            {isDefault ? "Default position" : "Reset to default"}
          </button>
        </div>

        <div className="muted subtitle-hint">
          Drag the caption in the preview, or use the slider. This placement is
          saved for this project only; every new project starts at the default.
          {project.stage === "DONE" ? " Re-render to apply it." : ""}
        </div>
        {update.error && <span className="banner compact">{update.error.message}</span>}
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
  const videoUrl = folder ? mediaUrl(`${folder}/final/${slug}.mp4`, project.final_video_version) : null;

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
              <Download />
              Download MP4
            </a>
          )}
        </div>
      </div>
      <TikTokPublish project={project} />
    </div>
  );
}

function TikTokPublish({ project }) {
  const { data: target } = useQuery({
    queryKey: ["tiktok-target", project.id],
    queryFn: () => api.tiktokPublishTarget(project.id),
  });
  const [caption, setCaption] = useState("");
  const [privacy, setPrivacy] = useState("SELF_ONLY");
  const [result, setResult] = useState(null);
  const [status, setStatus] = useState(null);

  useEffect(() => {
    if (target?.suggested_caption) setCaption(target.suggested_caption);
  }, [target?.suggested_caption]);

  const publish = useMutation({
    mutationFn: () =>
      api.tiktokPublish(project.id, { caption, privacy_level: privacy }),
    onSuccess: (data) => {
      setResult(data);
      setStatus(null);
    },
  });
  const check = useMutation({
    mutationFn: () => api.tiktokPublishStatus(project.id, result.publish_id),
    onSuccess: (data) => setStatus(data),
  });

  if (!target) return null;

  const blocked = !target.configured
    ? "TikTok is not configured in .env."
    : !target.group
    ? "This project has no content preset, so there is no account to post as."
    : !target.account
    ? `The "${target.group.name}" group has no TikTok account selected. Pick one in Settings.`
    : !target.video_ready
    ? "No finished video to publish yet."
    : "";

  return (
    <div className="panel-inset">
      <h3>Publish to TikTok</h3>
      {blocked ? (
        <div className="banner compact">{blocked}</div>
      ) : (
        <>
          <div className="key-row">
            <span>
              Posting as <strong>{target.account.display_name || target.account.open_id}</strong>
              {" via "}
              {target.group.name}
            </span>
            <span className="model-code">{Math.round(target.video_bytes / 1048576)} MB</span>
          </div>
          <div className="field">
            <label>Caption</label>
            <textarea value={caption} onChange={(e) => setCaption(e.target.value)} />
          </div>
          <div className="field">
            <label>Privacy</label>
            <select value={privacy} onChange={(e) => setPrivacy(e.target.value)}>
              {target.privacy_levels.map((level) => (
                <option key={level} value={level}>
                  {level}
                </option>
              ))}
            </select>
          </div>
          {publish.isError && <div className="banner compact">{String(publish.error.message)}</div>}
          {check.isError && <div className="banner compact">{String(check.error.message)}</div>}
          <div className="row">
            <button
              className="btn primary"
              disabled={publish.isPending || !caption.trim()}
              onClick={() => publish.mutate()}
            >
              <Upload />
              {publish.isPending ? "Uploading..." : "Publish"}
            </button>
            {result && (
              <button className="btn" disabled={check.isPending} onClick={() => check.mutate()}>
                <RefreshCw />
                {check.isPending ? "Checking..." : "Check status"}
              </button>
            )}
          </div>
          {result && (
            <div className="key-row">
              <span>Publish id</span>
              <span className="model-code">{result.publish_id}</span>
            </div>
          )}
          {status && (
            <div className="key-row">
              <span>Status</span>
              <span className="model-code">
                {status.status}
                {status.fail_reason ? ` (${status.fail_reason})` : ""}
              </span>
            </div>
          )}
        </>
      )}
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
  const regenAnimation = useMutation({
    mutationFn: () => api.regenSceneAnimation(projectId, scene.id),
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
  const animation = mediaUrl(scene.animation_path, mediaVersion);
  const audio = mediaUrl(scene.audio_path, mediaVersion);
  const isAnimation = scene.scene_type === "animation";
  const showClip = clip && scene.scene_type === "video";
  const showAnimation = animation && isAnimation;
  const busy = scene.status === "generating";
  const contextRefs = scene.context_refs || [];
  const continuityContext = scene.continuity_context || [];
  const excludedContextIds = scene.excluded_context_scene_ids || [];
  const imageVariants = scene.image_variants || [];
  const clipVariants = scene.clip_variants || [];
  const animationVariants = scene.animation_variants || [];
  const animationSpec = scene.animation_spec || null;
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
          ) : showAnimation ? (
            <video src={animation} muted loop playsInline
              onMouseOver={(e) => e.target.play()} onMouseOut={(e) => e.target.pause()} />
          ) : img ? (
            <img src={img} alt="" />
          ) : isAnimation ? (
            "animation renders at storyboard"
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
        {isAnimation && animationVariants.length > 1 && (
          <AssetVariants
            label="Animations"
            kind="clip"
            paths={animationVariants}
            activePath={scene.animation_path}
            pending={selectAsset.isPending || busy}
            onSelect={(path) => selectAsset.mutate({ kind: "animation", path })}
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
                      {ref.excluded ? <Undo2 /> : <X />}
                    </button>
                  </div>
                );
              })}
              {continuityContext
                .filter((item) => !contextRefs.some((ref) => ref.scene_number === item.source_scene))
                .map((item, idx) => (
                  <div key={`${item.source_scene}-${idx}`} className="context-ref missing">
                    <div className="context-ref-placeholder"><ImageOff /></div>
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
            {(animationSpec || isAnimation) && (
              <button
                className={isAnimation ? "on" : ""}
                onClick={() => save.mutate({ scene_type: "animation" })}
                title="Deterministic math/science animation synced to the narration"
              >
                Animation
              </button>
            )}
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
        {isAnimation && animationSpec && (
          <AnimationSpecEditor
            spec={animationSpec}
            pending={save.isPending || busy}
            onSave={(next) => save.mutate({ animation_spec: next })}
          />
        )}
        {audio && <AudioPlayer src={audio} />}
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
          {scene.approved && <Check />}
          {scene.approved ? "Approved" : "Approve"}
        </button>
        {img && (
          <button className="btn sm" disabled={regenImage.isPending} onClick={() => regenImage.mutate()}>
            <RefreshCw />
            Regen image
          </button>
        )}
        {audio && (
          <button className="btn sm" disabled={regenAudio.isPending} onClick={() => regenAudio.mutate()}>
            <RefreshCw />
            Regen audio
          </button>
        )}
        {scene.scene_type === "video" && img && (
          <button className="btn sm" disabled={regenClip.isPending} onClick={() => regenClip.mutate()}>
            <RefreshCw />
            Regen clip
          </button>
        )}
        {isAnimation && animationSpec && (
          <button className="btn sm" disabled={regenAnimation.isPending || busy} onClick={() => regenAnimation.mutate()}>
            <RefreshCw />
            Regen animation
          </button>
        )}
      </div>
    </div>
  );
}

function AnimationSpecEditor({ spec, pending, onSave }) {
  const [open, setOpen] = useState(false);
  const [code, setCode] = useState(spec.code || "");

  useEffect(() => {
    setCode(spec.code || "");
  }, [spec.code]);

  const dirty = code !== (spec.code || "");
  const lineCount = (spec.code || "").split("\n").length;

  return (
    <div className="anim-spec">
      <div className="anim-spec-head">
        <span className="tag">Manim code</span>
        {spec.title && <span className="anim-spec-title">{spec.title}</span>}
        <span className="anim-spec-lines">{lineCount} lines</span>
        <button type="button" className="btn ghost sm" onClick={() => setOpen((o) => !o)}>
          {open ? "Hide code" : "Edit code"}
        </button>
      </div>
      {open && (
        <div className="anim-spec-editor">
          <textarea
            value={code}
            onChange={(e) => setCode(e.target.value)}
            spellCheck={false}
            rows={16}
          />
          <div className="row" style={{ justifyContent: "flex-end", gap: 8 }}>
            <button
              className="btn sm"
              disabled={pending || !dirty}
              onClick={() => setCode(spec.code || "")}
            >
              Reset
            </button>
            <button
              className="btn sm primary"
              disabled={pending || !dirty}
              onClick={() => onSave({ code, title: spec.title || "" })}
            >
              Save code
            </button>
          </div>
          <p className="anim-spec-note">
            Full Manim: the body of construct(self). Sync to the voice with
            self.play_at(self.cue(&quot;phrase&quot;), ...), quoting the
            narration verbatim and in spoken order. After saving, click
            &ldquo;Regen animation&rdquo; to re-render.
          </p>
        </div>
      )}
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

