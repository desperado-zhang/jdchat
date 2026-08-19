const DEFAULT_CONFIG = {
  enabled: true,
  captureReceptionChatLog: true,
  captureLegacyRealtime: false,
  captureDom: false,
  captureSession: false,
  captureNetwork: false,
  captureFetch: true,
  captureXhr: true,
  captureWebSocket: false,
  autoScrollHistory: false,
  gatewayUrl: "http://127.0.0.1:8765",
  apiToken: "",
  maxBatchSize: 50,
  maxQueueSize: 2000,
  receptionMaxPages: 3,
  receptionMaxConversations: 30,
  receptionMaxRuntimeMinutes: 5,
};
const RECEPTION_COMMAND_TYPE = "jdchat-reception-collector-command";

const CHECKBOX_IDS = [
  "enabled",
  "captureReceptionChatLog",
  "captureLegacyRealtime",
  "captureDom",
  "captureSession",
  "captureNetwork",
  "captureFetch",
  "captureXhr",
  "captureWebSocket",
  "autoScrollHistory",
];
const NUMBER_CONFIG_IDS = [
  "receptionMaxPages",
  "receptionMaxConversations",
  "receptionMaxRuntimeMinutes",
];
let statusTimer = null;

document.addEventListener("DOMContentLoaded", init);

async function init() {
  const config = await readConfig();
  fillForm(config);
  for (const id of [...CHECKBOX_IDS, ...NUMBER_CONFIG_IDS, "gatewayUrl"]) {
    document.getElementById(id).addEventListener("change", saveConfig);
  }
  document.getElementById("startReceptionCollector").addEventListener("click", startReceptionCollector);
  document.getElementById("stopReceptionCollector").addEventListener("click", stopReceptionCollector);
  refreshCollectorStatus().catch(() => undefined);
  statusTimer = window.setInterval(() => {
    refreshCollectorStatus().catch(() => undefined);
  }, 1000);
  window.addEventListener("unload", () => {
    if (statusTimer) window.clearInterval(statusTimer);
  });
}

async function readConfig() {
  const { config } = await chrome.storage.local.get(["config"]);
  return { ...DEFAULT_CONFIG, ...(config || {}) };
}

function fillForm(config) {
  document.getElementById("gatewayUrl").value = config.gatewayUrl || DEFAULT_CONFIG.gatewayUrl;
  for (const id of CHECKBOX_IDS) {
    document.getElementById(id).checked = config[id] !== false;
  }
  for (const id of NUMBER_CONFIG_IDS) {
    document.getElementById(id).value = String(config[id] || DEFAULT_CONFIG[id]);
  }
}

async function saveConfig() {
  const current = await readConfig();
  const next = {
    ...current,
    gatewayUrl: document.getElementById("gatewayUrl").value.trim() || DEFAULT_CONFIG.gatewayUrl,
  };
  for (const id of CHECKBOX_IDS) {
    next[id] = document.getElementById(id).checked;
  }
  for (const id of NUMBER_CONFIG_IDS) {
    next[id] = numericValue(id, DEFAULT_CONFIG[id]);
  }
  await chrome.storage.local.set({ config: next });
  setStatus("已保存，刷新相关页面后生效");
}

async function startReceptionCollector() {
  await saveConfig();
  const config = await readConfig();
  setCollectorButtons(true);
  try {
    const response = await sendReceptionCommand("start", {
      maxPages: positiveInt(config.receptionMaxPages, DEFAULT_CONFIG.receptionMaxPages),
      maxConversations: positiveInt(
        config.receptionMaxConversations,
        DEFAULT_CONFIG.receptionMaxConversations,
      ),
      maxRuntimeMs:
        positiveInt(config.receptionMaxRuntimeMinutes, DEFAULT_CONFIG.receptionMaxRuntimeMinutes) * 60 * 1000,
    });
    renderReceptionResponse(response);
  } catch (error) {
    renderCollectorError(error);
  } finally {
    setCollectorButtons(false);
  }
}

async function stopReceptionCollector() {
  setCollectorButtons(true);
  try {
    const response = await sendReceptionCommand("stop");
    renderReceptionResponse(response);
  } catch (error) {
    renderCollectorError(error);
  } finally {
    setCollectorButtons(false);
  }
}

async function refreshCollectorStatus() {
  try {
    const response = await sendReceptionCommand("status");
    if (!response || response.ok === false) {
      throw new Error(response && response.error ? response.error : "状态读取失败");
    }
    renderCollectorStatus(response.status || response);
  } catch (_error) {
    document.getElementById("collectorStatus").textContent = "状态：请打开京麦聊天记录页面";
  }
}

async function sendReceptionCommand(command, options = {}) {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab || !tab.id) throw new Error("未找到当前标签页");
  return chrome.tabs.sendMessage(tab.id, {
    type: RECEPTION_COMMAND_TYPE,
    command,
    options,
  });
}

function renderReceptionResponse(response) {
  if (response && response.status) {
    renderCollectorStatus(response.status);
    return;
  }
  if (response && response.ok === false) {
    renderCollectorError(new Error(response.error || "命令执行失败"));
    return;
  }
  renderCollectorStatus(response || {});
}

function renderCollectorStatus(status) {
  const text = [
    `状态：${status.label || status.phase || "未知"}`,
    `页数：${status.currentPage || 0}/${status.maxPages || 0}`,
    `会话：${status.openedRows || 0}/${status.maxConversations || 0}`,
    `截获：${status.capturedDetails || 0}，失败：${status.failures || 0}`,
    status.lastAction ? `动作：${status.lastAction}` : "",
    status.lastError ? `错误：${status.lastError}` : "",
  ]
    .filter(Boolean)
    .join("\n");
  document.getElementById("collectorStatus").textContent = text;
}

function renderCollectorError(error) {
  const message = error && error.message ? error.message : String(error || "未知错误");
  document.getElementById("collectorStatus").textContent = `状态：命令发送失败\n错误：${message}`;
}

function setCollectorButtons(disabled) {
  document.getElementById("startReceptionCollector").disabled = disabled;
  document.getElementById("stopReceptionCollector").disabled = disabled;
}

function numericValue(id, fallback) {
  return positiveInt(document.getElementById(id).value, fallback);
}

function positiveInt(value, fallback) {
  const parsed = Number.parseInt(value, 10);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : fallback;
}

function setStatus(message) {
  const status = document.getElementById("status");
  status.textContent = message;
  window.clearTimeout(setStatus.timer);
  setStatus.timer = window.setTimeout(() => {
    status.textContent = "";
  }, 2500);
}
