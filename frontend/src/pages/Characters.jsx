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

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Character library</h1>
          <p>Global characters — projects reference them; images stay identity-locked.</p>
        </div>
        <button className="btn primary" onClick={() => setShowNew(true)}>
          + Add character
        </button>
      </div>

      {isLoading ? (
        <div className="empty">Loading…</div>
      ) : characters.length === 0 ? (
        <div className="empty">No characters yet.</div>
      ) : (
        <div className="grid">
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

  return (
    <div className="char-card">
      <div
        className="char-thumb"
        style={img ? { backgroundImage: `url(${img})` } : undefined}
      >
        {!img && "🗿"}
      </div>
      <div className="char-body">
        <h3>{character.name}</h3>
        <p>{character.description || "No description."}</p>
        <div className="char-used" style={{ marginBottom: 10 }}>
          Used in {character.used_in_projects} project
          {character.used_in_projects === 1 ? "" : "s"}
        </div>
        <div className="row" style={{ flexWrap: "wrap", gap: 6 }}>
          <button className="btn sm" onClick={() => setEditing(true)}>
            Edit
          </button>
          <button className="btn sm" disabled={regen.isPending} onClick={() => regen.mutate()}>
            {regen.isPending ? "…" : "Generate ref"}
          </button>
          <button className="btn sm" onClick={() => fileRef.current.click()}>
            Upload
          </button>
          <button className="btn sm danger" onClick={() => del.mutate()}>
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
          placeholder="King of the gods, silver beard, storm-grey robes, holds a lightning bolt…"
        />
      </div>
      {!editing && (
        <label className="row" style={{ fontSize: 13.5, cursor: "pointer" }}>
          <input
            type="checkbox"
            style={{ width: "auto" }}
            checked={generate}
            onChange={(e) => setGenerate(e.target.checked)}
          />
          Generate a reference image from the description now
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
          {save.isPending ? "Saving…" : editing ? "Save" : "Add"}
        </button>
      </div>
    </Modal>
  );
}
