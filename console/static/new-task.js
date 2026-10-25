(function () {
  "use strict";

  var form = document.getElementById("publish-form");
  if (!form) {
    return;
  }
  var pickButton = document.getElementById("folder-pick");
  var folderInput = document.getElementById("field-workdir");
  var folderStatus = document.getElementById("folder-status");
  var validateUrl = form.getAttribute("data-validate-url") || "/api/validate-folder";
  if (!pickButton || !folderInput || !folderStatus) {
    return;
  }

  var validateTimer = null;
  var validateSequence = 0;
  var pickOriginalHtml = pickButton.innerHTML;

  function setStatus(kind, message) {
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
      body: new URLSearchParams({ workdir: value }).toString()
    })
      .then(function (response) {
        return parseJson(response).then(function (data) {
          return { ok: response.ok, data: data };
        });
      })
      .then(function (result) {
        if (sequence !== validateSequence) {
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
        if (sequence !== validateSequence) {
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

  folderInput.addEventListener("input", function () {
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
    fetch("/api/pick-folder", {
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
        pickButton.disabled = false;
        pickButton.innerHTML = pickOriginalHtml;
      });
  });

  if (folderInput.value.trim()) {
    validateFolder();
  }
})();
