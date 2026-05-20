const state = {
  view: "dashboard",
  articles: [],
  accounts: [],
  selectedArticleId: null,
  selectedAccountId: null,
  selectedAccount: null,
  sort: "value",
  search: "",
  runStatus: null,
  runTimer: null,
  dailyStats: null,
};

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => Array.from(document.querySelectorAll(selector));

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!response.ok) {
    const text = await response.text();
    throw new Error(text || response.statusText);
  }
  return response.json();
}

function toast(message) {
  const node = $("#toast");
  node.textContent = message;
  node.classList.add("show");
  window.clearTimeout(toast.timer);
  toast.timer = window.setTimeout(() => node.classList.remove("show"), 2600);
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function stripHtml(value) {
  const element = document.createElement("div");
  element.innerHTML = value || "";
  return element.textContent || element.innerText || "";
}

function formatDate(ts) {
  if (!ts) return "-";
  const date = new Date(Number(ts) * 1000);
  if (Number.isNaN(date.getTime())) return "-";
  return date.toLocaleDateString("zh-CN", { month: "2-digit", day: "2-digit" });
}

function metric(label, value, tone = "") {
  return `<div class="metric ${tone}"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value ?? "-")}</strong></div>`;
}

function runTypeLabel(value) {
  const labels = {
    full_update: "同步并总结",
    sync: "只同步",
    summarize: "只总结",
  };
  return labels[value] || value || "任务";
}

function runStatusLabel(value) {
  const labels = {
    queued: "排队中",
    running: "运行中",
    success: "成功",
    failed: "失败",
    idle: "空闲",
  };
  return labels[value] || value || "-";
}

function formatDateTime(value) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value).replace("T", " ");
  return date.toLocaleString("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function statItem(label, value) {
  if (value === undefined || value === null || value === "") return "";
  return `<div><dt>${escapeHtml(label)}</dt><dd>${escapeHtml(value)}</dd></div>`;
}

function formatCompactNumber(value) {
  if (value === null || value === undefined || value === "") return "-";
  const number = Number(value);
  if (Number.isNaN(number)) return escapeHtml(value);
  return new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 0 }).format(number);
}

function summaryMethodLabel(article) {
  const summary = article.summary || {};
  if (summary.method === "llm") return "LLM摘要";
  if (summary.method === "heuristic") return "规则摘要";
  return "未总结";
}

function articleButton(article) {
  const summary = article.summary || {};
  const tags = article.tags || summary.tags || [];
  const active = article.id === state.selectedArticleId ? " active" : "";
  return `
    <button class="article-item${active}" data-article-id="${escapeHtml(article.id)}">
      <span class="score">${Number(article.value_score || summary.value_score || 0).toFixed(1)}</span>
      <span>
        <span class="article-title">${escapeHtml(article.title)}</span>
        <span class="article-meta">
          <span>${escapeHtml(article.mp_name || "未知公众号")}</span>
          <span>${formatDate(article.publish_time)}</span>
          <span>${article.has_content ? "全文" : "仅标题/摘要"}</span>
          <span>${escapeHtml(summaryMethodLabel(article))}</span>
          ${(tags || []).slice(0, 3).map((tag) => `<span class="tag">${escapeHtml(tag)}</span>`).join("")}
        </span>
      </span>
    </button>
  `;
}

function accountArticleItem(article) {
  const summary = article.summary || {};
  const tags = article.tags || summary.tags || [];
  return `
    <article class="account-article-item">
      <div class="account-article-head">
        <button class="article-link-button" data-article-id="${escapeHtml(article.id)}">
          <span class="article-link">${escapeHtml(article.title)}</span>
        </button>
        <span class="score small">${Number(article.value_score || summary.value_score || 0).toFixed(1)}</span>
      </div>
      <div class="article-meta">
        <span>${formatDate(article.publish_time)}</span>
        <span>${article.has_content ? "全文" : "仅标题/摘要"}</span>
        <span>${escapeHtml(summaryMethodLabel(article))}</span>
        ${(tags || []).slice(0, 4).map((tag) => `<span class="tag">${escapeHtml(tag)}</span>`).join("")}
        ${article.url ? `<a class="inline-link" href="${escapeHtml(article.url)}" target="_blank" rel="noreferrer">原文</a>` : ""}
      </div>
      <p class="account-article-summary">${escapeHtml(summary.one_sentence || article.description || "暂无摘要")}</p>
    </article>
  `;
}

