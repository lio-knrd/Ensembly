import { useEffect, useState } from "react";
import { Check, Link2, Minus, Plus, RefreshCw, Sparkles, Trash2 } from "lucide-react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api.js";
import AudioPlayer from "../components/AudioPlayer.jsx";
import Loading from "../components/Loading.jsx";

/** Read and clear the ?..._linked / ?..._error the OAuth callbacks redirect with.
 *
 * Both callbacks can only report back through the URL, so without this a failed
 * link lands on a silently unchanged page. Cleared from the address bar once
 * read, so a reload does not resurrect a stale message.
 */
function useLinkCallbackNotice() {
  const [notice, setNotice] = useState(null);
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const found = ["tiktok", "youtube"]
      .map((service) => {
        const linked = params.get(`${service}_linked`);
        const error = params.get(`${service}_error`);
        if (linked) return { service, ok: true, message: `Linked ${linked}.` };
        if (error) return { service, ok: false, message: error };
        return null;
      })
      .find(Boolean);
    if (!found) return;
    setNotice(found);
    window.history.replaceState({}, "", window.location.pathname);
  }, []);
  return [notice, () => setNotice(null)];
}

export default function Settings() {
  const qc = useQueryClient();
  const [linkNotice, dismissLinkNotice] = useLinkCallbackNotice();
  const { data: settings } = useQuery({ queryKey: ["settings"], queryFn: api.getSettings });
  const [duration, setDuration] = useState("");
  const [imageModel, setImageModel] = useState("");
  useEffect(() => {
    if (settings) setDuration(String(settings.default_duration_seconds));
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

  if (!settings) return <Loading full />;

  const durationDirty = duration !== "" && Number(duration) !== settings.default_duration_seconds;
  const imageModelDirty = Boolean(imageModel) && imageModel !== settings.models.image;

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Settings</h1>
          <p>Presets, defaults, and environment status.</p>
        </div>
      </div>

      {linkNotice && (
        <div className={"banner banner-notice" + (linkNotice.ok ? " success" : "")}>
          <span>
            <strong>{linkNotice.service === "youtube" ? "YouTube" : "TikTok"}:</strong>{" "}
            {linkNotice.message}
          </span>
          <button className="btn ghost sm" onClick={dismissLinkNotice}>
            Dismiss
          </button>
        </div>
      )}

      <PresetSection kind="platform" />
      <PresetSection kind="content" />
      <TikTokPanel />
      <YouTubePanel />

      <div className="panel">
        <h2>Defaults</h2>
        <p className="panel-sub">Applied to new projects and ideas.</p>
        <div className="defaults-row">
          <div className="field">
            <label>Default duration (s)</label>
            <input
              type="number"
              min="15"
              max="600"
              value={duration}
              onChange={(e) => setDuration(e.target.value)}
            />
          </div>
          <button
            className={"btn" + (durationDirty ? " primary" : "")}
            disabled={!durationDirty || saveDuration.isPending}
            onClick={() => saveDuration.mutate()}
          >
            {saveDuration.isPending ? "Saving..." : durationDirty ? "Save" : "Saved"}
          </button>
        </div>
      </div>

      <div className="panel">
        <h2>Active models</h2>
        <p className="panel-sub">
          Image generation can be switched here; other stages use the configured defaults.
        </p>

        <div className="key-row">
          <span>Script generation ({settings.active_llm_provider})</span>
          <span className="model-code">{settings.models.script}</span>
        </div>

        <div className="model-group">
          <span className="model-group-label">Image generation</span>
          <div className="model-options">
            {settings.image_model_options.map((option) => {
              const selected = option.id === imageModel;
              return (
                <button
                  type="button"
                  key={option.id}
                  className={"model-option" + (selected ? " selected" : "")}
                  onClick={() => setImageModel(option.id)}
                >
                  <span className="model-option-radio" />
                  <span className="model-option-body">
                    <span className="model-option-title">
                      <strong>{option.label}</strong>
                      <code>{option.id}</code>
                      {option.id === settings.models.image && (
                        <span className="badge">active</span>
                      )}
                    </span>
                    {option.description && <p>{option.description}</p>}
                  </span>
                </button>
              );
            })}
          </div>
          {imageModelDirty && (
            <div className="model-save-row">
              <button
                className="btn ghost"
                disabled={saveImageModel.isPending}
                onClick={() => setImageModel(settings.models.image)}
              >
                Reset
              </button>
              <button
                className="btn primary"
                disabled={saveImageModel.isPending}
                onClick={() => saveImageModel.mutate()}
              >
                {saveImageModel.isPending ? "Saving..." : "Save model"}
              </button>
            </div>
          )}
        </div>

        <div className="key-row">
          <span>Video generation</span>
          <span className="model-code">{settings.models.video}</span>
        </div>
        <div className="key-row">
          <span>Text-to-speech</span>
          <span className="model-code">{settings.models.tts}</span>
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
              {present ? <Check /> : <Minus />}
              {present ? "loaded" : "not set"}
            </span>
          </div>
        ))}
        <div className="key-row">
          <span>FFmpeg (render engine)</span>
          <span className={"key-status " + (settings.ffmpeg_available ? "ok" : "missing")}>
            {settings.ffmpeg_available ? <Check /> : <Minus />}
            {settings.ffmpeg_available ? "available" : "not found"}
          </span>
        </div>
      </div>
    </>
  );
}

