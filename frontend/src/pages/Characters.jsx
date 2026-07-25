import { useEffect, useRef, useState } from "react";
import { Pencil, Plus, RefreshCw, Trash2, Upload } from "lucide-react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, mediaUrl } from "../api.js";
import Modal from "../components/Modal.jsx";
import Loading from "../components/Loading.jsx";

const UNGROUPED = "none";

export default function Characters() {
  const [showNew, setShowNew] = useState(false);
  const [groupId, setGroupId] = useState(null);
  const { data: characters = [], isLoading } = useQuery({
    queryKey: ["characters"],
    queryFn: () => api.listCharacters(),
  });
  const { data: groups = [] } = useQuery({
    queryKey: ["content-presets"],
    queryFn: api.listContentPresets,
  });

  const countFor = (id) =>
    characters.filter((c) => (c.content_preset_id || UNGROUPED) === id).length;
  const ungroupedCount = countFor(UNGROUPED);
  const tabs = [
    ...groups.map((g) => ({ id: g.id, name: g.name })),
    ...(ungroupedCount ? [{ id: UNGROUPED, name: "Ungrouped" }] : []),
  ];
  // Land on the default group until the creator picks another tab.
  const activeId =
    groupId && tabs.some((t) => t.id === groupId)
      ? groupId
      : (groups.find((g) => g.is_default) || groups[0])?.id || UNGROUPED;
  const activeGroup = groups.find((g) => g.id === activeId) || null;
  const shown = characters.filter(
    (c) => (c.content_preset_id || UNGROUPED) === activeId
  );
  const readyCount = shown.filter((c) => defaultForm(c)?.reference_image_path || c.reference_image_path).length;
  const formCount = shown.reduce((sum, c) => sum + formsFor(c).length, 0);

  return (
    <>
      <div className="page-head character-page-head">
        <div>
          <h1>Character library</h1>
          <p>
            Character sheets belong to one group, so a project only ever casts
            from its own group.
          </p>
          <div className="character-stats">
            <span>{shown.length} characters</span>
            <span>{formCount} forms</span>
            <span>{readyCount} default sheets ready</span>
          </div>
        </div>
        <button className="btn primary" onClick={() => setShowNew(true)}>
          <Plus />
          Add character
        </button>
      </div>

      {tabs.length > 1 && (
        <div className="editorial-tabs">
          {tabs.map((tab) => (
            <button
              key={tab.id}
              className={tab.id === activeId ? "active" : ""}
              onClick={() => setGroupId(tab.id)}
            >
              {tab.name} ({countFor(tab.id)})
            </button>
          ))}
        </div>
      )}

      {isLoading ? (
        <Loading full />
      ) : shown.length === 0 ? (
        <div className="character-empty">
          <strong>No characters in {activeGroup?.name || "this group"} yet.</strong>
          <span>Add one to create a reusable reference sheet.</span>
        </div>
      ) : (
        <div className="character-list">
          {shown.map((c) => (
            <CharacterCard key={c.id} character={c} groups={groups} />
          ))}
        </div>
      )}

      {showNew && (
        <CharacterModal
          groups={groups}
          defaultGroupId={activeId === UNGROUPED ? "" : activeId}
          onClose={() => setShowNew(false)}
        />
      )}
    </>
  );
}

