(function () {
  "use strict";

  var root = document.getElementById("dispatch-panel");
  if (!root) {
    return;
  }
  var statusUrl = root.getAttribute("data-status-url");
  var initialState = root.getAttribute("data-state") || "idle";
  var pollMs = parseInt(root.getAttribute("data-poll-ms") || "1500", 10);
  if (!statusUrl || initialState !== "running") {
    return;
  }

  var live = document.getElementById("dispatch-live");
  var label = document.getElementById("dispatch-live-label");
  var current = document.getElementById("dispatch-current");
  var currentTitle = document.getElementById("dispatch-current-title");
  var counter = document.getElementById("dispatch-counter");
  var button = document.getElementById("dispatch-start");
  var timer = null;
  var errors = 0;
  var stopped = false;

  function setText(node, value) {
    if (node) {
      node.textContent = value;
    }
  }

  function paint(data) {
    if (live) {
      live.hidden = false;
    }
    if (button) {
      button.disabled = true;
      button.firstChild.nodeValue = "分发中…";
    }
    if (data.state === "running") {
      setText(label, "分发中…");
      setText(current, data.current_subtask || "准备中");
      setText(currentTitle, data.current_title || "");
      setText(
        counter,
        (data.done || 0) + "/" + (data.total || 0)
      );
      return false;
    }
    setText(label, data.state_label || "已结束");
    setText(current, data.current_subtask || "—");
    setText(currentTitle, data.message || "");
    setText(counter, (data.done || 0) + "/" + (data.total || 0));
    setText(label, "分发完成：成功 " + (data.success || 0) + " / 失败 " + (data.failed || 0));
    return true;
  }

  function schedule(delay) {
    if (stopped) {
      return;
    }
    timer = window.setTimeout(poll, document.hidden ? Math.max(delay, 8000) : delay);
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
            "分发状态暂时不可读：" + (data.error || data.message || "请稍候")
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
            : "分发中… 等待状态回报"
        );
        schedule(Math.max(pollMs, 3000));
      });
  }

  schedule(pollMs);
})();
