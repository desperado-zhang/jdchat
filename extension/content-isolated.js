(() => {
  const MAIN_SOURCE = "jdchat-capture-main";
  const MESSAGE_TYPE = "jdchat-capture-event";
  const CONTEXT_TYPE = "jdchat-capture-context";
  const RECEPTION_COMMAND_TYPE = "jdchat-reception-collector-command";
  const RECEPTION_PROGRESS_MESSAGE_TYPE = "jdchat-reception-capture-progress";
  const RECEPTION_DAILY_STATE_KEY = "receptionDailyCaptureState";
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
    receptionMaxPages: 500,
    receptionMaxConversations: 10000,
    receptionMaxRuntimeMinutes: 120,
    receptionDailyFullCapture: true,
    receptionAutoRefresh: true,
    receptionRefreshIntervalMinutes: 3,
    receptionIncrementalPages: 5,
    receptionStableTailRounds: 2,
  };
  const DEFAULT_RECEPTION_RUN_OPTIONS = {
    mode: "manual",
    maxPages: 500,
    maxConversations: 10000,
    maxRuntimeMs: 120 * 60 * 1000,
    detailWaitMs: 8000,
    minActionDelayMs: 800,
    maxActionDelayMs: 1500,
    pageWaitMs: 1800,
    maxFailures: 20,
    resetToFirstPage: false,
    refreshCurrentQuery: false,
    autoDetectTotal: false,
    incrementalPages: 5,
    stableTailRounds: 2,
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
  let receptionCollectorJob = null;
  let receptionNetworkSeenCount = 0;
  let lastReceptionChatLogAt = 0;
  let receptionRefreshTimer = null;
  let receptionRefreshNextAt = "";
  let receptionRefreshRunCount = 0;
  let receptionRefreshPaused = false;
  let receptionCollectorStatus = initialReceptionCollectorStatus();

  bootstrap().catch(() => startWithConfig(DEFAULT_CONFIG));

  async function bootstrap() {
    const config = await readConfig();
    startWithConfig(config);
  }

  function startWithConfig(config) {
    activeConfig = { ...DEFAULT_CONFIG, ...(config || {}) };
    const receptionEnabled = isReceptionPage() && activeConfig.captureReceptionChatLog !== false;
    const legacyRealtimeEnabled = isDongdongPage() && activeConfig.captureLegacyRealtime === true;
    injectMainScript(activeConfig, { receptionEnabled, legacyRealtimeEnabled });
    window.addEventListener("message", onMainWorldMessage, false);
    chrome.runtime.onMessage.addListener(onRuntimeMessage);
    chrome.storage.onChanged.addListener(onStorageChanged);
    if (legacyRealtimeEnabled && activeConfig.captureDom !== false) setTimeout(startRootWatcher, 1000);
    if (isReceptionPage()) syncReceptionRefreshSchedule({ immediate: true });
    window.addEventListener("beforeunload", () => {
      if (receptionCollectorJob) receptionCollectorJob.stopRequested = true;
      clearReceptionRefreshTimer();
      if (rootWatcher) clearInterval(rootWatcher);
      if (observer) observer.disconnect();
    });
  }

  async function readConfig() {
    const { config } = await chrome.storage.local.get(["config"]);
    return { ...DEFAULT_CONFIG, ...(config || {}) };
  }

  function onStorageChanged(changes, areaName) {
    if (areaName !== "local" || !changes.config) return;
    activeConfig = { ...DEFAULT_CONFIG, ...(changes.config.newValue || {}) };
    receptionRefreshPaused = false;
    syncReceptionRefreshSchedule({ immediate: true });
  }

  function injectMainScript(config, pageMode) {
    const script = document.createElement("script");
    script.src = chrome.runtime.getURL("injected-main.js");
    script.dataset.jdchatCapture = "main";
    script.dataset.captureReceptionChatLog = pageMode.receptionEnabled ? "1" : "0";
    script.dataset.legacyRealtimeCapture = pageMode.legacyRealtimeEnabled ? "1" : "0";
    script.dataset.captureSession = pageMode.legacyRealtimeEnabled && config.captureSession !== false ? "1" : "0";
    script.dataset.captureNetwork = pageMode.legacyRealtimeEnabled && config.captureNetwork === true ? "1" : "0";
    script.dataset.captureFetch = config.captureFetch === false ? "0" : "1";
    script.dataset.captureXhr = config.captureXhr === false ? "0" : "1";
    script.dataset.captureWebSocket = pageMode.legacyRealtimeEnabled && config.captureWebSocket === true ? "1" : "0";
    script.onload = () => script.remove();
    (document.documentElement || document.head).appendChild(script);
  }

  function isReceptionPage() {
    return location.hostname === "shop.jd.com" && location.pathname.includes("/jdm/kefu/kf-manage-lite");
  }

  function hasReceptionChatlogSurface() {
    if (!isReceptionPage()) return false;
    return Boolean(findReceptionRows().length || findReceptionSearchButton());
  }

  function isDongdongPage() {
    return location.hostname === "dongdong.jd.com";
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
    if (data.event.source === "reception_chatlog") {
      receptionNetworkSeenCount += 1;
      lastReceptionChatLogAt = Date.now();
    }
    rememberPageConversation(data.event);
    sendEvent(data.event);
  }

  function onRuntimeMessage(message, _sender, sendResponse) {
    if (!message || message.type !== RECEPTION_COMMAND_TYPE) return false;
    handleReceptionCommand(message)
      .then((response) => sendResponse(response))
      .catch((error) => sendResponse({ ok: false, error: String(error && error.message ? error.message : error) }));
    return true;
  }

  async function handleReceptionCommand(message) {
    if (message.command === "status") {
      if (!isReceptionPage()) {
        return { ok: false, error: "当前标签页不是京麦接待工具页面" };
      }
      return { ok: true, status: publicReceptionCollectorStatus() };
    }
    if (message.command === "stop") {
      if (!isReceptionPage()) {
        return { ok: false, error: "当前标签页不是京麦接待工具页面" };
      }
      receptionRefreshPaused = true;
      clearReceptionRefreshTimer();
      if (receptionCollectorJob) {
        receptionCollectorJob.stopRequested = true;
        updateReceptionCollectorStatus({ phase: "stopping", lastAction: "等待当前会话处理结束" });
      } else {
        updateReceptionCollectorStatus({ lastAction: "已停止自动刷新" });
      }
      return { ok: true, status: publicReceptionCollectorStatus() };
    }
    if (message.command === "start") {
      receptionRefreshPaused = false;
      return startReceptionCollector(message.options || {});
    }
    return { ok: false, error: "unknown reception collector command" };
  }

  function startReceptionCollector(options) {
    if (!isReceptionPage()) {
      updateReceptionCollectorStatus({
        phase: "failed",
        lastError: "当前标签页不是京麦接待工具页面",
      });
      return { ok: false, status: publicReceptionCollectorStatus() };
    }
    if (activeConfig.enabled === false) {
      updateReceptionCollectorStatus({
        phase: "failed",
        lastError: "总开关未启用",
      });
      return { ok: false, status: publicReceptionCollectorStatus() };
    }
    if (activeConfig.captureReceptionChatLog === false) {
      updateReceptionCollectorStatus({
        phase: "failed",
        lastError: "京麦聊天记录采集开关未启用",
      });
      return { ok: false, status: publicReceptionCollectorStatus() };
    }
    if (activeConfig.captureFetch === false && activeConfig.captureXhr === false) {
      updateReceptionCollectorStatus({
        phase: "failed",
        lastError: "fetch 与 XHR 监听至少需要开启一项",
      });
      return { ok: false, status: publicReceptionCollectorStatus() };
    }
    if (receptionCollectorJob && !receptionCollectorJob.done) {
      return { ok: true, status: publicReceptionCollectorStatus() };
    }

    const runOptions = normalizeReceptionRunOptions(options);
    const autoRefreshRun = options.autoRefreshRun === true;
    if (autoRefreshRun) receptionRefreshRunCount += 1;
    const captureDate = options.captureDate || localDateKey();
    const job = {
      id: `${Date.now()}-${Math.random().toString(16).slice(2)}`,
      jobKey: `reception_chatlog:${captureDate}`,
      mode: runOptions.mode,
      captureDate,
      options: runOptions,
      autoRefreshRun,
      stopRequested: false,
      done: false,
      startedAtMs: Date.now(),
      seenRowKeys: new Set(),
      totalCount: 0,
      totalPages: 0,
      stableRounds: 0,
    };
    receptionCollectorJob = job;
    receptionCollectorStatus = {
      phase: "ready",
      label: receptionCollectorPhaseLabel("ready"),
      mode: job.mode,
      captureDate,
      currentPage: readReceptionCurrentPage() || 1,
      maxPages: runOptions.maxPages,
      openedRows: 0,
      maxConversations: runOptions.maxConversations,
      capturedDetails: 0,
      failures: 0,
      maxFailures: runOptions.maxFailures,
      totalCount: 0,
      totalPages: 0,
      stableRounds: 0,
      autoRefreshRunCount: receptionRefreshRunCount,
      lastAction: autoRefreshRun ? "自动刷新当前查询结果" : "准备采集当前查询结果",
      lastError: "",
      startedAt: new Date().toISOString(),
      updatedAt: new Date().toISOString(),
    };
    reportReceptionCaptureProgress(job, "running");

    runReceptionCollector(job).catch((error) => {
      finishReceptionCollector(job, "failed", String(error && error.message ? error.message : error));
    });
    return { ok: true, status: publicReceptionCollectorStatus() };
  }

  async function runReceptionCollector(job) {
    updateReceptionCollectorStatus({ phase: "collecting_list", lastAction: "读取当前页可见会话" });
    if (job.options.refreshCurrentQuery) {
      updateReceptionCollectorStatus({ phase: "collecting_list", lastAction: "按当前筛选条件重新查询" });
      await clickReceptionSearchButton(job);
    }
    if (job.options.resetToFirstPage) {
      updateReceptionCollectorStatus({ phase: "collecting_list", lastAction: "返回第一页后刷新当前查询" });
      await goToReceptionFirstPage(job);
    }
    await waitForReceptionRows(job);
    prepareReceptionRunBounds(job);

    for (let pageOffset = 0; pageOffset < job.options.maxPages; pageOffset += 1) {
      if (shouldStopReceptionCollector(job)) break;
      const currentPage = readReceptionCurrentPage() || pageOffset + 1;
      updateReceptionCollectorStatus({ currentPage, phase: "collecting_list" });
      await waitForReceptionRows(job);

      let rowIndex = 0;
      while (!shouldStopReceptionCollector(job)) {
        if (receptionCollectorStatus.openedRows >= job.options.maxConversations) break;
        if (receptionCollectorStatus.failures >= job.options.maxFailures) break;

        const rows = findReceptionRows();
        if (rowIndex >= rows.length) break;
        const row = rows[rowIndex];
        rowIndex += 1;

        const rowKey = `${currentPage}:${rowIndex}:${hashString(receptionRowText(row))}`;
        if (job.seenRowKeys.has(rowKey)) continue;
        job.seenRowKeys.add(rowKey);
        await collectReceptionRow(row, job, rowIndex);
      }
      reportReceptionCaptureProgress(job, "running");

      if (receptionCollectorStatus.openedRows >= job.options.maxConversations) break;
      if (receptionCollectorStatus.failures >= job.options.maxFailures) break;
      if (pageOffset + 1 >= job.options.maxPages) break;

      updateReceptionCollectorStatus({ phase: "next_page", lastAction: "尝试进入下一页" });
      const moved = await clickNextReceptionPage(job);
      if (!moved) break;
    }

    if (receptionCollectorStatus.failures >= job.options.maxFailures) {
      finishReceptionCollector(job, "failed", "连续失败次数达到上限");
    } else if (job.stopRequested) {
      finishReceptionCollector(job, "stopped");
    } else {
      finishReceptionCollector(job, "finished");
    }
  }

  async function collectReceptionRow(row, job, rowIndex) {
    const viewButton = findReceptionViewButton(row);
    if (!viewButton) {
      incrementReceptionFailure("当前行未找到查看按钮");
      return;
    }

    viewButton.scrollIntoView({ block: "center", inline: "nearest" });
    await delay(randomInt(job.options.minActionDelayMs, job.options.maxActionDelayMs));
    if (shouldStopReceptionCollector(job)) return;

    const beforeNetworkCount = receptionNetworkSeenCount;
    const clickStartedAt = Date.now();
    updateReceptionCollectorStatus({
      phase: "opening_detail",
      lastAction: `打开第 ${rowIndex} 行查看`,
    });
    safeClick(viewButton);
    updateReceptionCollectorStatus({
      openedRows: receptionCollectorStatus.openedRows + 1,
    });

    const captured = await waitForReceptionChatLog(beforeNetworkCount, clickStartedAt, job);
    if (captured) {
      updateReceptionCollectorStatus({
        phase: "collecting_detail",
        capturedDetails: receptionCollectorStatus.capturedDetails + 1,
        lastAction: "已捕获聊天明细接口",
        lastError: "",
      });
    } else if (findReceptionDrawer()) {
      updateReceptionCollectorStatus({
        phase: "collecting_detail",
        lastAction: "查看抽屉已打开，本轮未触发新的明细接口",
        lastError: "",
      });
    } else {
      incrementReceptionFailure("等待 queryChatLog 明细接口超时");
    }

    updateReceptionCollectorStatus({ phase: "closing_detail", lastAction: "关闭聊天明细抽屉" });
    const closed = await closeReceptionDrawer();
    if (!closed && findReceptionDrawer()) incrementReceptionFailure("聊天明细抽屉关闭超时");
    await delay(randomInt(300, 700));
  }

  async function waitForReceptionRows(job) {
    const deadline = Date.now() + 10000;
    while (Date.now() < deadline) {
      if (shouldStopReceptionCollector(job)) return;
      if (findReceptionRows().length > 0) return;
      await delay(250);
    }
    throw new Error("当前页面没有可采集的查看按钮，请先进入聊天记录列表并查询");
  }

  function prepareReceptionRunBounds(job) {
    const pageInfo = readReceptionPaginationInfo();
    job.totalCount = pageInfo.totalCount || 0;
    job.totalPages = pageInfo.totalPages || 0;

    if (job.options.autoDetectTotal && pageInfo.totalPages) {
      job.options.maxPages = Math.min(pageInfo.totalPages, job.options.maxPages);
    }
    if (job.options.autoDetectTotal && pageInfo.totalCount) {
      job.options.maxConversations = Math.min(pageInfo.totalCount, job.options.maxConversations);
    }
    updateReceptionCollectorStatus({
      totalCount: job.totalCount,
      totalPages: job.totalPages,
      maxPages: job.options.maxPages,
      maxConversations: job.options.maxConversations,
      lastAction:
        job.options.mode === "backfill_today"
          ? `按当天列表全量采集 ${job.totalCount || "未知"} 条`
          : receptionCollectorStatus.lastAction,
    });
    reportReceptionCaptureProgress(job, "running");
  }

  async function waitForReceptionChatLog(beforeNetworkCount, clickStartedAt, job) {
    const deadline = Date.now() + job.options.detailWaitMs;
    while (Date.now() < deadline) {
      if (shouldStopReceptionCollector(job)) return false;
      if (receptionNetworkSeenCount > beforeNetworkCount) return true;
      if (lastReceptionChatLogAt >= clickStartedAt - 500) return true;
      await delay(250);
    }
    return false;
  }

  async function clickReceptionSearchButton(job) {
    const searchButton = findReceptionSearchButton();
    if (!searchButton) {
      updateReceptionCollectorStatus({ lastAction: "未找到查询按钮，沿用当前列表" });
      return;
    }
    const beforeKey = firstReceptionRowKey();
    safeClick(searchButton);
    await delay(job.options.pageWaitMs);
    await waitForReceptionPageChange(beforeKey, job, 5000);
  }

  async function goToReceptionFirstPage(job) {
    const currentPage = readReceptionCurrentPage();
    if (!currentPage || currentPage <= 1) return;

    for (let attempt = 0; attempt < 10; attempt += 1) {
      if (shouldStopReceptionCollector(job)) return;
      const page = readReceptionCurrentPage();
      if (page && page <= 1) return;

      const target = findReceptionPageNumberButton(1) || findPrevReceptionPageButton();
      if (!target) {
        updateReceptionCollectorStatus({ lastAction: "未找到第一页按钮，从当前页开始" });
        return;
      }

      const beforeKey = firstReceptionRowKey();
      safeClick(target);
      await delay(job.options.pageWaitMs);
      await waitForReceptionPageChange(beforeKey, job, 5000);
    }
  }

  async function clickNextReceptionPage(job) {
    const nextButton = findNextReceptionPageButton();
    if (!nextButton) {
      updateReceptionCollectorStatus({ lastAction: "未找到可用下一页，采集当前页后结束" });
      return false;
    }

    const beforeKey = firstReceptionRowKey();
    safeClick(nextButton);
    await delay(job.options.pageWaitMs);
    await waitForReceptionPageChange(beforeKey, job, 5000);
    return true;
  }

  async function waitForReceptionPageChange(beforeKey, job, timeoutMs) {
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
      if (shouldStopReceptionCollector(job)) return;
      const nextKey = firstReceptionRowKey();
      if (nextKey && nextKey !== beforeKey) return;
      await delay(250);
    }
  }

  async function closeReceptionDrawer() {
    const drawer = findReceptionDrawer();
    if (!drawer) return true;

    const closeButton = findReceptionDrawerCloseButton(drawer);
    if (closeButton) {
      safeClick(closeButton);
    } else {
      dispatchEscapeToCloseDrawer();
    }

    const deadline = Date.now() + 8000;
    while (Date.now() < deadline) {
      if (!findReceptionDrawer()) return true;
      dispatchEscapeToCloseDrawer();
      await delay(150);
    }
    return !findReceptionDrawer();
  }

  function findReceptionRows() {
    return [
      ...document.querySelectorAll(
        ".kf-manage-lite-table-tbody tr, .kf-manage-lite-table-row, tbody tr",
      ),
    ].filter((row) => isVisible(row) && findReceptionViewButton(row));
  }

  function findReceptionViewButton(row) {
    const candidates = [
      ...row.querySelectorAll("button, a, span[role='button'], .kf-manage-lite-btn, .action"),
    ];
    return candidates.find((node) => {
      if (!isVisible(node) || isDisabledControl(node)) return false;
      return elementLabel(node) === "查看";
    });
  }

  function findReceptionDrawer() {
    const drawers = [
      ...document.querySelectorAll(".chat-log-drawer, .kf-manage-lite-drawer-open, .kf-manage-lite-drawer-right"),
    ];
    return drawers.find((drawer) => isOpenReceptionDrawer(drawer)) || null;
  }

  function isOpenReceptionDrawer(drawer) {
    if (!isVisible(drawer)) return false;
    const wrapper = drawer.querySelector(".kf-manage-lite-drawer-content-wrapper");
    if (!wrapper) return true;

    const classText = String(wrapper.className || "");
    const style = window.getComputedStyle(wrapper);
    const rect = wrapper.getBoundingClientRect();
    if (classText.includes("drawer-content-wrapper-hidden")) return false;
    if (classText.includes("drawer-panel-motion-right-leave")) return false;
    return style.display !== "none" && style.visibility !== "hidden" && rect.width > 0 && rect.height > 0;
  }

  function findReceptionDrawerCloseButton(drawer) {
    const selectors = [
      ".kf-manage-lite-drawer-close",
      ".ant-drawer-close",
      "button[aria-label='Close']",
      "button[aria-label*='关闭']",
      "button[title*='关闭']",
    ];
    for (const root of [drawer, document]) {
      for (const selector of selectors) {
        const node = root.querySelector(selector);
        if (node && isVisible(node) && !isDisabledControl(node)) return node;
      }
    }
    return null;
  }

  function dispatchEscapeToCloseDrawer() {
    const eventInit = { key: "Escape", code: "Escape", bubbles: true, cancelable: true };
    for (const target of [document.activeElement, document.body, document, window]) {
      if (!target || typeof target.dispatchEvent !== "function") continue;
      target.dispatchEvent(new KeyboardEvent("keydown", eventInit));
      target.dispatchEvent(new KeyboardEvent("keyup", eventInit));
    }
  }

  function findNextReceptionPageButton() {
    const candidates = [
      ...document.querySelectorAll(
        ".kf-manage-lite-pagination-next, .ant-pagination-next, [title='下一页'], [aria-label='Next Page']",
      ),
    ];
    return candidates.find((node) => isVisible(node) && !isDisabledControl(node)) || null;
  }

  function findReceptionSearchButton() {
    const candidates = [
      ...document.querySelectorAll("button, a, span[role='button'], .kf-manage-lite-btn, .action"),
    ];
    return candidates.find((node) => {
      if (!isVisible(node) || isDisabledControl(node)) return false;
      return elementLabel(node) === "查询";
    }) || null;
  }

  function findPrevReceptionPageButton() {
    const candidates = [
      ...document.querySelectorAll(
        ".kf-manage-lite-pagination-prev, .ant-pagination-prev, [title='上一页'], [aria-label='Previous Page']",
      ),
    ];
    return candidates.find((node) => isVisible(node) && !isDisabledControl(node)) || null;
  }

  function findReceptionPageNumberButton(pageNumber) {
    const expected = String(pageNumber);
    const candidates = [
      ...document.querySelectorAll(
        ".kf-manage-lite-pagination-item, .ant-pagination-item, .kf-manage-lite-pagination-item-link, .ant-pagination-item-link",
      ),
    ];
    return candidates.find((node) => {
      if (!isVisible(node) || isDisabledControl(node)) return false;
      return elementLabel(node) === expected || node.getAttribute("title") === expected;
    }) || null;
  }

  function readReceptionCurrentPage() {
    const active = document.querySelector(
      ".kf-manage-lite-pagination-item-active, .ant-pagination-item-active",
    );
    const parsed = Number.parseInt(text(active), 10);
    return Number.isFinite(parsed) ? parsed : 0;
  }

  function readReceptionPaginationInfo() {
    const bodyText = text(document.body);
    const totalMatch = bodyText.match(/共\s*([\d,]+)\s*条/);
    const totalCount = totalMatch ? Number.parseInt(totalMatch[1].replace(/,/g, ""), 10) : 0;
    const pageNumbers = [
      ...document.querySelectorAll(
        ".kf-manage-lite-pagination-item, .ant-pagination-item, .kf-manage-lite-pagination-item-link, .ant-pagination-item-link",
      ),
    ]
      .map((node) => Number.parseInt(elementLabel(node) || node.getAttribute("title") || "", 10))
      .filter((value) => Number.isFinite(value) && value > 0);
    const rowCount = findReceptionRows().length;
    const visibleMaxPage = pageNumbers.length ? Math.max(...pageNumbers) : 0;
    const inferredTotalPages = totalCount && rowCount ? Math.ceil(totalCount / rowCount) : 0;
    const totalPages = Math.max(visibleMaxPage, inferredTotalPages);
    return {
      totalCount: Number.isFinite(totalCount) ? totalCount : 0,
      totalPages: Number.isFinite(totalPages) ? totalPages : 0,
      rowCount,
    };
  }

  function firstReceptionRowKey() {
    const row = findReceptionRows()[0];
    return row ? hashString(receptionRowText(row)) : "";
  }

  function receptionRowText(row) {
    return text(row).replace(/发起新会话/g, "").trim();
  }

  function shouldStopReceptionCollector(job) {
    if (!job || job.stopRequested) return true;
    if (Date.now() - job.startedAtMs > job.options.maxRuntimeMs) {
      job.stopRequested = true;
      updateReceptionCollectorStatus({ lastAction: "达到最长运行时间" });
      return true;
    }
    return false;
  }

  function incrementReceptionFailure(message) {
    updateReceptionCollectorStatus({
      failures: receptionCollectorStatus.failures + 1,
      lastError: message,
      lastAction: message,
    });
  }

  function finishReceptionCollector(job, phase, error = "") {
    if (receptionCollectorJob !== job) return;
    job.done = true;
    const terminalStatus = phase === "finished" ? "finished" : phase === "failed" ? "failed" : "stopped";
    updateReceptionCollectorStatus({
      phase,
      lastAction: receptionCollectorPhaseLabel(phase),
      lastError: error || receptionCollectorStatus.lastError,
      finishedAt: new Date().toISOString(),
    });
    persistReceptionDailyState(job, terminalStatus).catch(() => undefined);
    reportReceptionCaptureProgress(job, terminalStatus);
    receptionCollectorJob = null;
    syncReceptionRefreshSchedule({
      immediate: job.autoRefreshRun && job.mode === "backfill_today" && phase === "finished",
    });
  }

  function updateReceptionCollectorStatus(patch) {
    receptionCollectorStatus = {
      ...receptionCollectorStatus,
      ...patch,
      updatedAt: new Date().toISOString(),
    };
    receptionCollectorStatus.label = receptionCollectorPhaseLabel(receptionCollectorStatus.phase);
  }

  function publicReceptionCollectorStatus() {
    return {
      ...receptionCollectorStatus,
      chatLogDrawerVisible: Boolean(findReceptionDrawer()),
      chatLogTableRowCount: findReceptionRows().length,
      lastReceptionChatLogAt,
      autoRefreshConfigured: activeConfig.receptionAutoRefresh === true,
      autoRefreshEnabled: receptionAutoRefreshEnabled(),
      autoRefreshPaused: receptionRefreshPaused,
      autoRefreshIntervalMinutes: receptionRefreshIntervalMinutes(),
      nextAutoRefreshAt: receptionRefreshNextAt,
      autoRefreshRunCount: receptionRefreshRunCount,
    };
  }

  async function readReceptionDailyState() {
    const stored = await chrome.storage.local.get([RECEPTION_DAILY_STATE_KEY]);
    const state = stored[RECEPTION_DAILY_STATE_KEY];
    return state && typeof state === "object" ? state : {};
  }

  async function persistReceptionDailyState(job, terminalStatus) {
    if (!job || !job.captureDate) return;
    const current = await readReceptionDailyState();
    const previousTotal = Number.parseInt(current.totalCount, 10) || 0;
    let scheduleImmediateBackfill = false;
    const next = {
      ...current,
      captureDate: job.captureDate,
      jobKey: job.jobKey,
      lastMode: job.mode,
      lastStatus: terminalStatus,
      totalCount: job.totalCount || receptionCollectorStatus.totalCount || current.totalCount || 0,
      totalPages: job.totalPages || receptionCollectorStatus.totalPages || current.totalPages || 0,
      lastOpenedRows: receptionCollectorStatus.openedRows || 0,
      lastCapturedDetails: receptionCollectorStatus.capturedDetails || 0,
      lastRunAt: new Date().toISOString(),
    };

    if (job.mode === "backfill_today" && terminalStatus === "finished") {
      next.fullStatus = "finished";
      next.fullCompletedAt = new Date().toISOString();
      next.stableRounds = Math.max(Number.parseInt(current.stableRounds, 10) || 0, 1);
    } else if (job.mode === "incremental" && terminalStatus === "finished") {
      const nextTotal = Number.parseInt(next.totalCount, 10) || 0;
      const coveredRows = receptionCollectorStatus.openedRows || 0;
      if (previousTotal && nextTotal > previousTotal && nextTotal - previousTotal > coveredRows) {
        next.fullStatus = "needs_backfill";
        next.stableRounds = 0;
        scheduleImmediateBackfill = true;
      } else {
        next.fullStatus = current.fullStatus || "finished";
        next.stableRounds = (Number.parseInt(current.stableRounds, 10) || 0) + 1;
      }
    } else if (terminalStatus === "failed") {
      next.fullStatus = current.fullStatus || "needs_backfill";
    }

    await chrome.storage.local.set({ [RECEPTION_DAILY_STATE_KEY]: next });
    if (scheduleImmediateBackfill) syncReceptionRefreshSchedule({ immediate: true });
  }

  function reportReceptionCaptureProgress(job, status) {
    if (!job || !job.options || activeConfig.captureReceptionChatLog === false) return;
    const phase = receptionCollectorStatus.phase || "idle";
    const payload = {
      jobKey: job.jobKey,
      captureDate: job.captureDate,
      mode: job.mode,
      status,
      totalCount: job.totalCount || receptionCollectorStatus.totalCount || 0,
      totalPages: job.totalPages || receptionCollectorStatus.totalPages || 0,
      currentPage: receptionCollectorStatus.currentPage || 0,
      openedRows: receptionCollectorStatus.openedRows || 0,
      capturedDetails: receptionCollectorStatus.capturedDetails || 0,
      stableRounds: receptionCollectorStatus.stableRounds || job.stableRounds || 0,
      failureCount: receptionCollectorStatus.failures || 0,
      lastError: receptionCollectorStatus.lastError || "",
      lastAction: receptionCollectorStatus.lastAction || "",
      startedAt: receptionCollectorStatus.startedAt || new Date(job.startedAtMs).toISOString(),
      finishedAt: receptionCollectorStatus.finishedAt || undefined,
      statusPayload: {
        phase,
        label: receptionCollectorStatus.label || receptionCollectorPhaseLabel(phase),
        maxPages: job.options.maxPages,
        maxConversations: job.options.maxConversations,
        autoRefreshRun: job.autoRefreshRun,
        runCount: receptionRefreshRunCount,
      },
    };
    chrome.runtime.sendMessage({ type: RECEPTION_PROGRESS_MESSAGE_TYPE, progress: payload }).catch(() => undefined);
  }

  function initialReceptionCollectorStatus() {
    return {
      phase: "idle",
      label: receptionCollectorPhaseLabel("idle"),
      mode: "",
      captureDate: "",
      currentPage: 0,
      maxPages: DEFAULT_RECEPTION_RUN_OPTIONS.maxPages,
      openedRows: 0,
      maxConversations: DEFAULT_RECEPTION_RUN_OPTIONS.maxConversations,
      capturedDetails: 0,
      failures: 0,
      maxFailures: DEFAULT_RECEPTION_RUN_OPTIONS.maxFailures,
      totalCount: 0,
      totalPages: 0,
      stableRounds: 0,
      lastAction: "",
      lastError: "",
      startedAt: "",
      finishedAt: "",
      autoRefreshRunCount: 0,
      updatedAt: new Date().toISOString(),
    };
  }

  function receptionCollectorPhaseLabel(phase) {
    const labels = {
      idle: "空闲",
      ready: "准备中",
      collecting_list: "读取列表",
      opening_detail: "打开查看",
      collecting_detail: "等待明细",
      closing_detail: "关闭抽屉",
      next_page: "翻页",
      stopping: "停止中",
      stopped: "已停止",
      finished: "已完成",
      failed: "失败",
    };
    return labels[phase] || phase || "未知";
  }

  function normalizeReceptionRunOptions(options) {
    return {
      ...DEFAULT_RECEPTION_RUN_OPTIONS,
      mode: normalizeReceptionRunMode(options.mode),
      maxPages: clampPositiveInt(options.maxPages, DEFAULT_RECEPTION_RUN_OPTIONS.maxPages, 1, 500),
      maxConversations: clampPositiveInt(
        options.maxConversations,
        DEFAULT_RECEPTION_RUN_OPTIONS.maxConversations,
        1,
        10000,
      ),
      maxRuntimeMs: clampPositiveInt(
        options.maxRuntimeMs,
        DEFAULT_RECEPTION_RUN_OPTIONS.maxRuntimeMs,
        60_000,
        14_400_000,
      ),
      resetToFirstPage: options.resetToFirstPage === true,
      refreshCurrentQuery: options.refreshCurrentQuery === true,
      autoDetectTotal: options.autoDetectTotal === true,
      incrementalPages: clampPositiveInt(
        options.incrementalPages,
        DEFAULT_RECEPTION_RUN_OPTIONS.incrementalPages,
        1,
        100,
      ),
      stableTailRounds: clampPositiveInt(
        options.stableTailRounds,
        DEFAULT_RECEPTION_RUN_OPTIONS.stableTailRounds,
        1,
        10,
      ),
    };
  }

  function normalizeReceptionRunMode(mode) {
    return ["manual", "backfill_today", "incremental", "tail_check"].includes(mode) ? mode : "manual";
  }

  function syncReceptionRefreshSchedule(options = {}) {
    if (!isReceptionPage()) {
      clearReceptionRefreshTimer();
      return;
    }
    if (!receptionAutoRefreshEnabled()) {
      clearReceptionRefreshTimer();
      return;
    }
    if (receptionCollectorJob && !receptionCollectorJob.done) return;
    scheduleNextReceptionRefresh(options.immediate ? 3000 : receptionRefreshIntervalMs());
  }

  function scheduleNextReceptionRefresh(delayMs) {
    clearReceptionRefreshTimer();
    receptionRefreshNextAt = new Date(Date.now() + delayMs).toISOString();
    updateReceptionCollectorStatus({ lastAction: receptionCollectorStatus.lastAction });
    receptionRefreshTimer = setTimeout(() => {
      receptionRefreshTimer = null;
      receptionRefreshNextAt = "";
      runReceptionRefreshCycle().catch(() => {
        scheduleNextReceptionRefresh(receptionRefreshIntervalMs());
      });
    }, delayMs);
  }

  function clearReceptionRefreshTimer() {
    if (receptionRefreshTimer) clearTimeout(receptionRefreshTimer);
    receptionRefreshTimer = null;
    receptionRefreshNextAt = "";
  }

  async function runReceptionRefreshCycle() {
    if (!receptionAutoRefreshEnabled()) return;
    if (receptionCollectorJob && !receptionCollectorJob.done) {
      scheduleNextReceptionRefresh(receptionRefreshIntervalMs());
      return;
    }
    if (!hasReceptionChatlogSurface()) {
      updateReceptionCollectorStatus({ lastAction: "等待进入聊天记录列表" });
      scheduleNextReceptionRefresh(10_000);
      return;
    }

    const options = await nextReceptionAutoRunOptions();
    startReceptionCollector(options);
  }

  async function nextReceptionAutoRunOptions() {
    const captureDate = localDateKey();
    const dailyState = await readReceptionDailyState();
    const needsBackfill =
      activeConfig.receptionDailyFullCapture !== false &&
      (dailyState.captureDate !== captureDate || dailyState.fullStatus !== "finished");
    const mode = needsBackfill ? "backfill_today" : "incremental";
    return {
      mode,
      captureDate,
      maxPages:
        mode === "backfill_today"
          ? activeConfig.receptionMaxPages
          : activeConfig.receptionIncrementalPages,
      maxConversations: activeConfig.receptionMaxConversations,
      maxRuntimeMs:
        clampPositiveInt(activeConfig.receptionMaxRuntimeMinutes, DEFAULT_CONFIG.receptionMaxRuntimeMinutes, 1, 240) *
        60 *
        1000,
      autoRefreshRun: true,
      resetToFirstPage: true,
      refreshCurrentQuery: true,
      autoDetectTotal: mode === "backfill_today",
      incrementalPages: activeConfig.receptionIncrementalPages,
      stableTailRounds: activeConfig.receptionStableTailRounds,
    };
  }

  function receptionAutoRefreshEnabled() {
    return (
      isReceptionPage() &&
      !receptionRefreshPaused &&
      activeConfig.enabled !== false &&
      activeConfig.captureReceptionChatLog !== false &&
      activeConfig.receptionAutoRefresh === true
    );
  }

  function receptionRefreshIntervalMs() {
    return receptionRefreshIntervalMinutes() * 60 * 1000;
  }

  function receptionRefreshIntervalMinutes() {
    return clampPositiveInt(
      activeConfig.receptionRefreshIntervalMinutes,
      DEFAULT_CONFIG.receptionRefreshIntervalMinutes,
      1,
      240,
    );
  }

  function clampPositiveInt(value, fallback, min, max) {
    const parsed = Number.parseInt(value, 10);
    if (!Number.isFinite(parsed)) return fallback;
    return Math.min(Math.max(parsed, min), max);
  }

  function isVisible(node) {
    if (!(node instanceof Element)) return false;
    const rect = node.getBoundingClientRect();
    const style = window.getComputedStyle(node);
    return rect.width > 0 && rect.height > 0 && style.visibility !== "hidden" && style.display !== "none";
  }

  function isDisabledControl(node) {
    if (!node) return true;
    const classText = String(node.className || "");
    return (
      node.disabled === true ||
      node.getAttribute("aria-disabled") === "true" ||
      /\bdisabled\b|pagination-disabled/i.test(classText)
    );
  }

  function safeClick(node) {
    const target =
      node.matches("button, a, [role='button']") ? node : node.querySelector("button, a, [role='button']") || node;
    target.dispatchEvent(new MouseEvent("mousedown", { bubbles: true, cancelable: true, view: window }));
    target.dispatchEvent(new MouseEvent("mouseup", { bubbles: true, cancelable: true, view: window }));
    target.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, view: window }));
  }

  function randomInt(min, max) {
    const low = Math.min(min, max);
    const high = Math.max(min, max);
    return Math.floor(low + Math.random() * (high - low + 1));
  }

  function localDateKey(date = new Date()) {
    const year = date.getFullYear();
    const month = String(date.getMonth() + 1).padStart(2, "0");
    const day = String(date.getDate()).padStart(2, "0");
    return `${year}-${month}-${day}`;
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
      attributeFilter: [
        "src",
        "srcset",
        "data-src",
        "data-original",
        "data-origin",
        "data-url",
        "origin-src",
        "style",
        "class",
      ],
    });
  }

  function scanExistingMessages(root) {
    root.querySelectorAll(".message").forEach((node) => scanMessageNode(node));
    root.querySelectorAll(".message__image-wrap img, .message__image img").forEach((node) => scanImageNode(node));
  }

  function scanMessageNode(node, options = {}) {
    const messageNode =
      node.matches && node.matches(".message") ? node : node.querySelector && node.querySelector(".message");
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
    if (isReceptionPage()) return readReceptionPageContext();

    const selectedTab = document.querySelector(".c_tabs-tab_check");
    const selectedTabLabel = elementLabel(selectedTab);
    const activeSidebarTab = normalizeSidebarTab(selectedTabLabel);
    const historyListVisible =
      activeSidebarTab === "history" && !!document.querySelector(".list-compatible.recent-user-w");
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

  function readReceptionPageContext() {
    return {
      pageKind: "jingmai_reception_tools",
      capturedPageUrl: location.origin + location.pathname + location.hash,
      chatLogDrawerVisible: !!document.querySelector(".chat-log-drawer, .kf-manage-lite-drawer-open"),
      chatLogMessageNodeCount: document.querySelectorAll(".chat-log-detail .msg-block, .msg-block").length,
      chatLogTableRowCount: document.querySelectorAll("tbody tr, .kf-manage-lite-table-row").length,
      collectorPhase: receptionCollectorStatus.phase,
      collectorOpenedRows: receptionCollectorStatus.openedRows,
      collectorCapturedDetails: receptionCollectorStatus.capturedDetails,
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
