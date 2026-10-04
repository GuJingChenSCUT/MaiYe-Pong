"use strict";

(() => {
  const $ = id => document.getElementById(id);
  const state = { identity: null, csrf: null, config: {}, task: null, tasks: [], busy: false, poll: null, questionKey: null, editing: false, retry: null,
    sessionEpoch: 0, viewEpoch: 0, mutationToken: 0, listRequest: 0, stopSequence: 0, taskCache: new Map(), stops: new Map(), stopDialog: null, healthUnavailable: false, comparisonKey: null };
  const API = "/api/v1";
  const roleNames = { buyer: "我的任務", merchant: "商戶任務", operator: "異常與任務" };
  const preferences = { lowest_cost: "現金支出較低", fastest_delivery: "較早送達", easiest_returns: "退貨較方便" };
  const statuses = { draft: "正在整理需求", planning: "正在核對條件", queued: "等待派發", running: "正在核對", clarifying: "等你補充資料", needs_clarification: "等你補充資料", ready: "可繼續處理", proposed: "方案待你確認", awaiting_approval: "方案待你確認", approved: "已確認方案", enqueued: "已入隊", stopped: "委託已停止", stopped_before_dispatch: "已在派發前停止", cancelled: "已取消", blocked: "暫未能繼續", failed: "需要跟進", completed: "處理完成", unknown: "結果待核實", dispatch_committed: "已取得派發權", succeeded: "本地付款模擬完成", stop_requested: "已要求停止" };
  const paymentStates = { NOT_REQUESTED: "尚未提出付款", QUEUED: "等待派發", DISPATCH_COMMITTED: "已取得派發權", CLAIMED: "命令已領取", UNKNOWN: "付款結果待核實", SUCCEEDED: "本地模擬付款成功", FAILED: "付款已確認失敗", CANCELLED: "本地付款已取消", STOPPED: "已在派發前停止，未發起付款", STOPPED_BEFORE_DISPATCH: "已在派發前停止", PAYMENT_CANCEL_PENDING: "取消付款處理中" };
  const eventNames = { task_created: "已收到購物需求", user_input: "已收到購物需求", task_updated: "已更新購買條件", user_answer: "補充資料已儲存", answers_saved: "補充資料已儲存", model_run_queued: "已安排核對", model_run_claimed: "開始處理任務", model_run_finished: "本輪核對完成", model_retry_exhausted: "已到重試上限，需要跟進", run_queued: "已安排核對", run_started: "開始處理任務", run_completed: "本輪核對完成", run_blocked: "本輪處理受阻", clarification_requested: "需要補充資料", proposal_created: "比較方案已準備好", snapshot_registered: "已保存待確認方案", approved: "使用者已確認方案", user_approved: "使用者已確認方案並入隊", command_queued: "交易命令已入隊", stopped: "停止要求已處理", stop_requested: "收到停止要求", user_stop_requested: "收到停止要求", task_stopped: "委託已停止", snapshot_invalidated: "舊方案已失效", run_discarded: "已捨棄舊條件的結果", stale_model_result_discarded: "已捨棄舊條件的結果" };
  const fields = {
    text: { label: "購物需求", type: "textarea" },
    purchase_quantity: { label: "購買數量（幾件包裝）", type: "number", min: "1", step: "1" },
    cash_cap_minor: { label: "最高現金支出（HK$，含運費）", type: "number", min: "0.01", step: "0.01", money: true },
    destination_ref: { label: "香港收貨地點", type: "text", placeholder: "演練請勿填真實門牌" },
    preference: { label: "符合條件後，最重視", type: "select", options: preferences },
    "product.brand": { label: "商品品牌", type: "text" },
    "product.variant": { label: "款式／型號", type: "text" },
    "product.net_content": { label: "每件淨含量", type: "number", min: "1", step: "1" },
    "product.unit": { label: "淨含量單位", type: "select", options: { g: "克（g）", ml: "毫升（ml）", item: "個（item）" } },
    "product.pack_count": { label: "每件包裝內數量", type: "number", min: "1", step: "1" },
    "product.packaging": { label: "包裝形式", type: "text" },
    "product.origin": { label: "產地要求", type: "text" },
    "product.region_version": { label: "商品地區版本", type: "text" },
    requires_change_of_mind_return: { label: "是否必須可無理由退貨", type: "select", options: { false: "不作硬性要求", true: "必須可以" }, boolean: true },
    latest_delivery_epoch: { label: "最遲送達時間（香港時間）", type: "datetime-local", epoch: true }
  };

  function el(tag, text, className) {
    const node = document.createElement(tag);
    if (text !== undefined && text !== null) node.textContent = String(text);
    if (className) node.className = className;
    return node;
  }
  function clear(node) { node.replaceChildren(); }
  function valueAt(object, path) { return path.split(".").reduce((value, key) => value && value[key], object); }
  function setAt(object, path, value) { const keys = path.split("."); if (keys.length === 1) object[keys[0]] = value; else { object[keys[0]] ||= {}; object[keys[0]][keys[1]] = value; } }
  function normalized(value) { return String(value || "").toLowerCase(); }
  function asText(value, fallback = "未提供") { if (value === undefined || value === null || value === "") return fallback; if (Array.isArray(value)) return value.map(item => asText(item, "")).filter(Boolean).join("；"); if (typeof value === "object") return JSON.stringify(value); return String(value); }
  function money(minor, currency = "HKD") { if (minor === null || minor === undefined || minor === "" || !Number.isFinite(Number(minor))) return "金額待核實"; return new Intl.NumberFormat("zh-HK", { style: "currency", currency, currencyDisplay: "narrowSymbol" }).format(Number(minor) / 100); }
  function timestamp(value, full = false) { if (!value) return "—"; const date = new Date(typeof value === "number" ? value * 1000 : value); if (Number.isNaN(date.getTime())) return "—"; return new Intl.DateTimeFormat("zh-HK", { timeZone: "Asia/Hong_Kong", ...(full ? { month: "2-digit", day: "2-digit" } : {}), hour: "2-digit", minute: "2-digit", hour12: false }).format(date); }
  function buyer() { return state.identity?.role === "buyer"; }
  function syncView() {
    document.body.dataset.view = !state.identity ? "entry" : state.task ? "task" : "compose";
    document.body.dataset.role = state.identity?.role || "guest";
    if ($("compose-button")) $("compose-button").hidden = !buyer() || !state.task;
  }
  function updateComposerMode() {
    const live = document.querySelector('input[name="mode"]:checked')?.value === "live";
    if ($("composer-model-label")) $("composer-model-label").textContent = live ? "DeepSeek 整理需求" : "本地流程演練";
    if ($("composer-mode-note")) $("composer-mode-note").textContent = live
      ? "DeepSeek 幫你整理與核對。"
      : "先試一次購物委託。";
  }
  function defaultComposerMode() {
    const configured = state.config?.capabilities?.model === "configuration_present_unverified";
    document.querySelectorAll('input[name="mode"]').forEach(input => { input.checked = input.value === (configured ? "live" : "scripted"); });
    updateComposerMode();
  }
  function taskStatus(task) { return normalized(task?.status || task?.state); }
  function statusText(status) { return statuses[normalized(status)] || asText(status, "狀態待確認"); }
  function getSnapshot(task) { return task?.proposal?.snapshot || task?.snapshot || task?.proposal || {}; }
  function getQuote(task) { const snapshot = getSnapshot(task); return snapshot.quote || task?.proposal?.quote || {}; }
  function getOperation(task) { return task?.operation?.operation || task?.operation || null; }
  function paymentStatus(task) { const op = getOperation(task); return String(op?.payment_status || op?.payment_state || op?.state || "NOT_REQUESTED").toUpperCase(); }
  function stopEntry(taskId = state.task?.task_id) { return state.stops.get(taskId); }
  function stopUnresolved() { return ["pending", "uncertain"].includes(stopEntry()?.status); }
  function canRepeatTask() {
    return buyer() && !!state.task?.draft?.text && !state.busy && !state.healthUnavailable && !stopUnresolved()
      && ["NOT_REQUESTED", "STOPPED", "STOPPED_BEFORE_DISPATCH", "CANCELLED", "FAILED", "SUCCEEDED"].includes(paymentStatus(state.task));
  }
  function olderTask(task, current) {
    if (!current) return false;
    if (Number(task.state_version) !== Number(current.state_version)) return Number(task.state_version) < Number(current.state_version);
    const incoming = getOperation(task), previous = getOperation(current);
    return incoming?.operation_id === previous?.operation_id && Number(incoming?.version) < Number(previous?.version);
  }
  function rememberTask(task) {
    const current = state.taskCache.get(task.task_id);
    if (olderTask(task, current)) return current;
    state.taskCache.set(task.task_id, task);
    const entry = stopEntry(task.task_id);
    if (entry && (task.events || []).some(event => event.kind === "user_stop_requested")) {
      entry.status = "completed";
      entry.message = "服務端記錄已確認接納停止要求。請以付款與訂單狀態判斷結果；這不代表外部付款已取消。";
    }
    return task;
  }
  function unwrapTask(result) { return result?.task || result; }
  function csrfFrom(result) { return result?.csrf_token || result?.csrf || null; }
  function identityFrom(result) { return result?.actor || result?.identity || result?.user || (result?.role ? result : null); }
  function showNotice(message, error = false, retry = null) {
    const box = $("notice"); clear(box); box.className = error ? "notice is-error" : "notice";
    box.append(el("span", message)); box.hidden = false;
    if (retry) { const button = el("button", "重試同一要求", "button button-secondary"); button.type = "button"; button.addEventListener("click", retry); box.append(button); }
  }
  function clearNotice() { $("notice").hidden = true; state.retry = null; }
  function connection(message) { $("connection-status").textContent = message || ""; $("connection-status").hidden = !message; }
  async function refreshHealth() {
    let health;
    try {
      const response = await fetch(`${API}/health`, { credentials: "same-origin", signal: AbortSignal.timeout(5000) });
      health = await response.json();
    } catch { health = { status: "unavailable" }; }
    state.healthUnavailable = health.status !== "ready";
    const banner = $("service-health");
    banner.hidden = !state.healthUnavailable;
    banner.textContent = health.storage?.recovery_required
      ? "儲存服務出現故障，已暫停新增委託及批准。現有操作未能確認；請保留原任務，待恢復後核實，切勿重複付款。"
      : "服務暫未就緒，已暫停新增委託及批准。停止要求仍可送出，但必須收到後端確認才算生效。";
    busy(state.busy);
    setTimeout(refreshHealth, 10000);
  }
  function busy(active) {
    state.busy = active;
    document.querySelectorAll("form button[type=submit], #approve-button, #resume-button, #edit-task-button").forEach(button => { if (!button.closest("#stop-dialog")) button.disabled = active || stopUnresolved(); });
    const snapshot = getSnapshot(state.task);
    $("approve-button").disabled = active || state.healthUnavailable || stopUnresolved() || !$("approve-consent").checked || !(state.task?.proposal?.challenge_id || snapshot.challenge_id || state.task?.challenge_id);
    $("create-task-button").disabled = active || state.healthUnavailable;
    $("repeat-task-button").disabled = !canRepeatTask();
    $("workspace").setAttribute("aria-busy", String(active));
    renderStopControls();
  }
  function renderStopControls() {
    const entry = stopEntry(), acknowledged = (state.task?.events || []).some(event => event.kind === "user_stop_requested");
    $("stop-button").disabled = !buyer() || !state.task || entry?.status === "pending" || entry?.status === "completed" || acknowledged;
    $("stop-button").textContent = entry?.status === "pending" ? "正在送出停止要求…" : acknowledged || entry?.status === "completed" ? "已要求停止後續代辦" : entry?.status === "uncertain" ? "重試同一停止要求" : stopCopy(state.task).button;
    const dialog = state.stopDialog;
    $("confirm-stop-button").disabled = !dialog || dialog.sessionEpoch !== state.sessionEpoch || stopEntry(dialog.taskId)?.status === "pending";
    const feedback = $("stop-feedback"); clear(feedback); feedback.hidden = !entry;
    if (entry) {
      feedback.append(el("span", entry.message));
      if (entry.status === "uncertain") {
        const button = el("button", "重試同一停止要求", "button button-secondary"); button.type = "button";
        button.addEventListener("click", () => sendStop(entry.taskId, entry)); feedback.append(button);
      }
    }
  }
  function localGet(key) { try { return localStorage.getItem(key); } catch { return null; } }
  function localSet(key, value) { try { if (value) localStorage.setItem(key, value); else localStorage.removeItem(key); } catch { /* Privacy mode may disable storage. */ } }
  async function keyFor(path, body) {
    const subject = state.identity?.actor_id || "session";
    const input = `${subject}|${path}|${JSON.stringify(body)}`;
    let hash;
    if (globalThis.crypto?.subtle) { const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(input)); hash = Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, "0")).join(""); }
    else { let n = 0; for (let i = 0; i < input.length; i += 1) n = Math.imul(31, n) + input.charCodeAt(i) | 0; hash = String(n); }
    const slot = `hacku.idempotency.${hash}`;
    let key;
    try { key = sessionStorage.getItem(slot); } catch { /* Runtime retry still retains its key. */ }
    if (!key) { key = crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random().toString(36).slice(2)}`; try { sessionStorage.setItem(slot, key); } catch { /* Do not block the task for storage settings. */ } }
    return { key, slot };
  }
  async function request(path, { method = "GET", body, key } = {}) {
    const sessionEpoch = state.sessionEpoch;
    const headers = { Accept: "application/json" };
    if (body !== undefined) headers["Content-Type"] = "application/json";
    if (method !== "GET") { if (state.csrf) headers["X-CSRF-Token"] = state.csrf; if (key) headers["Idempotency-Key"] = key; }
    let response, result;
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 20000);
    try { response = await fetch(`${API}${path}`, { method, credentials: "same-origin", headers, signal: controller.signal, ...(body !== undefined ? { body: JSON.stringify(body) } : {}) }); result = await response.json(); }
    catch (cause) { const error = new Error("連線中斷，要求可能已被收到。請重試同一要求，或重新連線查看狀態。"); error.network = true; error.cause = cause; throw error; }
    finally { clearTimeout(timeout); }
    if (!response.ok) {
      const detail = result?.error;
      const message = result?.message || (typeof detail === "string" ? detail : detail?.message) || result?.detail;
      const error = new Error(asText(message, `要求未完成（${response.status}）`)); error.status = response.status; error.code = detail?.code || result?.code;
      if (response.status === 401 && path !== "/session" && sessionEpoch === state.sessionEpoch) showLogin();
      throw error;
    }
    return result;
  }
  async function mutate(path, body, onSuccess, method = "POST", original = null) {
    if (state.busy || (path.startsWith("/tasks/") && stopUnresolved())) return;
    const token = ++state.mutationToken, sessionEpoch = state.sessionEpoch, viewEpoch = state.viewEpoch;
    const match = path.match(/^\/tasks\/([^/]+)\//), taskId = match ? decodeURIComponent(match[1]) : null;
    const stopBefore = stopEntry(taskId)?.sequence;
    const current = () => sessionEpoch === state.sessionEpoch && viewEpoch === state.viewEpoch && (!taskId || state.task?.task_id === taskId);
    busy(true); clearNotice();
    const entry = original || await keyFor(path, body);
    try {
      if (!current()) return;
      const result = await request(path, { method, body, key: entry.key });
      try { sessionStorage.removeItem(entry.slot); } catch { /* Optional client cache only. */ }
      if (!current()) return;
      // A stop has its own request lane. A response from an earlier mutation must
      // not replace its UI or announce that the old plan can proceed.
      if (taskId && stopEntry(taskId)?.sequence !== stopBefore) { await refreshTask(false); return; }
      connection("");
      if (onSuccess) await onSuccess(result);
    } catch (error) {
      if (!current()) return;
      if (taskId && stopEntry(taskId)?.sequence !== stopBefore) { await refreshTask(false); return; }
      const retry = error.network ? () => mutate(path, body, onSuccess, method, entry) : null;
      showNotice(error.message, true, retry);
      if (error.status === 409 && state.task) await refreshTask(false);
    } finally { if (token === state.mutationToken) busy(false); }
  }
  async function sendStop(taskId, original = null) {
    if (!buyer() || !taskId || stopEntry(taskId)?.status === "pending") return;
    if (original && (original.sessionEpoch !== state.sessionEpoch || state.stops.get(taskId) !== original)) return;
    const entry = original || stopEntry(taskId) || { taskId, sessionEpoch: state.sessionEpoch, path: `/tasks/${encodeURIComponent(taskId)}/stop`, request: null };
    if (entry.status === "completed") return;
    entry.sequence = ++state.stopSequence;
    entry.status = "pending"; entry.message = "正在送出停止要求；尚未收到後端確認。";
    state.stops.set(taskId, entry); busy(state.busy);
    try {
      entry.request ||= await keyFor(entry.path, {});
      if (entry.sessionEpoch !== state.sessionEpoch) return;
      const result = await request(entry.path, { method: "POST", body: {}, key: entry.request.key });
      if (entry.sessionEpoch !== state.sessionEpoch) return;
      entry.status = "completed"; entry.message = "後端已接納停止要求。請以付款與訂單狀態判斷結果；這不代表外部付款已取消。";
      const task = rememberTask(unwrapTask(result));
      if (state.task?.task_id === taskId) { state.editing = false; acceptTask(task); }
      // Retain this stop's idempotency key. Refresh and network retries never
      // create a new stop operation or wait for the ordinary mutation lane.
      void refreshList();
      if (state.task?.task_id === taskId) void refreshTask(false);
    } catch (error) {
      if (entry.sessionEpoch !== state.sessionEpoch) return;
      if (entry.status === "completed") return; // A GET may already have confirmed the original stop.
      entry.status = error.network ? "uncertain" : "failed";
      entry.message = error.network ? "未收到停止回覆，不能確認已停止。可重試同一停止要求，或讀取服務端狀態。" : `停止要求未獲確認：${error.message}`;
    } finally { if (entry.sessionEpoch === state.sessionEpoch) busy(state.busy); }
  }
  function showLogin() {
    state.sessionEpoch += 1; state.viewEpoch += 1; state.mutationToken += 1;
    state.taskCache.clear(); state.stops.clear(); dismissStopDialog();
    state.identity = null; state.csrf = null; state.task = null; state.tasks = []; state.questionKey = null; state.editing = false;
    state.comparisonKey = null; syncView(); setModelBadge();
    $("task-form").reset(); $("access-code").value = "";
    $("approve-consent").checked = false;
    $("task-panel").hidden = true; $("task-record").textContent = "";
    ["task-list", "quote-grid", "event-list", "confirmation-details", "operation-details", "question-fields", "evidence-summary", "task-facts", "task-progress", "comparison-explanation", "recommendation", "payment-option-list", "payment-benefits", "user-value-counts", "stop-feedback", "task-id-label"].forEach(id => clear($(id)));
    $("task-description").textContent = ""; $("task-title").textContent = "購物任務";
    clearNotice(); connection("");
    if (state.poll) clearTimeout(state.poll);
    $("login-panel").hidden = false; $("workspace").hidden = true; $("logout-button").hidden = true;
    busy(false);
  }
  async function setSession(result, restoreTask = false) {
    state.sessionEpoch += 1; state.taskCache.clear(); state.stops.clear();
    state.identity = identityFrom(result); state.csrf = csrfFrom(result);
    if (!state.identity || !state.csrf) throw new Error("登入回應缺少有效身分或防偽權杖，暫不能操作任務。");
    $("task-panel").hidden = true;
    $("login-panel").hidden = true; $("workspace").hidden = false; $("logout-button").hidden = false;
    $("identity-label").textContent = roleNames[state.identity.role] || "可查看的任務";
    $("new-task-button").hidden = !buyer(); $("new-task-panel").hidden = !buyer(); $("empty-state").hidden = buyer();
    syncView(); defaultComposerMode(); setModelBadge();
    await refreshList();
    const saved = localGet(`hacku.task.${state.identity.actor_id}`);
    if (restoreTask && saved && state.tasks.some(task => task.task_id === saved)) await selectTask(saved);
    else if (!buyer() && state.tasks.length) await selectTask(state.tasks[0].task_id);
  }
  async function loadSocialLogin() {
    const container = $("social-login-options");
    if (!container) return;
    let providers = [];
    try { providers = (await request("/auth/providers")).providers || []; }
    catch { /* Older local servers still offer the access-code flow. */ }
    clear(container);
    const unavailable = [];
    for (const [name, label] of [["google", "Google"], ["wechat", "微信"]]) {
      const provider = providers.find(item => item.provider === name);
      const button = el("button", null, "social-login-button"); button.type = "button";
      const available = provider?.available === true;
      button.disabled = !available;
      const icon = el("img", null, "social-login-icon");
      icon.src = name === "google" ? "/assets/google-g.png" : "/assets/wechat.svg";
      icon.alt = ""; icon.width = 20; icon.height = 20;
      button.dataset.provider = name;
      button.append(icon, el("span", `使用 ${label} 繼續`, "social-login-label"));
      if (!available) {
        unavailable.push(label);
        button.setAttribute("aria-describedby", "social-login-note");
      }
      button.addEventListener("click", async () => {
        button.disabled = true;
        try {
          const result = await request(`/auth/${name}/start`, { method: "POST", body: {} });
          const target = new URL(result.authorization_url);
          if (target.protocol !== "https:" || target.host !== (name === "google" ? "accounts.google.com" : "open.weixin.qq.com")) throw new Error("登入地址未獲確認，請稍後再試。");
          window.location.assign(target.href);
        } catch (error) {
          $("social-login-note").textContent = error.message;
          $("social-login-note").hidden = false; button.disabled = false;
        }
      });
      container.append(button);
    }
    $("social-login-note").textContent = unavailable.length ? `${unavailable.join(" 及 ")}登入暫未開通，請先使用存取碼。` : "";
    $("social-login-note").hidden = !unavailable.length;
  }
  function setModelBadge(task) {
    const run = task?.run || {};
    const capabilities = task?.capabilities || state.config?.capabilities || {};
    const reported = task?.model_result?.model_status || run.model_status || task?.model_status || capabilities.model_status || (typeof capabilities.model === "string" ? capabilities.model : capabilities.model?.status);
    const mode = task?.mode || run.mode;
    let text = "模型：待核驗"; let cls = "badge";
    if (reported === "scripted" || mode === "scripted") text = "模型：本地演練，未呼叫";
    else if (reported === "live") { text = "模型：真實 DeepSeek"; cls += " badge-live"; }
    else if (reported === "blocked" || taskStatus(task) === "blocked") { text = "模型：受阻，未完成真實驗證"; cls += " badge-warning"; }
    else if (reported === "configuration_present_unverified") text = "模型：已配置，真實能力待核驗";
    else if (mode === "live") text = "模型：DeepSeek，等待執行記錄";
    else if (capabilities.deepseek_configured === false || state.config.deepseek_configured === false) { text = "模型：DeepSeek 尚未配置"; cls += " badge-warning"; }
    $("model-badge").textContent = text; $("model-badge").className = cls;
  }
  async function refreshList() {
    if (!state.identity) return;
    const sessionEpoch = state.sessionEpoch, requestId = ++state.listRequest;
    try {
      const response = await request("/tasks");
      if (sessionEpoch !== state.sessionEpoch || requestId !== state.listRequest) return;
      state.tasks = (Array.isArray(response) ? response : response.tasks || response.items || []).map(rememberTask);
      const list = $("task-list"); clear(list);
      if (!state.tasks.length) list.append(el("p", buyer() ? "說說你想買甚麼，開始第一份委託。" : "暫時沒有可查看的任務。", "task-list-empty"));
      for (const task of state.tasks) {
        const button = el("button", null, `task-list-item${task.task_id === state.task?.task_id ? " active" : ""}`); button.type = "button";
        button.dataset.taskId = task.task_id;
        button.append(el("strong", task.draft?.text || task.text || task.title || "購物任務"), el("small", `${statusText(task.status || task.state)} · ${timestamp(task.updated_at || task.created_at, true)}`));
        if (task.task_id === state.task?.task_id) button.setAttribute("aria-current", "page");
        button.addEventListener("click", () => selectTask(task.task_id)); list.append(button);
      }
    } catch (error) { if (sessionEpoch === state.sessionEpoch && requestId === state.listRequest && error.status !== 401) connection(error.message); }
  }
  async function selectTask(taskId) {
    if (state.busy) return;
    const viewEpoch = ++state.viewEpoch, sessionEpoch = state.sessionEpoch;
    dismissStopDialog();
    state.editing = false; state.questionKey = null;
    try { const result = await request(`/tasks/${encodeURIComponent(taskId)}`); if (viewEpoch !== state.viewEpoch || sessionEpoch !== state.sessionEpoch) return; acceptTask(unwrapTask(result)); clearNotice(); await refreshList(); }
    catch (error) { if (viewEpoch === state.viewEpoch && sessionEpoch === state.sessionEpoch) showNotice(error.message, true); }
  }
  function acceptTask(task) {
    if (!task?.task_id) throw new Error("回應缺少任務編號，請重新連線查核。");
    if (!state.identity) return;
    task = rememberTask(task);
    const changedSnapshot = getSnapshot(state.task).snapshot_id !== getSnapshot(task).snapshot_id;
    state.task = task;
    syncView();
    for (const button of $("task-list").querySelectorAll("[data-task-id]")) {
      if (button.dataset.taskId === task.task_id) {
        button.querySelector("small").textContent = `${statusText(task.status || task.state)} · ${timestamp(task.updated_at || task.created_at, true)}`;
      }
    }
    if (changedSnapshot) $("approve-consent").checked = false;
    localSet(`hacku.task.${state.identity.actor_id}`, task.task_id);
    $("new-task-panel").hidden = true; $("empty-state").hidden = true; $("task-panel").hidden = false;
    renderTask(task); schedulePoll();
  }
  async function refreshTask(showSuccess = false) {
    if (!state.task || !state.identity) return;
    const taskId = state.task.task_id, viewEpoch = state.viewEpoch, sessionEpoch = state.sessionEpoch;
    try { const result = await request(`/tasks/${encodeURIComponent(taskId)}`); if (viewEpoch !== state.viewEpoch || sessionEpoch !== state.sessionEpoch || state.task?.task_id !== taskId) return; acceptTask(unwrapTask(result)); connection(""); if (showSuccess) showNotice("已重新讀取服務端保存的任務狀態。本次讀取沒有向支付機構查單，也沒有重新付款。"); }
    catch (error) { if (viewEpoch !== state.viewEpoch || sessionEpoch !== state.sessionEpoch) return; if (error.status !== 401) connection(error.message); schedulePoll(); }
  }
  function schedulePoll() { if (state.poll) clearTimeout(state.poll); if (state.identity && state.task) state.poll = setTimeout(() => { if (!document.hidden) refreshTask(); else schedulePoll(); }, 2500); }
  function resetNewTask() {
    if (!buyer()) return;
    state.viewEpoch += 1; dismissStopDialog(); state.task = null; state.editing = false; state.questionKey = null; state.comparisonKey = null;
    if (state.poll) clearTimeout(state.poll);
    localSet(`hacku.task.${state.identity.actor_id}`, null); $("task-form").reset(); defaultComposerMode();
    $("task-form").querySelectorAll("details").forEach(item => { item.open = false; });
    if ($("history-details")) $("history-details").open = false;
    $("approve-consent").checked = false; $("task-panel").hidden = true; $("empty-state").hidden = true; $("new-task-panel").hidden = false;
    syncView(); clearNotice(); busy(state.busy); setModelBadge(); $("task-text").focus(); refreshList();
  }
  function addFact(text) { $("task-facts").append(el("span", text, "fact-chip")); }
  function renderTask(task) {
    const draft = task.draft || {}; const status = taskStatus(task); const operation = getOperation(task); const snapshot = getSnapshot(task);
    $("repeat-task-button").hidden = !buyer();
    $("repeat-task-button").disabled = !canRepeatTask();
    $("task-title").textContent = buyer() ? "這次購物交託" : "可查看的購物任務";
    $("task-id-label").textContent = `${task.task_id} · 條件版本 ${task.constraints_version ?? "—"}`;
    $("task-description").textContent = draft.text || task.text || (task.item ? productText(task.item.product) : "服務端未提供需求描述。");
    $("task-status").textContent = statusText(status);
    $("task-status").className = `status-pill${["blocked", "unknown", "failed"].includes(status) ? " warning" : ["stopped", "cancelled"].includes(status) ? " stopped" : ""}`;
    clear($("task-facts"));
    if (draft.purchase_quantity != null) addFact(`購買 ${draft.purchase_quantity} 件包裝`);
    if (draft.cash_cap_minor != null) addFact(`現金上限 ${money(draft.cash_cap_minor)}`);
    if (draft.destination_ref) addFact(`送往 ${draft.destination_ref}`);
    if (draft.preference) addFact(`偏好：${preferences[draft.preference] || draft.preference}`);
    if (task.item?.merchant_id) addFact(`商戶 ${task.item.merchant_id}`);
    if (task.item?.merchant_sku) addFact(`SKU ${task.item.merchant_sku}`);
    if (task.item?.purchase_quantity != null) addFact(`購買 ${task.item.purchase_quantity} 件包裝`);
    setModelBadge(task);
    const stages = ["交代需求", "核對條件", "比較方案", "由你確認", "入隊／跟進"];
    let progress = operation ? 4 : snapshot.snapshot_id ? 3 : task.comparison ? 2 : ["planning", "running", "queued", "clarifying", "needs_clarification"].includes(status) ? 1 : 0;
    clear($("task-progress")); stages.forEach((name, i) => { const item = el("li", name, i < progress ? "done" : i === progress ? "current" : ""); item.dataset.step = String(i + 1); if (i === progress) item.setAttribute("aria-current", "step"); $("task-progress").append(item); });
    const terminated = ["stopped", "stopped_before_dispatch", "cancelled", "completed"].includes(status);
    $("stop-button").hidden = !buyer() || terminated;
    $("stop-button").textContent = stopCopy(task).button;
    $("edit-task-button").hidden = !buyer() || !!operation || terminated;
    $("resume-button").hidden = !buyer() || !["blocked", "failed", "ready"].includes(status) || !!operation;
    $("task-action-hint").textContent = !buyer() ? "此身分只可查看已授權的任務資料。" : operation ? stopCopy(task).summary : terminated ? "這次委託已停止或結束。" : task.model_result?.reason || task.run?.reason || task.reason || "條件有變，可修改或停止委託。";
    renderQuestions(task);
    renderComparison(task);
    renderPaymentOptions(task);
    renderConfirmation(task);
    renderOperation(task);
    renderUserValue(task);
    renderEvents(task);
    $("task-record").textContent = JSON.stringify(task, null, 2);
    clear($("evidence-summary")); const pills = el("div", null, "evidence-pills");
    const run = task.model_result || task.run || {};
    const modelRequests = Array.isArray(run.model_requests) ? `${run.model_requests.length} 筆記錄` : asText(run.model_requests, "請查看執行記錄");
    [ `模型模式：${run.model_status || task.capabilities?.model || "待核實"}`, `商品：${task.capabilities?.goods || "synthetic_fixture"}`, "付款：local_simulator", `狀態版本：${task.state_version ?? "—"}`, `模型請求：${modelRequests}` ].forEach(text => pills.append(el("span", text, "badge")));
    $("evidence-summary").append(pills);
    if (task.metrics) $("evidence-summary").append(el("p", `本次操作記錄：交代 ${task.metrics.input_count ?? 0} 次、補答 ${task.metrics.answer_count ?? 0} 次、確認 ${task.metrics.approval_count ?? 0} 次。尚未與人工操作作實測對照。`, "field-help"));
    busy(state.busy);
  }
  function missingFields(task) {
    const raw = task.missing_fields || task.run?.missing_fields || [];
    const result = [];
    for (const item of raw) {
      const key = typeof item === "string" ? item : item.field || item.name || item.path;
      if (key === "product") result.push(...Object.keys(fields).filter(name => name.startsWith("product.")));
      else if (key) result.push(key);
    }
    return [...new Set(result)];
  }
  function renderQuestions(task) {
    const missing = missingFields(task);
    const editable = buyer() && !getOperation(task) && !["stopped", "cancelled", "completed"].includes(taskStatus(task));
    const list = state.editing ? Object.keys(fields) : missing;
    const shouldShow = editable && list.length > 0;
    $("clarification-panel").hidden = !shouldShow;
    if (!shouldShow) return;
    $("clarification-title").textContent = state.editing ? "想修改哪些購買條件？" : "還差這幾項，就可以繼續";
    const key = `${task.task_id}|${task.constraints_version}|${state.editing}|${list.join(",")}`;
    if (state.questionKey === key) return;
    state.questionKey = key; clear($("question-fields"));
    for (const name of list) {
      const definition = fields[name] || { label: name, type: "text" };
      const questions = task.questions || [];
      const question = Array.isArray(questions) ? questions.find(q => typeof q === "object" && (q.field === name || q.path === name)) : null;
      const wrapper = el("div"); const id = `answer-${name.replaceAll(".", "-")}`;
      const label = el("label", question?.question || question?.label || definition.label); label.htmlFor = id;
      const input = el(definition.type === "textarea" ? "textarea" : definition.type === "select" ? "select" : "input");
      input.id = id; input.name = name; input.dataset.field = name;
      if (definition.type === "select") { input.append(new Option("請選擇", "")); Object.entries(definition.options).forEach(([value, text]) => input.append(new Option(text, value))); }
      else if (definition.type !== "textarea") input.type = definition.type;
      if (definition.min) input.min = definition.min; if (definition.step) input.step = definition.step; if (definition.placeholder) input.placeholder = definition.placeholder;
      input.required = !state.editing || missing.includes(name);
      const current = valueAt(task.draft || {}, name);
      if (current !== null && current !== undefined) {
        if (definition.money) input.value = (Number(current) / 100).toFixed(2);
        else if (definition.epoch) input.value = new Date((Number(current) + 8 * 3600) * 1000).toISOString().slice(0, 16);
        else input.value = String(current);
      }
      wrapper.append(label, input); $("question-fields").append(wrapper);
    }
    $("clarification-help").textContent = `更新以條件版本 ${task.constraints_version} 為準；舊方案會失效。`;
  }
  function quoteCandidates(task) {
    const comparison = task.comparison || task.run?.comparison || {};
    const candidates = Array.isArray(comparison) ? comparison : comparison.candidates || comparison.quotes || [];
    return candidates.map(candidate => {
      const quote = candidate.quote || comparison.quotes?.find(item => item.quote_id === candidate.quote_id) || (getQuote(task).quote_id === candidate.quote_id ? getQuote(task) : candidate);
      const calculation = candidate.calculation || candidate.evaluation || candidate;
      return { quote, calculation, eligible: candidate.eligible !== false && calculation.eligible !== false };
    });
  }
  function productText(product) {
    if (!product) return "規格待核實";
    const fixture = product.brand === "SYNTHETIC" && product.variant === "plain-soap" && product.region_version === "HK-demo";
    // Translate only our exact fixture labels; retain the original source fields
    // in the saved evidence, and never relabel real merchant products.
    return [fixture ? "演練商品" : product.brand, fixture ? "原味肥皂" : product.variant,
      product.net_content != null ? `${product.net_content}${product.unit || ""}` : null,
      product.pack_count != null ? `每件 ${product.pack_count} 個裝` : null,
      fixture ? "香港版演練" : product.region_version].filter(Boolean).join(" · ");
  }
  function pair(parent, label, value, cls = "") { const row = el("div", null, `quote-line ${cls}`.trim()); row.append(el("span", label), el("span", value)); parent.append(row); }
  function deliveryText(delivery) { if (!delivery) return "配送條件待核實"; return delivery.label || delivery.description || (delivery.guaranteed_by_epoch ? `${timestamp(delivery.guaranteed_by_epoch, true)} 前送達` : delivery.estimated_days != null ? `預計 ${delivery.estimated_days} 天` : delivery.days != null ? `預計 ${delivery.days} 天` : delivery.estimated_arrival_epoch ? `預計 ${timestamp(delivery.estimated_arrival_epoch, true)}` : "送達時間待核實"); }
  function returnText(returns) { if (!returns) return "退貨條件待核實"; return returns.label || returns.description || returns.summary || (returns.change_of_mind === true || returns.change_of_mind_allowed === true ? `${returns.window_days ?? returns.days ?? "規定期限"} 天內可無理由退貨` : returns.change_of_mind === false || returns.change_of_mind_allowed === false ? "不支援無理由退貨" : asText(returns)); }
  function renderRewards(parent, quote, calculation) {
    const benefits = calculation.benefits || quote.benefits || {};
    const rewards = calculation.rewards || quote.rewards || benefits.rewards || {};
    if (Array.isArray(rewards)) {
      for (const kind of ["expected_cashback", "expected_points"]) {
        const group = rewards.filter(reward => reward.kind === kind);
        const label = kind === "expected_cashback" ? "預計返現" : "預計積分";
        if (!group.length) pair(parent, label, "尚無已核實權益");
        for (const reward of group) {
          const qualified = reward.eligibility === "eligible";
          const value = kind === "expected_cashback" ? money(reward.amount_minor) : reward.points == null ? "積分數待核實" : `${reward.points} 分`;
          pair(parent, label, qualified ? value : `資格未確認：${value}`);
          parent.append(el("p", `規則 ${asText(reward.rule_id)} · 來源 ${asText(reward.source_ref)} · ${timestamp(reward.observed_at, true)}；兌換條件：${asText(reward.redemption_conditions)}。預計獎勵不降低本次現金支出。`, "quote-evidence"));
        }
      }
      return;
    }
    const cashback = calculation.expected_cashback_minor ?? rewards.expected_cashback_minor ?? quote.expected_cashback_minor;
    const points = calculation.expected_points ?? rewards.expected_points ?? quote.expected_points;
    pair(parent, "預計返現", cashback == null ? "尚無已核實權益" : money(cashback));
    pair(parent, "預計積分", points == null ? "尚無已核實權益" : `${points} 分（不折抵本次現金）`);
    if (rewards.source || rewards.rule_source) parent.append(el("p", `權益來源：${asText(rewards.source || rewards.rule_source)}；觀察時間：${timestamp(rewards.observed_at, true)}；資格與兌換：${asText(rewards.eligibility || rewards.redemption_conditions, "尚待核實，不作執行依據")}`, "quote-evidence"));
  }
  function renderComparison(task) {
    const candidates = quoteCandidates(task); $("comparison-panel").hidden = candidates.length === 0;
    if (!candidates.length) { state.comparisonKey = null; return; }
    const comparison = task.comparison || {};
    const selected = comparison.selected_quote_id || task.proposal?.selected_quote_id || getQuote(task).quote_id;
    // Polling must not reset the user's horizontal position or close disclosures.
    const key = JSON.stringify([task.task_id, candidates, selected, task.draft?.preference, comparison.reason, comparison.tradeoffs, task.proposal?.reason, task.model_result?.reason, task.run?.reason]);
    if (state.comparisonKey === key) return;
    state.comparisonKey = key;
    const grid = $("quote-grid"), previousScroll = grid.scrollLeft;
    $("comparison-explanation").textContent = `比較 ${candidates.length} 個方案，優先考慮「${preferences[task.draft?.preference] || "已確認偏好"}」。`;
    clear(grid);
    for (const { quote, calculation, eligible } of candidates) {
      const recommended = quote.quote_id === selected;
      const card = el("article", null, `quote-card${recommended ? " recommended" : ""}${!eligible ? " ineligible" : ""}`);
      const merchant = quote.merchant_name || (quote.merchant_id === "fixture-merchant-A" ? "演示商戶 A" : quote.merchant_id === "fixture-merchant-B" ? "演示商戶 B" : quote.merchant_id) || "商戶待核實";
      const top = el("div", null, "quote-topline"); top.append(el("span", merchant, "quote-merchant"));
      if (recommended) top.append(el("span", "建議選擇", "quote-recommended"));
      card.append(top);
      const metrics = el("div", null, "quote-metrics");
      const price = el("div", null, "quote-price"); price.append(el("span", money(calculation.cash_minor ?? calculation.cash_total_minor ?? quote.cash_minor, quote.currency || "HKD")), el("small", `${quote.currency || "HKD"} · 含已列費用`));
      // QuoteV1 currently has no sourced customer-rating contract. Never turn
      // eligibility, recommendation rank or a model score into a product rating.
      const rating = el("div", null, "quote-rating");
      const star = el("span", "☆"); star.setAttribute("aria-hidden", "true");
      rating.append(star, el("span", "評分待核實")); rating.title = "目前商品來源沒有提供可核實的消費者評分。";
      metrics.append(price, rating); card.append(metrics, el("h3", productText(quote.product), "quote-product"));
      pair(card, "數量", quote.purchase_quantity != null ? `${quote.purchase_quantity} 件包裝` : "待核實");
      pair(card, "送達", deliveryText(quote.delivery)); pair(card, "退貨", returnText(quote.returns));
      const details = el("details", null, "quote-details"); details.append(el("summary", "費用明細與資料來源"));
      const detailBody = el("div", null, "quote-details-body"); details.append(detailBody);
      pair(detailBody, "商品金額", money(calculation.goods_minor ?? quote.line_subtotal_minor));
      pair(detailBody, "運費及其他費用", money(calculation.fees_minor));
      pair(detailBody, "即時優惠", calculation.discount_minor != null ? `−${money(calculation.discount_minor)}` : "待核實");
      pair(detailBody, "每件價格", money(quote.unit_price_minor));
      renderRewards(detailBody, quote, calculation);
      const methods = verifiedLocalOptions(quote);
      detailBody.append(el("p", methods.length ? "付款方式：LocalPSP 本地演練。香港微信尚未接通，會員權益尚未核驗。" : "付款方式待核實；沒有可使用的外部錢包。", "quote-evidence"));
      detailBody.append(el("p", `商戶：${asText(quote.merchant_id)} · SKU：${asText(quote.merchant_sku)} · 報價版本 ${asText(quote.version)} · ${timestamp(quote.observed_at || quote.observed_at_epoch, true)}（香港時間）`, "quote-evidence"));
      const sources = quote.evidence_refs || quote.source_refs || Object.values(quote.evidence || {}).map(item => `${item.evidence_id}（${item.source_url}）`);
      if (sources.length) { const evidence = el("details", null, "quote-evidence"); evidence.append(el("summary", `查看 ${sources.length} 項合成資料記錄`), el("p", asText(sources))); detailBody.append(evidence); }
      card.append(details);
      const blockers = calculation.blockers || quote.blockers || [];
      if (!eligible || blockers.length) card.append(el("p", `目前不可執行：${asText(blockers, "條件未通過")}`, "quote-warning"));
      grid.append(card);
    }
    grid.scrollLeft = previousScroll;
    requestAnimationFrame(updateQuoteNavigation);
    const reason = comparison.reason || task.proposal?.reason || task.model_result?.reason || task.run?.reason;
    $("recommendation").hidden = !reason; clear($("recommendation"));
    if (reason) { $("recommendation").append(el("strong", "為甚麼這樣選"), el("p", asText(reason))); if (comparison.tradeoffs?.length) $("recommendation").append(el("p", `取捨：${comparison.tradeoffs.map(item => item.kind === "additional_cash_for_preference" ? `為符合你的偏好，比最低支出多 ${money(item.additional_cash_minor)}` : item.kind === "delivery_time_unknown" ? "送達時間尚未確認" : asText(item)).join("；")}`)); }
  }
  function updateQuoteNavigation() {
    const grid = $("quote-grid"), cards = Array.from(grid.children);
    if (!cards.length) return;
    const left = grid.getBoundingClientRect().left;
    const nearest = cards.reduce((best, card, i) => Math.abs(card.getBoundingClientRect().left - left) < Math.abs(cards[best].getBoundingClientRect().left - left) ? i : best, 0);
    if ($("quote-position")) $("quote-position").textContent = `${nearest + 1} / ${cards.length}`;
    if ($("quote-prev")) $("quote-prev").disabled = grid.scrollLeft <= 2;
    if ($("quote-next")) $("quote-next").disabled = grid.scrollLeft >= grid.scrollWidth - grid.clientWidth - 2;
  }
  function moveQuote(direction) {
    const grid = $("quote-grid"), card = grid.firstElementChild;
    if (!card) return;
    const gap = parseFloat(getComputedStyle(grid).columnGap) || 16;
    grid.scrollBy({ left: direction * (card.getBoundingClientRect().width + gap), behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
  }
  function verifiedLocalOptions(quote) {
    if (quote.environment !== "local_simulator" || quote.provenance !== "synthetic_fixture") return [];
    return (Array.isArray(quote.payment_options) ? quote.payment_options : []).filter(method =>
      method.option_id === "local-psp" && method.eligibility === "eligible" &&
      method.provenance === "synthetic_fixture" && method.source_ref === "fixture://local-psp" &&
      typeof method.observed_at === "number" && Number.isFinite(method.observed_at));
  }
  function renderPaymentOptions(task) {
    const quote = getQuote(task); const snapshot = getSnapshot(task);
    const show = buyer() && !!snapshot.snapshot_id;
    $("payment-options-panel").hidden = !show;
    if (!show) return;
    clear($("payment-option-list")); clear($("payment-benefits"));
    const methods = verifiedLocalOptions(quote);
    if (!methods.length) $("payment-option-list").append(el("p", "這份快照沒有具備來源記錄的本地付款選項，付款方式待核實。", "quote-warning"));
    for (const method of methods) {
      const card = el("article", null, "payment-method-card");
      const heading = el("div", null, "payment-method-heading");
      heading.append(el("h3", "LocalPSP 本地演練"));
      card.append(heading);
      const paymentFees = (quote.fee_lines || []).filter(line => line.code === "payment");
      if (paymentFees.length === 1) pair(card, "付款費用（合成）", money(paymentFees[0].amount_minor, quote.currency));
      else pair(card, "付款費用", "尚未提供可核實的單獨費用");
      pair(card, "收款方記錄（合成）", asText(snapshot.payee_ref || quote.payee_ref));
      const evidence = el("details", null, "quote-evidence");
      evidence.append(el("summary", "來源記錄"), el("p", `${method.source_ref} · ${timestamp(method.observed_at, true)}（香港時間）。不是官方支付沙盒，沒有錢包開通證據。`)); card.append(evidence);
      $("payment-option-list").append(card);
    }
    $("payment-benefits").append(el("h3", "優惠與未來權益，分開看"));
    const calculation = snapshot.calculation || {};
    pair($("payment-benefits"), "本次即時優惠", calculation.discount_minor == null ? "待核實" : `−${money(calculation.discount_minor, quote.currency)}`);
    renderRewards($("payment-benefits"), quote, calculation);
    $("payment-benefits").append(el("p", "預計返現或積分不減少本次現金預算。這份演練沒有已核驗的真實會員優惠。", "field-help"));
  }
  function stopCopy(task) {
    const status = paymentStatus(task);
    if (status === "SUCCEEDED") return {
      button: "停止後續代辦", title: "停止後續代辦？",
      summary: "本地付款模擬已成功，商戶訂單仍須獨立確認。停止不會自動退款。",
      message: "目前記錄是本地付款模擬成功。這個要求只撤銷後續代辦權限，不會撤銷已記錄的付款，也不會提出退款。本原型尚未接入商戶訂單取消或退款。" };
    if (status === "UNKNOWN") return {
      button: "停止後續代辦", title: "停止後續代辦？",
      summary: "付款結果待核實。停止後續代辦不代表已取消付款，請勿另建付款。",
      message: "原操作的付款結果尚未確定。停止只撤銷後續代辦權限，不能確認原付款已取消；請保留原操作記錄，不要再建付款。頁面只能讀取已保存狀態，尚未接入支付機構查單或取消。" };
    if (["DISPATCH_COMMITTED", "CLAIMED"].includes(status)) return {
      button: "停止後續代辦", title: "付款可能已送出，仍要停止代辦？",
      summary: "操作已取得派發權，付款可能已送出。停止不能保證原付款未發生。",
      message: "操作已取得派發權，請求可能已送出。停止只撤銷後續代辦權限；原操作仍須核實，不能當作已停止付款。本原型尚未接入外部會話取消，請勿重新付款。" };
    if (status === "STOPPED") return {
      button: "停止這次委託", title: "這次委託已停止",
      summary: "這筆本地操作已在派發前停止，未發起付款；原記錄仍可查看。",
      message: "服務端記錄顯示這筆本地操作已在派發前停止，沒有發起付款。" };
    if (status === "FAILED") return {
      button: "停止後續代辦", title: "停止後續代辦？",
      summary: "本地付款已確認失敗，後續代辦可以停止；此處不會另建付款。",
      message: "目前記錄顯示本地付款失敗。停止將撤銷後續代辦權限，保留原任務與操作記錄；不會自動重新付款。" };
    return { button: "停止這次委託", title: "停止這次委託？",
      summary: "方案已確認，但入隊不等於付款。尚未派發時，可停止付款發起。",
      message: "停止要求會直達後端，撤銷後續代辦權限，阻止尚未派發的命令。若要求到達前已取得派發權，會保留實際付款狀態，不能保證付款未發生。" };
  }
  function renderUserValue(task) {
    const metrics = task.metrics;
    $("user-value-panel").hidden = !buyer() || !metrics;
    if (!buyer() || !metrics) return;
    clear($("user-value-counts"));
    for (const [field, label] of [["input_count", "交代需求"], ["answer_count", "補答或修改"], ["approval_count", "確認方案"]]) {
      const count = metrics[field]; const card = el("div", null, "value-count");
      card.append(el("strong", Number.isInteger(count) && count >= 0 ? `${count} 次` : "待核實"), el("span", label));
      $("user-value-counts").append(card);
    }
  }
  function detail(parent, label, value, strong = false) { parent.append(el("dt", label), el("dd", value, strong ? "strong-value" : "")); }
  function renderConfirmation(task) {
    const snapshot = getSnapshot(task); const quote = getQuote(task); const proposal = task.proposal || {}; const status = taskStatus(task);
    const visible = buyer() && snapshot.snapshot_id && !getOperation(task) && !["stopped", "cancelled", "blocked", "running", "queued", "clarifying"].includes(status);
    $("confirmation-panel").hidden = !visible; if (!visible) return;
    const calculation = snapshot.calculation || proposal.calculation || {};
    const wasOpen = $("confirmation-details").querySelector("details")?.open || false;
    clear($("confirmation-details"));
    const list = el("dl", null, "detail-table");
    const more = el("details", null, "technical-details"), moreList = el("dl", null, "detail-table");
    more.open = wasOpen; more.append(el("summary", "完整規格與方案記錄"), moreList);
    detail(list, "商品及規格", productText(quote.product));
    detail(moreList, "包裝／產地", `${asText(quote.product?.packaging)} ／ ${asText(quote.product?.origin)}`);
    detail(list, "銷售商戶（合成）", asText(quote.merchant_name || quote.merchant_id));
    detail(moreList, "商戶商品 SKU", asText(quote.merchant_sku));
    detail(list, "實際購買", `${asText(quote.purchase_quantity)} 件包裝 · 每件 ${asText(quote.product?.pack_count)} 個裝`);
    detail(list, "收款方（合成）", asText(snapshot.payee_ref || quote.payee_ref));
    detail(list, "收貨地點", asText(quote.destination_ref || snapshot.draft?.destination_ref));
    detail(list, "付款方式", verifiedLocalOptions(quote).length ? "LocalPSP 本地演練" : "待核實");
    detail(list, "商品金額", money(calculation.goods_minor ?? quote.line_subtotal_minor));
    detail(list, "費用／即時優惠", `${money(calculation.fees_minor)} ／ −${money(calculation.discount_minor)}`);
    detail(list, "本次批准的現金總額", money(calculation.cash_minor ?? calculation.cash_total_minor, quote.currency), true);
    detail(list, "配送及退貨", `${deliveryText(quote.delivery)}；${returnText(quote.returns)}`);
    detail(moreList, "預計獎勵", "不扣減本次現金預算；未核實的權益不納入計算。");
    detail(list, "方案有效至", `${timestamp(snapshot.expires_at, true)}（香港時間）`);
    detail(moreList, "方案對應記錄", `${snapshot.snapshot_id} · 條件版本 ${snapshot.constraints_version ?? task.constraints_version}`);
    $("confirmation-details").append(list, more);
    const hasChallenge = proposal.challenge_id || snapshot.challenge_id || task.challenge_id;
    $("approve-button").disabled = state.busy || state.healthUnavailable || stopUnresolved() || !$("approve-consent").checked || !hasChallenge;
    if (!hasChallenge) $("confirmation-details").append(el("p", "服務端尚未提供確認憑據，暫不能批准此方案。", "quote-warning"));
  }
  function renderOperation(task) {
    const operation = getOperation(task); $("operation-panel").hidden = !operation; if (!operation) return;
    const wasOpen = $("operation-details").querySelector("details")?.open || false;
    clear($("operation-details"));
    const payment = paymentStatus(task);
    const operationState = String(operation.state || operation.status || "UNKNOWN").toUpperCase();
    const pending = ["UNKNOWN", "DISPATCH_COMMITTED", "CLAIMED"].includes(payment);
    const banner = el("div", null, `operation-status${pending ? " warning" : ""}`);
    const title = payment === "UNKNOWN" ? "本地付款結果待核實" : payment === "SUCCEEDED" ? "本地付款模擬完成，尚未證明購買完成" : payment === "STOPPED" ? "已在派發前停止" : paymentStates[payment] || statusText(operationState);
    banner.append(el("strong", title));
    const description = payment === "UNKNOWN" ? "目前頁面只讀取已保存的狀態，尚未向支付機構查單。請保留原操作，核實前不要另建付款。" : pending ? "請求可能已送出；停止代辦不能證明付款已取消。此原型尚未接入外部會話取消或查單。" : payment === "STOPPED" ? "服務端記錄顯示，這筆本地操作在取得派發權前已停止，沒有發起付款。" : payment === "SUCCEEDED" ? "這是 LocalPSP 本地模擬結果，沒有移動真實資金。付款、商戶接受訂單及退款是不同狀態。" : payment === "FAILED" ? "本地付款已確認失敗，沒有官方錢包付款回執。保留原操作記錄；此頁不會自動另建付款。" : "入隊只代表已保存委託操作。本地付款不會自動派發，沒有官方錢包付款回執。";
    banner.append(el("p", description));
    const stopped = (task.events || []).some(event => event.kind === "user_stop_requested");
    if (stopped && pending) banner.append(el("p", "你已要求停止後續代辦；原付款狀態仍未確認，不能視為已取消付款。"));
    if (stopped && payment === "SUCCEEDED") banner.append(el("p", "你已要求停止後續代辦；此要求不會自動退款。"));
    $("operation-details").append(banner);
    const states = el("div", null, "transaction-states");
    for (const [label, value] of [
      ["委託操作", statusText(operationState)],
      ["付款", paymentStates[payment] || "狀態待核實"],
      ["商戶訂單", operation.order_status === "NOT_CREATED" ? "尚未建立" : asText(operation.order_status, "未提供")]
    ]) { const card = el("div", null, "transaction-state"); card.append(el("span", label), el("strong", value)); states.append(card); }
    $("operation-details").append(states);
    const list = el("dl", null, "detail-table");
    detail(list, "原操作編號", asText(operation.operation_id));
    detail(list, "已批准方案", asText(operation.snapshot_id));
    detail(list, "委託編號", asText(operation.mandate_id));
    detail(list, "委託操作狀態", statusText(operationState));
    detail(list, "付款狀態", paymentStates[payment] || "狀態待核實");
    detail(list, "商戶訂單狀態", operation.order_status === "NOT_CREATED" ? "尚未建立商戶訂單" : asText(operation.order_status, "未提供"));
    detail(list, "退款", "目前應用未接入；沒有已受理的退款申請");
    if (buyer()) {
      detail(list, "銷售商戶／收款方", `${asText(operation.merchant_id)} ／ ${asText(operation.payee_ref)}（合成）`);
      detail(list, "商品／費用／即時優惠", `${money(operation.goods_minor, operation.currency)} ／ ${money(operation.fees_minor, operation.currency)} ／ −${money(operation.discount_minor, operation.currency)}`);
      detail(list, "已批准的現金總額", money(operation.cash_minor ?? operation.amount_minor ?? operation.cash_total_minor, operation.currency), true);
      detail(list, "金額含義", payment === "SUCCEEDED" ? "本地模擬付款已記錄；沒有真實扣款" : "批准金額不等於已扣款；實際結果以付款狀態為準");
    }
    if (operation.provider_ref) detail(list, "本地付款模擬記錄", asText(operation.provider_ref));
    if (operation.stop_reason) detail(list, "停止原因", ({ USER_STOP: "使用者要求停止", RULE_STOP: "購買條件未通過", CONSTRAINTS_CHANGED: "購買條件已修改" })[operation.stop_reason] || "條件變更或核驗未通過，查看原記錄");
    const more = el("details", null, "technical-details"); more.open = wasOpen;
    more.append(el("summary", "金額與操作記錄"), list);
    $("operation-details").append(more);
  }
  function renderEvents(task) {
    const events = task.events || task.audit || [];
    clear($("event-list"));
    if (!events.length) { const item = el("li"); item.append(el("time", "—"), el("strong", "暫未提供事件記錄；重新連線可讀取最新狀態。")); $("event-list").append(item); return; }
    for (const event of [...events].slice(-24).reverse()) {
      const item = el("li"); const name = event.kind || event.type || event.event_type || event.event || event.action || "事件";
      const time = el("time", timestamp(event.at || event.timestamp || event.created_at));
      const body = el("div"); body.append(el("strong", eventNames[normalized(name)] || "任務記錄已更新"));
      const details = event.details || {};
      const changed = [];
      const answers = details.answers && typeof details.answers === "object" ? details.answers : {};
      const changedNames = Array.isArray(details.fields) ? details.fields : Object.keys(answers);
      for (const field of changedNames) {
        if (field === "product") {
          const product = answers.product;
          const productFields = product && typeof product === "object" ? Object.keys(product) : [];
          if (productFields.length) productFields.forEach(key => changed.push(fields[`product.${key}`]?.label || "商品規格"));
          else changed.push("商品規格");
        } else changed.push(fields[field]?.label || "其他購買條件");
      }
      const changedSummary = [...new Set(changed)].join("、");
      const outcome = statuses[normalized(details.outcome)] || "已處理，請查看任務狀態";
      const friendly = {
        user_input: details.mode === "scripted" ? "已選擇本地流程演練，本輪不呼叫真實模型。" : "已選擇真實 DeepSeek；如配置不齊，會明確停止。",
        user_answer: `${changedSummary ? `已補充或修改${changedSummary}。` : "補充資料已保存。"}將按最新條件重新核對，舊方案不再適用。`,
        model_run_queued: "需求已保存，等待處理。",
        model_run_claimed: "已開始核對這份任務的購買條件。",
        model_run_finished: `處理結果：${outcome}。`,
        stale_model_result_discarded: "新條件已生效，較早的模型結果不會覆蓋。",
        user_approved: "你確認的方案已綁定至委託及交易命令。",
        user_stop_requested: "已要求後端停止；是否已派發付款，以目前操作狀態為準。"
      };
      body.append(el("p", friendly[normalized(name)] || "相關記錄已保存，可展開查看對應記錄。"));
      item.append(time, body); $("event-list").append(item);
    }
  }
  function numericInput(id, multiplier = 1) { const text = $(id).value.trim(); if (!text) return undefined; return Math.round(Number(text) * multiplier * 1000000) / 1000000; }
  function initialFields() {
    const draft = { preference: $("preference").value, product: {} };
    const inputs = { purchase_quantity: ["purchase-quantity", true], cash_cap_minor: ["cash-cap", true, 100], destination_ref: ["destination"], "product.brand": ["product-brand"], "product.variant": ["product-variant"], "product.net_content": ["net-content", true], "product.unit": ["content-unit"], "product.pack_count": ["pack-count", true], "product.packaging": ["product-packaging"], "product.origin": ["product-origin"], "product.region_version": ["region-version"] };
    Object.entries(inputs).forEach(([name, [id, numeric, multiplier]]) => { let value = numeric ? numericInput(id, multiplier || 1) : $(id).value.trim(); if (name === "cash_cap_minor" && value !== undefined) value = Math.round(value); if (value !== undefined && value !== "") setAt(draft, name, value); });
    if (!Object.keys(draft.product).length) delete draft.product;
    draft.requires_change_of_mind_return = $("require-returns").value === "true";
    if ($("delivery-deadline").value) draft.latest_delivery_epoch = Math.floor(new Date(`${$("delivery-deadline").value}:00+08:00`).getTime() / 1000);
    return draft;
  }
  function repeatTask() {
    if (!canRepeatTask()) return;
    const draft = structuredClone(state.task.draft || {});
    resetNewTask();
    $("task-text").value = draft.text || "";
    const mappings = { purchase_quantity: "purchase-quantity", cash_cap_minor: "cash-cap", destination_ref: "destination", preference: "preference", "product.brand": "product-brand", "product.variant": "product-variant", "product.net_content": "net-content", "product.unit": "content-unit", "product.pack_count": "pack-count", "product.packaging": "product-packaging", "product.origin": "product-origin", "product.region_version": "region-version" };
    Object.entries(mappings).forEach(([name, id]) => { const value = valueAt(draft, name); if (value !== undefined && value !== null) $(id).value = name === "cash_cap_minor" ? (Number(value) / 100).toFixed(2) : String(value); });
    $("require-returns").value = String(draft.requires_change_of_mind_return === true);
    $("delivery-deadline").value = "";
    showNotice("已帶入上次的商品規格、數量、預算和偏好。請核對本次需要，重新設定送達期限；我們會重新查價，付款授權需重新確認。尚未建立新任務。");
    $("new-task-panel").scrollIntoView({ behavior: "smooth", block: "start" });
  }
  function applyTemplate() {
    const template = state.config.demo_template; if (!template) return;
    const draft = template.fields || template.draft || template;
    document.querySelector('input[name="mode"][value="scripted"]').checked = true; updateComposerMode();
    $("task-text").value = template.text || draft.text || "合成商品演練：補購指定規格，在預算內按所選偏好比較。";
    const mappings = { purchase_quantity: "purchase-quantity", cash_cap_minor: "cash-cap", destination_ref: "destination", preference: "preference", "product.brand": "product-brand", "product.variant": "product-variant", "product.net_content": "net-content", "product.unit": "content-unit", "product.pack_count": "pack-count", "product.packaging": "product-packaging", "product.origin": "product-origin", "product.region_version": "region-version" };
    Object.entries(mappings).forEach(([name, id]) => { const value = valueAt(draft, name); if (value !== undefined && value !== null) $(id).value = name === "cash_cap_minor" ? (Number(value) / 100).toFixed(2) : String(value); });
    showNotice("已填入明確標示的合成商品範例。你可以修改條件；這不代表已找到真實香港商品。修訂後請再提交任務。");
    $("task-text").focus();
  }

  $("login-form").addEventListener("submit", event => { event.preventDefault(); const accessCode = $("access-code").value; mutate("/session", { access_code: accessCode }, async result => { $("access-code").value = ""; await setSession(result); }); });
  $("logout-button").addEventListener("click", () => mutate("/session", {}, async () => { showLogin(); clearNotice(); }, "DELETE"));
  $("task-form").addEventListener("submit", event => {
    event.preventDefault(); const mode = document.querySelector('input[name="mode"]:checked').value;
    const body = { text: $("task-text").value.trim(), fields: initialFields(), mode, scenario: $("scenario").value };
    mutate("/intents", body, async result => { state.editing = false; state.questionKey = null; acceptTask(unwrapTask(result)); await refreshList(); $("task-panel").scrollIntoView({ behavior: "smooth", block: "start" }); });
  });
  $("task-form").addEventListener("invalid", event => {
    // A folded optional section must not hide the browser's validation target.
    let parent = event.target.parentElement;
    while (parent) { if (parent.tagName === "DETAILS") parent.open = true; parent = parent.parentElement; }
  }, true);
  $("answers-form").addEventListener("submit", event => {
    event.preventDefault(); if (!state.task) return; const answers = {};
    $("question-fields").querySelectorAll("[data-field]").forEach(input => {
      if (!input.value.trim()) return; const definition = fields[input.dataset.field] || {}; let value = input.value.trim();
      if (definition.money) value = Math.round(Number(value) * 100);
      else if (definition.epoch) value = Math.floor(new Date(`${value}:00+08:00`).getTime() / 1000);
      else if (definition.type === "number") value = Number(value);
      else if (definition.boolean) value = value === "true";
      setAt(answers, input.dataset.field, value);
    });
    const body = { expected_state_version: state.task.state_version, base_constraints_version: state.task.constraints_version, answers };
    mutate(`/tasks/${encodeURIComponent(state.task.task_id)}/answers`, body, async result => { state.editing = false; state.questionKey = null; acceptTask(unwrapTask(result)); showNotice("補充資料已保存，將按更新後的條件重新核對。"); await refreshList(); });
  });
  $("approve-consent").addEventListener("change", () => renderConfirmation(state.task));
  $("approve-button").addEventListener("click", () => {
    if (!state.task || !$("approve-consent").checked) return;
    const snapshot = getSnapshot(state.task); const proposal = state.task.proposal || {};
    const body = { snapshot_id: snapshot.snapshot_id, challenge_id: proposal.challenge_id || snapshot.challenge_id || state.task.challenge_id, expected_state_version: state.task.state_version };
    mutate(`/tasks/${encodeURIComponent(state.task.task_id)}/approvals`, body, async result => { acceptTask(unwrapTask(result)); $("approve-consent").checked = false; showNotice("已確認此方案。請查看入隊命令與對應方案的記錄；入隊不代表付款成功。"); await refreshList(); });
  });
  $("resume-button").addEventListener("click", () => { if (state.task) mutate(`/tasks/${encodeURIComponent(state.task.task_id)}/runs`, { expected_state_version: state.task.state_version }, async result => { acceptTask(unwrapTask(result)); await refreshList(); }); });
  $("edit-task-button").addEventListener("click", () => { state.editing = !state.editing; state.questionKey = null; renderQuestions(state.task); if (state.editing) $("clarification-panel").scrollIntoView({ behavior: "smooth", block: "start" }); });
  function dismissStopDialog() {
    state.stopDialog = null;
    const dialog = $("stop-dialog"); dialog.returnValue = "";
    if (dialog.open) dialog.close("");
  }
  $("stop-button").addEventListener("click", () => {
    if (!buyer() || !state.task || $("stop-button").disabled) return;
    const dialog = $("stop-dialog"), copy = stopCopy(state.task);
    dialog.returnValue = "";
    state.stopDialog = { taskId: state.task.task_id, sessionEpoch: state.sessionEpoch };
    $("stop-dialog-title").textContent = copy.title; $("stop-dialog-message").textContent = copy.message;
    renderStopControls(); dialog.showModal();
  });
  $("stop-dialog").querySelector("form").addEventListener("submit", event => {
    event.preventDefault();
    const target = state.stopDialog;
    const confirmed = event.submitter === $("confirm-stop-button") && target && target.sessionEpoch === state.sessionEpoch && target.taskId === state.task?.task_id;
    // Only this explicit submitter authorizes a request. Esc/close/old
    // returnValue never do. The target is captured before closing the dialog.
    dismissStopDialog();
    if (confirmed) void sendStop(target.taskId);
  });
  $("stop-dialog").addEventListener("cancel", () => { state.stopDialog = null; $("stop-dialog").returnValue = ""; });
  $("stop-dialog").addEventListener("close", () => { if (!$("stop-dialog").open) { state.stopDialog = null; $("stop-dialog").returnValue = ""; } });
  $("new-task-button").addEventListener("click", resetNewTask);
  $("compose-button")?.addEventListener("click", resetNewTask);
  document.querySelectorAll('input[name="mode"]').forEach(input => input.addEventListener("change", updateComposerMode));
  $("quote-prev")?.addEventListener("click", () => moveQuote(-1));
  $("quote-next")?.addEventListener("click", () => moveQuote(1));
  $("quote-grid").addEventListener("scroll", updateQuoteNavigation, { passive: true });
  window.addEventListener("resize", updateQuoteNavigation);
  $("repeat-task-button").addEventListener("click", repeatTask);
  $("refresh-list-button").addEventListener("click", refreshList);
  $("refresh-task-button").addEventListener("click", () => refreshTask(true));
  $("refresh-operation-button").addEventListener("click", () => refreshTask(true));
  $("inspect-operation-button").addEventListener("click", () => { if ($("activity-details")) $("activity-details").open = true; $("execution-records").open = true; $("records-panel").scrollIntoView({ behavior: "smooth", block: "start" }); $("execution-records").querySelector("summary").focus(); });
  $("demo-template-button").addEventListener("click", applyTemplate);
  document.addEventListener("visibilitychange", () => { if (!document.hidden && state.task) refreshTask(); });

  async function init() {
    void refreshHealth();
    void loadSocialLogin();
    syncView();
    try { state.config = await request("/config"); $("demo-template-button").hidden = !state.config.demo_template; setModelBadge(); defaultComposerMode(); }
    catch (error) { connection("暫時無法取得應用配置。請確認本地服務已啟動，再重新整理。"); }
    try { await setSession(await request("/session"), true); }
    catch (error) { showLogin(); if (error.status !== 401) $("login-feedback").textContent = error.message; }
    const locationUrl = new URL(window.location.href);
    if (locationUrl.searchParams.has("login")) {
      if (locationUrl.searchParams.get("login") === "retry") $("login-feedback").textContent = "登入未完成，請重新選擇登入方式。";
      locationUrl.searchParams.delete("login");
      history.replaceState(null, "", locationUrl.pathname + locationUrl.search + locationUrl.hash);
    }
  }
  init();
})();
