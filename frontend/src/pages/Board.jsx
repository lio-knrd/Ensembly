import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { Plus } from "lucide-react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, mediaUrl } from "../api.js";
import { COLUMNS, columnForStage } from "../stages.js";
import StatusPill from "../components/StatusPill.jsx";
import Modal from "../components/Modal.jsx";
import Loading from "../components/Loading.jsx";

export default function Board() {
  const [showNew, setShowNew] = useState(false);
  const { data: projects = [], isLoading } = useQuery({
    queryKey: ["projects"],
    queryFn: api.listProjects,
  });

  const byColumn = Object.fromEntries(COLUMNS.map((c) => [c.key, []]));
  for (const p of projects) byColumn[columnForStage(p.stage)].push(p);

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Board</h1>
          <p>Every project, across the pipeline.</p>
        </div>
        <button className="btn primary" onClick={() => setShowNew(true)}>
          <Plus />
          New project
        </button>
      </div>

      {isLoading ? (
        <Loading full />
      ) : projects.length === 0 ? (
        <div className="empty">
          No projects yet. Create one to start the pipeline.
        </div>
      ) : (
        <div className="board">
          {COLUMNS.map((col) => (
            <div className="column" key={col.key}>
              <div className="column-head">
                <h3>{col.label}</h3>
                <span className="column-count">{byColumn[col.key].length}</span>
              </div>
              <div className="column-body">
                {byColumn[col.key].map((p) => (
                  <ProjectCard key={p.id} project={p} />
                ))}
              </div>
            </div>
          ))}
        </div>
      )}

      {showNew && <NewProjectModal onClose={() => setShowNew(false)} />}
    </>
  );
}

function ProjectCard({ project }) {
  const navigate = useNavigate();
  const qc = useQueryClient();
  const thumb = mediaUrl(project.thumbnail, project.thumbnail_version);
  const del = useMutation({
    mutationFn: () => api.deleteProject(project.id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["projects"] }),
  });

  const deleteProject = (event) => {
    event.stopPropagation();
    const ok = window.confirm(`Delete "${project.title}"? This removes it from the board.`);
    if (ok) del.mutate();
  };

  return (
    <div className="card" onClick={() => navigate(`/project/${project.id}`)}>
      <div
        className="card-thumb"
        style={thumb ? { backgroundImage: `url(${thumb})` } : undefined}
      >
        {!thumb && "no preview yet"}
      </div>
      <div className="card-body">
        <p className="card-title">{project.title}</p>
        <div className="card-meta">
          <StatusPill stage={project.stage} />
          <span className="card-duration">{project.target_duration_seconds}s</span>
        </div>
        <div className="card-actions">
          <button
            className="btn danger sm"
            disabled={del.isPending}
            onClick={deleteProject}
          >
            {del.isPending ? "Deleting..." : "Delete"}
          </button>
        </div>
      </div>
    </div>
  );
}

