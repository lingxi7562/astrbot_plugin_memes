const bridge = window.AstrBotPluginPage;

let allMemes = [];
let filtered = [];
let currentConfig = {};
let currentMeme = null;
let thumbnailObserver = null;
const thumbnailLoaders = new WeakMap();

const gallery = document.getElementById("gallery");
const empty = document.getElementById("empty");
const loading = document.getElementById("loading");
const stats = document.getElementById("stats");
const filterInput = document.getElementById("filter-input");
const refreshBtn = document.getElementById("refresh-btn");
const settingsBtn = document.getElementById("settings-btn");
const settingsPanel = document.getElementById("settings-panel");
const settingsOverlay = document.getElementById("settings-overlay");
const settingsClose = document.getElementById("settings-close");
const settingsSave = document.getElementById("settings-save");
const lightbox = document.getElementById("lightbox");
const lightboxImg = document.getElementById("lightbox-img");
const lightboxInfo = document.getElementById("lightbox-info");
const lightboxClose = document.getElementById("lightbox-close");
const feedbackPositive = document.getElementById("feedback-positive");
const feedbackNegative = document.getElementById("feedback-negative");
const feedbackStatus = document.getElementById("feedback-status");

function showLoading() {
  loading.style.display = "flex";
  gallery.style.display = "none";
  empty.style.display = "none";
}

function hideLoading() {
  loading.style.display = "none";
}

function updateStats(text) {
  stats.textContent = text;
}

function showEmpty(title, hint) {
  gallery.style.display = "none";
  empty.style.display = "flex";
  empty.querySelector(".empty-text").textContent = title;
  empty.querySelector(".empty-hint").textContent = hint;
}

function readListItems(response) {
  if (Array.isArray(response)) {
    return response;
  }
  if (response && Array.isArray(response.items)) {
    return response.items;
  }
  throw new Error("列表响应格式无效");
}

async function fetchAllMemes() {
  const first = await bridge.apiGet("list", {
    page: 1,
    page_size: 100,
    sort: "filename",
  });
  const items = readListItems(first);
  if (Array.isArray(first)) {
    return items;
  }

  const pageCount = Number.isSafeInteger(first.pages) && first.pages > 0
    ? first.pages
    : 1;
  for (let page = 2; page <= pageCount; page += 1) {
    const response = await bridge.apiGet("list", {
      page,
      page_size: 100,
      sort: "filename",
    });
    items.push(...readListItems(response));
  }
  return items;
}

async function load() {
  showLoading();
  try {
    allMemes = await fetchAllMemes();
    filtered = [...allMemes];
    render();
    updateStats(`共 ${allMemes.length} 张`);
  } catch (err) {
    updateStats("加载失败");
    showEmpty("加载失败", "请检查索引配置后重试");
    console.error(err);
  } finally {
    hideLoading();
  }
}

function scheduleThumbnailLoad(target, loader) {
  if (!("IntersectionObserver" in window)) {
    void loader();
    return;
  }
  if (!thumbnailObserver) {
    thumbnailObserver = new IntersectionObserver((entries) => {
      for (const entry of entries) {
        if (!entry.isIntersecting) continue;
        thumbnailObserver.unobserve(entry.target);
        const loadThumbnail = thumbnailLoaders.get(entry.target);
        thumbnailLoaders.delete(entry.target);
        if (loadThumbnail) void loadThumbnail();
      }
    }, { rootMargin: "160px" });
  }
  thumbnailLoaders.set(target, loader);
  thumbnailObserver.observe(target);
}

