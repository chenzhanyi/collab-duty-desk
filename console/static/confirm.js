(() => {
  "use strict";

  const approvedMoves = new WeakSet();
  const pendingMoves = [];
  let active = null;

  function closePopover(restoreFocus = false) {
    if (!active) return;
    const { trigger, bubble } = active;
    active = null;
    bubble.remove();
    trigger.removeAttribute("aria-expanded");
    trigger.removeAttribute("aria-controls");
    trigger.removeAttribute("aria-haspopup");
    if (restoreFocus && trigger.isConnected) trigger.focus({ preventScroll: true });
  }

  function positionPopover() {
    if (!active) return;
    const { trigger, bubble } = active;
    const anchor = trigger.getBoundingClientRect();
    if (!trigger.isConnected || !anchor.width || !anchor.height
        || anchor.bottom <= 0 || anchor.top >= window.innerHeight
        || anchor.right <= 0 || anchor.left >= window.innerWidth) {
      closePopover();
      return;
    }
    const gap = 10;
    const edge = 12;
    const size = bubble.getBoundingClientRect();
    const left = Math.max(edge, Math.min(anchor.right - size.width, window.innerWidth - size.width - edge));
    const above = anchor.top >= size.height + gap + edge;
    const top = above ? anchor.top - size.height - gap : anchor.bottom + gap;
    bubble.dataset.side = above ? "above" : "below";
    bubble.style.left = `${left}px`;
    bubble.style.top = `${Math.max(edge, Math.min(top, window.innerHeight - size.height - edge))}px`;
    bubble.style.setProperty("--anchor-x", `${Math.max(20, Math.min(anchor.left + anchor.width / 2 - left, size.width - 20))}px`);
    bubble.style.visibility = "visible";
  }

  function openPopover(form, trigger, message) {
    if (active && active.form === form) {
      closePopover(true);
      return;
    }
    closePopover();
    const restoring = new URL(form.action, window.location.href).pathname.startsWith("/restore/");
    const action = restoring ? "恢复" : "归档";
    const bubble = document.createElement("div");
    bubble.className = "confirm-popover";
    bubble.id = "move-confirm-popover";
    bubble.setAttribute("role", "dialog");
    bubble.setAttribute("aria-labelledby", "move-confirm-title");
    bubble.setAttribute("aria-describedby", "move-confirm-message");
    bubble.style.visibility = "hidden";

    const title = document.createElement("p");
    title.className = "confirm-popover__title";
    title.id = "move-confirm-title";
    title.textContent = `确认${action}？`;
    const description = document.createElement("p");
    description.className = "confirm-popover__message";
    description.id = "move-confirm-message";
    description.textContent = message;
    const buttons = document.createElement("div");
    buttons.className = "confirm-popover__actions";
    const cancel = document.createElement("button");
    cancel.type = "button";
    cancel.className = "button button--ghost";
    cancel.textContent = "取消";
    cancel.addEventListener("click", () => closePopover(true));
    const confirm = document.createElement("button");
    confirm.type = "button";
    confirm.className = "button button--primary";
    confirm.textContent = `确认${action}`;
    confirm.addEventListener("click", () => {
      closePopover();
      approvedMoves.add(form);
      try {
        form.requestSubmit(trigger);
      } finally {
        approvedMoves.delete(form);
      }
    });
    buttons.append(cancel, confirm);
    bubble.append(title, description, buttons);
    document.body.append(bubble);
    active = { form, trigger, bubble };
    trigger.setAttribute("aria-haspopup", "dialog");
    trigger.setAttribute("aria-expanded", "true");
    trigger.setAttribute("aria-controls", bubble.id);
    positionPopover();
    if (active) cancel.focus({ preventScroll: true });
  }

  document.addEventListener("pointerdown", (event) => {
    if (active && !active.bubble.contains(event.target) && !active.trigger.contains(event.target)) closePopover();
  });
  document.addEventListener("focusin", (event) => {
    if (active && !active.bubble.contains(event.target) && !active.trigger.contains(event.target)) closePopover();
  });
  document.addEventListener("keydown", (event) => {
    if (active && event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();
      closePopover(true);
    }
  });
  window.addEventListener("resize", positionPopover);
  window.addEventListener("scroll", positionPopover, true);
  window.addEventListener("pageshow", () => {
    pendingMoves.splice(0).forEach(({ form, trigger, text }) => {
      form.removeAttribute("aria-busy");
      trigger.disabled = false;
      trigger.textContent = text;
    });
  });

  document.addEventListener("submit", (event) => {
    const form = event.target;
    if (event.defaultPrevented || !(form instanceof HTMLFormElement)) {
      return;
    }
    const message = form.dataset.confirm;
    if (message && form.matches(".move-form")) {
      const trigger = event.submitter || form.querySelector("button[type=submit]");
      if (!trigger) return;
      if (pendingMoves.length) {
        event.preventDefault();
      } else if (approvedMoves.has(form)) {
        approvedMoves.delete(form);
        pendingMoves.push({ form, trigger, text: trigger.textContent });
        form.setAttribute("aria-busy", "true");
        trigger.disabled = true;
        trigger.textContent = new URL(form.action, window.location.href).pathname.startsWith("/restore/") ? "恢复中…" : "归档中…";
      } else {
        event.preventDefault();
        openPopover(form, trigger, message);
      }
      return;
    }
    if (message && !window.confirm(message)) {
      event.preventDefault();
    }
  });
})();
