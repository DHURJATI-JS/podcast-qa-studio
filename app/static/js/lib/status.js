// Header "AI" pill: shows which AI will answer, or that none is available.
export const DOT_CLASS = {
  online: "bg-emerald-500",
  unknown: "bg-zinc-400",
  loading: "bg-amber-500",
  offline: "bg-rose-500",
  auth_error: "bg-rose-500",
  not_configured: "bg-zinc-300 dark:bg-zinc-600",
};

export function renderAIPill(summary) {
  const pill = document.getElementById("ai-pill");
  if (!pill || !summary) return;
  const dot = pill.querySelector("[data-dot]");
  const label = pill.querySelector("[data-label]");
  const ok = summary.any_available !== false;
  dot.className = "h-2 w-2 rounded-full " + (ok ? "bg-emerald-500" : "bg-rose-500");
  label.textContent = ok ? `AI: ${summary.active || "ready"}` : "AI unavailable";
  pill.title = (summary.providers || []).map((p) => `${p.label}: ${p.status_label}`).join("\n");
}
