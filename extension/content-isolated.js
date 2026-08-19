(() => {
  const MAIN_SOURCE = "jdchat-capture-main";
  const MESSAGE_TYPE = "jdchat-capture-event";
  const CONTEXT_TYPE = "jdchat-capture-context";
  const DEFAULT_CONFIG = {
    captureDom: true,
    captureSession: true,
    captureNetwork: false,
    captureFetch: true,
    captureXhr: true,
    captureWebSocket: false,
    autoScrollHistory: true,
  };
  const CONTEXT_RETRY_DELAY_MS = 750;
  const MAX_CONTEXT_RETRY_ATTEMPTS = 8;
  const HISTORY_SCROLL_DELAY_MS = 900;
  const HISTORY_SCROLL_MAX_STEPS = 80;
  const HISTORY_SCROLL_STABLE_ROUNDS = 3;
  const processedDomNodes = new WeakSet();
  const processedDomImages = new WeakSet();
  const pendingDomRetries = new WeakMap();
  const completedHistoryBackfills = new Set();
  let activeConfig = DEFAULT_CONFIG;
  let historyBackfillJob = null;
  let observer = null;
  let observedRoot = null;
  let rootWatcher = null;
  let latestPageConversation = null;

  bootstrap().catch(() => startWithConfig(DEFAULT_CONFIG));

  async function bootstrap() {
    const config = await readConfig();
    startWithConfig(config);
  }

  function startWithConfig(config) {
    activeConfig = { ...DEFAULT_CONFIG, ...(config || {}) };
    injectMainScript(activeConfig);
    window.addEventListener("message", onMainWorldMessage, false);
    if (activeConfig.captureDom !== false) setTimeout(startRootWatcher, 1000);
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
    if (!data || data.source !== MAIN_SOURCE || !data.event) return;
    if (data.type === CONTEXT_TYPE) {
      rememberPageConversation(data.event);
      return;
    }
    if (data.type !== MESSAGE_TYPE) return;
    rememberPageConversation(data.event);
    sendEvent(data.event);
  }

  function startRootWatcher() {
    refreshChatRoot();
    rootWatcher = setInterval(refreshChatRoot, 1000);
  }

  function refreshChatRoot() {
    const root = findChatRoot();
    if (!root) return;
    if (root !== observedRoot) {
      observedRoot = root;
      attachDomObserver(root);
    }
    scanExistingMessages(root);
    maybeStartHistoryBackfill(root);
  }

  function findChatRoot() {
    return document.querySelector("#t-chat-scroll, .chat-scroll-wrap");
  }

  function attachDomObserver(root) {
    if (observer) observer.disconnect();
    observer = new MutationObserver((mutations) => {
      for (const mutation of mutations) {
        if (mutation.type === "attributes") {
          scanClosestMessage(mutation.target);
          continue;
        }
        for (const node of mutation.addedNodes) {
          if (node.nodeType !== Node.ELEMENT_NODE) continue;
          scanMessageNode(node);
        }
      }
      maybeStartHistoryBackfill(root);
    });
    observer.observe(root, {
      childList: true,
      subtree: true,
      attributes: true,
      attributeFilter: ["src", "srcset", "data-src", "data-original", "data-origin", "data-url", "origin-src", "style", "class"],
    });
  }

  function scanExistingMessages(root) {
    root.querySelectorAll(".message").forEach((node) => scanMessageNode(node));
    root.querySelectorAll(".message__image-wrap img, .message__image img").forEach((node) => scanImageNode(node));
  }

  function scanMessageNode(node, options = {}) {
    const messageNode = node.matches && node.matches(".message") ? node : node.querySelector && node.querySelector(".message");
    if (!messageNode || processedDomNodes.has(messageNode)) return;

    const parsed = parseDomMessage(messageNode);
    if (!parsed) return;
    if (!options.allowFallback && shouldWaitForConversationContext(parsed)) {
      scheduleDomRetry(messageNode);
      return;
    }

    pendingDomRetries.delete(messageNode);
    processedDomNodes.add(messageNode);
    sendEvent(parsed);
  }

  function scanImageNode(node) {
    if (!(node instanceof HTMLImageElement)) return;
    if (processedDomImages.has(node) || !isChatMessageImage(node)) return;

    const messageNode = node.closest(".message");
    if (!messageNode) return;

    const parsed = parseDomMessage(messageNode, { image: node });
    if (!parsed || parsed.message.body.type !== "image") return;
    if (shouldWaitForConversationContext(parsed)) return;

    processedDomImages.add(node);
    sendEvent(parsed);
  }

  function scanClosestMessage(node) {
    if (!(node instanceof HTMLElement)) return;
    const messageNode = node.matches(".message") ? node : node.closest(".message");
    if (messageNode) scanMessageNode(messageNode);
  }

  function parseDomMessage(node, options = {}) {
    const direction = inferDomDirection(node);
    const contentNode = node.querySelector(".message__content");
    const textWrapper = node.querySelector(".message__text");
    const image = options.image || findMessageImage(node);
    const pendingImage = !image && hasImageContainer(node);
    const card = node.querySelector(".CardWrapper.ProductCard, .CardWrapper");
    const timeNode = node.querySelector(".message__time_str");
    const nicknameNode = node.querySelector(".message__nickname");
    if (pendingImage) return null;
    const bodyType = image ? "image" : card ? "template2" : direction === "system" ? "system" : "text";
    const content = text(contentNode || textWrapper || card || node);
    const imageInfo = image ? readImageInfo(image) : {};
    const imageUrl = imageInfo.url || "";
    const displayTime = text(timeNode);
    const nickname = text(nicknameNode);
    const customerName = readActiveCustomerName();
    const domStableId = hashString(
      [
        customerName,
        direction,
        bodyType,
        content,
        imageUrl,
        displayTime,
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
      conversation: domConversation(customerName),
      message: {
        id: `dom-${domStableId}`,
        direction,
        type: "chat_message",
        body: {
          type: bodyType,
          content: content || undefined,
          url: imageUrl || undefined,
          width: imageInfo.width || undefined,
          height: imageInfo.height || undefined,
        },
        displayTime: displayTime || undefined,
        nickname: nickname || undefined,
      },
      capturedAt: new Date().toISOString(),
    };
  }

  function rememberPageConversation(event) {
    const conversation = event && event.conversation;
    if (!conversation || typeof conversation !== "object" || Array.isArray(conversation)) return;
    if (!hasStableConversationIdentity(conversation)) return;
    latestPageConversation = cloneConversation(conversation);
    if (observedRoot) setTimeout(() => scanExistingMessages(observedRoot), 0);
  }

  function maybeStartHistoryBackfill(root) {
    if (activeConfig.autoScrollHistory === false) return;
    if (historyBackfillJob) return;
    if (!root || !document.contains(root)) return;
    if (!root.querySelector(".message")) return;

    const key = activeCustomerCaptureKey();
    if (!key || completedHistoryBackfills.has(key)) return;

    const job = { key };
    historyBackfillJob = job;
    runHistoryBackfill(root, job)
      .catch(() => undefined)
      .finally(() => {
        if (historyBackfillJob === job) historyBackfillJob = null;
      });
  }

  async function runHistoryBackfill(root, job) {
    let stableRounds = 0;
    for (let step = 0; step < HISTORY_SCROLL_MAX_STEPS; step += 1) {
      if (historyBackfillJob !== job || !document.contains(root)) return;
      scanExistingMessages(root);

      const before = scrollMetrics(root);
      root.scrollTop = 0;
      root.dispatchEvent(new Event("scroll", { bubbles: true }));
      root.dispatchEvent(new WheelEvent("wheel", { deltaY: -800, bubbles: true, cancelable: true }));

      await delay(HISTORY_SCROLL_DELAY_MS);
      scanExistingMessages(root);

      const after = scrollMetrics(root);
      const unchanged =
        after.messageCount === before.messageCount &&
        after.scrollHeight === before.scrollHeight &&
        after.scrollTop <= 2;
      stableRounds = unchanged ? stableRounds + 1 : 0;
      if (stableRounds >= HISTORY_SCROLL_STABLE_ROUNDS) break;
    }

    scanExistingMessages(root);
    completedHistoryBackfills.add(job.key);
  }

  function activeCustomerCaptureKey() {
    const conversation = latestPageConversation || {};
    const customerPin =
      conversation.customerPin ||
      conversation.customer_pin ||
      conversation.pin ||
      readSelectedCustomerHint().pin;
    if (customerPin) return `pin:${customerPin}`;
    const customerName =
      conversation.customerName ||
      conversation.customer_name ||
      conversation.name ||
      readActiveCustomerName();
    return customerName ? `name:${customerName}` : "";
  }

  function scrollMetrics(root) {
    return {
      scrollTop: Math.round(root.scrollTop),
      scrollHeight: Math.round(root.scrollHeight),
      clientHeight: Math.round(root.clientHeight),
      messageCount: root.querySelectorAll(".message").length,
    };
  }

  function delay(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
  }

  function findMessageImage(node) {
    const candidates = [...node.querySelectorAll(".message__image-wrap img, .message__image img, img")];
    return candidates.find((image) => isChatMessageImage(image)) || null;
  }

  function hasImageContainer(node) {
    return Boolean(node.querySelector(".message__image, .message__image-wrap, .message_img_loading"));
  }

  function isChatMessageImage(image) {
    const url = imageSourceUrl(image);
    if (!url) return false;
    const classText = [
      image.className,
      image.parentElement && image.parentElement.className,
      image.parentElement && image.parentElement.parentElement && image.parentElement.parentElement.className,
    ]
      .filter(Boolean)
      .join(" ");

    if (/message__avatar|avatar|message_img_loading|loading|spinner|lds-spinner/i.test(classText)) return false;
    if (isLoadingSvgUrl(url)) return false;

    if (image.closest(".message__image-wrap")) return true;
    return Boolean(image.closest(".message__image")) && (image.naturalWidth > 48 || image.width > 48);
  }

  function readImageInfo(image) {
    const url = imageSourceUrl(image);
    return {
      url,
      width: image.naturalWidth || image.width || undefined,
      height: image.naturalHeight || image.height || undefined,
    };
  }

  function imageSourceUrl(image) {
    return firstNonEmpty(
      image.currentSrc,
      image.src,
      image.getAttribute("data-src"),
      image.getAttribute("data-original"),
      image.getAttribute("data-origin"),
      image.getAttribute("data-url"),
      image.getAttribute("origin-src"),
      image.getAttribute("src"),
      firstSrcsetUrl(image.getAttribute("srcset")),
      backgroundImageUrl(image),
      parentImageUrl(image),
    );
  }

  function isLoadingSvgUrl(url) {
    if (!String(url).startsWith("data:image/svg")) return false;
    if (url.includes("lds-spinner") || url.includes("bGRzLXNwaW5uZXI")) return true;
    const [, payload = ""] = String(url).split(",", 2);
    try {
      return atob(payload).includes("lds-spinner");
    } catch (_error) {
      return false;
    }
  }

  function firstNonEmpty(...values) {
    return values.find((value) => typeof value === "string" && value.trim()) || "";
  }

  function firstSrcsetUrl(srcset) {
    if (!srcset) return "";
    return String(srcset).split(",")[0].trim().split(/\s+/)[0] || "";
  }

  function backgroundImageUrl(node) {
    const value = window.getComputedStyle(node).backgroundImage || "";
    const match = value.match(/url\(["']?(.+?)["']?\)/);
    return match ? match[1] : "";
  }

  function parentImageUrl(node) {
    const link = node.closest("a[href]");
    const href = link ? link.getAttribute("href") || "" : "";
    if (/\.(png|jpe?g|gif|webp|bmp|svg)(\?|#|$)/i.test(href)) return href;
    return "";
  }

  function hasStableConversationIdentity(conversation) {
    return Boolean(
      (conversation.app && conversation.pin) ||
        (conversation.customerApp && conversation.customerPin) ||
        (conversation.customer_app && conversation.customer_pin),
    );
  }

  function cloneConversation(conversation) {
    const cloned = {};
    for (const [key, value] of Object.entries(conversation)) {
      if (value == null || value === "") continue;
      if (typeof value === "object") continue;
      cloned[key] = value;
    }
    return cloned;
  }

  function domConversation(customerName) {
    const selectedCustomer = readSelectedCustomerHint();
    if (latestPageConversation && hasStableConversationIdentity(latestPageConversation)) {
      return {
        ...latestPageConversation,
        customerName:
          latestPageConversation.customerName ||
          latestPageConversation.customer_name ||
          latestPageConversation.name ||
          selectedCustomer.name ||
          customerName ||
          undefined,
      };
    }
    const normalizedCustomerName = selectedCustomer.name || stripCustomerDecorations(customerName);
    if (selectedCustomer.pin) {
      return {
        customerName: normalizedCustomerName || undefined,
        customerApp: selectedCustomer.app || "im.customer",
        customerPin: selectedCustomer.pin,
        venderId: selectedCustomer.venderId || undefined,
        sessionType: selectedCustomer.sessionType || "4",
      };
    }
    return {
      customerName: normalizedCustomerName || undefined,
      customerApp: "dom",
      customerPin: normalizedCustomerName ? `dom:${hashString(normalizedCustomerName)}` : "dom:unknown",
      sessionType: "dom",
    };
  }

  function shouldWaitForConversationContext(event) {
    return isDomFallbackConversation(event.conversation);
  }

  function isDomFallbackConversation(conversation) {
    if (!conversation || typeof conversation !== "object") return true;
    const customerApp = conversation.customerApp || conversation.customer_app || conversation.app;
    const customerPin = conversation.customerPin || conversation.customer_pin || conversation.pin;
    return !customerApp || customerApp === "dom" || !customerPin || String(customerPin).startsWith("dom:");
  }

  function scheduleDomRetry(messageNode) {
    const attempts = pendingDomRetries.get(messageNode) || 0;
    const nextAttempts = attempts + 1;
    pendingDomRetries.set(messageNode, nextAttempts);
    setTimeout(() => {
      if (processedDomNodes.has(messageNode)) return;
      scanMessageNode(messageNode, {
        allowFallback: nextAttempts >= MAX_CONTEXT_RETRY_ATTEMPTS,
      });
    }, CONTEXT_RETRY_DELAY_MS);
  }

  function readActiveCustomerName() {
    const selectedCustomer = readSelectedCustomerHint();
    if (selectedCustomer.name) return selectedCustomer.name;
    return stripCustomerDecorations(
      text(document.querySelector(".chat-head-name")) || text(document.querySelector(".chat-head-title")),
    );
  }

  function readSelectedCustomerHint() {
    const selected =
      document.querySelector(".alluser-item_check, .t-item-ck") ||
      document.querySelector(".alluser-item[aria-selected='true']");
    if (!selected) return {};

    const nameNode = selected.querySelector(".alluser-item-name") || selected.querySelector("[title]");
    const rawName = text(nameNode) || text(selected).replace(/\s+\d{1,2}:\d{2}.*$/, "");
    const parsedId = parseCustomerElementId(selected.id || selected.closest("[id]")?.id || "");
    const rawPin = (nameNode ? nameNode.getAttribute("title") || "" : "") || parsedId.pin || "";
    return {
      name: stripCustomerDecorations(rawName),
      pin: rawPin || undefined,
      app: parsedId.app || undefined,
      venderId: parsedId.venderId || undefined,
      sessionType: parsedId.sessionType || undefined,
    };
  }

  function parseCustomerElementId(id) {
    const match = String(id || "").match(/^u_(.+?)(imcustomer|imwaiter)([^:]+)$/i);
    if (!match) return {};
    return {
      pin: match[1],
      app: match[2].toLowerCase() === "imcustomer" ? "im.customer" : "im.waiter",
      venderId: match[3],
      sessionType: "4",
    };
  }

  function stripCustomerDecorations(value) {
    return String(value || "")
      .replace(/\s*\[[^\]]+\].*$/, "")
      .replace(/\s+转接客户.*$/, "")
      .replace(/\s+\d{1,2}:\d{2}.*$/, "")
      .replace(/\s+/g, " ")
      .trim();
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
      historyBackfillActive: Boolean(historyBackfillJob),
      historyBackfillCompletedCount: completedHistoryBackfills.size,
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
