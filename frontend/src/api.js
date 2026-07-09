// Thin REST client for the FastAPI backend.

async function req(path, options = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      detail = (await res.json()).detail || detail;
    } catch {
      /* ignore */
    }
    throw new Error(detail);
  }
  if (res.status === 204) return null;
  const ct = res.headers.get("content-type") || "";
  return ct.includes("application/json") ? res.json() : res.text();
}

export const mediaUrl = (relPath, version) => {
  if (!relPath) return null;
  const url = `/media/${relPath.replace(/^\/+/, "")}`;
  return version == null ? url : `${url}?v=${encodeURIComponent(version)}`;
};

export const api = {
  // Projects
  listProjects: () => req("/api/projects"),
  createProject: (body) =>
    req("/api/projects", { method: "POST", body: JSON.stringify(body) }),
  analyzeProjectScope: (body) =>
    req("/api/projects/analyze-scope", { method: "POST", body: JSON.stringify(body) }),
  createSplitProjects: (body) =>
    req("/api/projects/split", { method: "POST", body: JSON.stringify(body) }),
  getProject: (id) => req(`/api/projects/${id}`),
  deleteProject: (id) => req(`/api/projects/${id}`, { method: "DELETE" }),
  regenScript: (id) => req(`/api/projects/${id}/generate-script`, { method: "POST" }),
  approveScript: (id) => req(`/api/projects/${id}/approve-script`, { method: "POST" }),
  getCast: (id) => req(`/api/projects/${id}/cast`),
  generateCastSheet: (id, body) =>
    req(`/api/projects/${id}/cast/generate`, { method: "POST", body: JSON.stringify(body) }),
  generateMissingSheets: (id) =>
    req(`/api/projects/${id}/cast/generate-missing`, { method: "POST" }),
  approveCast: (id) => req(`/api/projects/${id}/approve-cast`, { method: "POST" }),
  approveStoryboard: (id) =>
    req(`/api/projects/${id}/approve-storyboard`, { method: "POST" }),
  approveClips: (id) => req(`/api/projects/${id}/approve-clips`, { method: "POST" }),
  stepBack: (id) => req(`/api/projects/${id}/step-back`, { method: "POST" }),
  stepForward: (id) => req(`/api/projects/${id}/step-forward`, { method: "POST" }),
  retryFailedStep: (id) =>
    req(`/api/projects/${id}/retry-failed-step`, { method: "POST" }),
  cancelProject: (id) => req(`/api/projects/${id}/cancel`, { method: "POST" }),
  rerender: (id) => req(`/api/projects/${id}/render`, { method: "POST" }),
  generateTitleCard: (id, body) =>
    req(`/api/projects/${id}/title-card`, { method: "POST", body: JSON.stringify(body) }),
  getProjectMusic: (id) => req(`/api/projects/${id}/music`),
  updateProjectMusic: (id, body) =>
    req(`/api/projects/${id}/music`, { method: "PATCH", body: JSON.stringify(body) }),
  selectProjectMusic: (id, body) =>
    req(`/api/projects/${id}/music/select`, { method: "POST", body: JSON.stringify(body) }),
  clearProjectMusic: (id) => req(`/api/projects/${id}/music`, { method: "DELETE" }),
  listMusicLibrary: () => req("/api/music/library"),
  searchMusic: (query, options = {}) => {
    const params = new URLSearchParams({
      q: query || "ambient",
      limit: String(options.limit || 20),
      instrumental: String(options.instrumental ?? true),
    });
    return req(`/api/music/search?${params.toString()}`);
  },

  // Scenes
  updateScene: (pid, sid, body) =>
    req(`/api/projects/${pid}/scenes/${sid}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),
  regenSceneImage: (pid, sid) =>
    req(`/api/projects/${pid}/scenes/${sid}/regenerate-image`, { method: "POST" }),
  regenSceneAudio: (pid, sid) =>
    req(`/api/projects/${pid}/scenes/${sid}/regenerate-audio`, { method: "POST" }),
  regenSceneClip: (pid, sid) =>
    req(`/api/projects/${pid}/scenes/${sid}/regenerate-clip`, { method: "POST" }),
  selectSceneAsset: (pid, sid, body) =>
    req(`/api/projects/${pid}/scenes/${sid}/select-asset`, {
      method: "POST",
      body: JSON.stringify(body),
    }),

  // Characters
  listCharacters: () => req("/api/characters"),
  createCharacter: (body) =>
    req("/api/characters", { method: "POST", body: JSON.stringify(body) }),
  updateCharacter: (id, body) =>
    req(`/api/characters/${id}`, { method: "PATCH", body: JSON.stringify(body) }),
  deleteCharacter: (id) => req(`/api/characters/${id}`, { method: "DELETE" }),
  regenReference: (id) =>
    req(`/api/characters/${id}/regenerate-reference`, { method: "POST" }),
  regenFormReference: (id, formId) =>
    req(`/api/characters/${id}/forms/${formId}/regenerate-reference`, { method: "POST" }),
  selectReference: (id, body) =>
    req(`/api/characters/${id}/select-reference`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  uploadReference: async (id, file) => {
    const fd = new FormData();
    fd.append("file", file);
    const res = await fetch(`/api/characters/${id}/reference`, {
      method: "POST",
      body: fd,
    });
    if (!res.ok) throw new Error("Upload failed");
    return res.json();
  },
  uploadFormReference: async (id, formId, file) => {
    const fd = new FormData();
    fd.append("file", file);
    const res = await fetch(`/api/characters/${id}/forms/${formId}/reference`, {
      method: "POST",
      body: fd,
    });
    if (!res.ok) throw new Error("Upload failed");
    return res.json();
  },

  // Presets
  listPlatformPresets: () => req("/api/presets/platform"),
  createPlatformPreset: (body) =>
    req("/api/presets/platform", { method: "POST", body: JSON.stringify(body) }),
  updatePlatformPreset: (id, body) =>
    req(`/api/presets/platform/${id}`, { method: "PATCH", body: JSON.stringify(body) }),
  deletePlatformPreset: (id) =>
    req(`/api/presets/platform/${id}`, { method: "DELETE" }),
  listContentPresets: () => req("/api/presets/content"),
  suggestContentStyle: (body) =>
    req("/api/presets/content/suggest-style", { method: "POST", body: JSON.stringify(body) }),
  createContentPreset: (body) =>
    req("/api/presets/content", { method: "POST", body: JSON.stringify(body) }),
  updateContentPreset: (id, body) =>
    req(`/api/presets/content/${id}`, { method: "PATCH", body: JSON.stringify(body) }),
  deleteContentPreset: (id) =>
    req(`/api/presets/content/${id}`, { method: "DELETE" }),
  listVoices: () => req("/api/voices"),

  // Ideas
  listIdeas: () => req("/api/ideas"),
  createIdea: (body) =>
    req("/api/ideas", { method: "POST", body: JSON.stringify(body) }),
  deleteIdea: (id) => req(`/api/ideas/${id}`, { method: "DELETE" }),
  convertIdea: (id, body) =>
    req(`/api/ideas/${id}/convert`, { method: "POST", body: JSON.stringify(body) }),
  listEditorialPlans: () => req("/api/ideas/plans"),
  createEditorialPlan: (body) =>
    req("/api/ideas/plans", { method: "POST", body: JSON.stringify(body) }),
  getEditorialPlan: (id) => req(`/api/ideas/plans/${id}`),
  updateEditorialPlan: (id, body) =>
    req(`/api/ideas/plans/${id}`, { method: "PATCH", body: JSON.stringify(body) }),
  deleteEditorialPlan: (id) => req(`/api/ideas/plans/${id}`, { method: "DELETE" }),
  createEditorialItem: (planId, body) =>
    req(`/api/ideas/plans/${planId}/items`, { method: "POST", body: JSON.stringify(body) }),
  updateEditorialItem: (planId, itemId, body) =>
    req(`/api/ideas/plans/${planId}/items/${itemId}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),
  deleteEditorialItem: (planId, itemId) =>
    req(`/api/ideas/plans/${planId}/items/${itemId}`, { method: "DELETE" }),
  reorderEditorialItems: (planId, itemIds) =>
    req(`/api/ideas/plans/${planId}/reorder`, {
      method: "POST",
      body: JSON.stringify({ item_ids: itemIds }),
    }),
  suggestEditorialItems: (planId, body) =>
    req(`/api/ideas/plans/${planId}/suggest`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  convertEditorialItem: (planId, itemId) =>
    req(`/api/ideas/plans/${planId}/items/${itemId}/convert`, {
      method: "POST",
      body: JSON.stringify({ start: true }),
    }),

  // Settings
  getSettings: () => req("/api/settings"),
  updateSettings: (body) =>
    req("/api/settings", { method: "PATCH", body: JSON.stringify(body) }),
};
