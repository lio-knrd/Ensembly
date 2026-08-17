import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { ArrowDown, ArrowUp, Plus, Sparkles } from "lucide-react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api.js";
import Modal from "../components/Modal.jsx";
import Loading from "../components/Loading.jsx";

const STATUSES = [
  ["suggested", "Suggested"],
  ["planned", "Planned"],
  ["in_progress", "In production"],
  ["completed", "Completed"],
  ["published", "Published"],
  ["skipped", "Skipped"],
];

export default function Ideas() {
  const [view, setView] = useState("plans");
  const [selectedPlanId, setSelectedPlanId] = useState("");
  const [planModal, setPlanModal] = useState(null);
  const qc = useQueryClient();
  const { data: plans = [] } = useQuery({
    queryKey: ["editorialPlans"],
    queryFn: api.listEditorialPlans,
  });

  useEffect(() => {
    if (!selectedPlanId && plans.length) setSelectedPlanId(plans[0].id);
    if (selectedPlanId && !plans.some((plan) => plan.id === selectedPlanId)) {
      setSelectedPlanId(plans[0]?.id || "");
    }
  }, [plans, selectedPlanId]);

  const invalidatePlans = () => {
    qc.invalidateQueries({ queryKey: ["editorialPlans"] });
    if (selectedPlanId) {
      qc.invalidateQueries({ queryKey: ["editorialPlan", selectedPlanId] });
    }
  };

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Editorial planning</h1>
          <p>Capture loose ideas, plan coherent series, and keep AI aware of what is already covered.</p>
        </div>
        {view === "plans" && (
          <button className="btn primary" onClick={() => setPlanModal({})}>
            <Plus />
            New plan
          </button>
        )}
      </div>

      <div className="editorial-tabs">
        <button className={view === "plans" ? "active" : ""} onClick={() => setView("plans")}>
          Series plans
        </button>
        <button className={view === "inbox" ? "active" : ""} onClick={() => setView("inbox")}>
          Global inbox
        </button>
      </div>

      {view === "inbox" ? (
        <IdeaInbox plans={plans} />
      ) : (
        <div className="editorial-layout">
          <PlanSidebar
            plans={plans}
            selectedId={selectedPlanId}
            onSelect={setSelectedPlanId}
          />
          {selectedPlanId ? (
            <PlanDetail
              planId={selectedPlanId}
              onEdit={(plan) => setPlanModal(plan)}
              onDeleted={() => {
                setSelectedPlanId("");
                invalidatePlans();
              }}
            />
          ) : (
            <div className="empty editorial-empty">
              Create a plan for a series, campaign, curriculum, or any ordered body of work.
            </div>
          )}
        </div>
      )}

      {planModal && (
        <PlanModal
          plan={planModal.id ? planModal : null}
          plans={plans}
          onClose={() => setPlanModal(null)}
          onSaved={(plan) => {
            setPlanModal(null);
            setSelectedPlanId(plan.id);
            invalidatePlans();
          }}
        />
      )}
    </>
  );
}

