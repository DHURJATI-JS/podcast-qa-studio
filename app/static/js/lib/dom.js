// Safe DOM helpers. Text is always inserted with textContent / text nodes - never innerHTML -
// so nothing a user or an AI model writes can ever be interpreted as HTML or script.

const SAFE_URL = /^(\/(?!\/)|https?:\/\/|#|blob:)/i;

export function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") el.className = value;
    else if (key === "text") el.textContent = String(value);
    else if (key === "dataset") Object.assign(el.dataset, value);
    else if (key.startsWith("on")) {
      if (typeof value === "function") el.addEventListener(key.slice(2).toLowerCase(), value);
      // string event handlers are never allowed
    } else if (key === "style" || key === "srcdoc" || key === "innerHTML") {
      continue;
    } else if ((key === "href" || key === "src" || key === "action" || key === "formaction") && !SAFE_URL.test(String(value))) {
      continue;
    } else if (value === true) el.setAttribute(key, "");
    else el.setAttribute(key, String(value));
  }
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}

export const $ = (selector, root = document) => root.querySelector(selector);
export const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));

export function clear(el) {
  if (el) while (el.firstChild) el.removeChild(el.firstChild);
  return el;
}