function TikTokPanel() {
  const qc = useQueryClient();
  const { data: status } = useQuery({ queryKey: ["tiktok"], queryFn: api.tiktokStatus });
  const [pending, setPending] = useState(null); // { url, state } while a login is open
  const [pastedUrl, setPastedUrl] = useState("");
  const [error, setError] = useState("");
  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ["tiktok"] });
    qc.invalidateQueries({ queryKey: ["contentPresets"] });
  };

  const start = useMutation({
    mutationFn: () => api.tiktokLinkStart({}),
    onSuccess: (data) => {
      setError("");
      setPending(data);
      window.open(data.url, "_blank", "noopener");
    },
    onError: (err) => setError(err.message),
  });
  const complete = useMutation({
    mutationFn: () =>
      api.tiktokLinkComplete({ redirected_url: pastedUrl.trim(), state: pending?.state }),
    onSuccess: () => {
      setError("");
      setPending(null);
      setPastedUrl("");
      invalidate();
    },
    onError: (err) => setError(err.message),
  });

  if (!status) return null;

  return (
    <div className="panel">
      <div className="row" style={{ justifyContent: "space-between" }}>
        <div>
          <h2>TikTok publishing</h2>
          <p className="panel-sub">
            Accounts are linked once here, then any content preset picks one to
            post as. Linking the same account twice is never needed.
          </p>
        </div>
        <button
          className="btn sm"
          disabled={!status.configured || start.isPending}
          onClick={() => start.mutate()}
          title={status.configured ? undefined : "Set TIKTOK_CLIENT_KEY and TIKTOK_CLIENT_SECRET first"}
        >
          <Link2 />
          {start.isPending ? "Opening..." : "Link account"}
        </button>
      </div>

      {!status.configured && (
        <div className="banner compact">
          Set <code>TIKTOK_CLIENT_KEY</code> and <code>TIKTOK_CLIENT_SECRET</code> in
          <code> .env</code> (from your TikTok developer app), then restart.
        </div>
      )}
      {status.configured && !status.redirect_uri && (
        <div className="banner compact">
          Set <code>TIKTOK_REDIRECT_URI</code> to the https URL registered on your
          TikTok app. TikTok rejects http and localhost redirects.
        </div>
      )}

      {pending && (
        <div className="style-assistant">
          <label className="preset-field-label">
            Finish the link: approve in the tab that opened, then paste the URL it
            landed on
          </label>
          <input
            value={pastedUrl}
            placeholder="https://your-host/api/tiktok/link/callback?code=...&state=..."
            onChange={(e) => setPastedUrl(e.target.value)}
          />
          <div className="row" style={{ justifyContent: "space-between", marginTop: 8 }}>
            <span className="style-assistant-note">
              Not needed if this app is reachable at the redirect URI - the link
              completes on its own.
            </span>
            <div className="row">
              <button className="btn ghost sm" onClick={() => { setPending(null); setPastedUrl(""); }}>
                Cancel
              </button>
              <button
                className="btn sm primary"
                disabled={!pastedUrl.trim() || complete.isPending}
                onClick={() => complete.mutate()}
              >
                {complete.isPending ? "Linking..." : "Complete link"}
              </button>
            </div>
          </div>
        </div>
      )}
      {error && <div className="banner compact">{error}</div>}

      {status.accounts.length === 0 ? (
        <div className="key-row">
          <span>No accounts linked yet.</span>
        </div>
      ) : (
        status.accounts.map((account) => (
          <TikTokAccountRow key={account.id} account={account} onChange={invalidate} />
        ))
      )}

      <p className="panel-sub" style={{ marginTop: 12 }}>
        Until TikTok audits the developer app, direct posts are forced to private
        (SELF_ONLY). Sending a video to TikTok drafts instead needs no audit - you
        publish it from the TikTok app at any audience you like.
      </p>
    </div>
  );
}

