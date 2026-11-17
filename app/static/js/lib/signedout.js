// "You've been signed out on this device" - shown with the server's reason (another device signed
// in, password reset, session expired). Only one device can be signed in to an account at a time.
import { h } from "./dom.js";

export function showSignedOut(message) {
  if (document.getElementById("signed-out-modal")) return;
  const next = encodeURIComponent(window.location.pathname);
  const modal = h(
    "div",
    {
      id: "signed-out-modal",
      class: "fixed inset-0 z-[60] flex items-center justify-center bg-zinc-950/70 p-4",
      role: "alertdialog",
      "aria-modal": "true",
      "aria-labelledby": "signed-out-title",
      "aria-describedby": "signed-out-text",
    },
    h(
      "div",
      { class: "card w-full max-w-md p-6 text-center shadow-2xl" },
      h(
        "div",
        { class: "mx-auto flex h-12 w-12 items-center justify-center rounded-full bg-rose-100 text-2xl dark:bg-rose-500/15", "aria-hidden": "true" },
        "🔒",
      ),
      h("h2", { id: "signed-out-title", class: "mt-3 text-lg font-semibold" }, "You've been signed out on this device"),
      h("p", { id: "signed-out-text", class: "mt-2 text-sm text-zinc-600 dark:text-zinc-300" }, message),
      h("a", { href: `/login?next=${next}`, class: "btn btn-primary mt-5 w-full" }, "Sign in again"),
    ),
  );
  document.body.append(modal);
  modal.querySelector("a").focus();
}
