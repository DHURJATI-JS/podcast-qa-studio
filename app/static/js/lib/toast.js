import { h } from "./dom.js";

const KIND_CLASS = {
  success: "alert-success",
  error: "alert-error",
  warn: "alert-warn",
  info: "alert-info",
};

export function toast(message, kind = "info", timeout = 5000) {
  const root = document.getElementById("toasts");
  if (!root || !message || document.getElementById("signed-out-modal")) return;
  const el = h(
    "div",
    {
      class: `alert ${KIND_CLASS[kind] || "alert-info"} pointer-events-auto flex items-start justify-between gap-3 shadow-lg`,
      role: kind === "error" ? "alert" : "status",
    },
    h("span", { text: message }),
    h("button", { type: "button", class: "btn-icon -my-1 h-6 w-6", "aria-label": "Dismiss", onclick: () => el.remove() }, "×"),
  );
  root.append(el);
  while (root.children.length > 4) root.firstElementChild.remove();
  if (timeout) setTimeout(() => el.remove(), timeout);
}