function TikTokAccountRow({ account, onChange }) {
  const refresh = useMutation({ mutationFn: () => api.tiktokRefreshAccount(account.id), onSuccess: onChange });
  const unlink = useMutation({ mutationFn: () => api.tiktokUnlink(account.id), onSuccess: onChange });
  const groups = account.groups.map((g) => g.name).join(", ");

  return (
    <div className="key-row">
      <span>
        <strong>{account.display_name || account.open_id}</strong>
        <span className="model-code" style={{ marginLeft: 8 }}>
          {groups || "no group selected"}
        </span>
        {account.needs_relink && (
          <span className="key-status missing" style={{ marginLeft: 8 }}>
            <Minus />
            re-link needed
          </span>
        )}
        {account.last_error && <div className="form-error">{account.last_error}</div>}
      </span>
      <span className="row">
        <button className="btn sm" disabled={refresh.isPending} onClick={() => refresh.mutate()}>
          <RefreshCw />
          {refresh.isPending ? "Refreshing..." : "Refresh"}
        </button>
        <button className="btn sm danger" disabled={unlink.isPending} onClick={() => unlink.mutate()}>
          <Trash2 />
          Unlink
        </button>
      </span>
    </div>
  );
}

function YouTubePanel() {
  const qc = useQueryClient();
  const { data: status } = useQuery({ queryKey: ["youtube"], queryFn: api.youtubeStatus });
  const [pending, setPending] = useState(null); // { url, state } while a login is open
  const [pastedUrl, setPastedUrl] = useState("");
  const [error, setError] = useState("");
  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ["youtube"] });
    qc.invalidateQueries({ queryKey: ["contentPresets"] });
  };

  const start = useMutation({
    mutationFn: () => api.youtubeLinkStart({}),
    onSuccess: (data) => {
      setError("");
      setPending(data);
      window.open(data.url, "_blank", "noopener");
    },
    onError: (err) => setError(err.message),
  });
  const complete = useMutation({
    mutationFn: () =>
      api.youtubeLinkComplete({ redirected_url: pastedUrl.trim(), state: pending?.state }),
    onSuccess: () => {
      setError("");
      setPending(null);
      setPastedUrl("");
      invalidate();
    },
    onError: (err) => setError(err.message),
  });

  // Google redirects back here, so a finished link shows up on a refetch even
  // though this tab never saw the code.
  useEffect(() => {
    if (!pending) return undefined;
    const timer = setInterval(() => {
      qc.invalidateQueries({ queryKey: ["youtube"] });
    }, 3000);
    return () => clearInterval(timer);
  }, [pending, qc]);

  if (!status) return null;

  return (
    <div className="panel">
      <div className="row" style={{ justifyContent: "space-between" }}>
        <div>
          <h2>YouTube publishing</h2>
          <p className="panel-sub">
            Channels are linked once here, then any content preset picks one to
            upload to. A 9:16 render under 3 minutes becomes a Short on its own.
          </p>
        </div>
        <button
          className="btn sm"
          disabled={!status.configured || start.isPending}
          onClick={() => start.mutate()}
          title={
            status.configured
              ? undefined
              : "Set YOUTUBE_CLIENT_ID and YOUTUBE_CLIENT_SECRET first"
          }
        >
          <Link2 />
          {start.isPending ? "Opening..." : "Link channel"}
        </button>
      </div>

      {!status.configured && (
        <div className="banner compact">
          Set <code>YOUTUBE_CLIENT_ID</code> and <code>YOUTUBE_CLIENT_SECRET</code> in
          <code> .env</code> (from your Google Cloud OAuth client), then restart.
        </div>
      )}
      {status.configured && (
        <div className="key-row">
          <span>Redirect URI (must be registered on the OAuth client)</span>
          <span className="model-code">{status.redirect_uri}</span>
        </div>
      )}

      {pending && (
        <div className="style-assistant">
          <label className="preset-field-label">
            Approve in the tab that opened. This finishes on its own - only paste
            the URL if the browser could not reach this app.
          </label>
          <input
            value={pastedUrl}
            placeholder={`${status.redirect_uri}?code=...&state=...`}
            onChange={(e) => setPastedUrl(e.target.value)}
          />
          <div className="row" style={{ justifyContent: "space-between", marginTop: 8 }}>
            <span className="style-assistant-note">
              Waiting for Google to redirect back...
            </span>
            <div className="row">
              <button
                className="btn ghost sm"
                onClick={() => {
                  setPending(null);
                  setPastedUrl("");
                }}
              >
                Cancel
              </button>
              <button
                className="btn sm primary"
                disabled={!pastedUrl.trim() || complete.isPending}
                onClick={() => complete.mutate()}
              >
                {complete.isPending ? "Linking..." : "Complete link"}
              </button>
            </div>
          </div>
        </div>
      )}
      {error && <div className="banner compact">{error}</div>}

      {status.accounts.length === 0 ? (
        <div className="key-row">
          <span>No channels linked yet.</span>
        </div>
      ) : (
        status.accounts.map((account) => (
          <YouTubeAccountRow key={account.id} account={account} onChange={invalidate} />
        ))
      )}

      <p className="panel-sub" style={{ marginTop: 12 }}>
        Until Google audits the API project, every upload is locked to private -
        publish it from YouTube Studio, or schedule it here. Uploads are capped
        at 100 per day, and while the OAuth consent screen is in Testing a link
        expires after 7 days.
      </p>
    </div>
  );
}

