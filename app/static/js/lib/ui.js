// Page-wide behaviour: theme toggle, account menu, flash dismiss, confirm dialogs and
// client-side double-submit protection for classic forms (the server enforces it too).

function setButtonsBusy(form, busy) {
  form.querySelectorAll('button:not([type="button"])').forEach((button) => {
    if (busy) {
      button.dataset.label = button.textContent;
      button.textContent = "Please wait…";
      button.disabled = true;
    } else {
      if (button.dataset.label) button.textContent = button.dataset.label;
      button.disabled = false;
    }
  });
}

export function initUI() {
  document.querySelectorAll("[data-theme-toggle]").forEach((button) =>
    button.addEventListener("click", () => {
      const dark = !document.documentElement.classList.contains("dark");
      document.documentElement.classList.toggle("dark", dark);
      try {
        localStorage.setItem("pq-theme", dark ? "dark" : "light");
      } catch {
        /* private mode */
      }
    }),
  );

  document.querySelectorAll("[data-menu]").forEach((menu) => {
    const button = menu.querySelector("[data-menu-button]");
    const panel = menu.querySelector("[data-menu-panel]");
    if (!button || !panel) return;
    const close = () => {
      panel.classList.add("hidden");
      button.setAttribute("aria-expanded", "false");
    };
    button.addEventListener("click", (event) => {
      event.stopPropagation();
      const open = panel.classList.toggle("hidden") === false;
      button.setAttribute("aria-expanded", String(open));
    });
    document.addEventListener("click", (event) => {
      if (!menu.contains(event.target)) close();
    });
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape") close();
    });
  });

  document.querySelectorAll("[data-dismiss]").forEach((button) =>
    button.addEventListener("click", () => {
      const flash = button.closest("[data-flash]");
      if (flash) flash.remove();
    }),
  );

  document.querySelectorAll("form[data-confirm]").forEach((form) =>
    form.addEventListener("submit", (event) => {
      if (!window.confirm(form.dataset.confirm)) {
        event.preventDefault();
        event.stopImmediatePropagation();
      }
    }),
  );

  document.querySelectorAll("form[data-once]").forEach((form) =>
    form.addEventListener("submit", (event) => {
      if (event.defaultPrevented) return;
      if (form.dataset.submitting === "1") {
        event.preventDefault();
        return;
      }
      form.dataset.submitting = "1";
      // Disable after the browser has captured the submitter, so the submission still goes out.
      setTimeout(() => setButtonsBusy(form, true), 0);
      setTimeout(() => {
        form.dataset.submitting = "";
        setButtonsBusy(form, false);
      }, 12000);
    }),
  );

  // Coming back with the browser's Back button: re-enable forms.
  window.addEventListener("pageshow", (event) => {
    if (!event.persisted) return;
    document.querySelectorAll("form[data-once]").forEach((form) => {
      form.dataset.submitting = "";
      setButtonsBusy(form, false);
    });
  });
}
