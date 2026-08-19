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
};
const RETRY_ALARM_NAME = "jdchat-flush-retry";
const LEGACY_QUEUE_KEY = "queue";
const RECEPTION_QUEUE_KEY = "receptionQueue";

let flushTimer = null;
let flushing = false;
let enqueueChain = Promise.resolve();

chrome.runtime.onInstalled.addListener(async () => {
  await ensureStorageDefaults();
  await ensureRetryAlarm();
  scheduleFlush(1000);
});

chrome.runtime.onStartup.addListener(() => {
  ensureStorageDefaults().catch(() => undefined);
  ensureRetryAlarm().catch(() => undefined);
  scheduleFlush(1000);
});

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name !== RETRY_ALARM_NAME) return;
  flushQueue().catch(() => undefined);
});

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (!message || message.type !== "jdchat-capture-event") return false;

  enqueueEvent(message.event)
    .then((result) => sendResponse(result))
    .catch((error) => sendResponse({ ok: false, error: String(error && error.message ? error.message : error) }));

  return true;
});

async function readConfig() {
  const { config } = await chrome.storage.local.get(["config"]);
  return { ...DEFAULT_CONFIG, ...(config || {}) };
}

async function ensureStorageDefaults() {
  const stored = await chrome.storage.local.get(["config", LEGACY_QUEUE_KEY, RECEPTION_QUEUE_KEY]);
  const next = {};
  if (!stored.config) next.config = DEFAULT_CONFIG;
  if (!Array.isArray(stored[LEGACY_QUEUE_KEY])) next[LEGACY_QUEUE_KEY] = [];
  if (!Array.isArray(stored[RECEPTION_QUEUE_KEY])) next[RECEPTION_QUEUE_KEY] = [];
  if (Object.keys(next).length) await chrome.storage.local.set(next);
}

async function readQueue(queueKey) {
  const stored = await chrome.storage.local.get([queueKey]);
  const queue = stored[queueKey];
  return Array.isArray(queue) ? queue : [];
}

async function writeQueue(queueKey, queue) {
  await chrome.storage.local.set({ [queueKey]: queue });
}

async function enqueueEvent(event) {
  const operation = enqueueChain.then(() => enqueueEventUnlocked(event));
  enqueueChain = operation.catch(() => undefined);
  return operation;
}

async function enqueueEventUnlocked(event) {
  const config = await readConfig();
  if (!config.enabled) return { ok: true, queued: false, disabled: true };
  if (!event || !event.source || !event.eventType) return { ok: false, queued: false, error: "invalid event" };
  if (isReceptionEvent(event) && config.captureReceptionChatLog === false) {
    return { ok: true, queued: false, disabled: true };
  }
  if (!isReceptionEvent(event) && config.captureLegacyRealtime !== true) {
    return { ok: true, queued: false, disabled: true };
  }

  const queueKey = queueKeyForEvent(event);
  const queue = await readQueue(queueKey);
  queue.push(event);
  const trimmed = queue.slice(-config.maxQueueSize);
  await writeQueue(queueKey, trimmed);
  scheduleFlush(500);
  return { ok: true, queued: true, queue: queueKey, size: trimmed.length };
}

function scheduleFlush(delayMs) {
  if (flushTimer) return;
  flushTimer = setTimeout(() => {
    flushTimer = null;
    flushQueue().catch(() => undefined);
  }, delayMs);
}

async function flushQueue() {
  if (flushing) return;
  flushing = true;
  try {
    const config = await readConfig();
    if (!config.enabled) return;

    const receptionOk = await flushQueueKey(config, RECEPTION_QUEUE_KEY, "/reception/chatlog/events");
    const legacyOk = await flushQueueKey(config, LEGACY_QUEUE_KEY, "/capture/events");
    if (!receptionOk || !legacyOk) {
      scheduleFlush(5000);
    }
  } finally {
    flushing = false;
  }
}

async function flushQueueKey(config, queueKey, endpointPath) {
  const queue = await readQueue(queueKey);
  if (!queue.length) return true;

  const batch = queue.slice(0, config.maxBatchSize);
  const remaining = queue.slice(batch.length);
  const headers = { "Content-Type": "application/json" };
  if (config.apiToken) headers.Authorization = `Bearer ${config.apiToken}`;

  let response;
  try {
    response = await fetch(`${config.gatewayUrl.replace(/\/$/, "")}${endpointPath}`, {
      method: "POST",
      headers,
      body: JSON.stringify({
        pluginInstanceId: await pluginInstanceId(),
        events: batch,
      }),
    });
  } catch (_error) {
    return false;
  }

  if (!response.ok) return false;

  await writeQueue(queueKey, remaining);
  if (remaining.length) scheduleFlush(500);
  return true;
}

async function ensureRetryAlarm() {
  await chrome.alarms.create(RETRY_ALARM_NAME, { periodInMinutes: 1 });
}

async function pluginInstanceId() {
  const { pluginInstanceId: existing } = await chrome.storage.local.get(["pluginInstanceId"]);
  if (existing) return existing;
  const id = crypto.randomUUID();
  await chrome.storage.local.set({ pluginInstanceId: id });
  return id;
}

function isReceptionEvent(event) {
  return typeof event.source === "string" && event.source.startsWith("reception_");
}

function queueKeyForEvent(event) {
  return isReceptionEvent(event) ? RECEPTION_QUEUE_KEY : LEGACY_QUEUE_KEY;
}
