(() => {
  const MAIN_SOURCE = "jdchat-capture-main";
  const MESSAGE_TYPE = "jdchat-capture-event";
  const DEFAULT_CONFIG = {
    captureDom: true,
    captureSession: true,
    captureNetwork: false,
    captureFetch: true,
    captureXhr: true,
    captureWebSocket: false,
  };
  const processedDomNodes = new WeakSet();
  let observer = null;
  let observedRoot = null;
  let rootWatcher = null;

  bootstrap().catch(() => startWithConfig(DEFAULT_CONFIG));

  async function bootstrap() {
    const config = await readConfig();
    startWithConfig(config);
  }

  function startWithConfig(config) {
    injectMainScript(config);
    window.addEventListener("message", onMainWorldMessage, false);
    if (config.captureDom !== false) startRootWatcher();
    window.addEventListener("beforeunload", () => {
      if (rootWatcher) clearInterval(rootWatcher);
      if (observer) observer.disconnect();
    });
  }

  async function readConfig() {
    const { config } = await chrome.storage.local.get(["config"]);
    return { ...DEFAULT_CONFIG, ...(config || {}) };
  }

  function injectMainScript(config) {
    const script = document.createElement("script");
    script.src = chrome.runtime.getURL("injected-main.js");
    script.dataset.jdchatCapture = "main";
    script.dataset.captureSession = config.captureSession === false ? "0" : "1";
    script.dataset.captureNetwork = config.captureNetwork === true ? "1" : "0";
    script.dataset.captureFetch = config.captureFetch === false ? "0" : "1";
    script.dataset.captureXhr = config.captureXhr === false ? "0" : "1";
    script.dataset.captureWebSocket = config.captureWebSocket === true ? "1" : "0";
    script.onload = () => script.remove();
    (document.documentElement || document.head).appendChild(script);
  }

  function onMainWorldMessage(event) {
    if (event.source !== window) return;
    const data = event.data;
    if (!data || data.source !== MAIN_SOURCE || data.type !== MESSAGE_TYPE || !data.event) return;
    sendEvent(data.event);
  }

  function startRootWatcher() {
    refreshChatRoot();
    rootWatcher = setInterval(refreshChatRoot, 1000);
  }

  function refreshChatRoot() {
    const root = findChatRoot();
    if (!root || root === observedRoot) return;
    observedRoot = root;
    attachDomObserver(root);
    scanExistingMessages(root);
  }

  function findChatRoot() {
    return document.querySelector("#t-chat-scroll, .chat-scroll-wrap");
  }

  function attachDomObserver(root) {
    if (observer) observer.disconnect();
    observer = new MutationObserver((mutations) => {
      for (const mutation of mutations) {
        for (const node of mutation.addedNodes) {
          if (node.nodeType !== Node.ELEMENT_NODE) continue;
          scanMessageNode(node);
        }
      }
    });
    observer.observe(root, { childList: true, subtree: true });
  }

  function scanExistingMessages(root) {
    root.querySelectorAll(".message").forEach((node) => scanMessageNode(node));
  }

  function scanMessageNode(node) {
    const messageNode = node.matches && node.matches(".message") ? node : node.querySelector && node.querySelector(".message");
    if (!messageNode || processedDomNodes.has(messageNode)) return;
    processedDomNodes.add(messageNode);

    const parsed = parseDomMessage(messageNode);
    if (!parsed) return;
    sendEvent(parsed);
  }

  function parseDomMessage(node) {
    const direction = inferDomDirection(node);
    const contentNode = node.querySelector(".message__content");
    const textWrapper = node.querySelector(".message__text");
    const image = node.querySelector(".message__image-wrap img, .message__image img");
    const card = node.querySelector(".CardWrapper.ProductCard, .CardWrapper");
    const timeNode = node.querySelector(".message__time_str");
    const nicknameNode = node.querySelector(".message__nickname");
    const bodyType = image ? "image" : card ? "template2" : direction === "system" ? "system" : "text";
    const content = text(contentNode || textWrapper || card || node);
    const imageUrl = image ? image.currentSrc || image.src || "" : "";
    const displayTime = text(timeNode);
    const nickname = text(nicknameNode);
    const customerName = text(document.querySelector(".chat-head-name")) || text(document.querySelector(".chat-head-title"));
    const domStableId = hashString(
      [
        customerName,
        direction,
        bodyType,
        content,
        imageUrl,
        displayTime,
        nodeIndex(node),
      ].join("|"),
    );

    if (!content && !imageUrl && !card) return null;

    return {
      eventId: `dom-${domStableId}`,
      source: "dom",
      eventType: "message",
      payload: {
        pageContext: readPageContext(),
      },
      conversation: {
        customerName: customerName || undefined,
        customerApp: "dom",
        customerPin: customerName ? `dom:${hashString(customerName)}` : "dom:unknown",
        sessionType: "dom",
      },
      message: {
        id: `dom-${domStableId}`,
        direction,
        type: "chat_message",
        body: {
          type: bodyType,
          content: content || undefined,
          url: imageUrl || undefined,
        },
        displayTime: displayTime || undefined,
        nickname: nickname || undefined,
      },
      capturedAt: new Date().toISOString(),
    };
  }

  function inferDomDirection(node) {
    if (node.querySelector(".message_right")) return "seller_or_waiter";
    if (node.querySelector(".message_left")) return "customer_or_external";
    if (node.querySelector(".message_center, .message__system")) return "system";
    return "unknown";
  }

  function text(node) {
    return (node && (node.innerText || node.textContent) ? node.innerText || node.textContent : "")
      .replace(/\s+/g, " ")
      .trim();
  }

  function nodeIndex(node) {
    const parent = node.parentElement;
    if (!parent) return 0;
    return Array.prototype.indexOf.call(parent.children, node);
  }

  function readPageContext() {
    const selectedTab = document.querySelector(".c_tabs-tab_check");
    const selectedTabLabel = elementLabel(selectedTab);
    const activeSidebarTab = normalizeSidebarTab(selectedTabLabel);
    const historyListVisible = activeSidebarTab === "history" && !!document.querySelector(".list-compatible.recent-user-w");
    return {
      activeSidebarTab,
      activeSidebarTabLabel: selectedTabLabel || undefined,
      historyListVisible,
      historyItemCount: historyListVisible
        ? document.querySelectorAll(".list-compatible.recent-user-w .alluser-item").length
        : 0,
      chatRootVisible: !!findChatRoot(),
      messageNodeCount: document.querySelectorAll("#t-chat-scroll .message, .chat-scroll-wrap .message").length,
      capturedPageUrl: location.origin + location.pathname,
    };
  }

  function normalizeSidebarTab(label) {
    if (/历史咨询/.test(label)) return "history";
    if (/正在咨询/.test(label)) return "current";
    if (/常用联系人/.test(label)) return "contacts";
    if (/群聊/.test(label)) return "group";
    if (/组织架构/.test(label)) return "organization";
    return "unknown";
  }

  function elementLabel(node) {
    if (!node) return "";
    return (
      node.getAttribute("title") ||
      node.getAttribute("aria-label") ||
      text(node)
    ).replace(/\s+/g, " ").trim();
  }

  function withPageContext(event) {
    const payload = event && event.payload && typeof event.payload === "object" && !Array.isArray(event.payload)
      ? { ...event.payload }
      : {};
    payload.pageContext = readPageContext();
    return { ...event, payload };
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

  function sendEvent(event) {
    chrome.runtime.sendMessage({ type: MESSAGE_TYPE, event: withPageContext(event) }, () => {
      if (chrome.runtime.lastError) {
        // Service worker may be asleep or unavailable during extension reloads. Drop rather than touch page state.
      }
    });
  }
})();
