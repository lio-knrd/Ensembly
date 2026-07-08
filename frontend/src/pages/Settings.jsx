import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api.js";

export default function Settings() {
  const qc = useQueryClient();
  const { data: settings } = useQuery({ queryKey: ["settings"], queryFn: api.getSettings });
  const [duration, setDuration] = useState("");
  const [imageModel, setImageModel] = useState("");
  useEffect(() => {
    if (settings) setDuration(settings.default_duration_seconds);
  }, [settings?.default_duration_seconds]);
  useEffect(() => {
    if (settings) setImageModel(settings.models.image);
  }, [settings?.models?.image]);

  const saveDuration = useMutation({
    mutationFn: () => api.updateSettings({ default_duration_seconds: Number(duration) }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["settings"] }),
  });
  const saveImageModel = useMutation({
    mutationFn: () => api.updateSettings({ active_image_model: imageModel }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["settings"] }),
  });

  if (!settings) return <div className="empty">Loading…</div>;

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Settings</h1>
          <p>Presets, defaults, and environment status.</p>
        </div>
      </div>

      <PresetSection kind="platform" />
      <PresetSection kind="content" />

      <div className="panel">
        <h2>Defaults</h2>
        <p className="panel-sub">Applied to new projects and ideas.</p>
        <div className="row" style={{ gap: 12 }}>
          <div style={{ width: 160 }}>
            <label style={{ fontSize: 12.5, fontWeight: 600, color: "var(--text-soft)" }}>
              Default duration (s)
            </label>
            <input
              type="number"
              min="15"
              max="600"
              value={duration}
              onChange={(e) => setDuration(e.target.value)}
            />
          </div>
          <button className="btn" style={{ alignSelf: "flex-end" }} onClick={() => saveDuration.mutate()}>
            Save
          </button>
        </div>
      </div>

      <div className="panel">
        <h2>Active models</h2>
        <p className="panel-sub">
          Image generation can be switched here; other stages still use the configured defaults.
        </p>
        <div className="key-row">
          <span>Script generation ({settings.active_llm_provider})</span>
          <span className="muted">{settings.models.script}</span>
        </div>
        <div className="key-row">
          <span>Image generation</span>
          <div className="row model-select-row">
            <select value={imageModel} onChange={(e) => setImageModel(e.target.value)}>
              {settings.image_model_options.map((option) => (
                <option key={option.id} value={option.id}>
                  {option.label}
                </option>
              ))}
            </select>
            <button
              className="btn sm"
              disabled={!imageModel || imageModel === settings.models.image || saveImageModel.isPending}
              onClick={() => saveImageModel.mutate()}
            >
              {saveImageModel.isPending ? "Saving..." : "Save"}
            </button>
          </div>
        </div>
        {settings.image_model_options
          .filter((option) => option.id === imageModel)
          .map((option) => (
            <div className="model-note" key={option.id}>
              {option.id} · {option.description}
            </div>
          ))}
        <div className="key-row">
          <span>Video generation</span>
          <span className="muted">{settings.models.video}</span>
        </div>
        <div className="key-row">
          <span>Text-to-speech</span>
          <span className="muted">{settings.models.tts}</span>
        </div>
      </div>

      <div className="panel">
        <h2>Environment</h2>
        <p className="panel-sub">
          Keys are loaded from <code>.env</code> and never displayed. Missing keys
          {settings.offline_fallback ? " fall back to offline placeholders." : " will error."}
        </p>
        {Object.entries(settings.api_keys).map(([name, present]) => (
          <div className="key-row" key={name}>
            <span style={{ textTransform: "capitalize" }}>{name} API key</span>
            <span className={"key-status " + (present ? "ok" : "missing")}>
              {present ? "loaded" : "not set"}
            </span>
          </div>
        ))}
        <div className="key-row">
          <span>FFmpeg (render engine)</span>
          <span className={"key-status " + (settings.ffmpeg_available ? "ok" : "missing")}>
            {settings.ffmpeg_available ? "available" : "not found"}
          </span>
        </div>
      </div>
    </>
  );
}

