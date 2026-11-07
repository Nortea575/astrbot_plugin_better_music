const bridge = window.AstrBotPluginView;

const PROVIDERS = [
  { id: "netease", label: "网易云音乐" },
  { id: "qq", label: "QQ音乐" },
  { id: "kugou", label: "酷狗音乐" },
];
const TOOL_DEFAULTS = [
  { id: "ffmpeg", label: "FFmpeg" },
  { id: "node", label: "Node.js" },
  { id: "netease", label: "网易云依赖" },
  { id: "qq", label: "QQ音乐Python依赖" },
  { id: "kugou", label: "酷狗依赖" },
];
const TEMPLATE_LABELS = {
  menu: "点歌菜单",
  search_results: "搜索结果",
  search_item: "歌曲信息",
  failure: "失败提示",
  play_selected: "已选择歌曲",
  play_sending: "开始发送",
  play_complete: "发送完成",
  song_log: "点歌日志",
};
const TEMPLATE_CATEGORIES = ["menu", "search", "playback", "errors", "log"];
const TEMPLATE_CATEGORY_BY_NAME = {
  menu: "menu",
  search_results: "search",
  search_item: "search",
  play_selected: "playback",
  play_sending: "playback",
  play_complete: "playback",
  failure: "errors",
  song_log: "log",
};
const ENVIRONMENT_CATEGORIES = ["normal", "cover"];
const LOGIN_TERMINAL_STATES = new Set([
  "success",
  "expired",
  "error",
  "cancelled",
  "canceled",
  "idle",
  "logged_out",
]);

const elements = {
  pageStatus: document.getElementById("page-status"),
  mainTabs: document.querySelector(".main-tabs"),
  mainTabIndicator: document.getElementById("main-tab-indicator"),
  environmentChecks: document.getElementById("environment-checks"),
  coverHealthChecks: document.getElementById("cover-health-checks"),
  environmentTabs: document.querySelector(".environment-tabs"),
  coverTestForm: document.getElementById("cover-test-form"),
  coverTestPlatform: document.getElementById("cover-test-platform"),
  coverTestSong: document.getElementById("cover-test-song"),
  coverTestVoice: document.getElementById("cover-test-voice"),
  coverTestSubmit: document.getElementById("cover-test-submit"),
  coverTestStatus: document.getElementById("cover-test-status"),
  coverTestResult: document.getElementById("cover-test-result"),
  coverTestResultDetails: document.getElementById("cover-test-result-details"),
  coverTestResultSegments: document.getElementById("cover-test-segments"),
  coverTestResultMessage: document.getElementById("cover-test-result-message"),
  toolList: document.getElementById("tool-list"),
  toolStatus: document.getElementById("tool-status"),
  installMissing: document.getElementById("install-missing"),
  searchForm: document.getElementById("search-form"),
  downloadStatus: document.getElementById("download-status"),
  searchResults: document.getElementById("search-results"),
  targetPlatform: document.getElementById("target-platform"),
  targetType: document.getElementById("target-type"),
  targetId: document.getElementById("target-id"),
  targetIdLabel: document.getElementById("target-id-label"),
  testForm: document.getElementById("test-form"),
  testStatus: document.getElementById("test-status"),
  fileGroups: document.getElementById("file-groups"),
  fileFilterForm: document.getElementById("file-filter-form"),
  filePlatform: document.getElementById("file-platform"),
  fileQuery: document.getElementById("file-query"),
  settingsForm: document.getElementById("settings-form"),
  settingsStatus: document.getElementById("settings-status"),
  settingsTabs: document.getElementById("settings-tabs"),
  textSettingsForm: document.getElementById("text-settings-form"),
  textSettingsStatus: document.getElementById("text-settings-status"),
  templateCategoryTabs: document.getElementById("template-category-tabs"),
  maxSongs: document.getElementById("max-songs"),
  passiveLlm: document.getElementById("passive-llm"),
  activeLlm: document.getElementById("active-llm"),
  passiveMaxSeconds: document.getElementById("passive-max-seconds"),
  passiveGapSeconds: document.getElementById("passive-gap-seconds"),
  activeVoice: document.getElementById("active-voice-id"),
  activeIncrementalOutput: document.getElementById("active-incremental-output"),
  activeIncrementalCapability: document.getElementById("active-incremental-capability"),
  activeMaxSeconds: document.getElementById("active-max-seconds"),
  activeGapSeconds: document.getElementById("active-gap-seconds"),
  songLogEnabled: document.getElementById("song-log-enabled"),
  songLogPlatform: document.getElementById("song-log-platform"),
  songLogTargetType: document.getElementById("song-log-target-type"),
  songLogTargetId: document.getElementById("song-log-target-id"),
  songLogTargetIdLabel: document.getElementById("song-log-target-id-label"),
  coverServiceUrl: document.getElementById("cover-service-url"),
  coverServiceToken: document.getElementById("cover-service-token"),
  coverClearToken: document.getElementById("cover-service-clear-token"),
  coverTokenStatus: document.getElementById("cover-token-status"),
  coverServiceStatus: document.getElementById("cover-service-status"),
  coverCheck: document.getElementById("cover-check"),
  coverGuideDownload: document.getElementById("cover-guide-download"),
  coverVoiceName: document.getElementById("cover-voice-name"),
  coverModelFile: document.getElementById("cover-model-file"),
  coverIndexFile: document.getElementById("cover-index-file"),
  coverImport: document.getElementById("cover-import"),
  coverImportStatus: document.getElementById("cover-import-status"),
  replyTemplateFields: document.getElementById("reply-template-fields"),
};

let currentOverview = null;
let currentEnvironment = null;
let activeProvider = "netease";
let activeMainTab = "environment";
let activeEnvironmentCategory = "normal";
let activeSettingsTab = "passive";
let activeTemplateCategory = "menu";
const loginPollers = new Map();
const requestIds = new Map();
let coverVoices = [];
let coverServiceDirty = false;
let coverServiceTestBlockMessage = "";
let coverTestSongsLoading = false;

function node(tag, className, text) {
  const result = document.createElement(tag);
  if (className) result.className = className;
  if (text !== undefined) result.textContent = text;
  return result;
}

function setStatus(target, message, kind = "") {
  if (!target) return;
  target.textContent = message || "";
  target.dataset.kind = kind;
  target.hidden = !message;
}

function setPageStatus(message, kind = "") {
  setStatus(elements.pageStatus, message, kind);
}

function buttonBusy(button, busy, busyText = "处理中…") {
  if (!button) return;
  if (busy) {
    if (!button.dataset.idleText) button.dataset.idleText = button.textContent;
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    button.textContent = busyText;
  } else {
    button.disabled = false;
    button.removeAttribute("aria-busy");
    if (button.dataset.idleText) button.textContent = button.dataset.idleText;
  }
}

async function apiGet(endpoint, params) {
  if (!bridge) throw new Error("AstrBot 页面接口未就绪，请刷新页面。 ");
  return bridge.apiGet(endpoint, params);
}

async function apiPost(endpoint, body) {
  if (!bridge) throw new Error("AstrBot 页面接口未就绪，请刷新页面。 ");
  return bridge.apiPost(endpoint, body);
}

async function apiUpload(endpoint, file) {
  if (!bridge || typeof bridge.upload !== "function") {
    throw new Error("当前页面接口不支持文件上传，请刷新页面或检查 AstrBot 版本。 ");
  }
  return bridge.upload(endpoint, file);
}

