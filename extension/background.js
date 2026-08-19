const DEFAULT_CONFIG = {
  enabled: true,
  gatewayUrl: "http://127.0.0.1:8765",
  apiToken: "",
  maxBatchSize: 50,
  maxQueueSize: 2000,
};
const RETRY_ALARM_NAME = "jdchat-flush-retry";

let flushTimer = null;
let flushing = false;

chrome.runtime.onInstalled.addListener(async () => {
  const { config } = await chrome.storage.local.get(["config"]);
  if (!config) {
    await chrome.storage.local.set({ config: DEFAULT_CONFIG, queue: [] });
  }
  await ensureRetryAlarm();
  scheduleFlush(1000);
});

chrome.runtime.onStartup.addListener(() => {
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

async function readQueue() {
  const { queue } = await chrome.storage.local.get(["queue"]);
  return Array.isArray(queue) ? queue : [];
}

async function writeQueue(queue) {
  await chrome.storage.local.set({ queue });
}

async function enqueueEvent(event) {
  const config = await readConfig();
  if (!config.enabled) return { ok: true, queued: false, disabled: true };
  if (!event || !event.source || !event.eventType) return { ok: false, queued: false, error: "invalid event" };

  const queue = await readQueue();
  queue.push(event);
  const trimmed = queue.slice(-config.maxQueueSize);
  await writeQueue(trimmed);
  scheduleFlush(500);
  return { ok: true, queued: true, size: trimmed.length };
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

    const queue = await readQueue();
    if (!queue.length) return;

    const batch = queue.slice(0, config.maxBatchSize);
    const remaining = queue.slice(batch.length);
    const headers = { "Content-Type": "application/json" };
    if (config.apiToken) headers.Authorization = `Bearer ${config.apiToken}`;

    let response;
    try {
      response = await fetch(`${config.gatewayUrl.replace(/\/$/, "")}/capture/events`, {
        method: "POST",
        headers,
        body: JSON.stringify({
          pluginInstanceId: await pluginInstanceId(),
          events: batch,
        }),
      });
    } catch (_error) {
      scheduleFlush(5000);
      return;
    }

    if (!response.ok) {
      scheduleFlush(5000);
      return;
    }

    await writeQueue(remaining);
    if (remaining.length) scheduleFlush(500);
  } finally {
    flushing = false;
  }
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
