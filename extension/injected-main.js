(() => {
  const SOURCE = "jdchat-capture-main";
  const MESSAGE_TYPE = "jdchat-capture-event";
  const SNAPSHOT_INTERVAL_MS = 5000;
  const MAX_MESSAGES_PER_BATCH = 100;
  const MAX_NETWORK_MESSAGES = 50;
  const ENABLE_NETWORK_HOOKS = window.__JDCHAT_CAPTURE_ENABLE_NETWORK_HOOKS__ === true;

  let snapshotTimer = null;

  window.__JDCHAT_CAPTURE_MAIN_READY__ = {
    version: "0.1.2",
    networkHooksEnabled: ENABLE_NETWORK_HOOKS,
    startedAt: new Date().toISOString(),
  };

  if (ENABLE_NETWORK_HOOKS) installPassiveNetworkHooks();
  scheduleSessionSnapshots();

  function scheduleSessionSnapshots() {
    setTimeout(() => emitSessionSnapshot("initial"), 1500);
    snapshotTimer = setInterval(() => emitSessionSnapshot("interval"), SNAPSHOT_INTERVAL_MS);
    window.addEventListener("beforeunload", () => {
      if (snapshotTimer) clearInterval(snapshotTimer);
    });
  }

  function emitSessionSnapshot(reason) {
    const session = window.session;
    if (!session || typeof session !== "object") return;

    const conversation = safeClone(session.customer || {});
    const rawMessages = readSessionMessages(session);
    for (const message of rawMessages.slice(-MAX_MESSAGES_PER_BATCH)) {
      if (!message || typeof message !== "object") continue;
      emitEvent({
        eventId: stableEventId("session", message),
        source: "session",
        eventType: "message",
        conversation,
        message: safeClone(message),
        payload: { reason },
        capturedAt: new Date().toISOString(),
      });
    }
  }

  function readSessionMessages(session) {
    try {
      if (typeof session.messages !== "function") return [];
      const value = session.messages();
      const plain = toPlain(value);
      if (Array.isArray(plain)) return plain;
      if (plain && typeof plain === "object") return Object.values(plain);
      return [];
    } catch (_error) {
      return [];
    }
  }

  function installPassiveNetworkHooks() {
    hookWebSocket();
    hookFetch();
    hookXhr();
  }

  function hookWebSocket() {
    const NativeWebSocket = window.WebSocket;
    if (!NativeWebSocket || NativeWebSocket.__jdchatCaptureWrapped) return;

    function WrappedWebSocket(...args) {
      const socket = new NativeWebSocket(...args);
      socket.addEventListener("message", (event) => {
        observeNetworkPayload("websocket", args[0], event.data);
      });
      return socket;
    }

    copyStaticWebSocketFields(WrappedWebSocket, NativeWebSocket);
    WrappedWebSocket.prototype = NativeWebSocket.prototype;
    WrappedWebSocket.__jdchatCaptureWrapped = true;
    window.WebSocket = WrappedWebSocket;
  }

  function copyStaticWebSocketFields(target, source) {
    for (const key of ["CONNECTING", "OPEN", "CLOSING", "CLOSED"]) {
      try {
        Object.defineProperty(target, key, { value: source[key], enumerable: true });
      } catch (_error) {
        // Ignore non-critical static field copy failures.
      }
    }
  }

  function hookFetch() {
    const nativeFetch = window.fetch;
    if (!nativeFetch || nativeFetch.__jdchatCaptureWrapped) return;

    async function wrappedFetch(...args) {
      const response = await nativeFetch.apply(this, args);
      const url = requestUrl(args[0]);
      if (isRelevantUrl(url)) {
        response
          .clone()
          .text()
          .then((text) => observeNetworkPayload("fetch", url, text))
          .catch(() => undefined);
      }
      return response;
    }

    wrappedFetch.__jdchatCaptureWrapped = true;
    window.fetch = wrappedFetch;
  }

  function hookXhr() {
    const nativeOpen = XMLHttpRequest.prototype.open;
    const nativeSend = XMLHttpRequest.prototype.send;
    if (nativeOpen.__jdchatCaptureWrapped || nativeSend.__jdchatCaptureWrapped) return;

    XMLHttpRequest.prototype.open = function patchedOpen(method, url, ...rest) {
      this.__jdchatCaptureUrl = url;
      return nativeOpen.call(this, method, url, ...rest);
    };

    XMLHttpRequest.prototype.send = function patchedSend(...args) {
      this.addEventListener("loadend", () => {
        const url = this.__jdchatCaptureUrl;
        if (!isRelevantUrl(url)) return;
        try {
          observeNetworkPayload("xhr", url, this.responseText);
        } catch (_error) {
          // Some response types do not expose responseText.
        }
      });
      return nativeSend.apply(this, args);
    };

    XMLHttpRequest.prototype.open.__jdchatCaptureWrapped = true;
    XMLHttpRequest.prototype.send.__jdchatCaptureWrapped = true;
  }

  function observeNetworkPayload(source, url, payload) {
    const parsed = parsePayload(payload);
    const messages = extractChatMessages(parsed).slice(0, MAX_NETWORK_MESSAGES);
    if (!messages.length) return;

    const conversation = safeClone((window.session && window.session.customer) || {});
    for (const message of messages) {
      emitEvent({
        eventId: stableEventId(source, message),
        source,
        eventType: "message",
        conversation,
        message: safeClone(message),
        payload: { url: sanitizeUrl(url) },
        capturedAt: new Date().toISOString(),
      });
    }
  }

  function extractChatMessages(value, out = [], depth = 0) {
    if (depth > 8 || out.length >= MAX_NETWORK_MESSAGES) return out;
    if (Array.isArray(value)) {
      for (const item of value) extractChatMessages(item, out, depth + 1);
      return out;
    }
    if (!value || typeof value !== "object") return out;

    if (value.type === "chat_message" && value.body && typeof value.body === "object") {
      out.push(value);
      return out;
    }

    for (const nestedKey of ["messages", "msgs", "nMsgs", "data", "result", "body", "payload"]) {
      if (nestedKey in value) extractChatMessages(value[nestedKey], out, depth + 1);
      if (out.length >= MAX_NETWORK_MESSAGES) return out;
    }
    return out;
  }

  function parsePayload(payload) {
    if (payload == null) return null;
    if (typeof payload === "object") return toPlain(payload);
    if (typeof payload !== "string") return payload;

    const trimmed = payload.trim();
    if (!trimmed) return null;
    try {
      return JSON.parse(trimmed);
    } catch (_error) {
      return { rawText: trimmed.slice(0, 2000) };
    }
  }

  function stableEventId(source, message) {
    const id = message && (message.id || message.msgId || message.msg_id);
    if (id) return `${source}-${id}`;
    const mid = message && message.mid;
    if (mid) return `${source}-mid-${mid}`;
    return `${source}-${hashString(JSON.stringify(safeClone(message)).slice(0, 4000))}`;
  }

  function emitEvent(event) {
    window.postMessage({ source: SOURCE, type: MESSAGE_TYPE, event }, "*");
  }

  function toPlain(value) {
    if (value && typeof value.toJS === "function") return value.toJS();
    return value;
  }

  function safeClone(value, depth = 0) {
    if (depth > 8) return "[MaxDepth]";
    if (value == null) return value;
    if (typeof value === "string") return value.length > 20000 ? value.slice(0, 20000) : value;
    if (typeof value !== "object") return value;
    const plain = toPlain(value);
    if (Array.isArray(plain)) return plain.slice(0, 200).map((item) => safeClone(item, depth + 1));

    const cloned = {};
    for (const [key, item] of Object.entries(plain)) {
      if (isSensitiveKey(key)) {
        cloned[key] = redactValue(item);
      } else {
        cloned[key] = safeClone(item, depth + 1);
      }
    }
    return cloned;
  }

  function isSensitiveKey(key) {
    return /cookie|token|access_token|authorization|password|secret|sign/i.test(String(key || ""));
  }

  function redactValue(value) {
    const text = typeof value === "string" ? value : JSON.stringify(value);
    return { redacted: true, len: text ? text.length : 0, hash: hashString(text || "") };
  }

  function requestUrl(input) {
    if (!input) return "";
    if (typeof input === "string") return input;
    if (input.url) return input.url;
    return "";
  }

  function isRelevantUrl(url) {
    return /api-dd\.jd\.com|api\.m\.jd\.com|vp\.jd\.com|dongdong\.jd\.com\/workbench/i.test(String(url || ""));
  }

  function sanitizeUrl(url) {
    return String(url || "").replace(/([?&](?:token|aid|pin|app|account|access_token|sign|body|param_json)=)[^&]+/gi, "$1<redacted>");
  }

  function hashString(input) {
    let hash = 2166136261;
    const value = String(input || "");
    for (let index = 0; index < value.length; index += 1) {
      hash ^= value.charCodeAt(index);
      hash += (hash << 1) + (hash << 4) + (hash << 7) + (hash << 8) + (hash << 24);
    }
    return (hash >>> 0).toString(16);
  }
})();
