const bridge = window.AstrBotPluginPage;
const PAGE_SIZE = 30;

let currentPage = 1;
let totalCount = 0;
let hasMore = false;
let currentSearch = "";
let isLoading = false;
let currentConfig = {};
let loadObserver = null;

const gallery = document.getElementById("gallery");
const empty = document.getElementById("empty");
const loading = document.getElementById("loading");
const loadSentinel = document.getElementById("load-sentinel");
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

function showLoading() {
  loading.style.display = "flex";
  gallery.style.display = "none";
  empty.style.display = "none";
  loadSentinel.style.display = "none";
}

function hideLoading() {
  loading.style.display = "none";
  gallery.style.display = "grid";
}

function updateStats(text) {
  stats.textContent = text;
}

function showSentinel(show) {
  loadSentinel.style.display = show ? "flex" : "none";
}

function sentinelText(text) {
  const span = loadSentinel.querySelector("span");
  if (span) span.textContent = text;
}

async function loadFirst() {
  currentPage = 1;
  currentSearch = filterInput.value.trim();
  gallery.innerHTML = "";
  showLoading();
  try {
    const params = { page: 1, page_size: PAGE_SIZE };
    if (currentSearch) params.search = currentSearch;
    const data = await bridge.apiGet("list", params);
    totalCount = data.total;
    hasMore = data.has_more;
    currentPage = 1;
    renderPage(data.items, true);
    if (data.total === 0) {
      gallery.style.display = "none";
      empty.style.display = "flex";
      empty.querySelector(".empty-text").textContent = currentSearch
        ? "没有匹配的表情包"
        : "表情包库为空";
      empty.querySelector(".empty-hint").textContent = currentSearch
        ? "试试其他关键词"
        : "请检查索引文件配置";
      showSentinel(false);
    } else {
      updateStats(
        currentSearch
          ? `搜索: ${currentSearch} · ${totalCount} 张`
          : `共 ${totalCount} 张`
      );
    }
  } catch (err) {
    updateStats("加载失败");
    gallery.style.display = "none";
    empty.style.display = "flex";
    empty.querySelector(".empty-text").textContent = "加载失败";
    empty.querySelector(".empty-hint").textContent = err.message;
    showSentinel(false);
    console.error(err);
  } finally {
    hideLoading();
  }
}

async function loadMore() {
  if (isLoading || !hasMore) return;
  isLoading = true;
  sentinelText("加载中...");
  const nextPage = currentPage + 1;
  try {
    const params = { page: nextPage, page_size: PAGE_SIZE };
    if (currentSearch) params.search = currentSearch;
    const data = await bridge.apiGet("list", params);
    hasMore = data.has_more;
    currentPage = data.page;
    totalCount = data.total;
    renderPage(data.items, false);
  } catch (err) {
    sentinelText("加载失败，下滑重试");
    console.error(err);
  } finally {
    isLoading = false;
  }
}

function renderPage(items, reset) {
  if (reset) {
    gallery.innerHTML = "";
    empty.style.display = "none";
    gallery.style.display = "grid";
  }

  if (items.length === 0) {
    showSentinel(false);
    return;
  }

  for (const meme of items) {
    const card = document.createElement("article");
    card.className = "card";
    card.style.animationDelay = `${Math.min(gallery.children.length * 0.01, 0.3)}s`;

    const imgWrap = document.createElement("div");
    imgWrap.className = "img-wrap";

    if (meme.thumb_b64) {
      const img = document.createElement("img");
      img.className = "thumb";
      img.loading = "lazy";
      img.src = `data:image/jpeg;base64,${meme.thumb_b64}`;
      img.alt = meme.filename;
      img.addEventListener("click", () => openLightbox(meme));
      imgWrap.appendChild(img);
    } else {
      const placeholder = document.createElement("div");
      placeholder.className = "img-placeholder";
      placeholder.textContent = "无图";
      imgWrap.appendChild(placeholder);
    }

    const info = document.createElement("div");
    info.className = "info";

    const name = document.createElement("div");
    name.className = "filename";
    name.textContent = meme.filename;
    name.title = meme.filename;
    info.appendChild(name);

    const tagsDiv = document.createElement("div");
    tagsDiv.className = "tags";
    for (const tag of (meme.tags || []).slice(0, 6)) {
      const tagSpan = document.createElement("span");
      tagSpan.className = "tag";
      tagSpan.textContent = tag;
      tagSpan.title = `点击筛选「${tag}」`;
      tagSpan.addEventListener("click", (e) => {
        e.stopPropagation();
        filterInput.value = tag;
        loadFirst();
      });
      tagsDiv.appendChild(tagSpan);
    }
    const tagCount = (meme.tags || []).length;
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

  if (hasMore) {
    showSentinel(true);
    sentinelText(`已加载 ${gallery.children.length} / ${totalCount} · 继续下滑`);
  } else {
    if (totalCount > PAGE_SIZE) {
      showSentinel(true);
      sentinelText(`已显示全部 ${totalCount} 张`);
    } else {
      showSentinel(false);
    }
  }
}

let debounceTimer;
filterInput.addEventListener("input", () => {
  clearTimeout(debounceTimer);
  debounceTimer = setTimeout(() => loadFirst(), 300);
});

function setupLoadObserver() {
  if (loadObserver) loadObserver.disconnect();
  loadObserver = new IntersectionObserver(
    (entries) => {
      if (entries[0].isIntersecting && hasMore && !isLoading) {
        loadMore();
      }
    },
    { rootMargin: "200px" }
  );
  loadObserver.observe(loadSentinel);
}

refreshBtn.addEventListener("click", async () => {
  refreshBtn.disabled = true;
  refreshBtn.classList.add("spinning");
  refreshBtn.querySelector("span").textContent = "刷新中";
  try {
    await bridge.apiPost("refresh");
    await loadFirst();
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
  lightboxImg.src = `data:image/jpeg;base64,${meme.thumb_b64}`;
  lightboxImg.alt = meme.filename;
  const tags = (meme.tags || []).join(" · ");
  lightboxInfo.innerHTML = `<span class="lb-name">${meme.filename}</span>${
    tags ? `<span class="lb-tags">${tags}</span>` : ""
  }`;
  lightbox.style.display = "flex";
}

function closeLightbox() {
  lightbox.style.display = "none";
  lightboxImg.src = "";
}

lightboxClose.addEventListener("click", closeLightbox);
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
    document.getElementById("setting-inject-enabled").checked = cfg.inject_prompt_enabled || false;
    document.getElementById("setting-inject-prompt").value = cfg.inject_prompt || "";
    toggleInjectFields();

    const embSelect = document.getElementById("setting-emb-provider");
    embSelect.innerHTML = '<option value="">自动选择</option>';
    for (const p of cfg.available_embedding_providers || []) {
      const selected = p.id === cfg.embedding_provider_id ? " selected" : "";
      embSelect.innerHTML += `<option value="${p.id}"${selected}>${p.id} (${p.type}, ${p.dim}维)</option>`;
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

document.getElementById("setting-inject-enabled").addEventListener("change", toggleInjectFields);

function toggleInjectFields() {
  const enabled = document.getElementById("setting-inject-enabled").checked;
  const group = document.getElementById("inject-prompt-group");
  if (enabled) {
    group.style.opacity = "";
    group.style.pointerEvents = "";
  } else {
    group.style.opacity = "0.5";
    group.style.pointerEvents = "none";
  }
}

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
      inject_prompt_enabled: document.getElementById("setting-inject-enabled").checked,
      inject_prompt: document.getElementById("setting-inject-prompt").value,
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
await loadFirst();
setupLoadObserver();
