(() => {
  const SOURCE = "jdchat-capture-main";
  const MESSAGE_TYPE = "jdchat-capture-event";
  const CONTEXT_TYPE = "jdchat-capture-context";
  const CONTEXT_INTERVAL_MS = 500;
  const SNAPSHOT_INTERVAL_MS = 5000;
  const MAX_MESSAGES_PER_BATCH = 100;
  const MAX_NETWORK_MESSAGES = 50;
  const MAX_RECEPTION_MESSAGES_PER_RESPONSE = 500;
  const MAX_RECEPTION_SESSION_CACHE = 2000;
  const scriptConfig = readScriptConfig();
  const CAPTURE_SESSION = readFlag("captureSession", true);
  const ENABLE_RECEPTION_CHATLOG = readFlag("captureReceptionChatLog", false);
  const ENABLE_GENERIC_NETWORK =
    readFlag("captureNetwork", false) || window.__JDCHAT_CAPTURE_ENABLE_NETWORK_HOOKS__ === true;
  const ENABLE_NETWORK_HOOKS = ENABLE_RECEPTION_CHATLOG || ENABLE_GENERIC_NETWORK;
  const ENABLE_FETCH_HOOK = ENABLE_NETWORK_HOOKS && readFlag("captureFetch", true);
  const ENABLE_XHR_HOOK = ENABLE_NETWORK_HOOKS && readFlag("captureXhr", true);
  const ENABLE_WEBSOCKET_HOOK = ENABLE_NETWORK_HOOKS && readFlag("captureWebSocket", false);
  const receptionSessionsByCid = new Map();

  let contextTimer = null;
  let snapshotTimer = null;

  window.__JDCHAT_CAPTURE_MAIN_READY__ = {
    version: "0.2.1",
    networkHooksEnabled: ENABLE_NETWORK_HOOKS,
    receptionChatLogEnabled: ENABLE_RECEPTION_CHATLOG,
    genericNetworkEnabled: ENABLE_GENERIC_NETWORK,
    hooks: {
      fetch: ENABLE_FETCH_HOOK,
      xhr: ENABLE_XHR_HOOK,
      websocket: ENABLE_WEBSOCKET_HOOK,
    },
    sessionSnapshotsEnabled: CAPTURE_SESSION,
    startedAt: new Date().toISOString(),
  };

  if (ENABLE_NETWORK_HOOKS) installPassiveNetworkHooks();
  if (CAPTURE_SESSION) scheduleSessionSnapshots();

  function readScriptConfig() {
    const script = document.currentScript;
    return script && script.dataset ? script.dataset : {};
  }

  function readFlag(name, fallback) {
    const value = scriptConfig[name];
    if (value === "1" || value === "true") return true;
    if (value === "0" || value === "false") return false;
    return fallback;
  }

  function scheduleSessionSnapshots() {
    setTimeout(() => emitSessionContext("initial"), 500);
    setTimeout(() => emitSessionSnapshot("initial"), 1500);
    contextTimer = setInterval(() => {
      emitSessionContext("interval");
    }, CONTEXT_INTERVAL_MS);
    snapshotTimer = setInterval(() => {
      emitSessionSnapshot("interval");
    }, SNAPSHOT_INTERVAL_MS);
    window.addEventListener("beforeunload", () => {
      if (contextTimer) clearInterval(contextTimer);
      if (snapshotTimer) clearInterval(snapshotTimer);
    });
  }

  function emitSessionContext(reason) {
    const session = window.session;
    if (!session || typeof session !== "object" || !session.customer) return;
    window.postMessage(
      {
        source: SOURCE,
        type: CONTEXT_TYPE,
        event: {
          conversation: safeClone(session.customer),
          payload: { reason },
          capturedAt: new Date().toISOString(),
        },
      },
      "*",
    );
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
    if (ENABLE_WEBSOCKET_HOOK) hookWebSocket();
    if (ENABLE_FETCH_HOOK) hookFetch();
    if (ENABLE_XHR_HOOK) hookXhr();
  }

  function hookWebSocket() {
    const NativeWebSocket = window.WebSocket;
    if (!NativeWebSocket || NativeWebSocket.__jdchatCaptureWrapped) return;

    const WrappedWebSocket = new Proxy(NativeWebSocket, {
      construct(target, args) {
        const socket = Reflect.construct(target, args);
        attachWebSocketObserver(socket, args[0]);
        return socket;
      },
      apply(target, _thisArg, args) {
        const socket = Reflect.construct(target, args);
        attachWebSocketObserver(socket, args[0]);
        return socket;
      },
    });

    copyStaticWebSocketFields(WrappedWebSocket, NativeWebSocket);
    WrappedWebSocket.__jdchatCaptureWrapped = true;
    window.WebSocket = WrappedWebSocket;
  }

  function attachWebSocketObserver(socket, url) {
    try {
      socket.addEventListener("message", (event) => {
        observeNetworkPayload("websocket", url, event.data).catch(() => undefined);
      });
    } catch (_error) {
      // Observation failure must not affect the page's socket lifecycle.
    }
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
      this.__jdchatCaptureMethod = method;
      return nativeOpen.call(this, method, url, ...rest);
    };

    XMLHttpRequest.prototype.send = function patchedSend(...args) {
      this.addEventListener("loadend", () => {
        const url = this.__jdchatCaptureUrl;
        if (!isRelevantUrl(url)) return;
        try {
          if (this.responseType && this.responseType !== "text" && this.responseType !== "json") return;
          const payload = this.responseType === "json" ? this.response : this.responseText;
          observeNetworkPayload("xhr", url, payload).catch(() => undefined);
        } catch (_error) {
          // Some response types do not expose responseText.
        }
      });
      return nativeSend.apply(this, args);
    };

    XMLHttpRequest.prototype.open.__jdchatCaptureWrapped = true;
    XMLHttpRequest.prototype.send.__jdchatCaptureWrapped = true;
  }

  async function observeNetworkPayload(source, url, networkPayload) {
    const parsed = parsePayload(networkPayload);
    if (ENABLE_RECEPTION_CHATLOG && isReceptionListUrl(url)) {
      rememberReceptionSessions(parsed);
      return;
    }

    if (ENABLE_RECEPTION_CHATLOG && isReceptionChatLogUrl(url)) {
      const emitted = await emitReceptionChatLogEvents(source, url, parsed);
      if (emitted > 0 || !ENABLE_GENERIC_NETWORK) return;
    }

    if (!ENABLE_GENERIC_NETWORK) return;

    const extracted = extractChatMessages(parsed).slice(0, MAX_NETWORK_MESSAGES);
    if (!extracted.length) return;

    for (const item of extracted) {
      const message = item.message;
      emitEvent({
        eventId: stableEventId(source, message),
        source,
        eventType: "message",
        conversation: deriveConversation(message),
        message: safeClone(message),
        payload: {
          networkContext: {
            url: sanitizeUrl(url),
            matchedPath: item.path,
            responseShape: describeShape(parsed),
          },
        },
        capturedAt: new Date().toISOString(),
      });
    }
  }

  function rememberReceptionSessions(parsed) {
    for (const row of extractReceptionSessionRows(parsed)) {
      if (!row || typeof row !== "object" || !row.cid) continue;
      receptionSessionsByCid.set(String(row.cid), row);
    }
    while (receptionSessionsByCid.size > MAX_RECEPTION_SESSION_CACHE) {
      const oldestKey = receptionSessionsByCid.keys().next().value;
      receptionSessionsByCid.delete(oldestKey);
    }
  }

  function extractReceptionSessionRows(value, out = [], depth = 0) {
    if (depth > 6 || out.length >= MAX_RECEPTION_SESSION_CACHE) return out;
    if (Array.isArray(value)) {
      for (const item of value) extractReceptionSessionRows(item, out, depth + 1);
      return out;
    }
    if (!value || typeof value !== "object") return out;

    if (Array.isArray(value.waiterSessionList)) {
      for (const row of value.waiterSessionList) {
        if (row && typeof row === "object" && row.cid) out.push(row);
      }
    }

    for (const key of ["data", "result", "body", "payload"]) {
      if (key in value) extractReceptionSessionRows(value[key], out, depth + 1);
    }
    return out;
  }

  async function emitReceptionChatLogEvents(source, url, parsed) {
    const cid = queryParam(url, "cid");
    const cidHash = cid ? await sha256Hex(cid) : "";
    const sessionRow = cid ? receptionSessionsByCid.get(String(cid)) : null;
    const messages = extractReceptionChatLogMessages(parsed).slice(0, MAX_RECEPTION_MESSAGES_PER_RESPONSE);
    let emitted = 0;

    for (const item of messages) {
      const message = item.message;
      const customer = firstNonEmpty(message.customer, sessionRow && sessionRow.customer);
      const waiter = firstNonEmpty(message.waiter, sessionRow && sessionRow.service);
      const customerHash = customer ? await sha256Hex(customer) : undefined;
      const waiterHash = waiter ? await sha256Hex(waiter) : undefined;
      const eventMessage = sanitizeReceptionMessage(message, { cidHash, customerHash, waiterHash });
      emitEvent({
        eventId: stableReceptionEventId(cidHash, eventMessage),
        source: "reception_chatlog",
        eventType: "message",
        conversation: buildReceptionConversation(sessionRow, message, { cidHash, customerHash, waiterHash }),
        message: eventMessage,
        payload: {
          networkContext: {
            source,
            url: sanitizeUrl(url),
            matchedPath: item.path,
            responseShape: describeShape(parsed),
          },
        },
        capturedAt: new Date().toISOString(),
      });
      emitted += 1;
    }

    return emitted;
  }

  function extractReceptionChatLogMessages(value, out = [], depth = 0, path = "$") {
    if (depth > 8 || out.length >= MAX_RECEPTION_MESSAGES_PER_RESPONSE) return out;
    if (Array.isArray(value)) {
      value.forEach((item, index) => extractReceptionChatLogMessages(item, out, depth + 1, `${path}[${index}]`));
      return out;
    }
    if (!value || typeof value !== "object") return out;

    if (Array.isArray(value.chatLogMessageList)) {
      value.chatLogMessageList.forEach((message, index) => {
        if (isReceptionChatLogMessage(message)) out.push({ message, path: `${path}.chatLogMessageList[${index}]` });
      });
    }

    for (const key of ["chatLogList", "data", "result", "body", "payload"]) {
      if (key in value) extractReceptionChatLogMessages(value[key], out, depth + 1, `${path}.${key}`);
      if (out.length >= MAX_RECEPTION_MESSAGES_PER_RESPONSE) return out;
    }
    return out;
  }

  function isReceptionChatLogMessage(value) {
    if (!value || typeof value !== "object") return false;
    const hasIdentity = value.mid != null || value.uuid || value.sid;
    const hasContent = value.content || value.imgUrl || value.img_url || value.mediaUrl;
    const hasTime = value.created || value.messageAt || value.message_at;
    return Boolean(hasTime && (hasIdentity || hasContent));
  }

  function buildReceptionConversation(row, message, hashes) {
    return {
      cidHash: hashes.cidHash || undefined,
      customerHash: hashes.customerHash || undefined,
      waiterHash: hashes.waiterHash || undefined,
      serviceHash: hashes.waiterHash || undefined,
      mallId: firstNonEmpty(message.mallId, row && row.mallId),
      mallName: row && row.mallName,
      groupId: row && row.groupId,
      groupName: row && row.groupName,
      sessionType: row && row.sessionType,
      sessionTypeDesc: row && row.sessionTypeDesc,
      consultationDate: row && row.consultationDate,
      allocateTime: row && row.allocateTime,
      goodsName: row && row.goodsName,
      goodsId: row && row.pid,
    };
  }

  function sanitizeReceptionMessage(message, hashes) {
    const rawMid = firstNonEmpty(message.mid);
    const rawUuid = firstNonEmpty(message.uuid);
    const hasMid = rawMid !== "";
    const identity = hasMid ? rawMid : rawUuid ? hashString(rawUuid) : hashString(JSON.stringify(message).slice(0, 1000));
    return {
      id: hashes.cidHash ? `jm:${hashes.cidHash}:${identity}` : `jm:${identity}`,
      mid: hasMid ? rawMid : undefined,
      created: firstNonEmpty(message.created, message.messageAt, message.message_at),
      waiterSend: toBoolean(message.waiterSend),
      type: firstNonEmpty(message.type) || (firstNonEmpty(message.imgUrl, message.img_url, message.mediaUrl) ? "image" : "text"),
      content: message.content != null ? String(message.content) : undefined,
      imgUrl: firstNonEmpty(message.imgUrl, message.img_url, message.mediaUrl, message.media_url),
      width: firstNonEmpty(message.width),
      height: firstNonEmpty(message.height),
      lang: firstNonEmpty(message.lang),
    };
  }

  function extractChatMessages(value, out = [], depth = 0, path = "$") {
    if (depth > 8 || out.length >= MAX_NETWORK_MESSAGES) return out;
    if (Array.isArray(value)) {
      value.forEach((item, index) => extractChatMessages(item, out, depth + 1, `${path}[${index}]`));
      return out;
    }
    if (!value || typeof value !== "object") return out;

    const candidate = normalizeMessageCandidate(value);
    if (candidate) {
      out.push({ message: candidate, path });
      return out;
    }

    for (const nestedKey of [
      "messages",
      "messageList",
      "msgList",
      "msgs",
      "nMsgs",
      "records",
      "list",
      "history",
      "chatMessages",
      "items",
      "rows",
      "data",
      "result",
      "body",
      "payload",
    ]) {
      if (nestedKey in value) extractChatMessages(value[nestedKey], out, depth + 1, `${path}.${nestedKey}`);
      if (out.length >= MAX_NETWORK_MESSAGES) return out;
    }
    return out;
  }

  function normalizeMessageCandidate(value) {
    const body = normalizeBody(value.body);
    if (value.type === "chat_message" && body && typeof body === "object") {
      return { ...value, body };
    }

    const hasMessageIdentity = Boolean(
      value.id || value.msgId || value.msg_id || value.messageId || value.mid || value.localId || value.localMid,
    );
    const hasPartyOrTime = Boolean(
      value.from || value.to || value.timestamp || value.datetime || value.clientTime || value.time,
    );
    const hasBodyContent = Boolean(
      (body && (body.type || body.content || body.url || body.template || body.data)) ||
        value.content ||
        value.text ||
        (value.msg && !("code" in value)),
    );

    if (!hasBodyContent) return null;
    if (!hasMessageIdentity && !hasPartyOrTime) return null;

    return {
      ...value,
      type: value.type || "chat_message",
      body: body || {
        type: value.content || value.text || value.msg ? "text" : undefined,
        content: value.content || value.text || value.msg,
      },
    };
  }

  function normalizeBody(body) {
    if (!body) return null;
    if (typeof body === "object") return body;
    if (typeof body !== "string") return null;
    const parsed = parsePayload(body);
    return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed : { type: "text", content: body };
  }

  function deriveConversation(message) {
    const body = message && message.body && typeof message.body === "object" ? message.body : {};
    const chatinfo = body.chatinfo && typeof body.chatinfo === "object" ? body.chatinfo : {};
    const param = body.param && typeof body.param === "object" ? body.param : {};
    const conversation = {
      venderId: chatinfo.venderId || param.venderId,
      venderName: chatinfo.venderName || param.venderName,
      customerApp: chatinfo.customerApp || param.customerApp,
      customerPin: chatinfo.customerPin || param.customerPin,
      sellerApp: chatinfo.sellerApp || param.sellerApp,
      sellerPin: chatinfo.sellerPin || param.sellerPin,
      sessionType: chatinfo.sessionType || param.sessionType,
      customerName: chatinfo.customerName || param.customerName,
    };
    const hasConversationField = Object.values(conversation).some((value) => value != null && value !== "");
    if (hasConversationField) return safeClone(conversation);
    return {};
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

  function describeShape(value) {
    if (Array.isArray(value)) return { type: "array", length: value.length };
    if (!value || typeof value !== "object") return { type: typeof value };
    return { type: "object", keys: Object.keys(value).slice(0, 20) };
  }

  function stableEventId(source, message) {
    const id = message && (message.id || message.msgId || message.msg_id);
    if (id) return `${source}-${id}`;
    const mid = message && message.mid;
    if (mid) return `${source}-mid-${mid}`;
    return `${source}-${hashString(JSON.stringify(safeClone(message)).slice(0, 4000))}`;
  }

  function stableReceptionEventId(cidHash, message) {
    const identity = message && (message.id || message.mid || hashString(JSON.stringify(message).slice(0, 1000)));
    return `reception-chatlog-${cidHash || "unknown"}-${identity}`;
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
    const text = String(url || "");
    if (ENABLE_RECEPTION_CHATLOG && (isReceptionListUrl(text) || isReceptionChatLogUrl(text))) return true;
    if (!ENABLE_GENERIC_NETWORK) return false;
    return /api-dd\.jd\.com|api\.m\.jd\.com|vp\.jd\.com|dongdong\.jd\.com\/workbench/i.test(text);
  }

  function isReceptionListUrl(url) {
    return /\/waiterSession\/jingmai\/queryList(?:\?|$)/i.test(String(url || ""));
  }

  function isReceptionChatLogUrl(url) {
    return /\/waiterSession\/queryChatLog(?:\?|$)/i.test(String(url || ""));
  }

  function sanitizeUrl(url) {
    return String(url || "").replace(
      /([?&](?:token|aid|pin|app|account|access_token|sign|body|param_json|cid|sid|uuid|customer|waiter|service|mallId|appId|toCid)=)[^&]+/gi,
      "$1<redacted>",
    );
  }

  function queryParam(url, name) {
    try {
      return new URL(String(url || ""), location.href).searchParams.get(name) || "";
    } catch (_error) {
      return "";
    }
  }

  function firstNonEmpty(...values) {
    for (const value of values) {
      if (value == null) continue;
      if (typeof value === "string" && !value.trim()) continue;
      return value;
    }
    return "";
  }

  function toBoolean(value) {
    if (typeof value === "boolean") return value;
    if (typeof value === "number") return value !== 0;
    if (typeof value === "string") return /^(1|true|yes|y)$/i.test(value.trim());
    return false;
  }

  async function sha256Hex(input) {
    const value = String(input || "");
    try {
      if (window.crypto && window.crypto.subtle && typeof TextEncoder !== "undefined") {
        const digest = await window.crypto.subtle.digest("SHA-256", new TextEncoder().encode(value));
        return Array.from(new Uint8Array(digest))
          .map((byte) => byte.toString(16).padStart(2, "0"))
          .join("");
      }
    } catch (_error) {
      // Fall back to the local non-cryptographic hash only when Web Crypto is unavailable.
    }
    return hashString(value);
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