function IdeaInbox({ plans }) {
  const qc = useQueryClient();
  const navigate = useNavigate();
  const [text, setText] = useState("");
  const [duration, setDuration] = useState(75);
  const [planId, setPlanId] = useState("");
  const { data: ideas = [] } = useQuery({ queryKey: ["ideas"], queryFn: api.listIdeas });
  const invalidate = () => qc.invalidateQueries({ queryKey: ["ideas"] });
  const add = useMutation({
    mutationFn: () => api.createIdea({
      text,
      target_duration_seconds: Number(duration),
      plan_id: planId || null,
    }),
    onSuccess: () => {
      setText("");
      invalidate();
    },
  });
  const del = useMutation({ mutationFn: api.deleteIdea, onSuccess: invalidate });
  const convert = useMutation({
    mutationFn: (id) => api.convertIdea(id, { start: true }),
    onSuccess: (project) => {
      qc.invalidateQueries({ queryKey: ["projects"] });
      qc.invalidateQueries({ queryKey: ["editorialPlans"] });
      invalidate();
      navigate(`/project/${project.id}`);
    },
  });
  const planNames = Object.fromEntries(plans.map((plan) => [plan.id, plan.name]));

  return (
    <>
      <div className="panel inbox-composer">
        <input
          placeholder="A topic, hook, question, or half-formed idea..."
          value={text}
          onChange={(event) => setText(event.target.value)}
          onKeyDown={(event) => event.key === "Enter" && text.trim() && add.mutate()}
        />
        <select value={planId} onChange={(event) => setPlanId(event.target.value)}>
          <option value="">Global inbox</option>
          {plans.map((plan) => <option key={plan.id} value={plan.id}>{plan.name}</option>)}
        </select>
        <input
          type="number"
          min="15"
          max="600"
          value={duration}
          onChange={(event) => setDuration(event.target.value)}
        />
        <button className="btn primary" disabled={!text.trim() || add.isPending} onClick={() => add.mutate()}>
          Add
        </button>
      </div>
      {ideas.length === 0 ? (
        <div className="empty">No loose ideas yet.</div>
      ) : (
        <div className="list">
          {ideas.map((idea) => (
            <div className="list-row" key={idea.id}>
              <div className="grow">
                <h4>{idea.text}</h4>
                <div className="sub">
                  <span>{idea.target_duration_seconds}s</span>
                  <span className="sep" />
                  <span>{idea.plan_id ? planNames[idea.plan_id] || "Assigned plan" : "Global inbox"}</span>
                </div>
              </div>
              <button className="btn sm primary" onClick={() => convert.mutate(idea.id)}>
                Turn into project
              </button>
              <button className="btn sm danger" onClick={() => del.mutate(idea.id)}>Delete</button>
            </div>
          ))}
        </div>
      )}
    </>
  );
}

function PlanSidebar({ plans, selectedId, onSelect }) {
  const byParent = useMemo(() => {
    const result = {};
    for (const plan of plans) (result[plan.parent_plan_id || "root"] ||= []).push(plan);
    return result;
  }, [plans]);
  const renderBranch = (parentId = "root", depth = 0) =>
    (byParent[parentId] || []).map((plan) => (
      <div key={plan.id}>
        <button
          className={`plan-nav-item ${selectedId === plan.id ? "active" : ""}`}
          style={{ paddingLeft: 14 + depth * 16 }}
          onClick={() => onSelect(plan.id)}
        >
          <strong>{plan.name}</strong>
          <span>{[plan.platform_preset_name, plan.content_preset_name].filter(Boolean).join(" / ") || "Global"}</span>
        </button>
        {renderBranch(plan.id, depth + 1)}
      </div>
    ));
  return <aside className="plan-sidebar">{plans.length ? renderBranch() : <span>No plans yet.</span>}</aside>;
}