function renderMetrics(data) {
  const articles = data.articles || {};
  const accounts = data.accounts || {};
  $("#metrics").innerHTML = [
    metric("文章", articles.total || 0),
    metric("全文", articles.full_text || 0),
    metric("已总结", articles.summarized || 0),
    metric("作者", accounts.total || 0),
    metric("均分", articles.avg_score ? Number(articles.avg_score).toFixed(1) : "-"),
  ].join("");

  const runs = data.runs || [];
  $("#runList").innerHTML =
    runs
      .map(
        (run) => `
          <div class="run">
            <strong>${escapeHtml(run.run_type)} · ${escapeHtml(run.status)}</strong>
            <small>${escapeHtml(run.started_at || "")}</small>
            <small>${escapeHtml(run.message || "")}</small>
          </div>
        `,
      )
      .join("") || `<div class="empty-state small">暂无运行记录。</div>`;

  $("#syncState").textContent = data.running
    ? "任务运行中"
    : data.next_run_at
      ? `下次 ${data.next_run_at.replace("T", " ")}`
      : "等待运行";
}

function renderWerssStatus(data) {
  const article = data?.system?.article || {};
  const queue = data?.content_queue || {};
  const queueText = queue.running ? "运行中" : queue.queue_size ? `${queue.queue_size} 待处理` : "空闲";
  $("#werssStatus").innerHTML = `
    <div><dt>连接</dt><dd>${data.available ? "正常" : "失败"}</dd></div>
    <div><dt>文章</dt><dd>${article.all_count ?? "-"} / 缺全文 ${article.no_content_count ?? "-"}</dd></div>
    <div><dt>补抓</dt><dd>${escapeHtml(queueText)}</dd></div>
  `;
}

function flattenStats(stats = {}) {
  if (stats.sync || stats.summaries || stats.profiles || stats.notification) {
    return [
      ["看到文章", stats.sync?.articles_seen],
      ["详情加载", stats.sync?.details_loaded],
      ["更新文章", stats.sync?.changed],
      ["缓存图片", stats.sync?.images_cached],
      ["摘要生成", stats.summaries?.summarized],
      ["LLM摘要", stats.summaries?.llm_summaries],
      ["规则摘要", stats.summaries?.heuristic_summaries],
      ["画像更新", stats.profiles?.profiled],
      ["错误", (stats.sync?.errors || 0) + (stats.summaries?.errors || 0)],
    ];
  }
  return [
    ["公众号", stats.accounts],
    ["看到文章", stats.articles_seen],
    ["详情加载", stats.details_loaded],
    ["更新文章", stats.changed],
    ["缓存图片", stats.images_cached],
    ["待总结", stats.pending],
    ["已总结", stats.summarized],
    ["LLM摘要", stats.llm_summaries],
    ["规则摘要", stats.heuristic_summaries],
    ["画像更新", stats.profiled],
    ["错误", stats.errors],
  ];
}

function renderRunStatus(status = {}) {
  state.runStatus = status;
  const running = Boolean(status.running);
  const failed = status.status === "failed";
  const progress = Number.isFinite(Number(status.progress)) ? Number(status.progress) : null;
  const taskNode = $("#taskStatus");
  taskNode.className = `task-card ${running ? "running" : failed ? "failed" : "idle"}`;
  $("#taskStage").textContent = status.stage || runStatusLabel(status.status);
  $("#taskPercent").textContent = progress === null ? runStatusLabel(status.status) : `${Math.round(progress)}%`;
  $("#taskBar").style.width = `${progress ?? 0}%`;

  const messageParts = [
    status.run_type ? runTypeLabel(status.run_type) : "",
    status.message || "",
    running ? "" : status.finished_at ? `完成时间 ${formatDateTime(status.finished_at)}` : "",
  ].filter(Boolean);
  $("#taskMessage").textContent = messageParts.join(" · ") || "还没有启动同步任务。";

  const statHtml = flattenStats(status.stats || {})
    .filter(([, value]) => value !== undefined && value !== null)
    .slice(0, 8)
    .map(([label, value]) => statItem(label, value))
    .join("");
  $("#taskStats").innerHTML = statHtml || statItem("状态", runStatusLabel(status.status || "idle"));

  ["#runFull", "#runSync", "#runSummarize"].forEach((selector) => {
    const button = $(selector);
    if (button) button.disabled = running;
  });

  $("#syncState").textContent = running
    ? `${runTypeLabel(status.run_type)}运行中`
    : status.latest_run
      ? `上次 ${runStatusLabel(status.latest_run.status)}`
      : $("#syncState").textContent;
}

