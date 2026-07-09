import { useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, mediaUrl } from "../api.js";
import Modal from "../components/Modal.jsx";

export default function Characters() {
  const [showNew, setShowNew] = useState(false);
  const { data: characters = [], isLoading } = useQuery({
    queryKey: ["characters"],
    queryFn: api.listCharacters,
  });
  const readyCount = characters.filter((c) => defaultForm(c)?.reference_image_path || c.reference_image_path).length;
  const formCount = characters.reduce((sum, c) => sum + formsFor(c).length, 0);

  return (
    <>
      <div className="page-head character-page-head">
        <div>
          <h1>Character library</h1>
          <p>Global character sheets used by projects for identity consistency.</p>
          <div className="character-stats">
            <span>{characters.length} characters</span>
            <span>{formCount} forms</span>
            <span>{readyCount} default sheets ready</span>
          </div>
        </div>
        <button className="btn primary" onClick={() => setShowNew(true)}>
          + Add character
        </button>
      </div>

      {isLoading ? (
        <div className="empty">Loading...</div>
      ) : characters.length === 0 ? (
        <div className="character-empty">
          <strong>No characters yet.</strong>
          <span>Add one to create a reusable reference sheet.</span>
        </div>
      ) : (
        <div className="character-list">
          {characters.map((c) => (
            <CharacterCard key={c.id} character={c} />
          ))}
        </div>
      )}

      {showNew && <CharacterModal onClose={() => setShowNew(false)} />}
    </>
  );
}

function CharacterCard({ character }) {
  const qc = useQueryClient();
  const [editing, setEditing] = useState(false);
  const fileRef = useRef();
  const invalidate = () => qc.invalidateQueries({ queryKey: ["characters"] });

  const del = useMutation({ mutationFn: () => api.deleteCharacter(character.id), onSuccess: invalidate });
  const regen = useMutation({ mutationFn: () => api.regenReference(character.id), onSuccess: invalidate });
  const upload = useMutation({
    mutationFn: (file) => api.uploadReference(character.id, file),
    onSuccess: invalidate,
  });

  const forms = formsFor(character);
  const primaryForm = defaultForm(character);
  const img = mediaUrl(
    primaryForm?.reference_image_path || character.reference_image_path,
    primaryForm?.reference_version ?? character.reference_version
  );
  const hasSheet = Boolean(primaryForm?.reference_image_path || character.reference_image_path);
  const hasDescription = Boolean(character.description?.trim());
  const styleText = (primaryForm?.reference_style_prompt || character.reference_style_prompt || "").trim();
  const promptText = (primaryForm?.reference_prompt || character.reference_prompt || "").trim();
  const variantCount = [
    ...(character.variant_paths || []),
    ...forms.flatMap((form) => form.variant_paths || []),
  ].length;
  const sheetButtonText = hasSheet
    ? "Regenerate default sheet"
    : "Generate default sheet";

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
          {hasSheet ? "Default ready" : "No default"}
        </div>
      </div>

      <div className="char-body">
        <div className="char-title-row">
          <div>
            <h3>{character.name}</h3>
            <div className="char-used">
              {character.used_in_projects} project{character.used_in_projects === 1 ? "" : "s"} / {forms.length} form{forms.length === 1 ? "" : "s"} / {variantCount} variant{variantCount === 1 ? "" : "s"}
            </div>
          </div>
          <button className="btn sm" onClick={() => setEditing(true)}>
            Edit
          </button>
        </div>

        <p className="char-description">{character.description || "No description yet."}</p>

        <div className="char-forms">
          {forms.map((form) => (
            <FormPill key={form.id} form={form} />
          ))}
        </div>

        <div className="char-sheet-meta">
          <div>
            <span>Default style</span>
            <strong title={styleText || ""}>
              {styleText || (hasSheet ? "No stored style" : "None yet")}
            </strong>
          </div>
          <div>
            <span>Sheet prompt source</span>
            <strong title={promptText || ""}>
              {promptText ? "Saved generation prompt" : "Character description"}
            </strong>
          </div>
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
            {regen.isPending ? "Generating new sheet..." : sheetButtonText}
          </button>
          <button className="btn" onClick={() => fileRef.current.click()}>
            Upload sheet
          </button>
          <button className="btn danger" onClick={() => del.mutate()}>
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
        <CharacterModal character={character} onClose={() => setEditing(false)} />
      )}
    </div>
  );
}

function FormPill({ form }) {
  const img = mediaUrl(form.reference_image_path, form.reference_version);
  const label = form.is_default ? "Default" : form.name || form.state || "Form";
  const detail = form.state || form.description || "";
  const variants = form.variant_paths?.length || 0;

  return (
    <div className={"char-form-pill" + (form.reference_image_path ? " ready" : " missing")}>
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
      <em>{variants} variant{variants === 1 ? "" : "s"}</em>
    </div>
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
          variant_paths: character.variant_paths || [],
          is_default: true,
        },
      ];
  return [...forms].sort((a, b) => Number(b.is_default) - Number(a.is_default) || (a.name || "").localeCompare(b.name || ""));
}

function defaultForm(character) {
  return formsFor(character).find((form) => form.is_default) || formsFor(character)[0];
}

function CharacterModal({ character, onClose }) {
  const qc = useQueryClient();
  const editing = !!character;
  const [name, setName] = useState(character?.name || "");
  const [description, setDescription] = useState(character?.description || "");
  const [generate, setGenerate] = useState(false);
  const invalidate = () => qc.invalidateQueries({ queryKey: ["characters"] });

  const save = useMutation({
    mutationFn: () =>
      editing
        ? api.updateCharacter(character.id, { name, description })
        : api.createCharacter({ name, description, generate_reference: generate }),
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