function CharacterCard({ character, groups }) {
  const qc = useQueryClient();
  const [editing, setEditing] = useState(false);
  const fileRef = useRef();
  const invalidate = () => qc.invalidateQueries({ queryKey: ["characters"] });
  const forms = formsFor(character);
  const fallbackForm = defaultForm(character);
  const [selectedFormId, setSelectedFormId] = useState(fallbackForm?.id || "");
  const selectedForm =
    forms.find((form) => form.id === selectedFormId) || fallbackForm;

  useEffect(() => {
    if (!forms.some((form) => form.id === selectedFormId)) {
      setSelectedFormId(fallbackForm?.id || "");
    }
  }, [forms, fallbackForm, selectedFormId]);

  const del = useMutation({ mutationFn: () => api.deleteCharacter(character.id), onSuccess: invalidate });
  const regen = useMutation({
    mutationFn: () =>
      selectedForm?.id && !selectedForm.id.endsWith("-default")
        ? api.regenFormReference(character.id, selectedForm.id)
        : api.regenReference(character.id),
    onSuccess: invalidate,
  });
  const selectSheet = useMutation({
    mutationFn: (path) =>
      api.selectReference(character.id, {
        path,
        form_id:
          selectedForm?.id && !selectedForm.id.endsWith("-default")
            ? selectedForm.id
            : null,
      }),
    onSuccess: invalidate,
  });
  const upload = useMutation({
    mutationFn: (file) =>
      selectedForm?.id && !selectedForm.id.endsWith("-default")
        ? api.uploadFormReference(character.id, selectedForm.id, file)
        : api.uploadReference(character.id, file),
    onSuccess: invalidate,
  });

  const img = mediaUrl(
    selectedForm?.reference_image_path,
    selectedForm?.reference_version
  );
  const hasSheet = Boolean(selectedForm?.reference_image_path);
  const descriptionText = (selectedForm?.description || character.description || "").trim();
  const hasDescription = Boolean(descriptionText);
  const styleText = (selectedForm?.reference_style_prompt || "").trim();
  const promptText = (selectedForm?.reference_prompt || "").trim();
  const formLabel = selectedForm?.is_default
    ? "Default"
    : selectedForm?.name || selectedForm?.state || "Form";
  const allVariantPaths = Array.from(new Set([
    ...(character.reference_variants || []),
    ...forms.flatMap((form) => form.reference_variants || []),
  ]));
  const selectedVariants = Array.from(new Set([
    ...(selectedForm?.reference_variants || []),
    ...(selectedForm?.reference_image_path
      ? [selectedForm.reference_image_path]
      : []),
  ]));
  const variantCount = allVariantPaths.length;
  const sheetButtonText = hasSheet
    ? `Regenerate ${formLabel}`
    : `Generate ${formLabel}`;

  return (
    <div className="char-card">
      <div className="char-media">
        <div
          className="char-thumb"
          style={img ? { backgroundImage: `url(${img})` } : undefined}
        >
          {!img && <span>No sheet</span>}
        </div>
        <div className={"char-sheet-state " + (hasSheet ? "ready" : "missing")}>
          {hasSheet ? `${formLabel} ready` : `No ${formLabel} sheet`}
        </div>
        {selectedVariants.length > 1 && (
          <div className="sheet-variant-list char-sheet-variants">
            {selectedVariants.map((path, index) => (
              <button
                type="button"
                key={path}
                className={"sheet-variant" + (path === selectedForm?.reference_image_path ? " active" : "")}
                disabled={selectSheet.isPending || path === selectedForm?.reference_image_path}
                onClick={() => selectSheet.mutate(path)}
                title={`${formLabel} sheet version ${index + 1}`}
              >
                <img src={mediaUrl(path)} alt="" />
              </button>
            ))}
          </div>
        )}
      </div>

      <div className="char-body">
        <div className="char-title-row">
          <div>
            <div className="scope-chips">
              <span>{character.content_preset_name || "Ungrouped"}</span>
            </div>
            <h3>{character.name}</h3>
            <div className="char-used">
              {character.used_in_projects} project{character.used_in_projects === 1 ? "" : "s"} / {forms.length} form{forms.length === 1 ? "" : "s"} / {variantCount} sheet version{variantCount === 1 ? "" : "s"}
            </div>
          </div>
          <button className="btn sm" onClick={() => setEditing(true)}>
            <Pencil />
            Edit
          </button>
        </div>

        <p className="char-description">
          {descriptionText || `No description for ${formLabel} yet.`}
        </p>

        <div className="char-forms">
          {forms.map((form) => (
            <FormPill
              key={form.id}
              form={form}
              selected={form.id === selectedForm?.id}
              onSelect={() => setSelectedFormId(form.id)}
            />
          ))}
        </div>

        <div className="char-sheet-meta">
          <div>
            <span>{formLabel} style</span>
            <strong title={styleText || ""}>
              {styleText || (hasSheet ? "No stored style" : "None yet")}
            </strong>
          </div>
          <div>
            <span>Selected form</span>
            <strong>{formLabel}</strong>
          </div>
        </div>
        <div className="char-prompt">
          <span>Generation prompt</span>
          <p>{promptText || "No saved prompt yet; the form description will be used."}</p>
        </div>

        {regen.isError && <div className="banner compact">{String(regen.error.message)}</div>}
        {del.isError && <div className="banner compact">{String(del.error.message)}</div>}
        {upload.isError && <div className="banner compact">{String(upload.error.message)}</div>}

        <div className="char-actions">
          <button
            className="btn"
            disabled={regen.isPending || !hasDescription}
            onClick={() => regen.mutate()}
            title={!hasDescription ? "Add a description before generating a sheet." : undefined}
          >
            <RefreshCw />
            {regen.isPending ? "Generating new sheet..." : sheetButtonText}
          </button>
          <button className="btn" onClick={() => fileRef.current.click()}>
            <Upload />
            Upload sheet
          </button>
          <button className="btn danger" onClick={() => del.mutate()}>
            <Trash2 />
            Delete
          </button>
          <input
            ref={fileRef}
            type="file"
            accept="image/*"
            hidden
            onChange={(e) => e.target.files[0] && upload.mutate(e.target.files[0])}
          />
        </div>
      </div>

      {editing && (
        <CharacterModal
          character={character}
          groups={groups}
          onClose={() => setEditing(false)}
        />
      )}
    </div>
  );
}