function requestIdFor(key) {
  if (!requestIds.has(key)) {
    const value = globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(16).slice(2)}`;
    requestIds.set(key, value);
  }
  return requestIds.get(key);
}

function clearRequestId(key) {
  requestIds.delete(key);
}

function pause(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

async function waitForJob(startResponse, onUpdate) {
  const jobId = startResponse?.job?.job_id;
  if (!jobId) throw new Error("任务没有返回编号，请刷新状态后再试。");
  let delay = 1000;
  const deadline = Date.now() + 20 * 60 * 1000;
  while (Date.now() < deadline) {
    const response = await apiGet("jobs/status", { job_id: jobId });
    const job = response.job;
    if (!job) throw new Error("任务状态响应无效。");
    if (onUpdate) onUpdate(job);
    if (job.status === "completed") return job.result;
    if (job.status === "failed") {
      const error = new Error(job.error || "后台任务失败。");
      error.jobStatus = job.status;
      throw error;
    }
    if (job.status === "cancelled") {
      const error = new Error("插件重载时已取消后台任务。");
      error.jobStatus = job.status;
      throw error;
    }
    await pause(delay);
    delay = Math.min(3500, delay + 350);
  }
  throw new Error("任务运行时间超过 20 分钟，请检查 AstrBot 日志与环境状态。");
}

function providerLabel(id) {
  return PROVIDERS.find((provider) => provider.id === id)?.label || id || "未知平台";
}

function displayStatus(value) {
  if (value === "ok") return "正常";
  if (value === "warning") return "需留意";
  if (value === "error") return "异常";
  return "未检测";
}

function renderEnvironment(environment, settings = currentOverview?.settings) {
  if (environment !== undefined) currentEnvironment = environment;
  const resolved = currentEnvironment;
  elements.environmentChecks.replaceChildren();

  if (!resolved) {
    const item = node("li", "check-row", "尚未检测环境。点击“检测环境”后读取本机工具和音乐平台依赖状态。");
    elements.environmentChecks.append(item);
  } else {
    const checks = Array.isArray(resolved.checks) ? resolved.checks : [];
    if (checks.length === 0) {
      elements.environmentChecks.append(node("li", "check-row", "检测完成，暂无额外状态。"));
    }
    for (const check of checks) {
      const row = node("li", "check-row");
      const label = node("span", "check-label", check.label || check.id || "环境检查");
      const detail = node("span", "check-detail", check.message || "");
      const status = node("span", `status-pill status-${check.status || "unknown"}`, displayStatus(check.status));
      row.append(label, detail, status);
      elements.environmentChecks.append(row);
    }
  }

  const customUrls = settings?.tool_urls || {};
  const runtimeTools = Array.isArray(resolved?.tools) ? resolved.tools : [];
  const toolById = new Map(runtimeTools.map((tool) => [tool.id, tool]));
  elements.toolList.replaceChildren();
  for (const fallback of TOOL_DEFAULTS) {
    const tool = toolById.get(fallback.id) || {};
    const row = node("div", "tool-row");
    const heading = node("div", "tool-heading");
    const title = node("h4", "tool-name", tool.label || fallback.label);
    const ready = tool.available === true;
    const statusText = ready ? "已就绪" : resolved ? "缺失" : "尚未检测";
    const status = node("span", `status-pill ${ready ? "status-ok" : resolved ? "status-error" : "status-unknown"}`, statusText);
    heading.append(title, status);

    const urlField = node("label", "field tool-url-field");
    const addressLabel = fallback.id === "qq"
      ? "SDK安装包URL（留空使用默认PyPI）"
      : "自定义下载地址（可留空使用默认源）";
    urlField.append(node("span", "", addressLabel));
    const input = node("input");
    input.type = "url";
    input.maxLength = 2048;
    input.autocomplete = "url";
    input.placeholder = tool.url || "https://";
    input.value = customUrls[fallback.id] || tool.custom_url || "";
    input.dataset.toolUrl = fallback.id;
    urlField.append(input);

    const install = node("button", "button quiet small", ready ? "重新补充" : "补充工具");
    install.type = "button";
    install.dataset.installTool = fallback.id;
    install.disabled = !resolved;
    if (ready) install.classList.add("is-ready");
    row.append(heading, urlField, install);
    elements.toolList.append(row);
  }

  elements.installMissing.disabled = !resolved || !runtimeTools.some((tool) => tool.available === false);
}

function accountSummary(account) {
  const summary = node("div", "account-lines");
  const loggedIn = account?.logged_in === true;
  const loginTag = node(
    "span",
    `status-pill ${loggedIn ? "status-ok" : "status-unknown"}`,
    loggedIn ? "已登录" : "未登录",
  );
  const name = account?.nickname || account?.user_id || (loggedIn ? "账号信息未提供" : "尚未登录");
  const accountLine = node("p", "account-line");
  accountLine.append(node("strong", "", "账号："), document.createTextNode(name));

  const membership = account?.membership || {};
  const membershipLabel = membership.label || "未知";
  const membershipLine = node("p", "account-line");
  membershipLine.append(node("strong", "", "会员："), document.createTextNode(membershipLabel));
  if (membership.expires_at) {
    membershipLine.append(document.createTextNode(`（有效期至 ${formatDate(membership.expires_at)}）`));
  }
  const message = account?.message ? node("p", "helper-text", account.message) : null;
  summary.append(loginTag, accountLine, membershipLine);
  if (message) summary.append(message);
  return summary;
}

function renderOverview(overview) {
  currentOverview = overview;
  renderPluginSettings(overview.settings);
  renderTextSettings(overview.settings, overview.template_schema);
  if (overview.environment) renderEnvironment(overview.environment, overview.settings);
  else renderEnvironment(undefined, overview.settings);

  const providers = new Map((overview.providers || []).map((item) => [item.id, item]));
  for (const provider of PROVIDERS) {
    const pane = document.querySelector(`[data-provider-panel="${provider.id}"]`);
    const accountTarget = document.getElementById(`account-${provider.id}`);
    const account = providers.get(provider.id)?.account || null;
    accountTarget.replaceChildren(accountSummary(account));
    document.querySelector(`[data-login-logout="${provider.id}"]`).hidden = !account?.logged_in;
    if (!loginPollers.has(provider.id)) {
      document.querySelector(`[data-login-start="${provider.id}"]`).hidden = false;
    }
    pane.hidden = provider.id !== activeProvider;
  }
  renderTargets(overview.targets || []);
}

function renderPluginSettings(settings = {}, force = false) {
  if (!settings || (!force && elements.settingsForm.dataset.dirty === "true")) return;
  if (Number.isInteger(settings.max_songs) && settings.max_songs > 0) {
    elements.maxSongs.value = String(settings.max_songs);
  }
  elements.passiveLlm.checked = settings.passive_llm === true;
  elements.activeLlm.checked = settings.active_llm === true;
  const passivePlayback = settings.passive_playback || {};
  const activePlayback = settings.active_playback || {};
  elements.passiveMaxSeconds.value = String(Number.isInteger(passivePlayback.max_seconds) && passivePlayback.max_seconds >= 0
    ? passivePlayback.max_seconds
    : 0);
  elements.passiveGapSeconds.value = String(Number.isFinite(passivePlayback.gap_seconds) && passivePlayback.gap_seconds >= 0
    ? passivePlayback.gap_seconds
    : 0);
  elements.activeMaxSeconds.value = String(Number.isInteger(activePlayback.max_seconds) && activePlayback.max_seconds >= 0
    ? activePlayback.max_seconds
    : 60);
  elements.activeGapSeconds.value = String(Number.isFinite(activePlayback.gap_seconds) && activePlayback.gap_seconds >= 0
    ? activePlayback.gap_seconds
    : 0);
  elements.activeIncrementalOutput.checked = activePlayback.incremental_output === true;
  const songLog = settings.song_log || {};
  elements.songLogEnabled.checked = songLog.enabled === true;
  renderSongLogTargets(currentOverview?.targets || [], typeof songLog.platform_id === "string" ? songLog.platform_id : "");
  elements.songLogTargetType.value = songLog.target_type === "group" ? "group" : "private";
  elements.songLogTargetId.value = typeof songLog.target_id === "string" ? songLog.target_id : "";
  updateSongLogTargetLabels();
  const coverService = settings.cover_service || {};
  elements.coverServiceUrl.value = typeof coverService.url === "string" ? coverService.url : "";
  elements.coverClearToken.checked = false;
  elements.coverClearToken.disabled = coverService.has_token !== true;
  elements.coverTokenStatus.textContent = coverService.has_token === true
    ? "已保存访问密钥；留空保持，输入新密钥可替换。"
    : "尚未保存访问密钥。";
  coverServiceDirty = false;
  renderVoiceOptions(coverVoices, typeof activePlayback.voice_id === "string" ? activePlayback.voice_id : "");
  syncPlaybackAvailability();
  elements.settingsForm.dataset.dirty = "false";
}

function renderVoiceOptions(voices = coverVoices, selectedId = elements.activeVoice.value) {
  coverVoices = Array.isArray(voices) ? voices.filter((voice) => voice && typeof voice.id === "string") : [];
  const selected = typeof selectedId === "string" ? selectedId : "";
  const options = [node("option", "", "选择音色")];
  options[0].value = "";
  const knownIds = new Set();
  for (const voice of coverVoices) {
    if (!voice.id || knownIds.has(voice.id)) continue;
    knownIds.add(voice.id);
    const option = node("option", "", voice.name ? `${voice.name}` : voice.id);
    option.value = voice.id;
    options.push(option);
  }
  if (selected && !knownIds.has(selected)) {
    const missing = node("option", "", `已配置音色（暂未读取）：${selected}`);
    missing.value = selected;
    options.push(missing);
  }
  elements.activeVoice.replaceChildren(...options);
  elements.activeVoice.value = selected;
  renderCoverTestVoiceOptions(coverVoices, elements.coverTestVoice?.value || "");
}

function renderCoverTestVoiceOptions(voices = coverVoices, selectedId = "") {
  if (!elements.coverTestVoice) return;
  const options = [node("option", "", voices.length ? "选择翻唱音色" : "暂无可用音色")];
  options[0].value = "";
  const ids = new Set();
  for (const voice of voices) {
    if (!voice.id || ids.has(voice.id)) continue;
    ids.add(voice.id);
    const option = node("option", "", voice.name ? `${voice.name}` : voice.id);
    option.value = voice.id;
    options.push(option);
  }
  elements.coverTestVoice.replaceChildren(...options);
  elements.coverTestVoice.value = ids.has(selectedId) ? selectedId : "";
  elements.coverTestVoice.disabled = ids.size === 0;
  updateCoverTestAvailability();
}

function renderSongLogTargets(targets = [], selectedId = elements.songLogPlatform.value) {
  if (!elements.songLogPlatform) return;
  const selected = typeof selectedId === "string" ? selectedId : "";
  const options = [node("option", "", targets.length ? "选择平台实例" : "未找到可用的平台实例")];
  options[0].value = "";
  const ids = new Set();
  for (const target of targets) {
    if (!target?.id || ids.has(target.id)) continue;
    ids.add(target.id);
    const online = target.bot?.online;
    const state = online === true ? "在线" : online === false ? "离线" : "状态未知";
    const option = node("option", "", `${target.name || target.adapter} · ${target.id}（${state}）`);
    option.value = target.id;
    options.push(option);
  }
  elements.songLogPlatform.replaceChildren(...options);
  elements.songLogPlatform.value = ids.has(selected) ? selected : "";
}

function updateSongLogTargetLabels() {
  if (!elements.songLogTargetType || !elements.songLogTargetIdLabel) return;
  const isGroup = elements.songLogTargetType.value === "group";
  elements.songLogTargetIdLabel.textContent = isGroup ? "群聊 ID" : "私聊 ID";
  elements.songLogTargetId.placeholder = isGroup ? "输入群号" : "输入接收方 ID";
}

function syncPlaybackAvailability() {
  const passiveEnabled = elements.passiveLlm.checked === true;
  elements.passiveMaxSeconds.disabled = !passiveEnabled;
  elements.passiveGapSeconds.disabled = !passiveEnabled;
  const activeEnabled = elements.activeLlm.checked === true;
  elements.activeVoice.disabled = !activeEnabled;
  elements.activeMaxSeconds.disabled = !activeEnabled;
  elements.activeGapSeconds.disabled = !activeEnabled;
}

function renderTextSettings(settings = {}, schema = {}, force = false) {
  if (!settings || (!force && elements.textSettingsForm.dataset.dirty === "true")) return;
  const defaults = schema?.defaults && typeof schema.defaults === "object" ? schema.defaults : {};
  const variableGroups = schema?.variables && typeof schema.variables === "object" ? schema.variables : {};
  const categoryByName = schema?.categories && typeof schema.categories === "object" ? schema.categories : {};
  const enabledDefaults = schema?.enabled_defaults && typeof schema.enabled_defaults === "object"
    ? schema.enabled_defaults
    : {};
  elements.replyTemplateFields.replaceChildren();
  for (const [name, defaultText] of Object.entries(defaults)) {
    const field = node("div", "template-field");
    field.dataset.templateCategory = categoryByName[name] || TEMPLATE_CATEGORY_BY_NAME[name] || "errors";
    const label = node("label", "field template-editor");
    const textarea = document.createElement("textarea");
    textarea.id = `reply-template-${name}`;
    textarea.name = `reply_templates.${name}`;
    textarea.rows = name === "menu" ? 6 : 4;
    textarea.autocomplete = "off";
    textarea.spellcheck = true;
    textarea.dataset.replyTemplate = name;
    textarea.value = typeof settings.reply_templates?.[name] === "string"
      ? settings.reply_templates[name]
      : defaultText;
    label.htmlFor = textarea.id;
    label.append(node("span", "template-title", TEMPLATE_LABELS[name] || name), textarea);

    const fieldParts = [label];
    if (name !== "song_log" && Object.prototype.hasOwnProperty.call(enabledDefaults, name) && typeof enabledDefaults[name] === "boolean") {
      const toggleLabel = node("label", "setting-toggle template-reply-toggle");
      const toggle = document.createElement("input");
      toggle.type = "checkbox";
      toggle.id = `reply-enabled-${name}`;
      toggle.name = `reply_enabled.${name}`;
      toggle.dataset.replyEnabled = name;
      toggle.checked = typeof settings.reply_enabled?.[name] === "boolean"
        ? settings.reply_enabled[name]
        : enabledDefaults[name] === true;
      toggle.setAttribute("aria-label", `发送此提示：${TEMPLATE_LABELS[name] || name}`);
      toggleLabel.htmlFor = toggle.id;
      toggleLabel.append(toggle, node("span", "", "发送此提示"));
      fieldParts.push(toggleLabel);
    }

    const defaultDisclosure = document.createElement("details");
    defaultDisclosure.className = "template-default-disclosure";
    defaultDisclosure.append(node("summary", "", "查看默认模板"));
    defaultDisclosure.append(node("p", "template-default", String(defaultText)));
    const variableList = node("ul", "template-variable-list");
    for (const [variable, description] of Object.entries(variableGroups[name] || {})) {
      const item = node("li", "template-variable");
      item.append(node("code", "", `{${variable}}`), document.createTextNode(` ${description}`));
      variableList.append(item);
    }

    field.append(...fieldParts, defaultDisclosure, variableList);
    elements.replyTemplateFields.append(field);
  }
  if (Object.keys(defaults).length === 0) {
    elements.replyTemplateFields.append(node("p", "empty-state", "回复模板信息尚未加载，请刷新页面后重试。"));
  }
  elements.textSettingsForm.dataset.dirty = "false";
  setActiveTemplateCategory(activeTemplateCategory);
}

function setActiveTemplateCategory(categoryId) {
  if (!TEMPLATE_CATEGORIES.includes(categoryId)) return;
  activeTemplateCategory = categoryId;
  elements.replyTemplateFields.setAttribute("aria-labelledby", `template-category-${categoryId}`);
  for (const tab of elements.templateCategoryTabs.querySelectorAll("[data-template-category]")) {
    const active = tab.dataset.templateCategory === categoryId;
    tab.setAttribute("aria-selected", String(active));
    tab.tabIndex = active ? 0 : -1;
    if (active) keepTabInView(elements.templateCategoryTabs, tab);
  }
  for (const field of elements.replyTemplateFields.querySelectorAll(".template-field[data-template-category]")) {
    field.hidden = field.dataset.templateCategory !== categoryId;
  }
}

function renderTargets(targets) {
  const previous = elements.targetPlatform.value;
  elements.targetPlatform.replaceChildren();
  if (targets.length === 0) {
    const option = node("option", "", "未找到可用的平台实例");
    option.value = "";
    elements.targetPlatform.append(option);
    elements.targetPlatform.disabled = true;
    document.getElementById("test-submit").disabled = true;
    return;
  }
  elements.targetPlatform.disabled = false;
  document.getElementById("test-submit").disabled = false;
  for (const target of targets) {
    const online = target.bot?.online;
    const onlineText = online === true ? "在线" : online === false ? "离线" : "状态未知";
    const details = [onlineText, target.bot?.version].filter(Boolean).join(" · ");
    const option = node("option", "", `${target.name || target.adapter} · ${target.id}（${details}）`);
    option.value = target.id;
    elements.targetPlatform.append(option);
  }
  if (targets.some((target) => target.id === previous)) elements.targetPlatform.value = previous;

  const selected = targets.find((target) => target.id === elements.targetPlatform.value) || targets[0];
  const recordState = selected.bot?.can_send_record === true
    ? "已通过手动测试"
    : selected.bot?.record_test?.status === "failed"
      ? "测试未通过，原因可能与目标或适配器有关"
      : "未测试";
  const detail = [selected.bot?.version ? `客户端 ${selected.bot.version}` : "", `录音发送能力：${recordState}`]
    .filter(Boolean)
    .join("；");
  setStatus(elements.testStatus, detail, selected.bot?.online === false ? "warning" : "");
  renderSongLogTargets(targets);
}

function setActiveProvider(providerId) {
  if (!PROVIDERS.some((provider) => provider.id === providerId)) return;
  activeProvider = providerId;
  for (const provider of PROVIDERS) {
    const active = provider.id === providerId;
    const tab = document.querySelector(`[data-provider-tab="${provider.id}"]`);
    tab.setAttribute("aria-selected", String(active));
    tab.tabIndex = active ? 0 : -1;
    document.querySelector(`[data-provider-panel="${provider.id}"]`).hidden = !active;
  }
}

function keepTabInView(container, tab) {
  if (!container || !tab) return;
  const containerRect = container.getBoundingClientRect();
  const leftEdge = containerRect.left + container.clientLeft + 4;
  const rightEdge = containerRect.left + container.clientLeft + container.clientWidth - 4;
  const tabRect = tab.getBoundingClientRect();
  const behavior = window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth";
  if (tabRect.left < leftEdge) {
    container.scrollBy({ left: tabRect.left - leftEdge, behavior });
  } else if (tabRect.right > rightEdge) {
    container.scrollBy({ left: tabRect.right - rightEdge, behavior });
  }
}

function updateMainTabIndicator() {
  const tab = document.querySelector(`[data-main-tab="${activeMainTab}"]`);
  if (!tab || !elements.mainTabIndicator) return;
  const navigation = elements.mainTabIndicator.parentElement;
  const tabRect = tab.getBoundingClientRect();
  const navigationRect = navigation.getBoundingClientRect();
  elements.mainTabIndicator.style.width = `${tabRect.width}px`;
  elements.mainTabIndicator.style.transform = `translateX(${tabRect.left - navigationRect.left}px)`;
}

function revealActiveMainTab() {
  const tab = document.querySelector(`[data-main-tab="${activeMainTab}"]`);
  keepTabInView(elements.mainTabs, tab);
  requestAnimationFrame(updateMainTabIndicator);
}

function setActiveMainTab(tabId) {
  const tabs = [...document.querySelectorAll("[data-main-tab]")];
  const activeTab = tabs.find((tab) => tab.dataset.mainTab === tabId);
  if (!activeTab) return;
  const changed = activeMainTab !== tabId;
  activeMainTab = tabId;
  const panels = [...document.querySelectorAll("[data-main-panel]")];
  panels.forEach((panel) => panel.classList.remove("is-animating"));
  for (const tab of tabs) {
    const active = tab.dataset.mainTab === tabId;
    tab.setAttribute("aria-selected", String(active));
    tab.tabIndex = active ? 0 : -1;
    const panel = document.querySelector(`[data-main-panel="${tab.dataset.mainTab}"]`);
    if (!panel) continue;
    panel.hidden = !active;
    panel.classList.toggle("is-active", active);
  }
  keepTabInView(elements.mainTabs, activeTab);
  if (changed && document.documentElement.dataset.viewReady === "true") {
    requestAnimationFrame(() => {
      document.querySelector(`[data-main-panel="${tabId}"]`)?.classList.add("is-animating");
    });
  }
  requestAnimationFrame(updateMainTabIndicator);
}

function setActiveEnvironmentTab(categoryId) {
  if (!ENVIRONMENT_CATEGORIES.includes(categoryId)) return;
  const changed = categoryId !== activeEnvironmentCategory;
  activeEnvironmentCategory = categoryId;
  const tabs = [...elements.environmentTabs.querySelectorAll("[data-environment-tab]")];
  const panels = [...document.querySelectorAll("[data-environment-panel]")];
  panels.forEach((panel) => panel.classList.remove("is-animating"));
  for (const tab of tabs) {
    const active = tab.dataset.environmentTab === categoryId;
    tab.setAttribute("aria-selected", String(active));
    tab.tabIndex = active ? 0 : -1;
    const panel = document.querySelector(`[data-environment-panel="${tab.dataset.environmentTab}"]`);
    if (!panel) continue;
    panel.hidden = !active;
    panel.classList.toggle("is-active", active);
  }
  if (changed && document.documentElement.dataset.viewReady === "true") {
    requestAnimationFrame(() => {
      document.querySelector(`[data-environment-panel="${categoryId}"]`)?.classList.add("is-animating");
    });
  }
}

function setActiveSettingsTab(tabId) {
  const tabs = [...elements.settingsTabs.querySelectorAll("[data-settings-tab]")];
  const activeTab = tabs.find((tab) => tab.dataset.settingsTab === tabId);
  if (!activeTab) return;
  const changed = activeSettingsTab !== tabId;
  activeSettingsTab = tabId;
  const panels = [...elements.settingsForm.querySelectorAll("[data-settings-panel]")];
  panels.forEach((panel) => panel.classList.remove("is-animating"));
  for (const tab of tabs) {
    const active = tab.dataset.settingsTab === tabId;
    tab.setAttribute("aria-selected", String(active));
    tab.tabIndex = active ? 0 : -1;
    const panel = panels.find((item) => item.dataset.settingsPanel === tab.dataset.settingsTab);
    if (!panel) continue;
    panel.hidden = !active;
    panel.classList.toggle("is-active", active);
  }
  keepTabInView(elements.settingsTabs, activeTab);
  if (changed && document.documentElement.dataset.viewReady === "true") {
    requestAnimationFrame(() => {
      elements.settingsForm.querySelector(`[data-settings-panel="${tabId}"]`)?.classList.add("is-animating");
    });
  }
}

function focusPluginSetting(fieldId) {
  const field = document.getElementById(fieldId);
  const panel = field?.closest("[data-settings-panel]");
  if (!field || !panel) return;
  setActiveSettingsTab(panel.dataset.settingsPanel);
  field.setAttribute("aria-invalid", "true");
  requestAnimationFrame(() => {
    field.scrollIntoView({ block: "nearest" });
    field.focus();
  });
}

function focusFieldForSettingsError(message = "") {
  if (/歌曲总数|max_songs/.test(message)) return "max-songs";
  if (/边生成|分段处理|incremental_output|active_playback/.test(message)) return "active-incremental-output";
  if (/被动.*最长/.test(message)) return "passive-max-seconds";
  if (/被动.*间隔/.test(message)) return "passive-gap-seconds";
  if (/被动|passive_playback/.test(message)) return "passive-llm";
  if (/主动.*音色/.test(message)) return "active-voice-id";
  if (/主动.*最长/.test(message)) return "active-max-seconds";
  if (/主动.*间隔/.test(message)) return "active-gap-seconds";
  if (/翻唱|cover_service/.test(message)) return "cover-service-url";
  if (/点歌日志/.test(message)) {
    return /平台实例|平台.*可用/.test(message) ? "song-log-platform" : "song-log-target-id";
  }
  return "";
}

function updateIncrementalCapability(health = null) {
  const supported = health?.capabilities?.incremental_segments;
  if (supported === true) {
    elements.activeIncrementalCapability.textContent = "当前翻唱服务支持分段处理。";
    elements.activeIncrementalCapability.dataset.kind = "success";
  } else if (supported === false) {
    elements.activeIncrementalCapability.textContent = "当前翻唱服务不支持分段处理；此开关会保留，翻唱继续整首流程。";
    elements.activeIncrementalCapability.dataset.kind = "warning";
  } else {
    elements.activeIncrementalCapability.textContent = "尚未确认翻唱服务的分段处理能力。此开关仍会按当前选择保存。";
    elements.activeIncrementalCapability.dataset.kind = "info";
  }
}

function setLoginStatus(providerId, message, kind = "") {
  setStatus(document.getElementById(`login-status-${providerId}`), message, kind);
}

function setQr(providerId, dataUrl) {
  const figure = document.getElementById(`qr-view-${providerId}`);
  let img = figure.querySelector("img");
  if (dataUrl && (dataUrl.startsWith("data:image/") || dataUrl.startsWith("https://"))) {
    if (!img) {
      img = node("img");
      img.id = `qr-image-${providerId}`;
      img.alt = figure.dataset.qrAlt || "音乐平台登录二维码";
      figure.prepend(img);
    }
    img.src = dataUrl;
    figure.hidden = false;
  } else {
    img?.remove();
    figure.hidden = true;
  }
}

function loginStateText(result) {
  const labels = {
    waiting: "等待扫码…",
    scanned: "已扫码，请在音乐 App 中确认登录。",
    success: "登录成功。",
    expired: "二维码已失效，请手动点击“开始登录”重新申请。",
    cancelled: "已取消登录。",
    canceled: "已取消登录。",
    idle: "当前没有进行中的登录。",
    error: "登录失败。",
  };
  return result.message || labels[result.status] || `登录状态：${result.status || "未知"}`;
}

function stopLoginPolling(providerId) {
  const timer = loginPollers.get(providerId);
  if (timer) clearTimeout(timer);
  loginPollers.delete(providerId);
  document.querySelector(`[data-login-cancel="${providerId}"]`).hidden = true;
  document.querySelector(`[data-login-start="${providerId}"]`).hidden = false;
}

function scheduleLoginPolling(providerId) {
  if (loginPollers.has(providerId)) return;
  const timer = setTimeout(() => pollLoginStatus(providerId), 1800);
  loginPollers.set(providerId, timer);
  document.querySelector(`[data-login-cancel="${providerId}"]`).hidden = false;
  document.querySelector(`[data-login-start="${providerId}"]`).hidden = true;
}

async function pollLoginStatus(providerId) {
  loginPollers.delete(providerId);
  try {
    const result = await apiGet("login/status", { provider: providerId });
    const state = String(result.status || "error");
    setLoginStatus(providerId, loginStateText(result), state === "error" ? "error" : "");
    if (result.qr_image) setQr(providerId, result.qr_image);
    if (state === "success") {
      stopLoginPolling(providerId);
      setQr(providerId, "");
      await refreshOverview();
      return;
    }
    if (LOGIN_TERMINAL_STATES.has(state)) {
      stopLoginPolling(providerId);
      setQr(providerId, "");
      return;
    }
    scheduleLoginPolling(providerId);
  } catch (error) {
    setLoginStatus(providerId, error.message || "读取登录状态失败。", "error");
    stopLoginPolling(providerId);
  }
}

async function startLogin(providerId, button) {
  setQr(providerId, "");
  buttonBusy(button, true, "正在申请…");
  setLoginStatus(providerId, "正在申请二维码…");
  try {
    const result = await apiPost("login/start", { provider: providerId });
    const state = String(result.status || "error");
    setLoginStatus(providerId, loginStateText(result), state === "error" ? "error" : "");
    if (result.qr_image) setQr(providerId, result.qr_image);
    if (state === "success") {
      await refreshOverview();
      return;
    }
    if (LOGIN_TERMINAL_STATES.has(state)) return;
    scheduleLoginPolling(providerId);
  } catch (error) {
    setLoginStatus(providerId, error.message || "申请登录二维码失败。", "error");
  } finally {
    buttonBusy(button, false);
  }
}

async function cancelLogin(providerId, button) {
  stopLoginPolling(providerId);
  buttonBusy(button, true);
  try {
    const result = await apiPost("login/cancel", { provider: providerId });
    setLoginStatus(providerId, loginStateText(result));
    setQr(providerId, "");
  } catch (error) {
    setLoginStatus(providerId, error.message || "取消登录失败。", "error");
  } finally {
    buttonBusy(button, false);
  }
}

async function logout(providerId, button) {
  buttonBusy(button, true);
  try {
    const result = await apiPost("login/logout", { provider: providerId });
    setLoginStatus(providerId, result.message || "已退出登录。");
    await refreshOverview();
  } catch (error) {
    setLoginStatus(providerId, error.message || "退出登录失败。", "error");
  } finally {
    buttonBusy(button, false);
  }
}

function customToolUrls() {
  const result = {};
  for (const tool of TOOL_DEFAULTS) {
    const input = document.querySelector(`[data-tool-url="${tool.id}"]`);
    result[tool.id] = input
      ? input.value.trim()
      : String(currentOverview?.settings?.tool_urls?.[tool.id] || "").trim();
  }
  return result;
}

function textSettingsPayload() {
  if (!currentOverview?.settings) throw new Error("插件设置仍在读取，请稍后再保存。 ");
  const replyTemplates = Object.fromEntries(
    [...elements.replyTemplateFields.querySelectorAll("[data-reply-template]")]
      .map((textarea) => [textarea.dataset.replyTemplate, textarea.value]),
  );
  const replyEnabled = Object.fromEntries(
    [...elements.replyTemplateFields.querySelectorAll("[data-reply-enabled]")]
      .map((checkbox) => [checkbox.dataset.replyEnabled, checkbox.checked === true]),
  );
  if (Object.keys(replyTemplates).length === 0) {
    Object.assign(replyTemplates, currentOverview.settings.reply_templates || {});
  }
  if (Object.keys(replyEnabled).length === 0) {
    Object.assign(replyEnabled, currentOverview.settings.reply_enabled || {});
  }
  return { reply_templates: replyTemplates, reply_enabled: replyEnabled };
}

function pluginSettingsPayload() {
  if (!currentOverview?.settings) throw new Error("插件设置仍在读取，请稍后再保存。 ");
  const maxSongs = Number(elements.maxSongs.value);
  if (!Number.isSafeInteger(maxSongs) || maxSongs < 1) {
    const error = new Error("歌曲总数上限必须是正整数。");
    error.fieldId = "max-songs";
    throw error;
  }
  const nonnegative = (input, label, integer = false) => {
    const raw = input.value.trim();
    const value = Number(raw);
    if (!raw || !Number.isFinite(value) || value < 0 || (integer && !Number.isSafeInteger(value))) {
      const error = new Error(`${label}必须是${integer ? "非负整数" : "非负有限数"}。`);
      error.fieldId = input.id;
      throw error;
    }
    return value;
  };
  const serviceUrl = elements.coverServiceUrl.value.trim();
  if (serviceUrl) {
    let parsed;
    try { parsed = new URL(serviceUrl); } catch {
      const error = new Error("翻唱服务 URL 必须是有效的 HTTP 或 HTTPS 地址。");
      error.fieldId = "cover-service-url";
      throw error;
    }
    if (!["http:", "https:"].includes(parsed.protocol) || !parsed.hostname) {
      const error = new Error("翻唱服务 URL 必须是有效的 HTTP 或 HTTPS 地址。");
      error.fieldId = "cover-service-url";
      throw error;
    }
  }
  const coverService = { url: serviceUrl };
  if (elements.coverClearToken.checked) coverService.token = "";
  else if (elements.coverServiceToken.value !== "") coverService.token = elements.coverServiceToken.value;
  const songLog = {
    enabled: elements.songLogEnabled.checked === true,
    platform_id: elements.songLogPlatform.value,
    target_type: elements.songLogTargetType.value,
    target_id: elements.songLogTargetId.value.trim(),
  };
  if (songLog.enabled) {
    if (!songLog.platform_id || !songLog.target_id) {
      const error = new Error("启用点歌日志时，请选择平台实例并填写私聊或群聊 ID。");
      error.fieldId = songLog.platform_id ? "song-log-target-id" : "song-log-platform";
      throw error;
    }
    if (!["private", "group"].includes(songLog.target_type)) {
      const error = new Error("点歌日志会话类型必须是私聊或群聊。");
      error.fieldId = "song-log-target-type";
      throw error;
    }
    if (!currentOverview?.targets?.some((target) => target.id === songLog.platform_id)) {
      const error = new Error("点歌日志平台实例已不可用，请重新选择当前目标列表中的实例。");
      error.fieldId = "song-log-platform";
      throw error;
    }
  }
  return {
    max_songs: maxSongs,
    passive_llm: elements.passiveLlm.checked === true,
    active_llm: elements.activeLlm.checked === true,
    passive_playback: {
      max_seconds: nonnegative(elements.passiveMaxSeconds, "被动最长播放时间", true),
      gap_seconds: nonnegative(elements.passiveGapSeconds, "被动分段发送间隔"),
    },
    active_playback: {
      voice_id: elements.activeVoice.value,
      max_seconds: nonnegative(elements.activeMaxSeconds, "主动最长演唱时间", true),
      gap_seconds: nonnegative(elements.activeGapSeconds, "主动分段发送间隔"),
      incremental_output: elements.activeIncrementalOutput.checked === true,
    },
    song_log: songLog,
    cover_service: coverService,
  };
}

function acceptSavedSettings(settings, { renderPlugin = true, renderText = true, forcePlugin = false, forceText = false } = {}) {
  const previous = currentOverview?.settings || {};
  const merged = { ...previous, ...(settings || {}) };
  for (const key of [
    "tool_urls",
    "reply_templates",
    "reply_enabled",
    "passive_playback",
    "active_playback",
    "song_log",
    "cover_service",
  ]) {
    if (settings?.[key] && typeof settings[key] === "object" && !Array.isArray(settings[key])) {
      merged[key] = { ...(previous[key] || {}), ...settings[key] };
    }
  }
  if (merged.cover_service && typeof merged.cover_service === "object") delete merged.cover_service.token;
  if (currentOverview) currentOverview.settings = merged;
  if (renderPlugin) renderPluginSettings(merged, forcePlugin);
  if (renderText) renderTextSettings(merged, currentOverview?.template_schema, forceText);
  renderEnvironment(undefined, merged);
  return merged;
}

async function saveToolUrls() {
  const result = await apiPost("settings", { tool_urls: customToolUrls() });
  acceptSavedSettings(result.settings, { renderPlugin: false, renderText: false });
  setPageStatus("自定义下载地址已保存。", "success");
  return result.settings;
}

async function saveTextSettings(event) {
  event.preventDefault();
  const button = document.getElementById("save-text-settings");
  buttonBusy(button, true, "保存中…");
  setStatus(elements.textSettingsStatus, "正在保存文本设置…");
  try {
    const result = await apiPost("settings", textSettingsPayload());
    acceptSavedSettings(result.settings, { forceText: true });
    setStatus(elements.textSettingsStatus, "文本设置已保存。", "success");
  } catch (error) {
    setStatus(elements.textSettingsStatus, error.message || "保存文本设置失败。", "error");
  } finally {
    buttonBusy(button, false);
  }
}

async function savePluginSettings(event) {
  event.preventDefault();
  const button = document.getElementById("save-settings");
  buttonBusy(button, true, "保存中…");
  setStatus(elements.settingsStatus, "正在保存插件设置…");
  try {
    const result = await apiPost("settings", pluginSettingsPayload());
    elements.coverServiceToken.value = "";
    elements.coverClearToken.checked = false;
    coverServiceDirty = false;
    acceptSavedSettings(result.settings, { forcePlugin: true });
    const removed = Number(result.settings?.retention?.removed || 0);
    const skipped = Number(result.settings?.retention?.skipped_in_use || 0);
    const cleanupNote = removed ? `已清理最早的 ${removed} 首歌曲及其文件。` : "";
    const busyNote = skipped ? `另有 ${skipped} 首歌曲正被使用，稍后才可清理。` : "";
    setStatus(elements.settingsStatus, `插件设置已保存。${cleanupNote}${busyNote}`, skipped ? "warning" : "success");
  } catch (error) {
    const message = error.message || "保存插件设置失败。";
    const fieldId = error.fieldId || focusFieldForSettingsError(message);
    if (fieldId) focusPluginSetting(fieldId);
    setStatus(elements.settingsStatus, message, "error");
  } finally {
    buttonBusy(button, false);
  }
}

function selectedVoiceForRefresh() {
  if (elements.settingsForm.dataset.dirty === "true") return elements.activeVoice.value;
  return currentOverview?.settings?.active_playback?.voice_id || elements.activeVoice.value || "";
}

async function refreshCoverVoices({ announce = false } = {}) {
  const result = await apiGet("cover/voices");
  const voices = Array.isArray(result?.voices) ? result.voices : [];
  renderVoiceOptions(voices, selectedVoiceForRefresh());
  if (announce) setStatus(elements.coverServiceStatus, `已读取 ${voices.length} 个音色。`, "success");
  return voices;
}

function coverHealthMessage(health = {}) {
  const isReady = health.ready === true;
  const parts = [`资源连通检查完成：${isReady ? "服务可用" : "服务未就绪"}。`];
  if (health.device) parts.push(`设备：${health.device}。`);
  if (health.engine) parts.push(`引擎：${health.engine}。`);
  if (health.message) parts.push(String(health.message));
  if (isCoverServiceV2Compatible(health)) {
    parts.push("服务协议完整兼容（pipeline v2，任务确认、取消和临时任务能力可用）。");
  } else if (isReady) {
    parts.push("翻唱服务需要升级到 pipeline v2，并提供任务确认、取消和临时任务能力；当前不能判定为完整兼容。");
  } else {
    parts.push("pipeline v2 与任务能力尚未确认；服务就绪后请重新检查。");
  }
  parts.push("此检查不运行推理；请使用短片段测试验证实际转换。");
  return parts.join(" ");
}

function isCoverServiceV2Compatible(health = {}) {
  const capabilities = health.capabilities || {};
  return health.pipeline_version === "2"
    && capabilities.job_ack === true
    && capabilities.job_cancel === true
    && capabilities.ephemeral_jobs === true;
}

function renderCoverHealthChecks(health = {}) {
  const checks = Array.isArray(health.checks) ? health.checks : [];
  elements.coverHealthChecks.replaceChildren();
  elements.coverHealthChecks.hidden = checks.length === 0;
  for (const check of checks) {
    const row = node("li", "check-row");
    row.append(
      node("span", "check-label", check.label || check.id || "资源检查"),
      node("span", "check-detail", check.message || ""),
      node("span", `status-pill status-${check.status || "unknown"}`, displayStatus(check.status)),
    );
    elements.coverHealthChecks.append(row);
  }
}

async function checkCoverService(button = elements.coverCheck) {
  if (coverServiceDirty) {
    setStatus(elements.coverServiceStatus, "请先保存服务地址或密钥，再检测翻唱服务。", "warning");
    return;
  }
  buttonBusy(button, true, "检测中…");
  setStatus(elements.coverServiceStatus, "正在检测已保存的翻唱服务配置…", "info");
  try {
    const result = await apiPost("cover/check", {});
    const health = result?.health || {};
    updateIncrementalCapability(health);
    const voices = Array.isArray(result?.voices) ? result.voices : [];
    coverServiceTestBlockMessage = health.ready === true && isCoverServiceV2Compatible(health)
      ? ""
      : health.ready === true
        ? "翻唱服务需要升级到 pipeline v2 并提供完整任务能力，升级后才能运行短片段推理。"
        : "翻唱服务未就绪，暂不可运行短片段推理。";
    renderVoiceOptions(voices, selectedVoiceForRefresh());
    renderCoverHealthChecks(health);
    updateCoverTestAvailability();
    setStatus(
      elements.coverServiceStatus,
      `${coverHealthMessage(health)} 已读取 ${voices.length} 个音色。`,
      health.ready === true && isCoverServiceV2Compatible(health) ? "success" : "warning",
    );
  } catch (error) {
    updateIncrementalCapability();
    coverServiceTestBlockMessage = "无法确认翻唱服务版本，请先检查服务后再运行短片段推理。";
    updateCoverTestAvailability();
    renderCoverHealthChecks({});
    setStatus(elements.coverServiceStatus, error.message || "检测翻唱服务失败。", "error");
  } finally {
    buttonBusy(button, false);
  }
}

function fileExtension(file) {
  return String(file?.name || "").split(".").pop().toLocaleLowerCase();
}

async function importCoverVoice(button = elements.coverImport) {
  const name = elements.coverVoiceName.value.trim();
  const modelFile = elements.coverModelFile.files?.[0];
  const indexFile = elements.coverIndexFile.files?.[0];
  if (!name) {
    setStatus(elements.coverImportStatus, "请填写音色名称。", "error");
    elements.coverVoiceName.focus();
    return;
  }
  if (!modelFile) {
    setStatus(elements.coverImportStatus, "请选择 .pt 或 .pth 模型文件。", "error");
    elements.coverModelFile.focus();
    return;
  }
  if (!["pt", "pth"].includes(fileExtension(modelFile))) {
    setStatus(elements.coverImportStatus, "模型文件仅支持 .pt 或 .pth 格式。", "error");
    elements.coverModelFile.focus();
    return;
  }
  if (indexFile && fileExtension(indexFile) !== "index") {
    setStatus(elements.coverImportStatus, "索引文件需使用 .index 格式。", "error");
    elements.coverIndexFile.focus();
    return;
  }
  buttonBusy(button, true, "上传中…");
  try {
    setStatus(elements.coverImportStatus, `正在上传模型文件（${modelFile.name}）…`, "info");
    const modelUpload = await apiUpload("cover/upload", modelFile);
    if (!modelUpload?.upload_id) throw new Error("模型文件上传没有返回编号，请重试。");
    let indexUpload = null;
    if (indexFile) {
      setStatus(elements.coverImportStatus, `正在上传索引文件（${indexFile.name}）…`, "info");
      indexUpload = await apiUpload("cover/upload", indexFile);
      if (!indexUpload?.upload_id) throw new Error("索引文件上传没有返回编号，请重试。");
    }
    setStatus(elements.coverImportStatus, "文件已上传，正在导入音色…", "info");
    const body = { name, model_upload_id: modelUpload.upload_id };
    if (indexUpload) body.index_upload_id = indexUpload.upload_id;
    const start = await apiPost("cover/import", body);
    const result = await waitForJob(start, (job) => {
      if (job.status === "running" || job.status === "queued") {
        setStatus(elements.coverImportStatus, "正在导入 RVC 音色…", "info");
      }
    });
    const importedVoice = result?.voice;
    if (!importedVoice?.id) throw new Error("任务已结束，但没有返回音色信息。");
    const preservedSelection = selectedVoiceForRefresh();
    try {
      const voicesResult = await apiGet("cover/voices");
      renderVoiceOptions(Array.isArray(voicesResult?.voices) ? voicesResult.voices : [], preservedSelection);
    } catch {
      const voices = coverVoices.filter((voice) => voice.id !== importedVoice.id);
      renderVoiceOptions([...voices, importedVoice], preservedSelection);
    }
    elements.coverModelFile.value = "";
    elements.coverIndexFile.value = "";
    setStatus(elements.coverImportStatus, `音色“${importedVoice.name || importedVoice.id}”已导入。请在“翻唱音色”中选择并保存设置；主动 LLM 不会自动开启。`, "success");
  } catch (error) {
    setStatus(elements.coverImportStatus, error.message || "导入音色失败。", "error");
  } finally {
    buttonBusy(button, false);
  }
}

async function checkEnvironment(button = document.getElementById("refresh-overview")) {
  const requestKey = "environment_check";
  buttonBusy(button, true, "检测中…");
  setStatus(elements.toolStatus, "正在检测本机工具与音乐平台依赖…");
  try {
    const start = await apiPost("environment/check", { request_id: requestIdFor(requestKey) });
    const result = await waitForJob(start, (job) => {
      if (job.status === "running" || job.status === "queued") {
        setStatus(elements.toolStatus, "正在检查工具版本、本地音频处理和平台依赖…");
      }
    });
    clearRequestId(requestKey);
    renderEnvironment(result.environment);
    if (Array.isArray(result.targets) && currentOverview) {
      currentOverview.targets = result.targets;
      renderTargets(result.targets);
    }
    const errors = (result.environment.checks || []).filter((check) => check.status === "error").length;
    setStatus(elements.toolStatus, errors ? `检测完成，有 ${errors} 项需要处理。` : "检测完成。", errors ? "warning" : "success");
  } catch (error) {
    if (error.jobStatus) clearRequestId(requestKey);
    setStatus(elements.toolStatus, error.message || "环境检测失败。", "error");
  } finally {
    buttonBusy(button, false);
  }
}

async function installTool(toolId, button) {
  const requestKey = `install:${toolId}`;
  buttonBusy(button, true, "补充中…");
  setStatus(elements.toolStatus, `正在补充${TOOL_DEFAULTS.find((tool) => tool.id === toolId)?.label || toolId}…`);
  try {
    await saveToolUrls();
    const start = await apiPost("environment/install", {
      tool_id: toolId,
      url: customToolUrls()[toolId] || "",
      request_id: requestIdFor(requestKey),
    });
    const result = await waitForJob(start, () => {
      setStatus(elements.toolStatus, `正在补充${TOOL_DEFAULTS.find((tool) => tool.id === toolId)?.label || toolId}…`);
    });
    clearRequestId(requestKey);
    setStatus(elements.toolStatus, result.message || (result.installed ? "工具补充完成。" : "工具补充未完成。"), result.installed ? "success" : "warning");
    await checkEnvironment(null);
  } catch (error) {
    if (error.jobStatus) clearRequestId(requestKey);
    setStatus(elements.toolStatus, error.message || "工具补充失败。", "error");
  } finally {
    buttonBusy(button, false);
  }
}

async function installMissingTools(button) {
  const tools = currentEnvironment?.tools || [];
  const missing = tools.filter((tool) => tool.available === false);
  if (missing.length === 0) {
    setStatus(elements.toolStatus, "没有检测到待补充的工具。", "success");
    return;
  }
  buttonBusy(button, true, "补充中…");
  try {
    await saveToolUrls();
    const failures = [];
    for (const tool of missing) {
      setStatus(elements.toolStatus, `正在补充${tool.label || tool.id}…`);
      const requestKey = `install:${tool.id}`;
      try {
        const start = await apiPost("environment/install", {
          tool_id: tool.id,
          url: customToolUrls()[tool.id] || "",
          request_id: requestIdFor(requestKey),
        });
        const result = await waitForJob(start, () => {
          setStatus(elements.toolStatus, `正在补充${tool.label || tool.id}…`);
        });
        clearRequestId(requestKey);
        if (!result.installed) failures.push(tool.label || tool.id);
      } catch (error) {
        if (error.jobStatus) clearRequestId(requestKey);
        failures.push(`${tool.label || tool.id}（${error.message || "失败"}）`);
      }
    }
    await checkEnvironment(null);
    if (failures.length) setStatus(elements.toolStatus, `已完成，仍需处理：${failures.join("、")}`, "warning");
    else setStatus(elements.toolStatus, "缺失工具已补充完成。", "success");
  } catch (error) {
    setStatus(elements.toolStatus, error.message || "保存下载地址失败。", "error");
  } finally {
    buttonBusy(button, false);
  }
}

async function refreshOverview() {
  const result = await apiGet("overview");
  renderOverview(result);
}

async function refreshFiles(button = null) {
  buttonBusy(button, true, "读取中…");
  try {
    const result = await apiGet("files", {
      platform: elements.filePlatform.value,
      query: elements.fileQuery.value.trim(),
    });
    renderFiles(result.files || [], elements.filePlatform.value);
  } catch (error) {
    elements.fileGroups.replaceChildren(node("p", "empty-state", error.message || "读取文件列表失败。"));
  } finally {
    buttonBusy(button, false);
  }
}

function updateCoverTestAvailability() {
  if (!elements.coverTestSubmit) return;
  const busy = elements.coverTestSubmit.getAttribute("aria-busy") === "true";
  elements.coverTestSubmit.disabled = busy
    || coverTestSongsLoading
    || Boolean(coverServiceTestBlockMessage)
    || !elements.coverTestSong.value
    || !elements.coverTestVoice.value;
}

async function refreshCoverTestSongs() {
  const selectedBefore = elements.coverTestSong.value;
  coverTestSongsLoading = true;
  updateCoverTestAvailability();
  elements.coverTestSong.replaceChildren(node("option", "", "读取已下载源歌…"));
  elements.coverTestSong.options[0].value = "";
  try {
    const result = await apiGet("files", { platform: elements.coverTestPlatform.value });
    const records = Array.isArray(result?.files) ? result.files : [];
    const options = [node("option", "", records.length ? "选择已下载源歌" : "该平台暂无已下载歌曲")];
    options[0].value = "";
    for (const record of records) {
      const option = node("option", "", `${record.title || "未命名歌曲"} — ${record.artist || "未知歌手"} · ${formatDuration(record.duration)}`);
      option.value = record.id;
      options.push(option);
    }
    elements.coverTestSong.replaceChildren(...options);
    elements.coverTestSong.value = records.some((record) => record.id === selectedBefore) ? selectedBefore : "";
    if (!records.length) setStatus(elements.coverTestStatus, "当前平台没有已下载源歌，请先在文件管理中下载一首歌曲。", "warning");
    else setStatus(elements.coverTestStatus, "");
  } catch (error) {
    const option = node("option", "", "无法读取已下载歌曲");
    option.value = "";
    elements.coverTestSong.replaceChildren(option);
    setStatus(elements.coverTestStatus, error.message || "读取已下载歌曲失败。", "error");
  } finally {
    coverTestSongsLoading = false;
    updateCoverTestAvailability();
  }
}

function secondsText(value) {
  const seconds = Number(value);
  if (!Number.isFinite(seconds) || seconds < 0) return "未知";
  return `${new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 2 }).format(seconds)} 秒`;
}

function renderCoverTestResult(result = {}) {
  const segments = Array.isArray(result.segments) ? result.segments : [];
  const segmentCount = Number(result.segment_count) || segments.length;
  const sampleParts = [
    Number(result.sample_rate) > 0 ? `${result.sample_rate} Hz` : "",
    Number(result.channels) > 0 ? `${result.channels} 声道` : "",
    Number(result.sample_width) > 0 ? `${Number(result.sample_width) * 8} bit` : "",
  ].filter(Boolean);
  const values = [
    ["推理耗时", secondsText(result.elapsed_seconds)],
    ["输入片段", `${secondsText(result.input_duration)}${Number.isFinite(Number(result.start_seconds)) ? `，从 ${secondsText(result.start_seconds)} 开始` : ""}`],
    ["输出时长", secondsText(result.output_duration)],
    ["输出格式", String(result.format || "未知").toUpperCase()],
    ["输出分段", `${segmentCount} 段`],
    ["音频参数", sampleParts.length ? sampleParts.join(" · ") : "未知"],
    ["任务清理", result.cleanup_confirmed === true ? "服务端已确认清理" : "清理未确认（任务可能仍在等待清理）"],
  ];
  elements.coverTestResultDetails.replaceChildren();
  for (const [label, value] of values) {
    const term = node("dt", "", label);
    const description = node("dd", "", value);
    elements.coverTestResultDetails.append(term, description);
  }
  elements.coverTestResultSegments.replaceChildren();
  elements.coverTestResultSegments.hidden = segments.length === 0;
  for (const [index, segment] of segments.entries()) {
    const parts = [
      segment.name || `第 ${index + 1} 段`,
      secondsText(segment.duration),
      formatSize(segment.size),
    ];
    elements.coverTestResultSegments.append(node("li", "", parts.join(" · ")));
  }
  elements.coverTestResultMessage.textContent = result.message || "";
  elements.coverTestResult.hidden = false;
}

async function testCoverService(event) {
  event.preventDefault();
  if (coverServiceTestBlockMessage) {
    setStatus(elements.coverTestStatus, coverServiceTestBlockMessage, "warning");
    return;
  }
  const songId = elements.coverTestSong.value;
  const voiceId = elements.coverTestVoice.value;
  if (!songId || !voiceId) {
    setStatus(elements.coverTestStatus, "请选择已下载源歌和翻唱音色后再运行测试。", "error");
    return;
  }
  const button = elements.coverTestSubmit;
  const songLabel = elements.coverTestSong.selectedOptions[0]?.textContent || "所选源歌";
  const voiceLabel = elements.coverTestVoice.selectedOptions[0]?.textContent || "所选音色";
  elements.coverTestResult.hidden = true;
  buttonBusy(button, true, "推理中…");
  setStatus(elements.coverTestStatus, `正在使用“${voiceLabel}”处理“${songLabel}”的短片段；不会发送消息。`, "info");
  try {
    const start = await apiPost("cover/test", { song_id: songId, voice_id: voiceId });
    const result = await waitForJob(start, (job) => {
      if (job.status === "running" || job.status === "queued") {
        setStatus(elements.coverTestStatus, `正在运行真实短片段推理：${songLabel} · ${voiceLabel}…`, "info");
      }
    });
    renderCoverTestResult(result || {});
    if (result?.ok === true && result?.cleanup_confirmed === true) {
      setStatus(elements.coverTestStatus, "短片段推理测试完成。", "success");
    } else if (result?.ok === true) {
      setStatus(elements.coverTestStatus, "短片段推理已完成，但服务端清理未确认。", "warning");
    } else {
      setStatus(elements.coverTestStatus, result?.message || "短片段推理没有成功完成。", "error");
    }
  } catch (error) {
    setStatus(elements.coverTestStatus, error.message || "短片段推理测试失败。", "error");
  } finally {
    buttonBusy(button, false);
    updateCoverTestAvailability();
  }
}

function renderFiles(records, platformId) {
  elements.fileGroups.replaceChildren();
  const group = node("section", "file-group");
  const heading = node("h3", "file-group-title", providerLabel(platformId));
  const list = node("div", "track-list");
  for (const record of records) list.append(trackEntry(record, platformId));
  group.append(heading, list);
  elements.fileGroups.append(group);
  if (records.length === 0) {
    const filter = elements.fileQuery.value.trim();
    const message = filter
      ? `在${providerLabel(platformId)}中没有找到匹配的已下载歌曲。`
      : `${providerLabel(platformId)}还没有下载歌曲。搜索并选择一首歌曲后，可在这里管理音频文件。`;
    list.append(node("p", "empty-state", message));
  }
}

function trackEntry(record, platformId) {
  const entry = node("article", "track-entry");
  const trackRow = node("div", "track-entry-row");
  const disclosure = node("details", "track-disclosure");
  const heading = node("div", "track-heading");
  const title = node("h5", "track-title", record.title || "未命名歌曲");
  const artist = node("span", "track-artist", record.artist || "未知歌手");
  heading.append(title, artist);

  const segments = Array.isArray(record.segments)
    ? record.segments.length
    : Number(record.segments || 0);
  const files = Array.isArray(record.files) ? record.files : [];
  const segmentCount = segments || files.filter((file) => file.kind === "segment").length;
  const covers = Array.isArray(record.covers) ? record.covers : [];
  const details = node("p", "track-details");
  details.textContent = [
    `下载于 ${formatDate(record.downloaded_at)}`,
    `时长 ${formatDuration(record.duration)}`,
    `原唱 ${segmentCount} 段`,
    `翻唱 ${covers.length} 个`,
  ].join(" · ");

  const summary = node("summary");
  summary.append(heading, details);
  const folder = node("div", "track-folder-content");
  const originalSection = node("section", "audio-kind-group source-audio-section");
  originalSection.append(node("h6", "audio-kind-title", "原唱文件"));
  const kindOrder = ["original", "transcoded", "segment"];
  const kindLabels = { original: "原音频", transcoded: "转码音频", segment: "语音分段" };
  for (const kind of kindOrder) {
    const kindFiles = files.filter((file) => file.kind === kind);
    if (!kindFiles.length) continue;
    const group = node("section", "audio-kind-group source-audio-kind");
    group.append(node("h6", "audio-kind-title", `${kindLabels[kind]}（${kindFiles.length}）`));
    const fileList = renderAudioFileList(kindFiles, record);
    group.append(fileList);
    originalSection.append(group);
  }
  if (!files.length) originalSection.append(node("p", "empty-state", "暂无原唱文件。"));
  folder.append(originalSection);

  const coverSection = node("section", "audio-kind-group cover-variants");
  coverSection.append(node("h6", "audio-kind-title", `已生成翻唱（${covers.length}）`));
  if (covers.length === 0) {
    coverSection.append(node("p", "empty-state", "这首源歌还没有生成翻唱变体。"));
  } else {
    for (const cover of covers) coverSection.append(coverEntry(record, platformId, cover));
  }
  folder.append(coverSection);

  disclosure.append(summary, folder);

  const actions = node("div", "track-entry-actions");
  const toggle = node("button", "button quiet small", "发送原唱");
  toggle.type = "button";
  toggle.setAttribute("aria-expanded", "false");
  toggle.dataset.toggleSongSend = "true";
  actions.append(toggle);
  trackRow.append(disclosure, actions);
  const sendPanel = buildSongSendPanel(record, segmentCount, null);
  sendPanel.hidden = true;
  entry.append(trackRow, sendPanel);
  return entry;
}

function renderAudioFileList(files, record, cover = null) {
  const fileList = node("ul", "audio-file-list");
  for (const file of files) {
    const row = node("li", "audio-file-row");
    const description = node("span", "audio-file-name", `${file.name || "音频文件"} · ${formatSize(file.size)}`);
    const download = node("button", "button quiet small", cover ? "下载" : "下载文件");
    download.type = "button";
    download.addEventListener("click", async () => {
      buttonBusy(download, true, "准备中…");
      try {
        const params = { song_id: record.id, filename: file.name };
        if (cover?.id) params.cover_id = cover.id;
        const downloadName = cover
          ? `${safeFilename(record.title)}-${safeFilename(cover.voice_name || "翻唱")}-${file.name}`
          : file.name;
        await bridge.download("files/download", params, downloadName);
        setPageStatus(`已开始下载 ${cover ? "翻唱文件" : "原唱文件"} ${file.name}。`, "success");
      } catch (error) {
        setPageStatus(error.message || "文件下载失败。", "error");
      } finally {
        buttonBusy(download, false);
      }
    });
    row.append(description, download);
    fileList.append(row);
  }
  return fileList;
}

function safeFilename(value) {
  return String(value || "音频")
    .replace(/[<>:"/\\|?*\u0000-\u001f]/g, "_")
    .replace(/\s+/g, " ")
    .trim()
    .slice(0, 64) || "音频";
}

function coverEntry(record, platformId, cover) {
  const entry = node("section", "cover-variant");
  const heading = node("div", "cover-variant-heading");
  const name = node("h5", "cover-variant-title", cover.voice_name || "未知音色");
  const voiceVersion = node("span", "cover-version", cover.voice_version ? `音色模型版本 ${shortVersion(cover.voice_version)}` : "音色模型版本未知");
  const processingVersion = cover.pipeline_version == null || cover.pipeline_version === ""
    ? "未知"
    : String(cover.pipeline_version);
  const processing = node("span", "cover-processing-version", `处理版本 ${processingVersion}`);
  heading.append(name, voiceVersion, processing);
  const isSendable = isCoverVariantSendable(cover);
  const statusMessage = String(cover.status_message || "").trim();
  let statusText = statusMessage;
  if (String(cover.pipeline_version) === "1" || cover.usable === false) {
    statusText = "旧处理版本，需要重新生成";
    if (statusMessage && statusMessage.replace(/[。.!?！？]$/, "") !== statusText) statusText += `。${statusMessage}`;
  } else if (!isSendable) {
    statusText = statusMessage || "处理版本未知，暂不可发送。";
  }
  const segments = Array.isArray(cover.segments)
    ? cover.segments.length
    : Number(cover.segments || cover.segment_count || 0);
  const metadata = node("p", "track-details cover-variant-details");
  metadata.textContent = [
    `生成于 ${formatDate(cover.created_at)}`,
    `时长 ${formatDuration(cover.duration)}`,
    `${segments} 段`,
  ].join(" · ");
  const status = node("p", `inline-status cover-variant-status`, statusText || (isSendable ? "可用于发送。" : "暂不可发送。"));
  status.dataset.kind = isSendable ? "success" : "warning";

  const files = Array.isArray(cover.files) ? cover.files : [];
  const fileList = renderAudioFileList(files, record, cover);
  if (files.length === 0) fileList.append(node("li", "empty-state", "缓存文件信息暂不可用。"));
  const actions = node("div", "cover-variant-actions");
  const toggle = node("button", "button quiet small", isSendable ? "发送此翻唱" : "此翻唱不可发送");
  toggle.type = "button";
  toggle.dataset.toggleCoverSend = "true";
  toggle.setAttribute("aria-expanded", "false");
  toggle.disabled = !isSendable;
  actions.append(toggle);
  const sendPanel = buildSongSendPanel(record, segments, cover);
  sendPanel.hidden = true;
  entry.append(heading, metadata, status, fileList, actions, sendPanel);
  return entry;
}

function isCoverVariantSendable(cover) {
  return String(cover?.pipeline_version) === "2" && cover?.usable === true;
}

function shortVersion(value) {
  const version = String(value || "");
  if (version.length <= 18) return version;
  return `${version.slice(0, 8)}…${version.slice(-6)}`;
}

function buildSongSendPanel(record, segmentCount, cover = null) {
  const sendPanel = node("section", "song-send-panel");
  const title = String(record.title || "未命名歌曲");
  const voiceName = cover?.voice_name || "未知音色";
  const displayTitle = cover ? `${title}（${voiceName}翻唱）` : title;
  sendPanel.append(node("h4", "song-send-title", cover ? `发送 ${voiceName} 翻唱` : "发送已下载歌曲"));
  const confirmation = node("p", "song-send-confirmation");
  const form = node("form", "song-send-form");
  form.dataset.songSend = "true";
  form.dataset.songId = String(record.id || "");
  form.dataset.songTitle = displayTitle;
  form.dataset.mediaTitle = title;
  form.dataset.songArtist = String(record.artist || "未知歌手");
  form.dataset.segmentCount = String(segmentCount);
  if (cover?.id) {
    form.dataset.coverId = String(cover.id);
    form.dataset.coverDescription = `${voiceName} · 音色模型版本 ${shortVersion(cover.voice_version || "未知")} · 处理版本 ${String(cover.pipeline_version || "未知")}`;
    form.dataset.coverUsable = String(isCoverVariantSendable(cover));
  }

  const platformField = node("label", "field");
  platformField.append(node("span", "", "平台实例"));
  const platformSelect = node("select");
  platformSelect.name = "platform_id";
  platformSelect.required = true;
  platformSelect.dataset.sendPlatform = "true";
  const targets = Array.isArray(currentOverview?.targets) ? currentOverview.targets : [];
  const placeholder = node("option", "", targets.length ? "选择平台实例" : "未找到可用的平台实例");
  placeholder.value = "";
  platformSelect.append(placeholder);
  if (targets.length === 0) {
    platformSelect.disabled = true;
  } else {
    for (const target of targets) {
      const online = target.bot?.online;
      const onlineText = online === true ? "在线" : online === false ? "离线" : "状态未知";
      const option = node("option", "", `${target.name || target.adapter} · ${target.id}（${onlineText}）`);
      option.value = target.id;
      platformSelect.append(option);
    }
    if (!cover) platformSelect.value = targets[0].id;
  }
  platformField.append(platformSelect);

  const typeField = node("label", "field");
  typeField.append(node("span", "", "会话类型"));
  const typeSelect = node("select");
  typeSelect.name = "target_type";
  typeSelect.dataset.sendType = "true";
  for (const [value, label] of [["private", "私聊"], ["group", "群聊"]]) {
    const option = node("option", "", label);
    option.value = value;
    typeSelect.append(option);
  }
  typeField.append(typeSelect);

  const targetField = node("label", "field");
  const targetLabel = node("span", "", "QQ 号");
  targetLabel.dataset.sendTargetLabel = "true";
  const targetInput = node("input");
  targetInput.name = "target_id";
  targetInput.type = "text";
  targetInput.maxLength = 128;
  targetInput.autocomplete = "off";
  targetInput.placeholder = "输入接收方 QQ 号";
  targetInput.required = true;
  targetInput.dataset.sendTarget = "true";
  targetField.append(targetLabel, targetInput);

  const submit = node("button", "button primary form-submit", `${cover ? "发送此翻唱" : "发送原唱"}（${segmentCount} 段）`);
  submit.type = "submit";
  const coverUsable = !cover || isCoverVariantSendable(cover);
  submit.disabled = targets.length === 0 || !coverUsable;
  const status = node("p", "inline-status song-send-status");
  status.hidden = true;
  status.setAttribute("role", "status");
  status.setAttribute("aria-live", "polite");
  form.append(platformField, typeField, targetField, submit);
  sendPanel.append(
    confirmation,
    form,
    node("p", "helper-text song-send-helper", cover
      ? "将发送所选音色的缓存翻唱并按缓存分段发送；服务离线时仍可发送。"
      : "按每段最多 60 秒的顺序发送已下载原唱。"),
    status,
  );
  for (const control of form.querySelectorAll("select, input")) control.disabled = !coverUsable || control.disabled;
  updateSongSendConfirmation(form, confirmation);
  return sendPanel;
}

function updateSongSendConfirmation(form, confirmation) {
  const platform = form.querySelector("[data-send-platform]");
  const targetType = form.querySelector("[data-send-type]").value;
  const targetId = form.querySelector("[data-send-target]").value.trim();
  const platformLabel = platform.selectedOptions[0]?.textContent || "未选择平台实例";
  const typeLabel = targetType === "group" ? "群聊" : "私聊";
  const targetLabelElement = form.querySelector("[data-send-target-label]");
  targetLabelElement.textContent = targetType === "group" ? "群号" : "QQ 号";
  form.querySelector("[data-send-target]").placeholder = targetType === "group" ? "输入群号" : "输入接收方 QQ 号";
  const targetLabel = targetId || `未填写${typeLabel} ID`;
  const media = form.dataset.coverId
    ? `${form.dataset.coverDescription}；${form.dataset.segmentCount} 段缓存音频`
    : `原唱；${form.dataset.segmentCount} 段音频`;
  confirmation.textContent = `《${form.dataset.mediaTitle || form.dataset.songTitle}》— ${form.dataset.songArtist}，${media}。发送目标：${platformLabel} · ${typeLabel} ${targetLabel}。点击“${form.dataset.coverId ? "发送此翻唱" : "发送原唱"}”后立即发送。`;
}

function formatDate(value) {
  if (!value) return "时间未知";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

function formatDuration(value) {
  const seconds = Math.max(0, Math.floor(Number(value) || 0));
  if (!seconds) return "未知";
  const minutes = Math.floor(seconds / 60);
  const rest = seconds % 60;
  return `${minutes}:${String(rest).padStart(2, "0")}`;
}

function formatSize(value) {
  const size = Number(value);
  if (!Number.isFinite(size) || size < 0) return "大小未知";
  if (size < 1024) return `${size} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let scaled = size / 1024;
  let index = 0;
  while (scaled >= 1024 && index < units.length - 1) {
    scaled /= 1024;
    index += 1;
  }
  return `${new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 1 }).format(scaled)} ${units[index]}`;
}