async function loadRunStatus() {
  const status = await api("/api/run/status");
  renderRunStatus(status);
  if (status.running) {
    startRunPolling();
  } else {
    stopRunPolling();
  }
  return status;
}

function startRunPolling() {
  if (state.runTimer) return;
  state.runTimer = window.setInterval(async () => {
    try {
      const status = await api("/api/run/status");
      const wasRunning = Boolean(state.runStatus?.running);
      renderRunStatus(status);
      if (!status.running) {
        stopRunPolling();
        if (wasRunning) await refreshAll();
      }
    } catch (error) {
      toast(error.message);
    }
  }, 2000);
}

function stopRunPolling() {
  if (!state.runTimer) return;
  window.clearInterval(state.runTimer);
  state.runTimer = null;
}

function renderTopArticles() {
  const top = state.articles.slice(0, 8);
  $("#topArticles").innerHTML = top.map(articleButton).join("") || `<div class="empty-state small">暂无文章。</div>`;
}

function renderArticleList() {
  $("#articleList").innerHTML =
    state.articles.map(articleButton).join("") || `<div class="empty-state small">暂无文章。</div>`;
}

function renderAccounts() {
  $("#accountGrid").innerHTML =
    state.accounts
      .map((account) => {
        const profile = account.profile || {};
        const strengths = profile.strengths || [];
        const tags = account.tags || profile.tags || [];
        const active = account.id === state.selectedAccountId ? " active" : "";
        return `
          <button class="account-card${active}" data-account-id="${escapeHtml(account.id)}">
            <div class="account-card-head">
              <h3>${escapeHtml(account.mp_name)}</h3>
              <span class="score small">${Number(account.score || profile.score || 0).toFixed(1)}</span>
            </div>
            <p>${escapeHtml(profile.capability_judgment || account.mp_intro || "暂无作者画像")}</p>
            <div class="article-meta">
              <span>${escapeHtml(account.article_count || 0)} 篇</span>
              <span>${escapeHtml(account.full_text_count || 0)} 全文</span>
              <span>${escapeHtml(account.confidence || profile.confidence || "待校准")}</span>
            </div>
            <div class="article-meta">${tags.slice(0, 5).map((tag) => `<span class="tag">${escapeHtml(tag)}</span>`).join("")}</div>
            <p class="account-strengths">${escapeHtml(strengths.slice(0, 3).join(" / ") || "暂无重点标签")}</p>
          </button>
        `;
      })
      .join("") || `<div class="empty-state small">暂无作者画像。</div>`;
}

function renderAccountDetail(account) {
  if (!account) {
    $("#accountDetail").innerHTML = `<div class="empty-state">点击左侧公众号作者卡片，查看该作者的文章和画像细节。</div>`;
    return;
  }
  const profile = account.profile || {};
  const tags = account.tags || profile.tags || [];
  const strengths = profile.strengths || [];
  const weaknesses = profile.weaknesses || [];
  const useCases = profile.best_use_cases || [];
  const articles = account.articles || [];

  $("#accountDetail").innerHTML = `
    <div class="account-detail-head">
      <div>
        <h3>${escapeHtml(account.mp_name)}</h3>
        <p>${escapeHtml(profile.capability_judgment || account.mp_intro || "暂无画像说明")}</p>
      </div>
      <div class="account-detail-score">
        <span>作者评分</span>
        <strong>${Number(account.score || profile.score || 0).toFixed(1)}</strong>
      </div>
    </div>

    <div class="detail-chip-row">
      ${tags.map((tag) => `<span class="tag">${escapeHtml(tag)}</span>`).join("")}
    </div>

    <div class="account-panels">
      <section class="subpanel">
        <h4>擅长方向</h4>
        <ul>${strengths.map((item) => `<li>${escapeHtml(item)}</li>`).join("") || "<li>暂无</li>"}</ul>
      </section>
      <section class="subpanel">
        <h4>使用限制</h4>
        <ul>${weaknesses.map((item) => `<li>${escapeHtml(item)}</li>`).join("") || "<li>暂无</li>"}</ul>
      </section>
      <section class="subpanel">
        <h4>推荐使用方式</h4>
        <ul>${useCases.map((item) => `<li>${escapeHtml(item)}</li>`).join("") || "<li>暂无</li>"}</ul>
      </section>
    </div>

    <section class="account-article-list">
      <div class="panel-head slim">
        <h4>该公众号文章</h4>
        <span>${escapeHtml(articles.length)} 篇样本</span>
      </div>
      <div class="account-article-items">
        ${articles.map(accountArticleItem).join("") || `<div class="empty-state small">暂无文章样本。</div>`}
      </div>
    </section>
  `;
}