function appendThumbnail(imgWrap, meme) {
  const filename = typeof meme.filename === "string" ? meme.filename : "";
  const img = document.createElement("img");
  img.className = "thumb";
  img.loading = "lazy";
  img.alt = filename;
  img.addEventListener("click", () => openLightbox(meme));

  if (typeof meme.thumb_b64 === "string" && meme.thumb_b64) {
    img.src = `data:image/jpeg;base64,${meme.thumb_b64}`;
    imgWrap.appendChild(img);
    return;
  }

  if (typeof meme.id !== "string" || !meme.id) {
    const placeholder = document.createElement("div");
    placeholder.className = "img-placeholder";
    placeholder.textContent = "无图";
    imgWrap.appendChild(placeholder);
    return;
  }

  const placeholder = document.createElement("div");
  placeholder.className = "img-placeholder";
  placeholder.textContent = "加载中…";
  img.style.display = "none";
  imgWrap.appendChild(img);
  imgWrap.appendChild(placeholder);

  scheduleThumbnailLoad(imgWrap, async () => {
    try {
      const result = await bridge.apiGet("thumbnail", { id: meme.id });
      if (!img.isConnected) return;
      if (result && typeof result.thumb_b64 === "string" && result.thumb_b64) {
        meme.thumb_b64 = result.thumb_b64;
        img.src = `data:image/jpeg;base64,${result.thumb_b64}`;
        img.style.display = "";
        placeholder.remove();
      } else {
        placeholder.textContent = "无图";
      }
    } catch (err) {
      if (placeholder.isConnected) placeholder.textContent = "缩略图不可用";
      console.error("加载缩略图失败:", err);
    }
  });
}

function render(list) {
  list = list || filtered;
  if (thumbnailObserver) {
    thumbnailObserver.disconnect();
    thumbnailObserver = null;
  }
  gallery.replaceChildren();

  if (list.length === 0) {
    const hasFilter = filterInput.value.trim().length > 0;
    showEmpty(
      hasFilter ? "没有匹配的表情包" : "表情包库为空",
      hasFilter ? "试试其他关键词" : "请检查索引文件配置"
    );
    return;
  }

  empty.style.display = "none";
  gallery.style.display = "grid";

  for (const meme of list) {
    const card = document.createElement("article");
    card.className = "card";
    card.style.animationDelay = `${Math.min(gallery.children.length * 0.02, 0.4)}s`;

    const imgWrap = document.createElement("div");
    imgWrap.className = "img-wrap";

    appendThumbnail(imgWrap, meme);

    const info = document.createElement("div");
    info.className = "info";

    const name = document.createElement("div");
    name.className = "filename";
    const filename = typeof meme.filename === "string" ? meme.filename : "";
    name.textContent = filename;
    name.title = filename;
    info.appendChild(name);

    const tagsDiv = document.createElement("div");
    tagsDiv.className = "tags";
    const tags = Array.isArray(meme.tags)
      ? meme.tags.filter((tag) => typeof tag === "string")
      : [];
    for (const tag of tags.slice(0, 6)) {
      const tagSpan = document.createElement("span");
      tagSpan.className = "tag";
      tagSpan.textContent = tag;
      tagSpan.title = `点击筛选「${tag}」`;
      tagSpan.addEventListener("click", (e) => {
        e.stopPropagation();
        filterInput.value = tag;
        applyFilter(tag);
      });
      tagsDiv.appendChild(tagSpan);
    }
    const tagCount = tags.length;
    if (tagCount > 6) {
      const more = document.createElement("span");
      more.className = "tag tag-more";
      more.textContent = `+${tagCount - 6}`;
      tagsDiv.appendChild(more);
    }
    info.appendChild(tagsDiv);

    card.appendChild(imgWrap);
    card.appendChild(info);
    gallery.appendChild(card);
  }
}

function applyFilter(query) {
  const q = (query !== undefined ? query : filterInput.value)
    .toLowerCase()
    .trim();
  if (!q) {
    filtered = [...allMemes];
  } else {
    filtered = allMemes.filter(
      (m) => {
        const tags = Array.isArray(m.tags)
          ? m.tags.filter((tag) => typeof tag === "string")
          : [];
        const filename = typeof m.filename === "string" ? m.filename : "";
        return tags.some((tag) => tag.toLowerCase().includes(q)) ||
          filename.toLowerCase().includes(q);
      }
    );
  }
  render();
  updateStats(
    filtered.length === allMemes.length
      ? `共 ${allMemes.length} 张`
      : `${filtered.length} / ${allMemes.length} 张`
  );
}