async function searchSongs(event) {
  event.preventDefault();
  const button = document.getElementById("search-submit");
  const platform = document.getElementById("search-platform").value;
  const query = document.getElementById("search-query").value.trim();
  if (!query) return;
  const requestKey = "search";
  elements.searchResults.replaceChildren();
  buttonBusy(button, true, "搜索中…");
  setStatus(elements.downloadStatus, `正在搜索${providerLabel(platform)}候选歌曲…`);
  try {
    const start = await apiPost("search", { platform, query, request_id: requestIdFor(requestKey) });
    const result = await waitForJob(start, (job) => {
      if (job.status === "running" || job.status === "queued") {
        setStatus(elements.downloadStatus, `正在搜索${providerLabel(platform)}候选歌曲…`);
      }
    });
    clearRequestId(requestKey);
    const candidates = Array.isArray(result.candidates) ? result.candidates : [];
    if (candidates.length === 0) {
      setStatus(elements.downloadStatus, "没有找到候选歌曲。请换个关键词或确认分享链接后再试。", "warning");
      elements.searchResults.append(node("p", "empty-state", "没有可下载的结果。"));
      return;
    }
    renderSearchResults(candidates, platform);
    setStatus(elements.downloadStatus, `找到 ${candidates.length} 首候选歌曲；请选择一首下载。`, "success");
  } catch (error) {
    if (error.jobStatus) clearRequestId(requestKey);
    setStatus(elements.downloadStatus, error.message || "搜索失败。", "error");
  } finally {
    buttonBusy(button, false);
  }
}

