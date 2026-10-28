(function () {
  "use strict";

  var form = document.getElementById("publish-form");
  if (!form) {
    return;
  }
  var pickButton = document.getElementById("folder-pick");
  var folderInput = document.getElementById("field-workdir");
  var folderStatus = document.getElementById("folder-status");
  var workMode = document.getElementById("field-work-mode");
  var modeHelp = document.getElementById("work-mode-help");
  var selfRoot = form.getAttribute("data-self-root") || "";
  var lastProjectPath = "";
  var validateUrl = form.getAttribute("data-validate-url") || "/api/validate-folder";
  if (!pickButton || !folderInput || !folderStatus) {
    return;
  }

  var folderKind = "idle";
  var validateTimer = null;
  var validateSequence = 0;
  var pickOriginalHtml = pickButton.innerHTML;

  function setStatus(kind, message) {
    folderKind = kind;
    folderInput.setAttribute("aria-invalid", kind === "error" ? "true" : "false");
    folderStatus.className = "folder-status is-" + kind;
    folderStatus.textContent = message;
  }

  function parseJson(response) {
    return response.json().catch(function () {
      return {};
    });
  }

  function validateFolder() {
    var value = folderInput.value.trim();
    var selectedMode = workMode ? workMode.value : "isolated";
    var sequence = ++validateSequence;
    if (!value) {
      setStatus("idle", "尚未选择。可以点左侧按钮，或直接填写绝对路径。");
      return Promise.resolve();
    }
    setStatus("busy", "正在校验文件夹…");
    return fetch(validateUrl, {
      method: "POST",
      headers: {
        "Accept": "application/json",
        "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"
      },
      body: new URLSearchParams({ workdir: value, work_mode: selectedMode }).toString()
    })
      .then(function (response) {
        return parseJson(response).then(function (data) {
          return { ok: response.ok, data: data };
        });
      })
      .then(function (result) {
        if (sequence !== validateSequence || folderInput.value.trim() !== value || (workMode && workMode.value !== selectedMode)) {
          return;
        }
        if (result.ok && result.data.ok) {
          folderInput.value = result.data.path || value;
          setStatus("ok", "路径有效 · " + (result.data.path || value));
        } else {
          setStatus("error", result.data.error || result.data.message || "这个文件夹不可用。");
        }
      })
      .catch(function () {
        if (sequence !== validateSequence || folderInput.value.trim() !== value || (workMode && workMode.value !== selectedMode)) {
          return;
        }
        setStatus("warn", "暂时无法校验；发布时服务端仍会检查这个路径。");
      });
  }

  function scheduleValidation(delay) {
    if (validateTimer) {
      window.clearTimeout(validateTimer);
    }
    validateTimer = window.setTimeout(validateFolder, delay);
  }

  function applyWorkMode() {
    if (!workMode) return;
    var maintaining = workMode.value === "self";
    validateSequence += 1;
    if (maintaining) {
      if (folderInput.value && folderInput.value !== selfRoot) lastProjectPath = folderInput.value;
      folderInput.value = selfRoot;
    } else if (folderInput.value === selfRoot) {
      folderInput.value = lastProjectPath;
    }
    folderInput.readOnly = maintaining;
    pickButton.disabled = maintaining;
    pickButton.hidden = maintaining;
    folderInput.closest('.folder-picker').classList.toggle('is-self', maintaining);
    if (modeHelp) modeHelp.textContent = maintaining
      ? "直接维护当前工作台源码。任务档案和运行数据分开保留；验收使用临时环境，完成后由你决定何时重启。"
      : workMode.value === "project"
        ? "修改已有项目：Agent 直接编辑项目中的代码、测试和文档，交付说明留在本任务档案。"
        : "生成独立产物：每个子任务只在所选目录的“子任务/编号”中写入成果，沿用旧版规则。";
    validateFolder();
  }
  if (workMode) workMode.addEventListener("change", applyWorkMode);

  folderInput.addEventListener("input", function () {
    validateSequence += 1;
    setStatus("idle", "路径已更改，等待校验…");
    scheduleValidation(500);
  });
  folderInput.addEventListener("blur", function () {
    validateFolder();
  });

  pickButton.addEventListener("click", function () {
    if (pickButton.disabled) {
      return;
    }
    pickButton.disabled = true;
    pickButton.textContent = "正在等待系统选择…";
    setStatus("busy", "请在本机文件夹选择框中选一个项目文件夹。");
    var pickingMode = workMode ? workMode.value : "isolated";
    fetch("/api/pick-folder?work_mode=" + encodeURIComponent(pickingMode), {
      method: "POST",
      headers: { "Accept": "application/json" }
    })
      .then(function (response) {
        return parseJson(response).then(function (data) {
          return { ok: response.ok, data: data };
        });
      })
      .then(function (result) {
        var data = result.data || {};
        if (workMode && workMode.value !== pickingMode) return;
        if (result.ok && data.ok && data.path) {
          folderInput.value = data.path;
          if (data.valid === false) {
            setStatus("error", data.error || "选中的文件夹不能作为任务项目目录。");
          } else {
            validateFolder();
          }
        } else if (data.cancelled) {
          setStatus("idle", "已取消选择。也可以直接在输入框填写绝对路径。");
        } else {
          setStatus(
            "error",
            (data.error || "无法打开原生文件夹选择框。") +
              (data.hint ? " " + data.hint : " 请直接手动填写绝对路径。")
          );
        }
      })
      .catch(function () {
        setStatus("error", "原生选择框请求失败；请直接在输入框手动填写绝对路径。");
      })
      .finally(function () {
        pickButton.disabled = !!workMode && workMode.value === "self";
        pickButton.innerHTML = pickOriginalHtml;
      });
  });

  var idea = document.getElementById("field-idea");
  var title = document.getElementById("field-title");
  var automatic = document.getElementById("field-auto-advance");
  var draftStatus = document.getElementById("draft-status");
  var publishButton = form.querySelector(".publish-button");
  var originalDisabled = publishButton ? publishButton.disabled : false;
  var submitting = false;
  var draftTimer = null;

  function paintPublish() {
    var count = document.getElementById("idea-count");
    if (count && idea) count.textContent = idea.value.length + " / " + idea.maxLength + " 字";
    if (publishButton && !submitting) {
      publishButton.textContent = automatic && automatic.checked ? "发布并开始执行 →" : "仅发布并登记 →";
    }
  }

  function saveDraft() {
    try {
      localStorage.setItem("collab:publishDraft", JSON.stringify({
        idea: idea ? idea.value : "", title: title ? title.value : "",
        workdir: folderInput.value, work_mode: workMode ? workMode.value : "isolated", automatic: automatic ? automatic.checked : true
      }));
      if (draftStatus) draftStatus.textContent = "草稿已保存在本机 · 发布成功后清除";
    } catch (_) {
      if (draftStatus) draftStatus.textContent = "浏览器未允许保存草稿，请保留本页直到发布完成";
    }
    paintPublish();
  }

  try {
    var draft = JSON.parse(localStorage.getItem("collab:publishDraft") || "null");
    if (draft && idea && !idea.value && title && !title.value) {
      idea.value = typeof draft.idea === "string" ? draft.idea.slice(0, idea.maxLength) : "";
      title.value = typeof draft.title === "string" ? draft.title.slice(0, title.maxLength) : "";
      if (!folderInput.value && typeof draft.workdir === "string") folderInput.value = draft.workdir.slice(0, folderInput.maxLength);
      if (workMode && Array.from(workMode.options).some(option => option.value === draft.work_mode)) workMode.value = draft.work_mode;
      if (automatic && typeof draft.automatic === "boolean") automatic.checked = draft.automatic;
      if (draftStatus) draftStatus.textContent = "已恢复上次草稿 · 仅保存在本机浏览器";
    }
  } catch (_) { /* An invalid or unavailable local draft must never block publishing. */ }

  form.addEventListener("input", function () {
    paintPublish();
    if (draftTimer) window.clearTimeout(draftTimer);
    draftTimer = window.setTimeout(saveDraft, 300);
  });
  form.addEventListener("change", saveDraft);
  form.addEventListener("submit", function (event) {
    if (submitting || folderKind === "error") {
      event.preventDefault();
      if (!submitting) folderInput.focus();
      return;
    }
    saveDraft();
    try { sessionStorage.setItem("collab:publishSubmitted", String(Date.now())); } catch (_) { /* Optional draft cleanup. */ }
    submitting = true;
    if (publishButton) { publishButton.disabled = true; publishButton.textContent = "正在发布…"; }
    form.setAttribute("aria-busy", "true");
  });
  window.addEventListener("pageshow", function () {
    submitting = false;
    if (publishButton) publishButton.disabled = originalDisabled;
    form.removeAttribute("aria-busy");
    paintPublish();
  });
  var clear = document.getElementById("draft-clear");
  if (clear) clear.addEventListener("click", function () {
    if (!window.confirm("清空当前草稿？已发布的任务不会受影响。")) return;
    if (draftTimer) window.clearTimeout(draftTimer);
    try { localStorage.removeItem("collab:publishDraft"); } catch (_) { /* Optional storage. */ }
    if (idea) idea.value = "";
    if (title) title.value = "";
    folderInput.value = "";
    if (workMode) workMode.value = "project";
    applyWorkMode();
    validateSequence += 1;
    setStatus("idle", "请选择项目文件夹。");
    if (draftStatus) draftStatus.textContent = "草稿已清除";
    paintPublish();
    if (idea) idea.focus();
  });
  paintPublish();

  if (workMode) {
    applyWorkMode();
  } else if (folderInput.value.trim()) {
    validateFolder();
  }
})();
