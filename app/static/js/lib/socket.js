// Socket.IO connection (WebSocket only). The CSRF token is sent in the auth payload and the
// server also checks the Origin and the session cookie, so other sites can't hijack it.
import { io } from "socket.io-client";
import { csrfToken } from "./api.js";

const listeners = new Map();
const EVENTS = [
  "ai:status",
  "job:update",
  "job:delta",
  "job:reset",
  "job:question",
  "job:skipped",
  "job:flags",
  "job:feedback",
  "job:done",
  "job:error",
  "session:check",
];
let socket = null;

export const bus = {
  on(event, fn) {
    if (!listeners.has(event)) listeners.set(event, new Set());
    listeners.get(event).add(fn);
    return () => listeners.get(event)?.delete(fn);
  },
  emit(event, data) {
    listeners.get(event)?.forEach((fn) => {
      try {
        fn(data);
      } catch (error) {
        console.error(error);
      }
    });
  },
};

export function connectSocket() {
  if (socket || document.body.dataset.signedIn !== "1") return socket;
  socket = io({
    path: "/socket.io",
    transports: ["websocket"],
    auth: { csrf: csrfToken() },
    reconnectionDelayMax: 10000,
  });
  EVENTS.forEach((event) => socket.on(event, (data) => bus.emit(event, data)));
  let connectedOnce = false;
  socket.on("connect", () => {
    bus.emit(connectedOnce ? "socket:reconnect" : "socket:connect");
    connectedOnce = true;
  });
  socket.on("disconnect", (reason) => {
    bus.emit("socket:disconnect", reason);
    // The server closes every socket of a user after sign-out / password change / session
    // revocation. Socket.IO never auto-reconnects after a server-side disconnect, so try once:
    // a still-valid session reconnects (and the page resyncs), a revoked one is refused.
    if (reason === "io server disconnect") setTimeout(() => socket.connect(), 1500);
  });
  socket.on("connect_error", (error) => {
    bus.emit("socket:error", error && error.message);
    // The server refused us because this device was signed out: ask it why (shown to the user).
    if (error && error.message === "signed_out") bus.emit("session:check");
  });
  return socket;
}

export function socketRequest(event, data) {
  return new Promise((resolve) => {
    if (!socket || !socket.connected) return resolve(null);
    socket.timeout(5000).emit(event, data, (error, response) => resolve(error ? null : response));
  });
}