function renderArticleDetail(article) {
  state.selectedArticleId = article.id;
  const summary = article.summary || {};
  const tags = article.tags || summary.tags || [];
  const fallbackText = stripHtml(article.content_text || article.description || "");
  const plainParagraphs = fallbackText
    .split(/\n{2,}|(?<=[。！？!?])/)
    .map((line) => line.trim())
    .filter(Boolean)
    .slice(0, 80);
  const contentHtml = article.rendered_html
    ? `<div class="article-rich-content">${article.rendered_html}</div>`
    : `<div class="article-content">${plainParagraphs.map((line) => `<p>${escapeHtml(line)}</p>`).join("") || "<p>暂无正文。</p>"}</div>`;

  $("#articleDetail").innerHTML = `
    <h3>${escapeHtml(article.title)}</h3>
    <div class="article-meta">
      <span>${escapeHtml(article.mp_name)}</span>
      <span>${formatDate(article.publish_time)}</span>
      <span>评分 ${Number(article.value_score || summary.value_score || 0).toFixed(1)}</span>
      <span>${escapeHtml(summaryMethodLabel(article))}</span>
      ${tags.slice(0, 5).map((tag) => `<span class="tag">${escapeHtml(tag)}</span>`).join("")}
    </div>
    <div class="detail-actions">
      <button class="action primary" id="markRead">${article.read_status === "read" ? "标为未读" : "标为已读"}</button>
      <button class="action" id="toggleFavorite">${article.favorite ? "取消收藏" : "收藏"}</button>
      ${article.url ? `<a class="action" href="${escapeHtml(article.url)}" target="_blank" rel="noreferrer">打开原文</a>` : ""}
    </div>
    <section class="article-summary">
      <strong>${escapeHtml(summary.one_sentence || "暂无摘要")}</strong>
      <p class="summary-thesis">${escapeHtml(summary.thesis || "")}</p>
      <ul>
        ${(summary.key_points || []).map((point) => `<li>${escapeHtml(point)}</li>`).join("")}
      </ul>
      <p>${escapeHtml(summary.why_read || "")}</p>
      <div class="article-meta">
        <span>难度 ${escapeHtml(summary.difficulty || "-")}</span>
        <span>置信度 ${escapeHtml(summary.confidence || "-")}</span>
        <span>阅读时长 ${escapeHtml(summary.reading_time_minutes || "-")} 分钟</span>
        <span>${summary.limited_evidence ? "证据有限" : "证据较完整"}</span>
      </div>
    </section>
    ${contentHtml}
  `;

  $("#markRead").addEventListener("click", async () => {
    const next = article.read_status === "read" ? "unread" : "read";
    await api(`/api/articles/${encodeURIComponent(article.id)}`, {
      method: "PATCH",
      body: JSON.stringify({ read_status: next }),
    });
    toast(next === "read" ? "已标为已读" : "已标为未读");
    await loadArticles();
    const updated = await api(`/api/articles/${encodeURIComponent(article.id)}`);
    renderArticleDetail(updated);
  });

  $("#toggleFavorite").addEventListener("click", async () => {
    await api(`/api/articles/${encodeURIComponent(article.id)}`, {
      method: "PATCH",
      body: JSON.stringify({ favorite: !article.favorite }),
    });
    toast(!article.favorite ? "已收藏" : "已取消收藏");
    await loadArticles();
    const updated = await api(`/api/articles/${encodeURIComponent(article.id)}`);
    renderArticleDetail(updated);
  });
}

async function loadDashboard() {
  const data = await api("/api/dashboard");
  renderMetrics(data);
}

async function loadWerssStatus() {
  const data = await api("/api/werss/status");
  renderWerssStatus(data);
}

async function loadArticles() {
  const params = new URLSearchParams({ sort: state.sort, limit: "80" });
  if (state.search) params.set("search", state.search);
  const data = await api(`/api/articles?${params}`);
  state.articles = data.list || [];
  renderTopArticles();
  renderArticleList();
}