function renderSearchResults(candidates, platform) {
  elements.searchResults.replaceChildren();
  for (const candidate of candidates) {
    const result = node("article", "search-result");
    const copy = node("div", "search-result-copy");
    copy.append(node("h3", "search-result-title", candidate.title || "未知歌曲"));
    const metadata = [
      candidate.artist || "未知歌手",
      candidate.album || "未知专辑",
      formatDuration(candidate.duration),
    ].join(" · ");
    copy.append(node("p", "search-result-meta", metadata));
    const download = node("button", "button primary small", "下载此歌曲");
    download.type = "button";
    download.addEventListener("click", () => downloadCandidate(platform, candidate, download));
    result.append(copy, download);
    elements.searchResults.append(result);
  }
}

async function downloadCandidate(platform, candidate, button) {
  const query = String(candidate?.query || "").trim();
  if (!query) {
    setStatus(elements.downloadStatus, "候选歌曲缺少精确选择编号，请重新搜索。", "error");
    return;
  }
  const requestKey = `download:${platform}:${query}`;
  buttonBusy(button, true, "下载中…");
  setStatus(elements.downloadStatus, `正在下载《${candidate.title || "未知歌曲"}》…`);
  try {
    const start = await apiPost("download", {
      platform,
      query,
      id: candidate.id,
      request_id: requestIdFor(requestKey),
    });
    const result = await waitForJob(start, () => {
      setStatus(elements.downloadStatus, `正在下载并处理《${candidate.title || "未知歌曲"}》…`);
    });
    clearRequestId(requestKey);
    const song = result.song || {};
    setStatus(
      elements.downloadStatus,
      `已下载《${song.title || candidate.title || "未命名歌曲"}》${song.artist ? ` — ${song.artist}` : ""}。`,
      "success",
    );
    elements.filePlatform.value = platform;
    await refreshFiles();
  } catch (error) {
    if (error.jobStatus) clearRequestId(requestKey);
    setStatus(elements.downloadStatus, error.message || "音频下载失败。", "error");
  } finally {
    buttonBusy(button, false);
  }
}

