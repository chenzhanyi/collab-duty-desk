(function () {
  "use strict";

  var root = document.getElementById("revise-panel");
  if (!root) {
    return;
  }
  var statusUrl = root.getAttribute("data-status-url");
  var initialState = root.getAttribute("data-state") || "idle";
  var pollMs = parseInt(root.getAttribute("data-poll-ms") || "1500", 10);
  if (!statusUrl || initialState !== "running") {
    return;
  }

  var label = document.getElementById("revise-live-label");
  var current = document.getElementById("revise-current");
  var currentTitle = document.getElementById("revise-current-title");
  var note = document.getElementById("revise-live-note");
  var timer = null;
  var errors = 0;
  var stopped = false;

  function setText(node, value) {
    if (node) {
      node.textContent = value;
    }
  }

  function schedule(delay) {
    if (stopped) {
      return;
    }
    timer = window.setTimeout(poll, delay);
  }

  function finish() {
    stopped = true;
    if (timer) {
      window.clearTimeout(timer);
    }
    window.setTimeout(function () {
      window.location.reload();
    }, 900);
  }

  function paint(data) {
    if (data.running) {
      var step = data.step || "";
      setText(
        label,
        step === "review" ? "修正中… 正在重新审核" : "修正中…"
      );
      setText(
        current,
        data.current_subtask || (step === "review" ? "重新审核" : "准备中")
      );
      setText(currentTitle, data.current_title || "");
      setText(
        note,
        data.message ||
          (step === "review"
            ? "审核角色正在复核修正后的产物与证据。"
            : "正在按修正计划重新分发子任务。")
      );
      return false;
    }
    setText(
      label,
      data.state === "stopped" ? "继续修正已停止" : "继续修正结束"
    );
    setText(note, data.error || data.message || "正在刷新最新结论…");
    return true;
  }

  function poll() {
    if (stopped) {
      return;
    }
    fetch(statusUrl, {
      headers: { "Accept": "application/json" },
      cache: "no-store"
    })
      .then(function (response) {
        return response.json().catch(function () {
          return {};
        }).then(function (data) {
          return { ok: response.ok, data: data };
        });
      })
      .then(function (result) {
        var data = result.data || {};
        errors = 0;
        if (!result.ok) {
          setText(
            label,
            "修正状态暂时不可读：" + (data.error || data.message || "请稍候")
          );
          schedule(Math.max(pollMs, 3000));
          return;
        }
        if (paint(data)) {
          finish();
          return;
        }
        schedule(pollMs);
      })
      .catch(function () {
        errors += 1;
        setText(
          label,
          errors >= 3
            ? "网络暂时不可用，仍在等待；可以刷新本页查看进度。"
            : "修正中… 等待状态回报"
        );
        schedule(Math.max(pollMs, 3000));
      });
  }

  schedule(pollMs);
})();
