(function () {
  "use strict";

  var root = document.getElementById("register-poll");
  if (!root) {
    return;
  }
  var statusUrl = root.getAttribute("data-status-url");
  var taskUrl = root.getAttribute("data-task-url") || "";
  var initialStatus = root.getAttribute("data-initial-status") || "registering";
  var pollMs = parseInt(root.getAttribute("data-poll-ms") || "2000", 10);
  var live = document.getElementById("register-live-status");
  var failurePanel = document.getElementById("register-failure");
  var errorMessage = document.getElementById("register-error-message");
  var logCode = document.getElementById("register-log-code");
  var badge = root.querySelector(".register-badge");
  var timer = null;
  var stopped = initialStatus !== "registering";
  var errors = 0;

  if (!statusUrl || stopped) {
    return;
  }

  function setText(node, value) {
    if (node) {
      node.textContent = value;
    }
  }

  function showFailure(payload) {
    stopped = true;
    if (timer) {
      window.clearTimeout(timer);
    }
    if (badge) {
      badge.textContent = "登记失败";
    }
    if (live) {
      live.hidden = true;
    }
    setText(errorMessage, payload.error || payload.message || "总控登记失败；请查看日志后重试。");
    setText(logCode, payload.log || "尚未生成");
    if (failurePanel) {
      failurePanel.hidden = false;
      failurePanel.scrollIntoView({ block: "nearest", behavior: "smooth" });
    }
  }

  function schedule(delay) {
    if (stopped) {
      return;
    }
    timer = window.setTimeout(poll, delay);
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
          setText(live, "登记状态暂时不可读：" + (data.error || data.message || "请稍候"));
          schedule(Math.max(pollMs, 3000));
          return;
        }
        if (data.status === "registered") {
          stopped = true;
          setText(live, "登记完成，正在进入任务详情…");
          window.location.assign(data.task_url || taskUrl);
          return;
        }
        if (data.status === "register_failed") {
          showFailure(data);
          return;
        }
        setText(live, "正在登记 · 总控仍在写任务单与子任务");
        schedule(pollMs);
      })
      .catch(function () {
        errors += 1;
        setText(
          live,
          errors >= 3
            ? "网络暂时不可用，仍在等待；可以刷新本页重试查看。"
            : "正在登记 · 等待状态回报"
        );
        schedule(Math.max(pollMs, 3000));
      });
  }

  schedule(pollMs);
})();