function PlanDetail({ planId, onEdit, onDeleted }) {
  const qc = useQueryClient();
  const navigate = useNavigate();
  const [workModal, setWorkModal] = useState(null);
  const [instruction, setInstruction] = useState("What should come next? Propose five logical works.");
  const [aiOverview, setAiOverview] = useState("");
  const { data: plan, isLoading } = useQuery({
    queryKey: ["editorialPlan", planId],
    queryFn: () => api.getEditorialPlan(planId),
  });
  const refresh = () => {
    qc.invalidateQueries({ queryKey: ["editorialPlan", planId] });
    qc.invalidateQueries({ queryKey: ["editorialPlans"] });
  };
  const suggest = useMutation({
    mutationFn: () => api.suggestEditorialItems(planId, { instruction, count: 5 }),
    onSuccess: (result) => {
      setAiOverview(result.overview);
      refresh();
    },
  });
  const update = useMutation({
    mutationFn: ({ id, body }) => api.updateEditorialItem(planId, id, body),
    onSuccess: refresh,
  });
  const remove = useMutation({
    mutationFn: (id) => api.deleteEditorialItem(planId, id),
    onSuccess: refresh,
  });
  const reorder = useMutation({
    mutationFn: (ids) => api.reorderEditorialItems(planId, ids),
    onSuccess: refresh,
  });
  const convert = useMutation({
    mutationFn: (id) => api.convertEditorialItem(planId, id),
    onSuccess: (project) => {
      qc.invalidateQueries({ queryKey: ["projects"] });
      refresh();
      navigate(`/project/${project.id}`);
    },
  });
  const deletePlan = useMutation({
    mutationFn: () => api.deleteEditorialPlan(planId),
    onSuccess: onDeleted,
  });

  if (isLoading || !plan) return <Loading full label="Loading plan" />;
  const items = plan.items || [];
  const move = (index, delta) => {
    const target = index + delta;
    if (target < 0 || target >= items.length) return;
    const ids = items.map((item) => item.id);
    [ids[index], ids[target]] = [ids[target], ids[index]];
    reorder.mutate(ids);
  };

  return (
    <section className="plan-detail">
      <div className="plan-detail-head">
        <div>
          <div className="scope-chips">
            <span>{plan.ordering_mode || "custom"} order</span>
            {plan.platform_preset_name && <span>{plan.platform_preset_name}</span>}
            {plan.content_preset_name && <span>{plan.content_preset_name}</span>}
          </div>
          <h2>{plan.name}</h2>
          <p>{plan.description || "No purpose documented yet."}</p>
        </div>
        <div className="row">
          <button className="btn sm" onClick={() => onEdit(plan)}>Edit plan</button>
          <button
            className="btn sm danger"
            onClick={() => window.confirm(`Delete "${plan.name}" and its timeline?`) && deletePlan.mutate()}
          >
            Delete
          </button>
        </div>
      </div>
      {plan.editorial_rules && (
        <div className="editorial-rules"><strong>Editorial rules</strong><p>{plan.editorial_rules}</p></div>
      )}

      <div className="ai-planner">
        <div>
          <strong>Ask the editorial planner</strong>
          <span>It sees the rules and compact coverage of every work below.</span>
        </div>
        <textarea value={instruction} onChange={(event) => setInstruction(event.target.value)} rows={2} />
        <button className="btn primary" disabled={!instruction.trim() || suggest.isPending} onClick={() => suggest.mutate()}>
          <Sparkles />
          {suggest.isPending ? "Planning..." : "Suggest next works"}
        </button>
      </div>
      {aiOverview && <div className="banner compact">{aiOverview}</div>}
      {suggest.isError && <div className="banner">{String(suggest.error.message)}</div>}

      <div className="timeline-head">
        <div><h3>Timeline</h3><span>{items.length} works</span></div>
        <button className="btn" onClick={() => setWorkModal({})}>
          <Plus />
          Add existing or planned work
        </button>
      </div>
      {items.length === 0 ? (
        <div className="empty">No works yet. Add an existing video or ask the AI what should come first.</div>
      ) : (
        <div className="editorial-timeline">
          {items.map((item, index) => (
            <EditorialCard
              key={item.id}
              item={item}
              index={index}
              total={items.length}
              onMove={move}
              onEdit={() => setWorkModal(item)}
              onStatus={(status) => update.mutate({ id: item.id, body: { status } })}
              onDelete={() => remove.mutate(item.id)}
              onConvert={() => convert.mutate(item.id)}
              onOpenProject={() => navigate(`/project/${item.project_id}`)}
            />
          ))}
        </div>
      )}
      {workModal && (
        <WorkModal
          planId={planId}
          item={workModal.id ? workModal : null}
          onClose={() => setWorkModal(null)}
          onSaved={() => {
            setWorkModal(null);
            refresh();
          }}
        />
      )}
    </section>
  );
}

