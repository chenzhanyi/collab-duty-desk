(() => {
  "use strict";
  const drawer = document.getElementById("queue-drawer");
  if (!drawer) return;
  const $ = id => document.getElementById(id);
  const escape = value => String(value || "").replace(/[&<>"']/g, char => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char]));
  const number = value => String(value).padStart(2, "0");
  let state = null;
  let candidates = [];
  let selected = new Set();
  let tab = "order";
  let busy = false;
  let timer;
  let epoch = 0;
  let opener;
  let drafts = [{ title: "", idea: "" }];
  let requestId = crypto.randomUUID();
  let previousFolder = "";
  const labels = { ready: "准备就绪", running: "正在执行", pausing: "等待暂停", paused: "已暂停", blocked: "需要处理", completed: "全部完成" };

  function feedback(message = "", error = false) {
    const node = $("queue-feedback");
    node.hidden = !message;
    node.textContent = message;
    node.classList.toggle("is-error", error);
    node.setAttribute("role", error ? "alert" : "status");
  }

  function selectTab(name, focus = false) {
    tab = name;
    drawer.querySelectorAll("[data-queue-tab]").forEach(button => {
      const active = button.dataset.queueTab === name;
      button.setAttribute("aria-selected", String(active));
      button.tabIndex = active ? 0 : -1;
      if (active && focus) button.focus();
      $("queue-" + button.dataset.queueTab).hidden = !active;
    });
    if (name === "add") refresh(true);
    if (name === "batch") paintDraftsIfEmpty();
  }

  function open(name = "order") {
    opener = document.activeElement;
    if (!drawer.open) drawer.showModal();
    document.body.classList.add("queue-is-open");
    selectTab(name);
    refresh(name === "add");
  }
  drawer.addEventListener("close", () => {
    document.body.classList.remove("queue-is-open");
    if (opener && opener.isConnected) opener.focus({ preventScroll: true });
  });
  drawer.addEventListener("click", event => { if (event.target === drawer && event.clientX < drawer.getBoundingClientRect().left) drawer.close(); });
  document.addEventListener("click", event => {
    if (event.target.closest("[data-queue-open]")) open();
    if (event.target.closest("[data-queue-compose]")) { drawer.open ? selectTab("batch") : open("batch"); }
    if (event.target.closest("[data-queue-choose]")) selectTab("add");
    if (event.target.closest("[data-queue-close]")) drawer.close();
  });
  drawer.querySelectorAll("[data-queue-tab]").forEach(button => {
    button.addEventListener("click", () => selectTab(button.dataset.queueTab));
    button.addEventListener("keydown", event => {
      const tabs = ["order", "add", "batch"];
      let index = tabs.indexOf(tab);
      if (event.key === "ArrowRight") index = (index + 1) % tabs.length;
      else if (event.key === "ArrowLeft") index = (index + tabs.length - 1) % tabs.length;
      else if (event.key === "Home") index = 0;
      else if (event.key === "End") index = 2;
      else return;
      event.preventDefault();
      selectTab(tabs[index], true);
    });
  });

  async function refresh(withCandidates = false) {
    window.clearTimeout(timer);
    const generation = epoch;
    try {
      const response = await fetch("/api/queue" + (withCandidates ? "?candidates=1" : ""), { cache: "no-store", headers: { Accept: "application/json" } });
      const data = await response.json();
      if (generation !== epoch) return;
      if (!response.ok || !data.ok) throw new Error(data.message || "队列状态暂时无法读取。");
      paint(data);
    } catch (error) {
      if (generation === epoch && drawer.open) feedback(error instanceof TypeError ? "连接暂时不可用，恢复后会自动更新队列。" : error.message, true);
    } finally {
      window.clearTimeout(timer);
      timer = window.setTimeout(() => refresh(drawer.open && tab === "add"), document.hidden ? 8000 : (state && state.active ? 1500 : 5000));
    }
  }

  async function action(name, fields = {}) {
    if (busy) return null;
    busy = true;
    epoch += 1;
    feedback();
    const buttons = [...drawer.querySelectorAll("#queue-start, #queue-pause, #queue-add-selected, #queue-save-batch, #queue-clear, [data-queue-move], [data-queue-remove]")];
    const disabled = buttons.map(button => button.disabled);
    buttons.forEach(button => { button.disabled = true; });
    drawer.setAttribute("aria-busy", "true");
    try {
      const response = await fetch("/api/queue/" + name, {
        method: "POST", headers: { Accept: "application/json", "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8" },
        body: new URLSearchParams(fields)
      });
      const data = await response.json();
      if (!response.ok || !data.ok) throw new Error(data.message || "操作未完成，请刷新队列后重试。");
      paint(data);
      return data;
    } catch (error) {
      feedback(error instanceof TypeError ? "连接中断，请先查看队列是否已更新，再重试；当前草稿已保留。" : error.message, true);
      return null;
    } finally {
      busy = false;
      drawer.removeAttribute("aria-busy");
      buttons.forEach((button, index) => { if (button.isConnected) button.disabled = disabled[index]; });
      if (state) paintControls();
      refresh(drawer.open && tab === "add");
    }
  }

  function paintControls() {
    $("queue-start").hidden = state.active;
    $("queue-start").disabled = busy || !state.pending;
    $("queue-start").textContent = ["paused", "blocked"].includes(state.state) ? "继续执行 →" : "开始执行 →";
    $("queue-pause").hidden = !state.active;
    $("queue-pause").disabled = busy || state.state === "pausing";
    $("queue-pause").textContent = state.state === "pausing" ? "当前任务结束后暂停…" : "当前任务完成后暂停";
    $("queue-footer-hint").textContent = state.active ? "关闭面板或切换页面，队列仍会继续执行。" : "顺序自动保存，点击开始后才会执行。";
    $("queue-add-selected").disabled = busy || !selected.size;
  }

  function paint(data) {
    if (state && data.revision < state.revision) return;
    const old = state;
    state = data;
    const active = data.items.find(item => item.status === "running");
    const waiting = data.items.filter(item => ["pending", "blocked"].includes(item.status));
    const done = data.items.filter(item => item.status === "completed");
    document.querySelectorAll("[data-queue-count]").forEach(node => { node.textContent = data.pending + Number(Boolean(active)); node.classList.toggle("is-live", data.active); });
    document.querySelectorAll("[data-queue-summary]").forEach(node => { node.textContent = active ? `正在处理 ${active.id} · ${active.title}` : (data.items.length ? `${labels[data.state] || "执行队列"} · ${data.pending} 张待处理` : "把任务排好，一次开始。"); });
    document.querySelectorAll("[data-queue-preview]").forEach(node => { node.textContent = waiting.length ? "接下来：" + waiting.slice(0, 3).map(item => item.id + " " + item.title).join(" → ") + (waiting.length > 3 ? ` · 另 ${waiting.length - 3} 张` : "") : (data.items.length ? data.message : "批量布置 · 按序执行 · 审核通过后继续下一张"); });
    $("queue-total").textContent = number(data.items.length);
    $("queue-state-label").textContent = labels[data.state] || "准备就绪";
    $("queue-state-label").dataset.state = data.state;
    $("queue-progress-text").textContent = data.items.length ? `已完成 ${done.length} / ${data.items.length}` : "还没有待处理任务";
    $("queue-progress-fill").style.width = (data.items.length ? done.length / data.items.length * 100 : 0) + "%";
    $("queue-policy").textContent = ["blocked", "paused", "pausing"].includes(data.state) ? data.message : "每张任务审核通过后，才会执行下一张。遇到失败或须修正时暂停。";
    if (!old || JSON.stringify(active) !== JSON.stringify(old.items.find(item => item.status === "running"))) {
      $("queue-current").innerHTML = active ? `<article class="queue-current-card"><div class="queue-current-top"><span><i></i> 正在处理</span><code>${escape(active.id)}</code></div><a href="${escape(active.url)}" class="queue-current-title">${escape(active.title)} <span>↗</span></a><div class="queue-current-stage"><span class="queue-live-bars" aria-hidden="true"><i></i><i></i><i></i></span><strong>${escape(active.stage)}</strong></div><p>${escape(active.message)}</p><a class="queue-current-link" href="${escape(active.url)}#workflow">查看当前任务进度 →</a></article>` : "";
    }
    $("queue-empty").hidden = data.items.length > 0;
    $("queue-waiting-count").textContent = waiting.length + " 张待处理";
    drawer.querySelector(".queue-list-heading").hidden = !waiting.length;
    if (!old || old.order_revision !== data.order_revision || JSON.stringify(old.items.filter(item => item.status === "blocked")) !== JSON.stringify(data.items.filter(item => item.status === "blocked"))) {
      const focused = document.activeElement;
      const focusId = focused && focused.dataset.queueId;
      const focusMove = focused && focused.dataset.queueMove;
      $("queue-list").innerHTML = waiting.map((item, index) => `<li class="queue-item${item.status === "blocked" ? " is-blocked" : ""}"><span class="queue-sequence">${number(done.length + Number(Boolean(active)) + index + 1)}</span><div class="queue-item-content"><div class="queue-item-meta"><code>${escape(item.id)}</code><span>${item.status === "blocked" ? "需要处理" : (index === 0 && active ? "下一张" : "等待执行")}</span></div><a class="queue-item-title" href="${escape(item.url)}">${escape(item.title)}</a>${item.status === "blocked" ? `<p class="queue-item-error">${escape(item.message)}</p><a class="text-link" href="${escape(item.url)}#workflow">处理这张任务 ↗</a>` : ""}</div><div class="queue-item-tools"><button type="button" data-queue-move="up" data-queue-id="${escape(item.id)}" aria-label="上移 ${escape(item.id)}" ${index === 0 ? "disabled" : ""}>↑</button><button type="button" data-queue-move="down" data-queue-id="${escape(item.id)}" aria-label="下移 ${escape(item.id)}" ${index === waiting.length - 1 ? "disabled" : ""}>↓</button><button type="button" data-queue-remove data-queue-id="${escape(item.id)}" aria-label="移出 ${escape(item.id)}">×</button></div></li>`).join("");
      if (focusId) {
        const next = [...$("queue-list").querySelectorAll("button")].find(button => button.dataset.queueId === focusId && button.dataset.queueMove === focusMove && !button.disabled);
        if (next) next.focus({ preventScroll: true });
      }
      $("queue-completed").innerHTML = done.map(item => `<li><span>✓</span><a href="${escape(item.url)}"><code>${escape(item.id)}</code> ${escape(item.title)}</a></li>`).join("");
    }
    $("queue-finished").hidden = !done.length;
    $("queue-finished-label").textContent = "已完成 " + done.length + " 张";
    if (data.candidates) {
      const changed = JSON.stringify(candidates) !== JSON.stringify(data.candidates);
      candidates = data.candidates;
      const available = new Set(candidates.map(item => item.id));
      selected = new Set([...selected].filter(id => available.has(id)));
      if (changed) paintCandidates();
    }
    paintControls();
  }

  $("queue-start").addEventListener("click", () => action("start"));
  $("queue-pause").addEventListener("click", () => action("pause"));
  $("queue-clear").addEventListener("click", () => action("clear_completed", { order_revision: state.order_revision }));
  $("queue-list").addEventListener("click", event => {
    const move = event.target.closest("[data-queue-move]");
    const remove = event.target.closest("[data-queue-remove]");
    if (busy || !state) return;
    if (remove) action("remove", { id: remove.dataset.queueId, order_revision: state.order_revision });
    if (move) {
      const ids = state.items.filter(item => ["pending", "blocked"].includes(item.status)).map(item => item.id);
      const index = ids.indexOf(move.dataset.queueId);
      const next = index + (move.dataset.queueMove === "up" ? -1 : 1);
      if (next < 0 || next >= ids.length) return;
      [ids[index], ids[next]] = [ids[next], ids[index]];
      action("reorder", { ids: JSON.stringify(ids), order_revision: state.order_revision });
    }
  });

  function paintCandidates() {
    const query = $("queue-search").value.trim().toLocaleLowerCase();
    const filtered = candidates.filter(item => (item.id + " " + item.title).toLocaleLowerCase().includes(query));
    $("queue-candidates").innerHTML = filtered.length ? filtered.map(item => `<label class="queue-candidate"><input type="checkbox" value="${escape(item.id)}" ${selected.has(item.id) ? "checked" : ""}><span><span class="queue-candidate-meta"><code>${escape(item.id)}</code><small>${escape(item.label)}</small></span><strong>${escape(item.title)}</strong></span><span class="queue-candidate-check" aria-hidden="true">✓</span></label>`).join("") : `<p class="queue-no-candidates">${query ? "没有匹配的任务，换个关键词试试。" : "没有可添加的任务。可以切换到「批量布置」，直接安排新任务。"}</p>`;
    $("queue-selection-count").textContent = selected.size ? "已选 " + selected.size + " 张 · 按编号顺序加入，可再调整" : "尚未选择任务";
    $("queue-add-selected").disabled = busy || !selected.size;
  }
  $("queue-search").addEventListener("input", paintCandidates);
  $("queue-candidates").addEventListener("change", event => {
    if (event.target.matches("input[type=checkbox]")) {
      event.target.checked ? selected.add(event.target.value) : selected.delete(event.target.value);
      $("queue-selection-count").textContent = selected.size ? "已选 " + selected.size + " 张 · 按编号顺序加入，可再调整" : "尚未选择任务";
      $("queue-add-selected").disabled = busy || !selected.size;
    }
  });
  $("queue-add-selected").addEventListener("click", async () => {
    const ids = [...selected].sort((a, b) => Number(a.slice(1)) - Number(b.slice(1)));
    if (await action("add", { ids: JSON.stringify(ids) })) {
      selected.clear();
      selectTab("order");
      feedback("已加入队列，可以调整顺序后开始执行。");
    }
  });

  function saveDrafts() {
    try { localStorage.setItem("collab:queueDraft", JSON.stringify({ drafts, work_mode: $("queue-work-mode").value, workdir: $("queue-workdir").value, requestId })); }
    catch (_) { $("queue-draft-note").textContent = "浏览器无法保存草稿，离开页面前请先保存到队列。"; }
  }
  function paintDraftsIfEmpty() { if (!$("queue-drafts").children.length) paintDrafts(); }
  function paintDrafts() {
    $("queue-drafts").innerHTML = drafts.map((draft, index) => `<fieldset class="queue-draft"><legend><span>${number(index + 1)}</span>任务 ${index + 1}</legend><button class="queue-draft-remove" type="button" data-draft-remove="${index}" aria-label="移除草稿任务 ${index + 1}" ${drafts.length === 1 ? "hidden" : ""}>×</button><label for="queue-draft-title-${index}">任务标题 <small>可选，由总控补充</small></label><input class="field-input" id="queue-draft-title-${index}" data-draft-index="${index}" data-draft-field="title" maxlength="120" value="${escape(draft.title)}" placeholder="例如：先整理现有数据"><label for="queue-draft-idea-${index}">要完成什么</label><textarea class="field-input" id="queue-draft-idea-${index}" data-draft-index="${index}" data-draft-field="idea" rows="4" maxlength="120000" required placeholder="描述目标、验收要求和限制…">${escape(draft.idea)}</textarea></fieldset>`).join("");
    $("queue-add-draft").disabled = drafts.length >= 20;
  }
  function workModeChanged() {
    const mode = $("queue-work-mode").value;
    const input = $("queue-workdir");
    if (mode === "self") {
      if (input.value !== drawer.dataset.selfRoot) previousFolder = input.value;
      input.value = drawer.dataset.selfRoot;
    } else if (input.value === drawer.dataset.selfRoot) input.value = previousFolder;
    input.readOnly = mode === "self";
    $("queue-pick-folder").hidden = mode === "self";
    $("queue-folder-hint").textContent = mode === "self" ? "所有任务会按顺序维护当前工作台，交付记录单独保存。" : (mode === "isolated" ? "所有任务会按顺序在这个文件夹生成独立产物。" : "所有任务会按顺序维护这份项目源码。");
  }
  try {
    const draft = JSON.parse(localStorage.getItem("collab:queueDraft") || "null");
    if (draft && Array.isArray(draft.drafts) && draft.drafts.length && draft.drafts.length <= 20 && draft.drafts.every(item => item && typeof item.idea === "string" && typeof item.title === "string")) {
      drafts = draft.drafts;
      requestId = draft.requestId || requestId;
      if (["project", "self", "isolated"].includes(draft.work_mode)) $("queue-work-mode").value = draft.work_mode;
      $("queue-workdir").value = draft.workdir || "";
      $("queue-draft-note").textContent = "已恢复批量草稿 · 最多 20 张。";
    }
  } catch (_) { /* Optional local draft storage. */ }
  workModeChanged();
  $("queue-batch-form").addEventListener("input", event => {
    if (busy) return;
    const field = event.target;
    if (field.dataset.draftIndex !== undefined) drafts[Number(field.dataset.draftIndex)][field.dataset.draftField] = field.value;
    requestId = crypto.randomUUID();
    saveDrafts();
  });
  $("queue-work-mode").addEventListener("change", () => { workModeChanged(); requestId = crypto.randomUUID(); saveDrafts(); });
  $("queue-add-draft").addEventListener("click", () => {
    if (drafts.length >= 20 || busy) return;
    drafts.push({ title: "", idea: "" });
    requestId = crypto.randomUUID();
    paintDrafts(); saveDrafts();
    $("queue-draft-idea-" + (drafts.length - 1)).focus();
  });
  $("queue-drafts").addEventListener("click", event => {
    const button = event.target.closest("[data-draft-remove]");
    if (button && drafts.length > 1 && !busy) {
      drafts.splice(Number(button.dataset.draftRemove), 1);
      requestId = crypto.randomUUID();
      paintDrafts(); saveDrafts();
    }
  });
  $("queue-pick-folder").addEventListener("click", async () => {
    const button = $("queue-pick-folder");
    const mode = $("queue-work-mode").value;
    const original = $("queue-workdir").value;
    button.disabled = true;
    try {
      const response = await fetch("/api/pick-folder?work_mode=" + encodeURIComponent(mode), { method: "POST", headers: { Accept: "application/json" } });
      const data = await response.json();
      if (mode !== $("queue-work-mode").value || original !== $("queue-workdir").value) return;
      if (!response.ok || !data.ok || !data.valid) { if (!data.cancelled) feedback(data.error || "未能选择文件夹，也可以直接填写路径。", true); return; }
      $("queue-workdir").value = data.path;
      requestId = crypto.randomUUID(); saveDrafts(); feedback();
    } catch (_) { feedback("未能打开文件夹选择框，可以直接填写路径。", true); }
    finally { button.disabled = false; }
  });
  $("queue-batch-form").addEventListener("submit", async event => {
    event.preventDefault();
    if (busy) return;
    saveDrafts();
    const fields = { tasks: JSON.stringify(drafts), workdir: $("queue-workdir").value, work_mode: $("queue-work-mode").value, request_id: requestId };
    const inputs = [...$("queue-batch-form").querySelectorAll("input, textarea, select, #queue-add-draft, [data-draft-remove], #queue-pick-folder")];
    inputs.forEach(input => { input.disabled = true; });
    $("queue-save-batch").textContent = "正在保存…";
    const result = await action("batch", fields);
    inputs.forEach(input => { input.disabled = false; });
    $("queue-save-batch").textContent = "保存到队列 →";
    if (result) {
      const count = drafts.length;
      drafts = [{ title: "", idea: "" }];
      requestId = crypto.randomUUID();
      paintDrafts(); saveDrafts(); selectTab("order");
      feedback(`已保存 ${count} 张任务。排好顺序后，点击「开始执行」。`);
    }
  });
  document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(drawer.open && tab === "add"); });
  refresh();
  if (new URLSearchParams(window.location.search).get("queue") === "open") open();
})();