async function loadAccounts() {
  state.accounts = await api("/api/accounts");
  renderAccounts();
  if (state.selectedAccountId) {
    const match = state.accounts.find((item) => item.id === state.selectedAccountId);
    if (!match) {
      state.selectedAccountId = null;
      state.selectedAccount = null;
      renderAccountDetail(null);
    }
  }
}

async function loadAccountDetail(accountId) {
  state.selectedAccountId = accountId;
  renderAccounts();
  state.selectedAccount = await api(`/api/accounts/${encodeURIComponent(accountId)}`);
  renderAccountDetail(state.selectedAccount);
}

async function loadConfig() {
  const config = await api("/api/config");
  const form = $("#configForm");
  for (const [key, value] of Object.entries(config)) {
    const field = form.elements[key];
    if (!field) continue;
    if (field.type === "checkbox") {
      field.checked = Boolean(value);
    } else if (typeof value === "object" && value?.configured) {
      field.placeholder = value.preview || "已配置，留空则不修改";
      field.value = "";
    } else if (typeof value !== "object") {
      field.value = value ?? "";
    }
  }
}

async function loadDailyStats() {
  const data = await api("/api/stats/daily?days=14");
  state.dailyStats = data;

  const totals = data.totals || {};
  $("#dailySummary").innerHTML = [
    metric("近 14 天新增", totals.articles || 0),
    metric("近 14 天已总结", totals.summarized || 0),
    metric("累计 Token", totals.total_tokens || 0),
    metric("统计说明", "按天汇总"),
  ].join("");

  $("#dailyStatsBody").innerHTML =
    (data.rows || [])
      .map((row) => `
        <tr>
          <td>${escapeHtml(row.day)}</td>
          <td>${formatCompactNumber(row.new_articles)}</td>
          <td>${formatCompactNumber(row.summarized)}</td>
          <td>${formatCompactNumber(row.llm_summaries)}</td>
          <td>${formatCompactNumber(row.heuristic_summaries)}</td>
          <td>${formatCompactNumber(row.prompt_tokens)}</td>
          <td>${formatCompactNumber(row.completion_tokens)}</td>
          <td>${formatCompactNumber(row.total_tokens)}</td>
        </tr>
      `)
      .join("") || `<tr><td colspan="8">暂无统计数据</td></tr>`;
  $("#dailyStatsNote").textContent = data.token_note || "";
}

async function refreshAll() {
  await Promise.allSettled([
    loadDashboard(),
    loadArticles(),
    loadAccounts(),
    loadConfig(),
    loadWerssStatus(),
    loadDailyStats(),
  ]);
  await loadRunStatus();
  if (state.selectedAccountId) {
    await loadAccountDetail(state.selectedAccountId);
  }
}

function switchView(view) {
  state.view = view;
  $$(".nav-item").forEach((item) => item.classList.toggle("active", item.dataset.view === view));
  $$(".view").forEach((node) => node.classList.remove("active"));
  $(`#${view}View`).classList.add("active");
  const titles = {
    dashboard: ["总览", "按阅读价值组织你的公众号信息流。"],
    reader: ["阅读队列", "优先处理高分文章，保留已读与收藏状态。"],
    accounts: ["作者画像", "按作者能力、方向和样本文章做信息源管理。"],
    settings: ["配置", "连接 WeRSS 与任意 OpenAI-compatible 大模型接口。"],
  };
  $("#viewTitle").textContent = titles[view][0];
  $("#viewSubtitle").textContent = titles[view][1];
}

async function runTask(path, message) {
  const result = await api(path, { method: "POST" });
  toast(result.status === "busy" ? result.message : result.message || message);
  const status = await loadRunStatus();
  if (result.status !== "busy" && status.running) {
    startRunPolling();
  } else if (result.status !== "busy") {
    await refreshAll();
  }
}