function NewProjectModal({ onClose }) {
  const qc = useQueryClient();
  const navigate = useNavigate();
  const [title, setTitle] = useState("");
  const [topic, setTopic] = useState("");
  const [duration, setDuration] = useState(75);
  const [splitPlan, setSplitPlan] = useState(null);

  const { data: platforms = [] } = useQuery({
    queryKey: ["platformPresets"],
    queryFn: api.listPlatformPresets,
  });
  const { data: contents = [] } = useQuery({
    queryKey: ["contentPresets"],
    queryFn: api.listContentPresets,
  });
  const [platformId, setPlatformId] = useState("");
  const [contentId, setContentId] = useState("");

  const projectInput = () => ({
    title,
    topic_prompt: topic,
    target_duration_seconds: Number(duration),
    platform_preset_id: platformId || null,
    content_preset_id: contentId || null,
  });
  const finish = (project) => {
    qc.invalidateQueries({ queryKey: ["projects"] });
    onClose();
    navigate(`/project/${project.id}`);
  };
  const create = useMutation({
    mutationFn: (resolvedTitle) =>
      api.createProject({
        ...projectInput(),
        title: resolvedTitle || title || topic,
        title_is_custom: Boolean(title.trim()),
      }),
    onSuccess: finish,
  });
  const analyze = useMutation({
    mutationFn: () => api.analyzeProjectScope(projectInput()),
    onSuccess: (plan) => {
      if (plan.split_recommended) {
        setSplitPlan(plan);
      } else {
        create.mutate(title || plan.suggested_title || topic);
      }
    },
  });
  const createSplit = useMutation({
    mutationFn: () => api.createSplitProjects({ ...projectInput(), analysis: splitPlan }),
    onSuccess: ({ projects }) => finish(projects[0]),
  });

  if (splitPlan) {
    const splitError = create.error || createSplit.error;
    return (
      <Modal title="Make this a two-part series?" onClose={onClose}>
        <p>{splitPlan.reason}</p>
        <div className="split-preview">
          <div>
            <strong>Part 1</strong>
            <span>{splitPlan.part_1.focus}</span>
          </div>
          <div>
            <strong>Part 2</strong>
            <span>{splitPlan.part_2.focus}</span>
          </div>
        </div>
        {splitError && <div className="banner">{String(splitError.message)}</div>}
        <div className="modal-actions">
          <button
            className="btn ghost"
            disabled={create.isPending || createSplit.isPending}
            onClick={() => create.mutate(title || splitPlan.suggested_title || topic)}
          >
            No, keep one video
          </button>
          <button
            className="btn primary"
            disabled={create.isPending || createSplit.isPending}
            onClick={() => createSplit.mutate()}
          >
            {createSplit.isPending ? "Creating both..." : "Yes, create both parts"}
          </button>
        </div>
      </Modal>
    );
  }

  return (
    <Modal title="New project" onClose={onClose}>
      <div className="field">
        <label>Topic / idea</label>
        <input
          autoFocus
          placeholder="e.g. Theseus and the Minotaur"
          value={topic}
          onChange={(e) => setTopic(e.target.value)}
        />
      </div>
      <div className="field">
        <label>Working title (optional)</label>
        <input
          placeholder="AI suggests one if left blank"
          value={title}
          onChange={(e) => setTitle(e.target.value)}
        />
      </div>
      <div className="inline-fields">
        <div className="field">
          <label>Target duration (seconds)</label>
          <input
            type="number"
            min="15"
            max="600"
            value={duration}
            onChange={(e) => setDuration(e.target.value)}
          />
        </div>
      </div>
      <div className="inline-fields">
        <div className="field">
          <label>Platform preset</label>
          <select value={platformId} onChange={(e) => setPlatformId(e.target.value)}>
            <option value="">Default</option>
            {platforms.map((p) => (
              <option key={p.id} value={p.id}>
                {p.name}
                {p.is_default ? " (default)" : ""}
              </option>
            ))}
          </select>
        </div>
        <div className="field">
          <label>Content preset</label>
          <select value={contentId} onChange={(e) => setContentId(e.target.value)}>
            <option value="">Default</option>
            {contents.map((c) => (
              <option key={c.id} value={c.id}>
                {c.name}
                {c.is_default ? " (default)" : ""}
              </option>
            ))}
          </select>
        </div>
      </div>
      {(analyze.isError || create.isError) && (
        <div className="banner">{String((analyze.error || create.error).message)}</div>
      )}
      <div className="modal-actions">
        <button className="btn ghost" onClick={onClose}>
          Cancel
        </button>
        <button
          className="btn primary"
          disabled={!topic.trim() || analyze.isPending || create.isPending}
          onClick={() => analyze.mutate()}
        >
          {analyze.isPending
            ? "Checking scope..."
            : create.isPending
              ? "Creating..."
              : "Create & generate script"}
        </button>
      </div>
    </Modal>
  );
}