function YouTubeAccountRow({ account, onChange }) {
  const refresh = useMutation({
    mutationFn: () => api.youtubeRefreshAccount(account.id),
    onSuccess: onChange,
  });
  const unlink = useMutation({
    mutationFn: () => api.youtubeUnlink(account.id),
    onSuccess: onChange,
  });
  const groups = account.groups.map((g) => g.name).join(", ");

  return (
    <div className="key-row">
      <span>
        <strong>{account.title || account.channel_id}</strong>
        <span className="model-code" style={{ marginLeft: 8 }}>
          {groups || "no group selected"}
        </span>
        {account.needs_relink && (
          <span className="key-status missing" style={{ marginLeft: 8 }}>
            <Minus />
            re-link needed
          </span>
        )}
        {account.last_error && <div className="form-error">{account.last_error}</div>}
      </span>
      <span className="row">
        <button className="btn sm" disabled={refresh.isPending} onClick={() => refresh.mutate()}>
          <RefreshCw />
          {refresh.isPending ? "Refreshing..." : "Refresh"}
        </button>
        <button
          className="btn sm danger"
          disabled={unlink.isPending}
          onClick={() => unlink.mutate()}
        >
          <Trash2 />
          Unlink
        </button>
      </span>
    </div>
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
    ? "Delivery format: duration, hook/ending conventions, required metadata."
    : "Subject matter and tone, the topic layer. Pair any content with any platform.";

  const list = isPlatform ? api.listPlatformPresets : api.listContentPresets;
  const create = isPlatform ? api.createPlatformPreset : api.createContentPreset;
  const update = isPlatform ? api.updatePlatformPreset : api.updateContentPreset;
  const remove = isPlatform ? api.deletePlatformPreset : api.deleteContentPreset;

  const { data: presets = [] } = useQuery({ queryKey: [key], queryFn: list });
  const { data: voices = [] } = useQuery({
    queryKey: ["voices"],
    queryFn: api.listVoices,
    enabled: !isPlatform,
  });
  const { data: tiktok } = useQuery({
    queryKey: ["tiktok"],
    queryFn: api.tiktokStatus,
    enabled: !isPlatform,
  });
  const { data: youtube } = useQuery({
    queryKey: ["youtube"],
    queryFn: api.youtubeStatus,
    enabled: !isPlatform,
  });
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
          <Plus />
          Add
        </button>
      </div>

      {presets.map((p) => (
        <PresetEditor
          key={p.id}
          preset={p}
          promptField={promptField}
          styleField={styleField}
          voiceOptions={voices}
          tiktokAccounts={tiktok?.accounts || []}
          youtubeAccounts={youtube?.accounts || []}
          onSave={(body) => update(p.id, body).then(invalidate)}
          onDelete={() => remove(p.id).then(invalidate)}
        />
      ))}

      {adding && (
        <PresetEditor
          promptField={promptField}
          styleField={styleField}
          voiceOptions={voices}
          preset={{ name: "", [promptField]: "", is_default: false }}
          isNew
          onSave={(body) => create(body).then(() => { setAdding(false); invalidate(); })}
          onCancel={() => setAdding(false)}
        />
      )}
    </div>
  );
}