let debounceTimer;
filterInput.addEventListener("input", () => {
  clearTimeout(debounceTimer);
  debounceTimer = setTimeout(() => applyFilter(), 200);
});

refreshBtn.addEventListener("click", async () => {
  refreshBtn.disabled = true;
  refreshBtn.classList.add("spinning");
  refreshBtn.querySelector("span").textContent = "刷新中";
  try {
    const result = await bridge.apiPost("refresh");
    if (result.status === "ok") {
      updateStats(`已刷新 · ${result.count} 张`);
      await load();
    } else {
      updateStats("刷新失败: " + (result.message || "未知错误"));
    }
  } catch (err) {
    updateStats("刷新失败: " + err.message);
  } finally {
    refreshBtn.disabled = false;
    refreshBtn.classList.remove("spinning");
    refreshBtn.querySelector("span").textContent = "刷新";
  }
});

function openLightbox(meme) {
  if (!meme.thumb_b64) return;
  currentMeme = meme;
  feedbackStatus.textContent = "";
  lightboxImg.src = `data:image/jpeg;base64,${meme.thumb_b64}`;
  lightboxImg.alt = typeof meme.filename === "string" ? meme.filename : "";
  const tags = Array.isArray(meme.tags)
    ? meme.tags.filter((tag) => typeof tag === "string").join(" · ")
    : "";
  const name = document.createElement("span");
  name.className = "lb-name";
  name.textContent = lightboxImg.alt;
  lightboxInfo.replaceChildren(name);
  if (tags) {
    const tagList = document.createElement("span");
    tagList.className = "lb-tags";
    tagList.textContent = tags;
    lightboxInfo.appendChild(tagList);
  }
  lightbox.style.display = "flex";
}

async function submitFeedback(rating) {
  if (!currentMeme || typeof currentMeme.id !== "string") return;
  feedbackPositive.disabled = true;
  feedbackNegative.disabled = true;
  feedbackStatus.textContent = "提交中…";
  try {
    const result = await bridge.apiPost("feedback", {
      id: currentMeme.id,
      rating,
    });
    feedbackStatus.textContent = result && result.status === "ok"
      ? "已记录，谢谢反馈"
      : "反馈未保存";
  } catch (err) {
    feedbackStatus.textContent = "反馈失败，请稍后重试";
    console.error("提交反馈失败:", err);
  } finally {
    feedbackPositive.disabled = false;
    feedbackNegative.disabled = false;
  }
}

function closeLightbox() {
  lightbox.style.display = "none";
  lightboxImg.src = "";
  currentMeme = null;
}

lightboxClose.addEventListener("click", closeLightbox);
feedbackPositive.addEventListener("click", () => void submitFeedback(1));
feedbackNegative.addEventListener("click", () => void submitFeedback(-1));
lightbox.querySelector(".lightbox-backdrop").addEventListener("click", closeLightbox);
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") {
    if (settingsPanel.classList.contains("open")) {
      closeSettings();
    } else {
      closeLightbox();
    }
  }
});

async function loadConfig() {
  try {
    const cfg = await bridge.apiGet("config");
    currentConfig = cfg;

    document.getElementById("setting-match-mode").value = cfg.match_mode;
    document.getElementById("setting-fallback").checked = cfg.embedding_fallback;
    document.getElementById("setting-max-candidates").value = cfg.max_match_candidates;
    document.getElementById("setting-min-score").value = cfg.min_tag_score;
    document.getElementById("setting-thumb-size").value = cfg.thumbnail_size;
    document.getElementById("setting-auto-refresh").checked = cfg.auto_refresh;

    const embSelect = document.getElementById("setting-emb-provider");
    embSelect.replaceChildren();
    const automaticOption = document.createElement("option");
    automaticOption.value = "";
    automaticOption.textContent = "自动选择";
    embSelect.appendChild(automaticOption);
    for (const p of cfg.available_embedding_providers || []) {
      if (!p || typeof p.id !== "string") continue;
      const option = document.createElement("option");
      const providerType = typeof p.type === "string" ? p.type : "unknown";
      const providerDim = Number.isFinite(p.dim) ? p.dim : "?";
      option.value = p.id;
      option.textContent = `${p.id} (${providerType}, ${providerDim}维)`;
      option.selected = p.id === cfg.embedding_provider_id;
      embSelect.appendChild(option);
    }
    embSelect.value = cfg.embedding_provider_id;

    updateEmbeddingStatus(cfg);
    toggleEmbeddingFields(cfg.match_mode);
  } catch (err) {
    console.error("加载配置失败:", err);
  }
}