async function sendTestAudio(event) {
  event.preventDefault();
  const button = document.getElementById("test-submit");
  const platformId = elements.targetPlatform.value;
  const targetType = elements.targetType.value;
  const targetId = elements.targetId.value.trim();
  if (!platformId || !targetId) {
    setStatus(elements.testStatus, "请选择平台实例并填写目标 ID。", "error");
    return;
  }
  const targetLabel = targetType === "group" ? "群聊" : "私聊";
  const requestKey = "test_audio";
  buttonBusy(button, true, "测试发送中…");
  setStatus(elements.testStatus, `正在合成约 65 秒提示音并分段发送到所选${targetLabel}…`);
  try {
    const start = await apiPost("test-audio", {
      platform_id: platformId,
      target_type: targetType,
      target_id: targetId,
      request_id: requestIdFor(requestKey),
    });
    const result = await waitForJob(start, () => {
      setStatus(elements.testStatus, "正在生成提示音、切分音频并逐段发送…");
    });
    clearRequestId(requestKey);
    const sent = Number(result.sent || 0);
    const total = Number(result.total || 0);
    const complete = result.ok === true && total > 0 && sent === total && !result.failed_segment;
    const failed = result.failed_segment ? `；第 ${result.failed_segment} 段失败` : "";
    const outcome = `${complete ? "65 秒提示音发送完成" : "65 秒提示音发送未完成"}：已发送 ${sent}/${total} 段${failed}。${result.message || ""}`;
    try {
      await refreshOverview();
    } catch {
      // Keep the completed send result visible if refreshing status fails.
    }
    setStatus(elements.testStatus, outcome, complete ? "success" : "warning");
  } catch (error) {
    if (error.jobStatus) clearRequestId(requestKey);
    try {
      await refreshOverview();
    } catch {
      // Preserve the send error if refreshing status fails.
    }
    setStatus(elements.testStatus, error.message || "合成提示音发送失败。", "error");
  } finally {
    buttonBusy(button, false);
  }
}