function EditorialCard({
  item, index, total, onMove, onEdit, onStatus, onDelete, onConvert, onOpenProject,
}) {
  return (
    <article className={`editorial-card status-${item.status}`}>
      <div className="timeline-marker"><span>{index + 1}</span></div>
      <div className="editorial-card-body">
        <div className="editorial-card-title">
          <div>
            {item.part_group_title && (
              <span className="part-label">{item.part_group_title} / Part {item.part_number}</span>
            )}
            <h4>{item.title}</h4>
          </div>
          <select value={item.status} onChange={(event) => onStatus(event.target.value)}>
            {STATUSES.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
          </select>
        </div>
        {item.summary && <p>{item.summary}</p>}
        <div className="coverage-box">
          <strong>Coverage</strong>
          <span>{item.coverage_summary || "Not documented; AI cannot reliably avoid overlap yet."}</span>
        </div>
        {item.ai_rationale && <div className="ai-rationale">Why this comes next: {item.ai_rationale}</div>}
        <div className="editorial-card-meta">
          <span>{item.source_type}</span>
          <span>{item.target_duration_seconds}s rough length</span>
          {item.external_url && <a href={item.external_url} target="_blank" rel="noreferrer">External link</a>}
        </div>
        <div className="editorial-card-actions">
          <button className="btn sm icon" title="Move up" disabled={index === 0} onClick={() => onMove(index, -1)}>
            <ArrowUp />
          </button>
          <button className="btn sm icon" title="Move down" disabled={index === total - 1} onClick={() => onMove(index, 1)}>
            <ArrowDown />
          </button>
          <button className="btn sm" onClick={onEdit}>Edit context</button>
          {item.status === "suggested" && <button className="btn sm" onClick={() => onStatus("planned")}>Accept</button>}
          {item.project_id ? (
            <button className="btn sm primary" onClick={onOpenProject}>Open project</button>
          ) : item.status !== "published" && item.status !== "completed" ? (
            <button className="btn sm primary" onClick={onConvert}>Create project</button>
          ) : null}
          <button className="btn sm danger" onClick={onDelete}>Remove</button>
        </div>
      </div>
    </article>
  );
}

function PlanModal({ plan, plans, onClose, onSaved }) {
  const [form, setForm] = useState({
    name: plan?.name || "",
    description: plan?.description || "",
    editorial_rules: plan?.editorial_rules || "",
    ordering_mode: plan?.ordering_mode || "custom",
    parent_plan_id: plan?.parent_plan_id || "",
    platform_preset_id: plan?.platform_preset_id || "",
    content_preset_id: plan?.content_preset_id || "",
  });
  const { data: platforms = [] } = useQuery({ queryKey: ["platformPresets"], queryFn: api.listPlatformPresets });
  const { data: contents = [] } = useQuery({ queryKey: ["contentPresets"], queryFn: api.listContentPresets });
  const save = useMutation({
    mutationFn: () => {
      const body = {
        ...form,
        parent_plan_id: form.parent_plan_id || null,
        platform_preset_id: form.platform_preset_id || null,
        content_preset_id: form.content_preset_id || null,
      };
      return plan ? api.updateEditorialPlan(plan.id, body) : api.createEditorialPlan(body);
    },
    onSuccess: onSaved,
  });
  const field = (key) => ({
    value: form[key],
    onChange: (event) => setForm({ ...form, [key]: event.target.value }),
  });
  return (
    <Modal title={plan ? "Edit editorial plan" : "New editorial plan"} onClose={onClose}>
      <div className="field"><label>Name</label><input autoFocus {...field("name")} /></div>
      <div className="field"><label>Purpose / scope</label><textarea rows={3} {...field("description")} /></div>
      <div className="field">
        <label>Editorial rules</label>
        <textarea
          rows={6}
          placeholder="Define ordering, boundaries, allowed deviations, callbacks, depth, audience..."
          {...field("editorial_rules")}
        />
      </div>
      <div className="inline-fields">
        <div className="field">
          <label>Ordering</label>
          <select {...field("ordering_mode")}>
            <option value="custom">Custom</option>
            <option value="chronological">Chronological</option>
            <option value="thematic">Thematic</option>
            <option value="progressive">Progressive difficulty</option>
            <option value="release">Release schedule</option>
          </select>
        </div>
        <div className="field">
          <label>Parent plan</label>
          <select {...field("parent_plan_id")}>
            <option value="">None / top level</option>
            {plans.filter((candidate) => candidate.id !== plan?.id).map((candidate) => (
              <option key={candidate.id} value={candidate.id}>{candidate.name}</option>
            ))}
          </select>
        </div>
      </div>
      <div className="inline-fields">
        <div className="field">
          <label>Platform context</label>
          <select {...field("platform_preset_id")}>
            <option value="">Global / inherit</option>
            {platforms.map((preset) => <option key={preset.id} value={preset.id}>{preset.name}</option>)}
          </select>
        </div>
        <div className="field">
          <label>Content context</label>
          <select {...field("content_preset_id")}>
            <option value="">Global / inherit</option>
            {contents.map((preset) => <option key={preset.id} value={preset.id}>{preset.name}</option>)}
          </select>
        </div>
      </div>
      {save.isError && <div className="banner">{String(save.error.message)}</div>}
      <div className="modal-actions">
        <button className="btn ghost" onClick={onClose}>Cancel</button>
        <button className="btn primary" disabled={!form.name.trim() || save.isPending} onClick={() => save.mutate()}>
          Save plan
        </button>
      </div>
    </Modal>
  );
}

function WorkModal({ planId, item, onClose, onSaved }) {
  const [form, setForm] = useState({
    title: item?.title || "",
    summary: item?.summary || "",
    coverage_summary: item?.coverage_summary || "",
    notes: item?.notes || "",
    status: item?.status || "published",
    source_type: item?.source_type || "external",
    external_url: item?.external_url || "",
    target_duration_seconds: item?.target_duration_seconds || 75,
    part_group_title: item?.part_group_title || "",
    part_number: item?.part_number || "",
  });
  const save = useMutation({
    mutationFn: () => {
      const body = {
        ...form,
        target_duration_seconds: Number(form.target_duration_seconds),
        part_number: form.part_number ? Number(form.part_number) : null,
      };
      return item
        ? api.updateEditorialItem(planId, item.id, body)
        : api.createEditorialItem(planId, body);
    },
    onSuccess: onSaved,
  });
  const field = (key) => ({
    value: form[key],
    onChange: (event) => setForm({ ...form, [key]: event.target.value }),
  });
  return (
    <Modal title={item ? "Edit work context" : "Add work"} onClose={onClose}>
      <div className="field"><label>Title</label><input autoFocus {...field("title")} /></div>
      <div className="field"><label>Short summary</label><textarea rows={3} {...field("summary")} /></div>
      <div className="field">
        <label>Coverage summary</label>
        <textarea
          rows={4}
          placeholder="What exactly is covered? Where does it begin/end? What remains for later?"
          {...field("coverage_summary")}
        />
      </div>
      <div className="inline-fields">
        <div className="field">
          <label>Status</label>
          <select {...field("status")}>{STATUSES.map(([v, l]) => <option key={v} value={v}>{l}</option>)}</select>
        </div>
        {!item && (
          <div className="field">
            <label>Source</label>
            <select {...field("source_type")}>
              <option value="external">External production</option>
              <option value="manual">Planned manually</option>
            </select>
          </div>
        )}
      </div>
      <div className="field"><label>External URL (optional)</label><input {...field("external_url")} /></div>
      <div className="inline-fields">
        <div className="field"><label>Part group</label><input placeholder="e.g. Titanomachy" {...field("part_group_title")} /></div>
        <div className="field"><label>Part number</label><input type="number" min="1" {...field("part_number")} /></div>
        <div className="field"><label>Rough seconds</label><input type="number" min="15" max="600" {...field("target_duration_seconds")} /></div>
      </div>
      <div className="field"><label>Internal notes</label><textarea rows={2} {...field("notes")} /></div>
      {save.isError && <div className="banner">{String(save.error.message)}</div>}
      <div className="modal-actions">
        <button className="btn ghost" onClick={onClose}>Cancel</button>
        <button className="btn primary" disabled={!form.title.trim() || save.isPending} onClick={() => save.mutate()}>
          Save work
        </button>
      </div>
    </Modal>
  );
}