function PresetEditor({
  preset,
  promptField,
  styleField,
  voiceOptions = [],
  tiktokAccounts = [],
  youtubeAccounts = [],
  isNew,
  onSave,
  onDelete,
  onCancel,
}) {
  const [name, setName] = useState(preset.name);
  const [prompt, setPrompt] = useState(preset[promptField] || "");
  const [style, setStyle] = useState(styleField ? preset[styleField] || "" : "");
  const [animStyle, setAnimStyle] = useState(
    styleField ? preset.animation_style_prompt || "" : ""
  );
  const [motionStyle, setMotionStyle] = useState(
    styleField ? preset.motion_style_prompt || "" : ""
  );
  const [visualMode, setVisualMode] = useState(preset.visual_mode || "mixed");
  const [panelSeconds, setPanelSeconds] = useState(preset.panel_seconds ?? 4);
  const [panelParallax, setPanelParallax] = useState(preset.panel_parallax !== false);
  const panelsMode = visualMode === "panels";
  const [voiceId, setVoiceId] = useState(styleField ? preset.voice_id || "" : "");
  const [enableAnimations, setEnableAnimations] = useState(!!preset.enable_animations);
  const [voiceSearch, setVoiceSearch] = useState("");
  const [styleAssistantOpen, setStyleAssistantOpen] = useState(false);
  const [styleGuidance, setStyleGuidance] = useState("");
  const [styleError, setStyleError] = useState("");
  const [isDefault, setIsDefault] = useState(preset.is_default);
  const [accountId, setAccountId] = useState(preset.tiktok_account_id || "");
  const [ytAccountId, setYtAccountId] = useState(preset.youtube_account_id || "");
  const [saving, setSaving] = useState(false);
  const qc = useQueryClient();
  // The account lives behind its own endpoint, so it saves on change instead of
  // riding along with the preset body (which would clear it from older forms).
  const selectAccount = useMutation({
    mutationFn: (id) => api.tiktokSelectGroupAccount(preset.id, id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["tiktok"] });
      qc.invalidateQueries({ queryKey: ["contentPresets"] });
    },
  });
  const selectYtAccount = useMutation({
    mutationFn: (id) => api.youtubeSelectGroupAccount(preset.id, id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["youtube"] });
      qc.invalidateQueries({ queryKey: ["contentPresets"] });
    },
  });
  const selectedVoice = voiceOptions.find((voice) => voice.id === voiceId) || null;
  const filteredVoices = voiceOptions.filter((voice) => {
    const haystack = [voice.label, voice.description, voice.source, ...Object.values(voice.labels || {})]
      .filter(Boolean)
      .join(" ")
      .toLowerCase();
    return haystack.includes(voiceSearch.trim().toLowerCase());
  });

  // Dirty tracking so the save button only lights up when something changed.
  const dirty =
    isNew ||
    name !== preset.name ||
    prompt !== (preset[promptField] || "") ||
    isDefault !== preset.is_default ||
    (styleField &&
      (style !== (preset[styleField] || "") ||
        animStyle !== (preset.animation_style_prompt || "") ||
        motionStyle !== (preset.motion_style_prompt || "") ||
        visualMode !== (preset.visual_mode || "mixed") ||
        Number(panelSeconds) !== (preset.panel_seconds ?? 4) ||
        panelParallax !== (preset.panel_parallax !== false) ||
        voiceId !== (preset.voice_id || "") ||
        enableAnimations !== !!preset.enable_animations));

  const save = () => {
    setSaving(true);
    const body = { name, [promptField]: prompt, is_default: isDefault };
    if (styleField) body[styleField] = style;
    if (styleField) body.animation_style_prompt = animStyle;
    if (styleField) body.motion_style_prompt = motionStyle;
    if (styleField) body.visual_mode = visualMode;
    if (styleField) body.panel_seconds = Number(panelSeconds) || 4;
    if (styleField) body.panel_parallax = panelParallax;
    if (styleField) body.voice_id = voiceId;
    if (styleField) body.enable_animations = enableAnimations;
    Promise.resolve(onSave(body)).finally(() => setSaving(false));
  };

  const suggestStyle = useMutation({
    mutationFn: () =>
      api.suggestContentStyle({
        content_prompt: prompt,
        current_style_prompt: style,
        guidelines: styleGuidance,
      }),
    onSuccess: (data) => {
      setStyle(data.image_style_prompt || "");
      setStyleError("");
      setStyleAssistantOpen(false);
    },
    onError: (error) => setStyleError(error.message || "Could not generate style."),
  });

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
      <label className="preset-field-label">Prompt (subject &amp; tone, sent to the script LLM)</label>
      <textarea value={prompt} onChange={(e) => setPrompt(e.target.value)} />
      {styleField && (
        <>
          <label className="preset-field-label">How this group tells its stories</label>
          <div className="seg visual-mode-seg">
            <button
              type="button"
              className={!panelsMode ? "on" : ""}
              onClick={() => setVisualMode("mixed")}
            >
              Stills + video
            </button>
            <button
              type="button"
              className={panelsMode ? "on" : ""}
              onClick={() => setVisualMode("panels")}
            >
              Panels (manhwa)
            </button>
          </div>
          <div className="style-assistant-note">
            {panelsMode
              ? "No video is generated for this group at all - the script AI cannot even mark a scene as video, so nothing can run up a generation bill. The story is told in many more panels, each with a real environment described, and the motion comes from the camera move over each panel."
              : "The script AI may mark pivotal beats as generated video clips, which are billed per second by the video provider."}
          </div>
          {panelsMode && (
            <>
              <label className="preset-field-label">Seconds of narration per panel</label>
              <input
                type="number"
                min="1.5"
                max="12"
                step="0.5"
                value={panelSeconds}
                onChange={(e) => setPanelSeconds(e.target.value)}
              />
              <div className="style-assistant-note">
                Lower means more panels for the same runtime - a 75 second video at
                4s per panel is around 19 panels. This is a target the script AI
                aims for, not a hard cut.
              </div>
              <label className="preset-field-label">2.5D parallax</label>
              <label className="row" style={{ fontSize: 13, cursor: "pointer", gap: 8 }}>
                <input
                  type="checkbox"
                  style={{ width: "auto" }}
                  checked={panelParallax}
                  onChange={(e) => setPanelParallax(e.target.checked)}
                />
                Estimate depth for each panel and move near and far parts of the
                image at different rates, so the camera move reads as
                dimensional instead of flat.
              </label>
              <div className="style-assistant-note">
                Adds roughly 25 seconds of render time per panel and runs
                entirely on this machine. Turn it off for faster renders; panels
                then use the plain camera move.
              </div>
            </>
          )}
          <label className="preset-field-label">
            Image style (applied to character sheets &amp; scene images, not the script)
          </label>
          <div className="preset-field-actions">
            <button
              type="button"
              className="btn ghost sm"
              disabled={suggestStyle.isPending}
              onClick={() => setStyleAssistantOpen((open) => !open)}
              title="Generate a consistent image style prompt from the content prompt"
            >
              <Sparkles />
              AI style
            </button>
          </div>
          <textarea
            value={style}
            onChange={(e) => setStyle(e.target.value)}
            placeholder="e.g. Cinematic oil-painting, warm dramatic lighting, cohesive palette..."
          />
          {styleAssistantOpen && (
            <div className="style-assistant">
              <label className="preset-field-label">Optional style guidance</label>
              <textarea
                value={styleGuidance}
                onChange={(e) => setStyleGuidance(e.target.value)}
                placeholder="Mention desired look, medium, level of realism, or character rules to preserve."
              />
              <div className="row" style={{ justifyContent: "space-between" }}>
                <span className="style-assistant-note">
                  Uses the content prompt, current style text, and these notes.
                </span>
                <button
                  type="button"
                  className="btn sm primary"
                  disabled={suggestStyle.isPending || (!prompt.trim() && !style.trim() && !styleGuidance.trim())}
                  onClick={() => suggestStyle.mutate()}
                >
                  {suggestStyle.isPending ? "Generating..." : "Generate"}
                </button>
              </div>
              {styleError && <div className="form-error">{styleError}</div>}
            </div>
          )}
          {!panelsMode && (
            <>
              <label className="preset-field-label">
                Motion style (applied to video scenes only, never sent to the image
                model or the script)
              </label>
              <textarea
                value={motionStyle}
                onChange={(e) => setMotionStyle(e.target.value)}
                placeholder="How things move: how much the camera does vs. the characters, what drifts continuously, what the model must not add."
              />
              <div className="style-assistant-note">
                Describe movement only. The image style above deliberately never
                reaches the video model - look and lighting words are what make it
                paint effects over a frozen frame instead of animating it.
              </div>
            </>
          )}
          <label className="preset-field-label">Voice generation</label>
          <input
            className="voice-search"
            value={voiceSearch}
            placeholder={`Search ${voiceOptions.length} ElevenLabs voices`}
            onChange={(e) => setVoiceSearch(e.target.value)}
          />
          <div className="voice-field">
            <select value={voiceId} onChange={(e) => setVoiceId(e.target.value)}>
              <option value="">Configured default</option>
              {filteredVoices.map((voice) => (
                <option key={voice.id} value={voice.id}>
                  {voice.label}{voice.source ? ` (${voice.source})` : ""}
                </option>
              ))}
            </select>
            {selectedVoice?.preview_url ? (
              <AudioPlayer key={selectedVoice.id} src={selectedVoice.preview_url} compact />
            ) : (
              <span className="voice-empty-preview">
                {voiceId ? "No preview provided" : "Select a voice to preview"}
              </span>
            )}
          </div>
          {selectedVoice?.description && (
            <div className="voice-note">{selectedVoice.description}</div>
          )}
          <label className="preset-field-label">Deterministic animations</label>
          <label className="row" style={{ fontSize: 13, cursor: "pointer", gap: 8 }}>
            <input
              type="checkbox"
              style={{ width: "auto" }}
              checked={enableAnimations}
              onChange={(e) => setEnableAnimations(e.target.checked)}
            />
            Let the script AI author full Manim animations (graphs, geometry,
            equations, anything) synced to the narration, where they explain
            better than an AI image or video.
          </label>
          {enableAnimations && (
            <>
              <label className="preset-field-label">
                Animation style (applied to animation scenes only, not the script or images)
              </label>
              <textarea
                value={animStyle}
                onChange={(e) => setAnimStyle(e.target.value)}
                placeholder="e.g. Two accent colors on the dark background, large numbers in the upper third, small labels beneath what they name, no gradients or glow..."
              />
            </>
          )}
          {!isNew && (
            <>
              <label className="preset-field-label">TikTok account</label>
              <select
                value={accountId}
                disabled={selectAccount.isPending}
                onChange={(e) => {
                  setAccountId(e.target.value);
                  selectAccount.mutate(e.target.value);
                }}
              >
                <option value="">Not published to TikTok</option>
                {tiktokAccounts.map((account) => (
                  <option key={account.id} value={account.id}>
                    {account.display_name || account.open_id}
                    {account.groups.length ? ` (also: ${account.groups.map((g) => g.name).join(", ")})` : ""}
                  </option>
                ))}
              </select>
              <div className="style-assistant-note">
                {tiktokAccounts.length
                  ? "Pick an account already linked in TikTok publishing below. Saved immediately."
                  : "No accounts linked yet - link one in TikTok publishing below."}
              </div>
              <label className="preset-field-label">YouTube channel</label>
              <select
                value={ytAccountId}
                disabled={selectYtAccount.isPending}
                onChange={(e) => {
                  setYtAccountId(e.target.value);
                  selectYtAccount.mutate(e.target.value);
                }}
              >
                <option value="">Not published to YouTube</option>
                {youtubeAccounts.map((account) => (
                  <option key={account.id} value={account.id}>
                    {account.title || account.channel_id}
                    {account.groups.length
                      ? ` (also: ${account.groups.map((g) => g.name).join(", ")})`
                      : ""}
                  </option>
                ))}
              </select>
              <div className="style-assistant-note">
                {youtubeAccounts.length
                  ? "Pick a channel already linked in YouTube publishing below. Saved immediately."
                  : "No channels linked yet - link one in YouTube publishing below."}
              </div>
            </>
          )}
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
          <button
            className={"btn sm" + (dirty ? " primary" : "")}
            disabled={!dirty || !name.trim() || saving}
            onClick={save}
          >
            {saving ? "Saving..." : isNew ? "Create" : dirty ? "Save changes" : "Saved"}
          </button>
        </div>
      </div>
    </div>
  );
}