async function testLlmConnection() {
  const form = $("#configForm");
  const payload = {};
  for (const field of Array.from(form.elements)) {
    if (!field.name) continue;
    if (!field.name.startsWith("llm_") && field.name !== "allow_llm") continue;
    if (field.type === "checkbox") {
      payload[field.name] = field.checked;
    } else if (field.value !== "") {
      payload[field.name] = field.type === "number" ? Number(field.value) : field.value;
    }
  }
  const node = $("#llmTestResult");
  node.className = "test-result pending";
  node.textContent = "正在测试模型连接...";
  try {
    const result = await api("/api/config/test-llm", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    const bits = [
      result.ok ? "连接成功" : "连接失败",
      result.model ? `模型 ${result.model}` : "",
      result.status_code ? `HTTP ${result.status_code}` : "",
      result.latency_ms ? `延迟 ${result.latency_ms} ms` : "",
      result.total_tokens ? `Tokens ${result.total_tokens}` : "",
      result.message || "",
    ].filter(Boolean);
    node.className = `test-result ${result.ok ? "success" : "error"}`;
    node.textContent = bits.join(" · ");
  } catch (error) {
    node.className = "test-result error";
    node.textContent = `连接测试失败 · ${error.message}`;
  }
}

async function importBackupFile(file) {
  if (!file) return;
  const node = $("#backupResult");
  node.className = "test-result pending";
  node.textContent = `正在导入 ${file.name}，请不要关闭页面...`;
  const form = new FormData();
  form.append("file", file);
  try {
    const result = await api("/api/backup/import", {
      method: "POST",
      headers: {},
      body: form,
    });
    const restored = result.restored || {};
    node.className = "test-result success";
    node.textContent = [
      "导入完成",
      restored.database ? "数据库已恢复" : "",
      restored.config ? "配置已恢复" : "",
      `媒体 ${restored.media_files || 0} 个`,
    ].filter(Boolean).join(" · ");
    await refreshAll();
  } catch (error) {
    node.className = "test-result error";
    node.textContent = `导入失败 · ${error.message}`;
  }
}

function bindEvents() {
  $$(".nav-item").forEach((item) => item.addEventListener("click", () => switchView(item.dataset.view)));
  $$("[data-view-link]").forEach((item) => item.addEventListener("click", () => switchView(item.dataset.viewLink)));

  $("#refreshAll").addEventListener("click", async () => {
    await refreshAll();
    toast("面板已刷新");
  });

  $("#searchInput").addEventListener("input", debounce(async (event) => {
    state.search = event.target.value.trim();
    await loadArticles();
  }, 250));

  $("#sortSelect").addEventListener("change", async (event) => {
    state.sort = event.target.value;
    await loadArticles();
  });

  document.addEventListener("click", async (event) => {
    const articleTarget = event.target.closest("[data-article-id]");
    if (articleTarget) {
      const article = await api(`/api/articles/${encodeURIComponent(articleTarget.dataset.articleId)}`);
      switchView("reader");
      renderArticleDetail(article);
      renderArticleList();
      return;
    }

    const accountTarget = event.target.closest("[data-account-id]");
    if (accountTarget) {
      await loadAccountDetail(accountTarget.dataset.accountId);
    }
  });

  $("#runFull").addEventListener("click", () => runTask("/api/run/full", "已启动同步并总结"));
  $("#runSync").addEventListener("click", () => runTask("/api/run/sync", "已启动同步"));
  $("#runSummarize").addEventListener("click", () => runTask("/api/run/summarize", "已启动总结"));
  $("#testLlm").addEventListener("click", testLlmConnection);
  $("#refreshDailyStats").addEventListener("click", async () => {
    await loadDailyStats();
    toast("近况看板已刷新");
  });
  $("#exportBackup").addEventListener("click", () => {
    $("#backupResult").className = "test-result pending";
    $("#backupResult").textContent = "正在生成备份包，浏览器会自动下载。";
    window.location.href = "/api/backup/export";
  });
  $("#importBackup").addEventListener("change", async (event) => {
    await importBackupFile(event.target.files?.[0]);
    event.target.value = "";
  });

  $("#configForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const payload = {};
    for (const field of Array.from(form.elements)) {
      if (!field.name) continue;
      if (field.type === "checkbox") {
        payload[field.name] = field.checked;
      } else if (field.value !== "") {
        payload[field.name] = field.type === "number" ? Number(field.value) : field.value;
      }
    }
    await api("/api/config", { method: "PUT", body: JSON.stringify(payload) });
    toast("配置已保存");
    await loadConfig();
  });
}

function debounce(fn, wait) {
  let timer;
  return (...args) => {
    window.clearTimeout(timer);
    timer = window.setTimeout(() => fn(...args), wait);
  };
}

bindEvents();
renderAccountDetail(null);
refreshAll().catch((error) => toast(error.message));
