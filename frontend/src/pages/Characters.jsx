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
  const readyCount = characters.filter((c) => c.reference_image_path).length;

  return (
    <>
      <div className="page-head character-page-head">
        <div>
          <h1>Character library</h1>
          <p>Global character sheets used by projects for identity consistency.</p>
          <div className="character-stats">
            <span>{characters.length} characters</span>
            <span>{readyCount} sheets ready</span>
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
        <div className="character-grid">
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

  const img = mediaUrl(character.reference_image_path);
  const hasSheet = Boolean(character.reference_image_path);
  const hasDescription = Boolean(character.description?.trim());
  const styleText = character.reference_style_prompt?.trim();
  const promptText = character.reference_prompt?.trim();
  const sheetButtonText = hasSheet
    ? "Regenerate character sheet from description"
    : "Generate character sheet from description";

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
          {hasSheet ? "Sheet ready" : "Missing sheet"}
        </div>
      </div>

      <div className="char-body">
        <div className="char-title-row">
          <div>
            <h3>{character.name}</h3>
            <div className="char-used">
              Used in {character.used_in_projects} project
              {character.used_in_projects === 1 ? "" : "s"}
            </div>
          </div>
          <button className="btn sm" onClick={() => setEditing(true)}>
            Edit
          </button>
        </div>

        <p className="char-description">{character.description || "No description yet."}</p>

        <div className="char-sheet-meta">
          <div>
            <span>Generated style</span>
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
            className="btn primary"
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
