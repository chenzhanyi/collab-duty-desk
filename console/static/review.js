(function () {
  "use strict";

  var root = document.getElementById("review-panel");
  if (!root) {
    return;
  }
  var statusUrl = root.getAttribute("data-status-url");
  var initialState = root.getAttribute("data-state") || "idle";
  var pollMs = parseInt(root.getAttribute("data-poll-ms") || "1500", 10);
  if (!statusUrl || initialState !== "running") {
    return;
  }

  var label = document.getElementById("review-live-label");
  var note = document.getElementById("review-live-note");
  var counter = document.getElementById("review-counter");
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
    setText(label, data.state === "running" ? "审核中…" : (data.state_label || "审核结束"));
    setText(note, data.message || data.error || "正在复核验收标准与证据。");
    setText(counter, (data.done || 0) + "/" + (data.total || 0));
    return data.state !== "running";
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
            "审核状态暂时不可读：" + (data.error || data.message || "请稍候")
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
            : "审核中… 等待状态回报"
        );
        schedule(Math.max(pollMs, 3000));
      });
  }

  schedule(pollMs);
})();