function updateEmbeddingStatus(cfg) {
  const statusEl = document.getElementById("embedder-status");
  if (!cfg.available_embedding_providers || cfg.available_embedding_providers.length === 0) {
    statusEl.textContent = "未检测到可用的 Embedding Provider";
    statusEl.className = "setting-hint setting-warn";
  } else if (cfg.embedder_status === "on") {
    statusEl.textContent = "已启用，每次搜索实时计算向量";
    statusEl.className = "setting-hint setting-ok";
  } else {
    statusEl.textContent = `已检测到 ${cfg.available_embedding_providers.length} 个 Provider（未绑定）`;
    statusEl.className = "setting-hint";
  }
}

function toggleEmbeddingFields(mode) {
  const embSettings = document.getElementById("embedding-settings");
  const fallback = document.getElementById("setting-fallback").closest(".setting-group");
  if (mode === "keyword") {
    embSettings.style.opacity = "0.5";
    embSettings.style.pointerEvents = "none";
    fallback.style.opacity = "0.5";
    fallback.style.pointerEvents = "none";
  } else {
    embSettings.style.opacity = "";
    embSettings.style.pointerEvents = "";
    fallback.style.opacity = "";
    fallback.style.pointerEvents = "";
  }
}

document.getElementById("setting-match-mode").addEventListener("change", (e) => {
  toggleEmbeddingFields(e.target.value);
});

function openSettings() {
  settingsPanel.classList.add("open");
  settingsOverlay.style.display = "block";
  loadConfig();
}

function closeSettings() {
  settingsPanel.classList.remove("open");
  settingsOverlay.style.display = "none";
}

settingsBtn.addEventListener("click", openSettings);
settingsClose.addEventListener("click", closeSettings);
settingsOverlay.addEventListener("click", closeSettings);

settingsSave.addEventListener("click", async () => {
  settingsSave.disabled = true;
  settingsSave.textContent = "保存中...";
  try {
    const payload = {
      match_mode: document.getElementById("setting-match-mode").value,
      embedding_provider_id: document.getElementById("setting-emb-provider").value,
      embedding_fallback: document.getElementById("setting-fallback").checked,
      max_match_candidates: parseInt(document.getElementById("setting-max-candidates").value) || 10,
      min_tag_score: parseFloat(document.getElementById("setting-min-score").value) || 0.0,
      thumbnail_size: parseInt(document.getElementById("setting-thumb-size").value) || 200,
      auto_refresh: document.getElementById("setting-auto-refresh").checked,
    };
    const result = await bridge.apiPost("config", payload);
    if (result.status === "ok") {
      settingsSave.textContent = "已保存";
      settingsSave.classList.add("saved");
      setTimeout(() => {
        settingsSave.textContent = "保存设置";
        settingsSave.classList.remove("saved");
      }, 2000);
      updateStats(result.message);
      await loadConfig();
    } else {
      settingsSave.textContent = "保存失败";
      setTimeout(() => {
        settingsSave.textContent = "保存设置";
      }, 2000);
    }
  } catch (err) {
    settingsSave.textContent = "保存失败";
    setTimeout(() => {
      settingsSave.textContent = "保存设置";
    }, 2000);
  } finally {
    settingsSave.disabled = false;
  }
});

await bridge.ready();
await load();
