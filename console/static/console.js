(() => {
  "use strict";

  // Actions stay on the task page when a precondition fails. Server validation
  // remains authoritative; every form also works as a normal POST without JS.
  let actionPending = false;
  document.querySelectorAll('.workdir-form[data-self-root]').forEach(form => {
    const select = form.querySelector('[name=work_mode]');
    const input = form.querySelector('[name=workdir]');
    let previous = input.value === form.dataset.selfRoot ? '' : input.value;
    const update = () => {
      if (select.value === 'self') {
        if (input.value !== form.dataset.selfRoot) previous = input.value;
        input.value = form.dataset.selfRoot;
      } else if (input.value === form.dataset.selfRoot) input.value = previous;
      input.readOnly = select.value === 'self';
    };
    select.addEventListener('change', update);
    update();
  });
  document.addEventListener("submit", async (event) => {
    const form = event.target;
    if (event.defaultPrevented || !(form instanceof HTMLFormElement)) return;
    const path = new URL(form.action, window.location.href).pathname;
    if (!/^\/(advance|dispatch|review|revise)\/T\d+(\/stop)?$|^\/handoff\/retry\/T\d+$|^\/register\/T\d+\/retry$|^\/task\/T\d+\/(workdir|owner-role)$/i.test(path)) return;
    event.preventDefault();
    if (actionPending) return;
    actionPending = true;
    const body = new URLSearchParams();
    new FormData(form).forEach((value, key) => body.append(key, String(value)));
    if (event.submitter && event.submitter.name) body.set(event.submitter.name, event.submitter.value);
    const editing = /\/(workdir|owner-role)$/.test(path);
    const buttons = Array.from(document.querySelectorAll("#workflow button[type=submit], #handoff-panel button[type=submit], .handoff-retry-form button, #register-failure button, .workdir-form button"));
    const states = buttons.map(button => ({ button, disabled: button.disabled, text: button.innerHTML }));
    const submitter = event.submitter || form.querySelector("button[type=submit]");
    const panel = editing ? form : (form.closest("section") || form.parentElement);
    let feedback = panel.querySelector(".action-feedback");
    if (!feedback) {
      feedback = document.createElement("div");
      feedback.className = "action-feedback";
      feedback.setAttribute("role", "status");
      feedback.setAttribute("tabindex", "-1");
      panel.append(feedback);
    }
    buttons.forEach(button => { button.disabled = true; });
    form.setAttribute("aria-busy", "true");
    const stopping = path.endsWith("/stop");
    if (submitter) submitter.textContent = stopping ? "正在停止…" : "正在处理…";
    feedback.classList.remove("is-error");
    feedback.setAttribute("role", "status");
    feedback.textContent = stopping ? "正在停止并清理当前步骤，请等待确认。" : "正在检查任务并启动操作…";
    try {
      const response = await fetch(path, {
        method: "POST",
        headers: { "Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8" },
        body: body.toString()
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok || data.ok === false) {
        const message = data.message || data.error || "操作没有完成，请查看当前任务状态。";
        throw new Error(message + (data.hint ? " " + data.hint : ""));
      }
      feedback.textContent = data.message || "已收到操作，正在更新任务状态…";
      if (editing) { window.location.reload(); return; }
      const workflow = document.getElementById("workflow");
      const destination = workflow ? workflow.dataset.taskUrl + "#workflow" : window.location.href;
      window.location.assign(destination);
      // Assigning the same fragment does not reload the document.
      if (workflow && window.location.pathname === workflow.dataset.taskUrl) window.location.reload();
    } catch (error) {
      feedback.setAttribute("role", "alert");
      feedback.classList.add("is-error");
      feedback.textContent = error instanceof TypeError
        ? "连接中断，请刷新确认操作是否已开始，再决定是否重试。"
        : error.message;
      feedback.focus({ preventScroll: true });
      states.forEach(({ button, disabled, text }) => { button.disabled = disabled; button.innerHTML = text; });
      actionPending = false;
      form.removeAttribute("aria-busy");
    }
  });

  const filters = document.getElementById("task-filters");
  if (filters) {
    const search = document.getElementById("task-search");
    const select = document.getElementById("task-filter");
    const rows = Array.from(document.querySelectorAll(".task-item[data-search]"));
    const update = () => {
      const query = search.value.trim().toLocaleLowerCase();
      let count = 0;
      rows.forEach(row => {
        const phase = row.dataset.phase;
        const status = select.value;
        const matchesStatus = status === "all"
          || (status === "attention" && ["revise", "handoff_blocked"].includes(phase))
          || (status === "running" && ["dispatching", "reviewing"].includes(phase))
          || status === phase;
        row.hidden = !matchesStatus || !row.dataset.search.toLocaleLowerCase().includes(query);
        if (!row.hidden) count += 1;
      });
      document.getElementById("task-filter-count").textContent = `显示 ${count} / ${rows.length} 个任务`;
      document.getElementById("filter-empty").hidden = count > 0;
      const params = new URLSearchParams(window.location.search);
      query ? params.set("q", search.value) : params.delete("q");
      select.value === "all" ? params.delete("status") : params.set("status", select.value);
      window.history.replaceState(null, "", window.location.pathname + (params.size ? "?" + params : ""));
    };
    const saved = new URLSearchParams(window.location.search);
    search.value = saved.get("q") || "";
    if (Array.from(select.options).some(option => option.value === saved.get("status"))) select.value = saved.get("status");
    filters.hidden = false;
    search.addEventListener("input", update);
    select.addEventListener("change", update);
    update();
  }

  document.querySelectorAll("details[data-persist]").forEach(details => {
    const key = "collab:" + details.dataset.persist;
    try { details.open = sessionStorage.getItem(key) === "open"; } catch (_) { /* Storage may be unavailable. */ }
    details.addEventListener("toggle", () => {
      try { sessionStorage.setItem(key, details.open ? "open" : "closed"); } catch (_) { /* Optional preference. */ }
    });
  });

  // A successful publish navigates here; failed publishes keep their draft.
  if (document.getElementById("register-poll")) {
    try {
      const submitted = Number(sessionStorage.getItem("collab:publishSubmitted") || 0);
      if (Date.now() - submitted < 60000) localStorage.removeItem("collab:publishDraft");
      sessionStorage.removeItem("collab:publishSubmitted");
    } catch (_) { /* Draft storage is optional. */ }
  }
})();
