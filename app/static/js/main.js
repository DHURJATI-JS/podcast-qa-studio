// Entry point (bundled + minified by esbuild into static/dist/app.min.js).
import { api } from "./lib/api.js";
import { bus, connectSocket } from "./lib/socket.js";
import { renderAIPill } from "./lib/status.js";
import { initUI } from "./lib/ui.js";
import { initLogin, initStrength } from "./pages/auth.js";
import { initWorkspace } from "./pages/workspace.js";

function boot() {
  initUI();
  initStrength();
  const page = document.body.dataset.page;
  if (page === "login") initLogin();
  if (document.body.dataset.signedIn === "1") {
    connectSocket();
    bus.on("ai:status", renderAIPill);
    // One login per account: when the account signs in elsewhere the server pushes this event;
    // /api/me then answers 401 with the reason (shown in a dialog) - or 200 if this tab is still fine.
    bus.on("session:check", () => api("GET", "/api/me").catch(() => {}));
    api("GET", "/api/ai/status")
      .then(renderAIPill)
      .catch(() => {});
    // Like Google's account chip: the session only holds a user id; the name is fetched by it.
    api("GET", "/api/me")
      .then((me) => document.querySelectorAll("[data-greeting]").forEach((el) => (el.textContent = me.greeting)))
      .catch(() => {});
  }
  if (page === "workspace") initWorkspace();
}

if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
else boot();
