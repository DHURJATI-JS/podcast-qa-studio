// fetch() wrapper: same-origin cookies, CSRF header on every request, optional Idempotency-Key
// (double-submit protection), friendly error messages.
import { showSignedOut } from "./signedout.js";

export class ApiError extends Error {
  constructor(message, status, data) {
    super(message);
    this.status = status;
    this.data = data || {};
  }
}

export function csrfToken() {
  const meta = document.querySelector('meta[name="csrf-token"]');
  return meta ? meta.getAttribute("content") || "" : "";
}

export function newKey() {
  if (window.crypto && typeof window.crypto.randomUUID === "function") return window.crypto.randomUUID();
  const bytes = new Uint8Array(16);
  window.crypto.getRandomValues(bytes); // works on plain-http LAN addresses too
  return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
}

export async function api(method, url, body, { idem = false, signal } = {}) {
  const headers = { Accept: "application/json", "X-CSRF-Token": csrfToken() };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (idem) headers["Idempotency-Key"] = typeof idem === "string" ? idem : newKey();
  let response;
  try {
    response = await fetch(url, {
      method,
      headers,
      body: body !== undefined ? JSON.stringify(body) : undefined,
      credentials: "same-origin",
      signal,
    });
  } catch (error) {
    if (error && error.name === "AbortError") throw error;
    throw new ApiError("Network problem - check your connection and try again.", 0);
  }
  let data = null;
  if ((response.headers.get("content-type") || "").includes("application/json")) {
    try {
      data = await response.json();
    } catch {
      data = null;
    }
  }
  if (!response.ok) {
    if (response.status === 401) {
      // Signed out on this device (e.g. the account signed in elsewhere): explain why, change nothing.
      if (data && data.code === "signed_out") showSignedOut(data.detail);
      else window.location.href = "/login?next=" + encodeURIComponent(window.location.pathname);
    }
    const message = (data && typeof data.detail === "string" && data.detail) || `Request failed (${response.status}).`;
    throw new ApiError(message, response.status, data);
  }
  return data;
}