function PresetSection({ kind }) {
  const qc = useQueryClient();
  const isPlatform = kind === "platform";
  const key = isPlatform ? "platformPresets" : "contentPresets";
  const promptField = isPlatform ? "format_prompt" : "content_prompt";
  // Content presets carry a separate image-style field (applied to images only).
  const styleField = isPlatform ? null : "image_style_prompt";
  const title = isPlatform ? "Platform presets" : "Content presets";
  const sub = isPlatform
    ? "Delivery format — duration, hook/ending conventions, required metadata."
    : "Subject matter and tone — the topic layer. Pair any content with any platform.";

  const list = isPlatform ? api.listPlatformPresets : api.listContentPresets;
  const create = isPlatform ? api.createPlatformPreset : api.createContentPreset;
  const update = isPlatform ? api.updatePlatformPreset : api.updateContentPreset;
  const remove = isPlatform ? api.deletePlatformPreset : api.deleteContentPreset;

  const { data: presets = [] } = useQuery({ queryKey: [key], queryFn: list });
  const invalidate = () => qc.invalidateQueries({ queryKey: [key] });
  const [adding, setAdding] = useState(false);

  return (
    <div className="panel">
      <div className="row" style={{ justifyContent: "space-between" }}>
        <div>
          <h2>{title}</h2>
          <p className="panel-sub">{sub}</p>
        </div>
        <button className="btn sm" onClick={() => setAdding(true)}>
          + Add
        </button>
      </div>

      {presets.map((p) => (
        <PresetEditor
          key={p.id}
          preset={p}
          promptField={promptField}
          styleField={styleField}
          onSave={(body) => update(p.id, body).then(invalidate)}
          onDelete={() => remove(p.id).then(invalidate)}
        />
      ))}

      {adding && (
        <PresetEditor
          promptField={promptField}
          styleField={styleField}
          preset={{ name: "", [promptField]: "", is_default: false }}
          isNew
          onSave={(body) => create(body).then(() => { setAdding(false); invalidate(); })}
          onCancel={() => setAdding(false)}
        />
      )}
    </div>
  );
}

function PresetEditor({ preset, promptField, styleField, isNew, onSave, onDelete, onCancel }) {
  const [name, setName] = useState(preset.name);
  const [prompt, setPrompt] = useState(preset[promptField] || "");
  const [style, setStyle] = useState(styleField ? preset[styleField] || "" : "");
  const [isDefault, setIsDefault] = useState(preset.is_default);
  const [saving, setSaving] = useState(false);

  const save = () => {
    setSaving(true);
    const body = { name, [promptField]: prompt, is_default: isDefault };
    if (styleField) body[styleField] = style;
    Promise.resolve(onSave(body)).finally(() => setSaving(false));
  };

  return (
    <div className="preset-item">
      <div className="preset-item-head">
        <input
          style={{ maxWidth: 240 }}
          value={name}
          placeholder="Preset name"
          onChange={(e) => setName(e.target.value)}
        />
        {preset.is_default && <span className="badge">default</span>}
      </div>
      <label className="preset-field-label">Prompt (subject &amp; tone — sent to the script LLM)</label>
      <textarea value={prompt} onChange={(e) => setPrompt(e.target.value)} />
      {styleField && (
        <>
          <label className="preset-field-label">
            Image style (applied to character sheets &amp; scene images — not the script)
          </label>
          <textarea
            value={style}
            onChange={(e) => setStyle(e.target.value)}
            placeholder="e.g. Cinematic oil-painting, warm dramatic lighting, cohesive palette…"
          />
        </>
      )}
      <div className="row" style={{ marginTop: 10, justifyContent: "space-between" }}>
        <label className="row" style={{ fontSize: 13, cursor: "pointer" }}>
          <input
            type="checkbox"
            style={{ width: "auto" }}
            checked={isDefault}
            onChange={(e) => setIsDefault(e.target.checked)}
          />
          Default
        </label>
        <div className="row">
          {isNew ? (
            <button className="btn ghost sm" onClick={onCancel}>
              Cancel
            </button>
          ) : (
            <button className="btn danger sm" onClick={onDelete}>
              Delete
            </button>
          )}
          <button className="btn sm primary" disabled={!name.trim() || saving} onClick={save}>
            {saving ? "Saving…" : "Save"}
          </button>
        </div>
      </div>
    </div>
  );
}
