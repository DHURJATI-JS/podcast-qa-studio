// Google-style sign in: "Choose an account" (accounts remembered on this device, names fetched
// by user id on the server), then a two-step identifier -> password flow with an account chip.
// Works without JavaScript too (both fields are shown).

const AVATAR_CLASSES = [
  "bg-violet-600",
  "bg-rose-600",
  "bg-amber-600",
  "bg-emerald-600",
  "bg-sky-600",
  "bg-fuchsia-600",
  "bg-teal-600",
  "bg-indigo-600",
];

export function initLogin() {
  const root = document.querySelector("[data-login]");
  if (!root) return;
  const form = root.querySelector("[data-login-form]");
  const chooser = root.querySelector("[data-chooser]");
  const stepIdentifier = form.querySelector("[data-step-identifier]");
  const stepPassword = form.querySelector("[data-step-password]");
  const identifier = form.querySelector("#identifier");
  const password = form.querySelector("#password");
  const chipLabel = form.querySelector("[data-chip-label]");
  const chipAvatar = form.querySelector("[data-chip-avatar]");
  const title = root.querySelector("[data-login-title]");
  const subtitle = root.querySelector("[data-login-subtitle]");
  const defaultSubtitle = subtitle.textContent;
  const hasError = Boolean(root.querySelector(".alert-error"));

  function showChooser() {
    form.classList.add("hidden");
    chooser.classList.remove("hidden");
    title.textContent = "Choose an account";
    subtitle.textContent = defaultSubtitle;
  }

  function showIdentifier() {
    if (chooser) chooser.classList.add("hidden");
    form.classList.remove("hidden");
    stepIdentifier.classList.remove("hidden");
    stepPassword.classList.add("hidden");
    title.textContent = "Sign in";
    subtitle.textContent = defaultSubtitle;
    identifier.focus();
  }

  function showPassword(name, initial, color) {
    if (chooser) chooser.classList.add("hidden");
    form.classList.remove("hidden");
    stepIdentifier.classList.add("hidden");
    stepPassword.classList.remove("hidden");
    const value = identifier.value.trim();
    chipLabel.textContent = value;
    chipAvatar.textContent = (initial || value.charAt(0) || "?").toUpperCase();
    chipAvatar.className = "avatar h-6 w-6 text-xs " + (AVATAR_CLASSES[Number(color)] || "bg-violet-600");
    title.textContent = name ? `Hi ${name.split(" ")[0]}` : "Welcome";
    subtitle.textContent = "";
    password.value = "";
    password.focus();
  }

  if (!chooser || chooser.classList.contains("hidden") || hasError) {
    if (hasError && identifier.value.trim()) showPassword();
    else showIdentifier();
  }

  const next = form.querySelector("[data-next]");
  next.addEventListener("click", () => {
    if (!identifier.value.trim()) {
      identifier.focus();
      return;
    }
    showPassword();
  });
  identifier.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      next.click();
    }
  });
  form.querySelector("[data-change-account]").addEventListener("click", () => (chooser ? showChooser() : showIdentifier()));
  if (chooser) {
    chooser.querySelectorAll("[data-account]").forEach((button) =>
      button.addEventListener("click", () => {
        identifier.value = button.dataset.identifier;
        showPassword(button.dataset.name, button.dataset.initial, button.dataset.color);
      }),
    );
  }
  form.querySelector("[data-show-password]").addEventListener("change", (event) => {
    password.type = event.target.checked ? "text" : "password";
  });
  // Capture phase so this runs before the generic double-submit guard.
  form.addEventListener(
    "submit",
    (event) => {
      if (!identifier.value.trim()) {
        event.preventDefault();
        showIdentifier();
      } else if (!password.value) {
        event.preventDefault();
        showPassword();
      }
    },
    true,
  );
}

export function initStrength() {
  document.querySelectorAll("[data-strength-input]").forEach((input) => {
    const form = input.closest("form");
    const bar = form.querySelector("[data-strength-bar]");
    const label = form.querySelector("[data-strength-label]");
    if (!bar || !label) return;
    const levels = [
      ["w-1/12", "bg-rose-500", "Too short - use at least 10 characters"],
      ["w-1/4", "bg-rose-500", "Weak"],
      ["w-2/4", "bg-amber-500", "Fair"],
      ["w-3/4", "bg-lime-500", "Good"],
      ["w-full", "bg-emerald-500", "Strong"],
      ["w-full", "bg-emerald-600", "Very strong"],
    ];
    input.addEventListener("input", () => {
      const value = input.value;
      let score = 0;
      if (value.length >= 10) score++;
      if (value.length >= 14) score++;
      if (/[a-z]/.test(value) && /[A-Z]/.test(value)) score++;
      if (/\d/.test(value)) score++;
      if (/[^A-Za-z0-9]/.test(value)) score++;
      const [width, color, text] = value.length < 10 ? levels[0] : levels[Math.min(score, 5)];
      bar.className = `h-full rounded-full transition-all ${width} ${color}`;
      label.textContent = value ? text : "At least 10 characters. Longer passphrases are best.";
    });
  });
}