async function sendStoredSong(form) {
  if (form.dataset.busy === "true") return;
  const songId = form.dataset.songId || "";
  const title = form.dataset.songTitle || "未命名歌曲";
  const coverId = form.dataset.coverId || "";
  const platform = form.querySelector("[data-send-platform]").value;
  const targetType = form.querySelector("[data-send-type]").value;
  const targetId = form.querySelector("[data-send-target]").value.trim();
  const button = form.querySelector('button[type="submit"]');
  const status = form.closest(".song-send-panel").querySelector(".song-send-status");
  const requestKey = `song_send:${songId}:${coverId}:${platform}:${targetType}:${targetId}`;
  const targetName = form.querySelector("[data-send-platform]").selectedOptions[0]?.textContent || platform;
  const targetLabel = `${targetName} · ${targetType === "group" ? "群聊" : "私聊"} ${targetId}`;
  if (coverId && form.dataset.coverUsable !== "true") {
    setStatus(status, "旧处理版本，需要重新生成；此翻唱缓存不能发送。", "warning");
    return;
  }
  if (!songId || !platform || !targetId) {
    setStatus(status, "请先选择机器人实例并填写 QQ 号或群号。", "error");
    return;
  }

  form.dataset.busy = "true";
  for (const control of form.querySelectorAll("select, input")) control.disabled = true;
  buttonBusy(button, true, "正在发送…");
  setStatus(status, `正在向 ${targetLabel} 按顺序发送《${title}》…`, "info");
  try {
    const body = {
      song_id: songId,
      platform_id: platform,
      target_type: targetType,
      target_id: targetId,
      request_id: requestIdFor(requestKey),
    };
    if (coverId) body.cover_id = coverId;
    const start = await apiPost("files/send", body);
    const result = await waitForJob(start, (job) => {
      if (job.status === "running" || job.status === "queued") {
        setStatus(status, `后台任务正在向 ${targetLabel} 顺序发送《${title}》…`, "info");
      }
    });
    clearRequestId(requestKey);
    const sent = Number(result.sent || 0);
    const total = Number(result.total || 0);
    const complete = result.ok === true && total > 0 && sent === total && !result.failed_segment;
    const resultTarget = result.target || { platform_id: platform, target_type: targetType, target_id: targetId };
    const resultPlatform = currentOverview?.targets?.find((target) => target.id === resultTarget.platform_id);
    const resultPlatformLabel = resultPlatform
      ? `${resultPlatform.name || resultPlatform.adapter} · ${resultPlatform.id}`
      : resultTarget.platform_id;
    const resultTargetLabel = `${resultPlatformLabel} · ${resultTarget.target_type === "group" ? "群聊" : "私聊"} ${resultTarget.target_id}`;
    const displayedTitle = coverId ? title : (result.song?.title || title);
    if (complete) {
      setStatus(status, `《${displayedTitle}》发送完成：已发送 ${sent}/${total} 段，目标 ${resultTargetLabel}。`, "success");
    } else {
      const failed = result.failed_segment ? `失败片段：${result.failed_segment}。` : "";
      const rawDetail = String(result.message || "").trim();
      const detail = rawDetail ? `${rawDetail}${/[。.!?！？]$/.test(rawDetail) ? "" : "。"}` : "";
      setStatus(
        status,
        `《${displayedTitle}》发送未完成：已发送 ${sent}/${total} 段，目标 ${resultTargetLabel}。${failed}${detail}已发送内容不会自动重发。`,
        "warning",
      );
    }
  } catch (error) {
    if (error.jobStatus) clearRequestId(requestKey);
    setStatus(
      status,
      `发送任务未完成：${error.message || "发送失败"}。若已有部分音频送达，插件不会自动重发；请先检查目标会话。`,
      "error",
    );
  } finally {
    form.dataset.busy = "false";
    for (const control of form.querySelectorAll("select, input")) control.disabled = false;
    buttonBusy(button, false);
  }
}