function FormPill({ form, selected, onSelect }) {
  const img = mediaUrl(form.reference_image_path, form.reference_version);
  const label = form.is_default ? "Default" : form.name || form.state || "Form";
  const detail = form.state || form.description || "";
  const variants = form.reference_variants?.length || 0;

  return (
    <button
      type="button"
      className={
        "char-form-pill" +
        (form.reference_image_path ? " ready" : " missing") +
        (selected ? " selected" : "")
      }
      onClick={onSelect}
      aria-pressed={selected}
    >
      <div
        className="char-form-thumb"
        style={img ? { backgroundImage: `url(${img})` } : undefined}
      >
        {!img && "No ref"}
      </div>
      <div className="char-form-copy">
        <strong>{label}</strong>
        <span title={detail}>{detail || (form.is_default ? "Standard appearance" : "No notes")}</span>
      </div>
      <em>{variants} version{variants === 1 ? "" : "s"}</em>
    </button>
  );
}

function formsFor(character) {
  const forms = character.forms?.length
    ? character.forms
    : [
        {
          id: `${character.id}-default`,
          name: "Default",
          state: "",
          description: character.description || "",
          reference_image_path: character.reference_image_path,
          reference_prompt: character.reference_prompt,
          reference_style_prompt: character.reference_style_prompt,
          reference_version: character.reference_version,
          reference_variants: character.reference_variants || [],
          variant_paths: character.variant_paths || [],
          is_default: true,
        },
      ];
  return [...forms].sort((a, b) => Number(b.is_default) - Number(a.is_default) || (a.name || "").localeCompare(b.name || ""));
}

function defaultForm(character) {
  return formsFor(character).find((form) => form.is_default) || formsFor(character)[0];
}

function CharacterModal({ character, groups = [], defaultGroupId = "", onClose }) {
  const qc = useQueryClient();
  const editing = !!character;
  const [name, setName] = useState(character?.name || "");
  const [description, setDescription] = useState(character?.description || "");
  const [contentPresetId, setContentPresetId] = useState(
    editing ? character.content_preset_id || "" : defaultGroupId
  );
  const [generate, setGenerate] = useState(false);
  const invalidate = () => qc.invalidateQueries({ queryKey: ["characters"] });

  const save = useMutation({
    mutationFn: () =>
      editing
        ? api.updateCharacter(character.id, {
            name,
            description,
            content_preset_id: contentPresetId || null,
          })
        : api.createCharacter({
            name,
            description,
            content_preset_id: contentPresetId || null,
            generate_reference: generate,
          }),
    onSuccess: () => {
      invalidate();
      onClose();
    },
  });

  return (
    <Modal title={editing ? "Edit character" : "Add character"} onClose={onClose}>
      <div className="field">
        <label>Name</label>
        <input autoFocus value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. Zeus" />
      </div>
      <div className="field">
        <label>Group</label>
        <select
          value={contentPresetId}
          onChange={(e) => setContentPresetId(e.target.value)}
        >
          {groups.map((group) => (
            <option key={group.id} value={group.id}>
              {group.name}
            </option>
          ))}
          <option value="">Ungrouped</option>
        </select>
      </div>
      <div className="field">
        <label>Description (appearance notes, used to build prompts)</label>
        <textarea
          value={description}
          onChange={(e) => setDescription(e.target.value)}
          placeholder="King of the gods, silver beard, storm-grey robes, holds a lightning bolt..."
        />
      </div>
      {!editing && (
        <label className="check-row">
          <input
            type="checkbox"
            checked={generate}
            onChange={(e) => setGenerate(e.target.checked)}
          />
          Generate a character sheet from this description now
        </label>
      )}
      {save.isError && <div className="banner">{String(save.error.message)}</div>}
      <div className="modal-actions">
        <button className="btn ghost" onClick={onClose}>
          Cancel
        </button>
        <button
          className="btn primary"
          disabled={!name.trim() || save.isPending}
          onClick={() => save.mutate()}
        >
          {save.isPending ? "Saving..." : editing ? "Save" : "Add"}
        </button>
      </div>
    </Modal>
  );
}
