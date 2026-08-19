const DEFAULT_CONFIG = {
  enabled: true,
  captureDom: true,
  captureSession: true,
  captureNetwork: false,
  captureFetch: true,
  captureXhr: true,
  captureWebSocket: false,
  autoScrollHistory: true,
  gatewayUrl: "http://127.0.0.1:8765",
  apiToken: "",
  maxBatchSize: 50,
  maxQueueSize: 2000,
};

const CHECKBOX_IDS = [
  "enabled",
  "captureDom",
  "captureSession",
  "captureNetwork",
  "captureFetch",
  "captureXhr",
  "captureWebSocket",
  "autoScrollHistory",
];

document.addEventListener("DOMContentLoaded", init);

async function init() {
  const config = await readConfig();
  fillForm(config);
  for (const id of [...CHECKBOX_IDS, "gatewayUrl"]) {
    document.getElementById(id).addEventListener("change", saveConfig);
  }
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
  await chrome.storage.local.set({ config: next });
  setStatus("已保存，刷新咚咚页面后生效");
}

function setStatus(message) {
  const status = document.getElementById("status");
  status.textContent = message;
  window.clearTimeout(setStatus.timer);
  setStatus.timer = window.setTimeout(() => {
    status.textContent = "";
  }, 2500);
}