function installEventHandlers() {
  const mainTabButtons = [...document.querySelectorAll("[data-main-tab]")];
  for (const tab of mainTabButtons) {
    tab.addEventListener("click", () => setActiveMainTab(tab.dataset.mainTab));
    tab.addEventListener("keydown", (event) => {
      if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
      event.preventDefault();
      const index = mainTabButtons.findIndex((item) => item.dataset.mainTab === activeMainTab);
      let next = index;
      if (event.key === "ArrowRight") next = (index + 1) % mainTabButtons.length;
      if (event.key === "ArrowLeft") next = (index - 1 + mainTabButtons.length) % mainTabButtons.length;
      if (event.key === "Home") next = 0;
      if (event.key === "End") next = mainTabButtons.length - 1;
      setActiveMainTab(mainTabButtons[next].dataset.mainTab);
      mainTabButtons[next].focus();
    });
  }

  const settingsTabButtons = [...elements.settingsTabs.querySelectorAll("[data-settings-tab]")];
  for (const tab of settingsTabButtons) {
    tab.addEventListener("click", () => setActiveSettingsTab(tab.dataset.settingsTab));
    tab.addEventListener("keydown", (event) => {
      if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
      event.preventDefault();
      const index = settingsTabButtons.findIndex((item) => item.dataset.settingsTab === activeSettingsTab);
      let next = index;
      if (event.key === "ArrowRight") next = (index + 1) % settingsTabButtons.length;
      if (event.key === "ArrowLeft") next = (index - 1 + settingsTabButtons.length) % settingsTabButtons.length;
      if (event.key === "Home") next = 0;
      if (event.key === "End") next = settingsTabButtons.length - 1;
      const nextTab = settingsTabButtons[next];
      setActiveSettingsTab(nextTab.dataset.settingsTab);
      nextTab.focus();
    });
  }

  const environmentTabButtons = [...elements.environmentTabs.querySelectorAll("[data-environment-tab]")];
  for (const tab of environmentTabButtons) {
    tab.addEventListener("click", () => setActiveEnvironmentTab(tab.dataset.environmentTab));
    tab.addEventListener("keydown", (event) => {
      if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
      event.preventDefault();
      const index = environmentTabButtons.findIndex((item) => item.dataset.environmentTab === activeEnvironmentCategory);
      let next = index;
      if (event.key === "ArrowRight") next = (index + 1) % environmentTabButtons.length;
      if (event.key === "ArrowLeft") next = (index - 1 + environmentTabButtons.length) % environmentTabButtons.length;
      if (event.key === "Home") next = 0;
      if (event.key === "End") next = environmentTabButtons.length - 1;
      const category = environmentTabButtons[next];
      setActiveEnvironmentTab(category.dataset.environmentTab);
      category.focus();
    });
  }

  const categoryTabButtons = [...elements.templateCategoryTabs.querySelectorAll("[data-template-category]")];
  for (const tab of categoryTabButtons) {
    tab.addEventListener("click", () => setActiveTemplateCategory(tab.dataset.templateCategory));
    tab.addEventListener("keydown", (event) => {
      if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
      event.preventDefault();
      const index = categoryTabButtons.findIndex((item) => item.dataset.templateCategory === activeTemplateCategory);
      let next = index;
      if (event.key === "ArrowRight") next = (index + 1) % categoryTabButtons.length;
      if (event.key === "ArrowLeft") next = (index - 1 + categoryTabButtons.length) % categoryTabButtons.length;
      if (event.key === "Home") next = 0;
      if (event.key === "End") next = categoryTabButtons.length - 1;
      const category = categoryTabButtons[next];
      setActiveTemplateCategory(category.dataset.templateCategory);
      category.focus();
    });
  }

  document.querySelectorAll("[data-provider-tab]").forEach((tab) => {
    tab.addEventListener("click", () => setActiveProvider(tab.dataset.providerTab));
    tab.addEventListener("keydown", (event) => {
      if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
      event.preventDefault();
      const index = PROVIDERS.findIndex((provider) => provider.id === activeProvider);
      let next = index;
      if (event.key === "ArrowRight") next = (index + 1) % PROVIDERS.length;
      if (event.key === "ArrowLeft") next = (index - 1 + PROVIDERS.length) % PROVIDERS.length;
      if (event.key === "Home") next = 0;
      if (event.key === "End") next = PROVIDERS.length - 1;
      const provider = PROVIDERS[next];
      setActiveProvider(provider.id);
      document.querySelector(`[data-provider-tab="${provider.id}"]`).focus();
    });
  });

  document.querySelectorAll("[data-login-start]").forEach((button) => {
    button.addEventListener("click", () => startLogin(button.dataset.loginStart, button));
  });
  document.querySelectorAll("[data-login-cancel]").forEach((button) => {
    button.addEventListener("click", () => cancelLogin(button.dataset.loginCancel, button));
  });
  document.querySelectorAll("[data-login-logout]").forEach((button) => {
    button.addEventListener("click", () => logout(button.dataset.loginLogout, button));
  });

  elements.toolList.addEventListener("click", (event) => {
    const button = event.target.closest("[data-install-tool]");
    if (button) installTool(button.dataset.installTool, button);
  });
  document.getElementById("save-tool-urls").addEventListener("click", async (event) => {
    const button = event.currentTarget;
    buttonBusy(button, true, "保存中…");
    try {
      await saveToolUrls();
    } catch (error) {
      setStatus(elements.toolStatus, error.message || "保存自定义下载地址失败。", "error");
    } finally {
      buttonBusy(button, false);
    }
  });
  elements.installMissing.addEventListener("click", (event) => installMissingTools(event.currentTarget));
  document.getElementById("refresh-overview").addEventListener("click", (event) => checkEnvironment(event.currentTarget));
  elements.fileFilterForm.addEventListener("submit", (event) => {
    event.preventDefault();
    refreshFiles(document.getElementById("refresh-files"));
  });
  elements.fileGroups.addEventListener("click", (event) => {
    const button = event.target.closest("[data-toggle-song-send], [data-toggle-cover-send]");
    if (!button) return;
    const panel = button.matches("[data-toggle-cover-send]")
      ? button.closest(".cover-variant")?.querySelector(".song-send-panel")
      : button.closest(".track-entry")?.querySelector(".song-send-panel");
    if (!panel) return;
    const open = panel.hidden;
    panel.hidden = !open;
    button.setAttribute("aria-expanded", String(open));
    if (!button.dataset.openText) button.dataset.openText = button.textContent;
    button.textContent = open ? "收起发送" : button.dataset.openText;
    if (open) panel.querySelector("[data-send-platform]")?.focus();
  });
  elements.fileGroups.addEventListener("submit", (event) => {
    const form = event.target.closest("form[data-song-send]");
    if (!form) return;
    event.preventDefault();
    sendStoredSong(form);
  });
  for (const eventName of ["input", "change"]) {
    elements.fileGroups.addEventListener(eventName, (event) => {
      const form = event.target.closest("form[data-song-send]");
      if (!form) return;
      const confirmation = form.closest(".song-send-panel").querySelector(".song-send-confirmation");
      updateSongSendConfirmation(form, confirmation);
    });
  }
  elements.filePlatform.addEventListener("change", () => refreshFiles());
  elements.searchForm.addEventListener("submit", searchSongs);
  elements.testForm.addEventListener("submit", sendTestAudio);
  elements.coverTestForm.addEventListener("submit", testCoverService);
  elements.coverTestPlatform.addEventListener("change", refreshCoverTestSongs);
  elements.coverTestSong.addEventListener("change", updateCoverTestAvailability);
  elements.coverTestVoice.addEventListener("change", updateCoverTestAvailability);
  elements.settingsForm.addEventListener("submit", savePluginSettings);
  elements.settingsForm.addEventListener("input", (event) => {
    if (event.target.closest("[data-transient]")) return;
    elements.settingsForm.dataset.dirty = "true";
  });
  elements.settingsForm.addEventListener("change", (event) => {
    if (event.target.closest("[data-transient]")) return;
    elements.settingsForm.dataset.dirty = "true";
    syncPlaybackAvailability();
  });
  elements.settingsForm.addEventListener("input", (event) => {
    event.target.removeAttribute?.("aria-invalid");
  });
  elements.settingsForm.addEventListener("change", (event) => {
    event.target.removeAttribute?.("aria-invalid");
  });
  elements.coverServiceUrl.addEventListener("input", () => { coverServiceDirty = true; });
  elements.coverServiceToken.addEventListener("input", () => {
    coverServiceDirty = true;
    if (elements.coverServiceToken.value) elements.coverClearToken.checked = false;
  });
  elements.coverClearToken.addEventListener("change", () => { coverServiceDirty = true; });
  elements.coverCheck.addEventListener("click", () => checkCoverService());
  elements.coverGuideDownload.addEventListener("click", async (event) => {
    const button = event.currentTarget;
    buttonBusy(button, true, "准备中…");
    try {
      await bridge.download("cover/deployment", {}, "翻唱服务接入与接口说明.md");
      setPageStatus("已开始下载翻唱服务接入与接口说明。", "success");
    } catch (error) {
      setPageStatus(error.message || "下载翻唱服务接入与接口说明失败。", "error");
    } finally {
      buttonBusy(button, false);
    }
  });
  elements.coverImport.addEventListener("click", () => importCoverVoice());
  elements.textSettingsForm.addEventListener("submit", saveTextSettings);
  elements.textSettingsForm.addEventListener("input", () => {
    elements.textSettingsForm.dataset.dirty = "true";
  });
  elements.textSettingsForm.addEventListener("change", () => {
    elements.textSettingsForm.dataset.dirty = "true";
  });
  elements.targetType.addEventListener("change", () => {
    const isGroup = elements.targetType.value === "group";
    elements.targetIdLabel.textContent = isGroup ? "群聊 ID" : "私聊 ID";
    elements.targetId.placeholder = isGroup ? "输入群号" : "输入接收方 ID";
  });
  elements.targetPlatform.addEventListener("change", () => {
    if (currentOverview) renderTargets(currentOverview.targets || []);
  });
  elements.songLogTargetType.addEventListener("change", updateSongLogTargetLabels);

  window.addEventListener("beforeunload", () => {
    for (const providerId of loginPollers.keys()) stopLoginPolling(providerId);
  });
  window.addEventListener("resize", revealActiveMainTab);
  elements.mainTabs.addEventListener("scroll", updateMainTabIndicator, { passive: true });
}

async function start() {
  installEventHandlers();
  setActiveMainTab("environment");
  setActiveEnvironmentTab("normal");
  setActiveSettingsTab("passive");
  try {
    if (!bridge) throw new Error("AstrBot 页面接口未就绪，请刷新页面。");
    const context = await bridge.ready();
    const applyTheme = (value) => {
      document.documentElement.dataset.theme = value?.isDark ? "dark" : "light";
    };
    applyTheme(context);
    bridge.onContext((next) => applyTheme(next));
    await refreshOverview();
    try {
      await refreshCoverVoices();
    } catch (error) {
      setStatus(elements.coverServiceStatus, `暂时无法读取音色列表：${error.message || "请检查翻唱服务配置。"}`, "warning");
    }
    await refreshFiles();
    await refreshCoverTestSongs();
    document.documentElement.dataset.viewReady = "true";
    updateMainTabIndicator();
    setPageStatus("插件状态已读取。环境检测需手动启动，登录也只在点击后开始。", "success");
  } catch (error) {
    setPageStatus(error.message || "读取插件状态失败，请刷新页面重试。", "error");
  }
}

start();
