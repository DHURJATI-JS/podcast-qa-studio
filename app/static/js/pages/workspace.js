// Podcast workspace: question list, AI jobs streamed over Socket.IO, run-time budget,
// mock interview + feedback (guest mode), Mira's voice, standing instructions, PDF exports.
import { $, $$, clear, h } from "../lib/dom.js";
import { api } from "../lib/api.js";
import { toast } from "../lib/toast.js";
import { bus } from "../lib/socket.js";
import { bytes, clamp, countWords, fmt, shortDate } from "../lib/format.js";
import { DOT_CLASS } from "../lib/status.js";
import { Avatar } from "../voice/avatar.js";
import { Voice } from "../voice/voice.js";

const CATEGORY_CLASS = {
  opener: "bg-sky-100 text-sky-800 dark:bg-sky-500/15 dark:text-sky-200",
  background: "bg-zinc-100 text-zinc-700 dark:bg-zinc-800 dark:text-zinc-300",
  core: "bg-violet-100 text-violet-800 dark:bg-violet-500/15 dark:text-violet-200",
  story: "bg-amber-100 text-amber-800 dark:bg-amber-500/15 dark:text-amber-200",
  challenge: "bg-rose-100 text-rose-800 dark:bg-rose-500/15 dark:text-rose-200",
  rapid_fire: "bg-lime-100 text-lime-800 dark:bg-lime-500/15 dark:text-lime-200",
  audience: "bg-teal-100 text-teal-800 dark:bg-teal-500/15 dark:text-teal-200",
  closing: "bg-indigo-100 text-indigo-800 dark:bg-indigo-500/15 dark:text-indigo-200",
  follow_up: "bg-pink-100 text-pink-800 dark:bg-pink-500/15 dark:text-pink-200",
};
const SOURCE_LABEL = { ai: "AI", user: "You", ai_followup: "AI follow-up" };
const JOB_LABEL = {
  generate_questions: "Mira is writing questions",
  rewrite_questions: "Mira is rewriting questions",
  answer_feedback: "Mira is checking your answer",
  next_question: "Mira is thinking of a question",
  review_repeats: "Mira is double-checking possible repeats",
};
const JOB_GROUP = {
  generate_questions: "questions",
  rewrite_questions: "questions",
  next_question: "questions",
  review_repeats: "questions",
  answer_feedback: "feedback",
};

export function initWorkspace() {
  const dataEl = document.getElementById("page-data");
  const root = $("[data-workspace]");
  if (!dataEl || !root) return;
  const data = JSON.parse(dataEl.textContent);
  const S = {
    podcast: data.state.podcast,
    questions: data.state.questions || [],
    answers: data.state.answers || {},
    exports: data.state.exports || [],
    limits: data.limits,
    categories: data.categories,
    flagHelp: data.flag_help,
    prefs: data.prefs,
    selected: new Set(),
    currentId: null,
    drafts: {},
    draftFor: null,
    jobs: new Map(),
  };
  const isHost = S.podcast.mode === "host";
  const pid = S.podcast.id;
  const mine = (event) => event && event.podcast_id === pid;
  const ordered = () => [...S.questions].sort((a, b) => a.position - b.position);

  // ------------------------------------------------------------------ voice + avatar
  const avatar = new Avatar($("[data-mira-wrap]"));
  const voiceSelect = $("[data-voice-select]");
  const speedInput = $("[data-voice-speed]");
  data.voices.forEach((v) => voiceSelect.append(h("option", { value: v.id, selected: v.id === S.prefs.voice }, v.label)));
  speedInput.value = String(S.prefs.speed || 0.95);
  const voice = new Voice({
    avatar,
    caption: $("[data-caption]"),
    status: $("[data-voice-status]"),
    getVoice: () => voiceSelect.value,
    getSpeed: () => Number(speedInput.value) || 0.95,
  });
  $("[data-stop-voice]").addEventListener("click", () => voice.stop());
  api("GET", "/api/tts/voices")
    .then((r) => voice.setInitialStatus(r.status))
    .catch(() => {});

  // ------------------------------------------------------------------ busy state (double-submit protection)
  function refreshBusy() {
    const busyGroups = new Set([...S.jobs.values()].map((view) => JOB_GROUP[view.kind]));
    $$("[data-ai-action]", root).forEach((button) => {
      const group = button.hasAttribute("data-submit-answer") ? "feedback" : "questions";
      button.disabled = busyGroups.has(group) || button.dataset.pending === "1";
      button.title = button.disabled ? "Mira is already working on this - wait or cancel it." : "";
    });
  }

  async function startJob(button, url, body) {
    if (button) {
      button.dataset.pending = "1";
      refreshBusy();
    }
    try {
      const result = await api("POST", url, body, { idem: true });
      if (result && result.job) ensureJobView(result.job);
      return result;
    } catch (error) {
      if (!(error.status === 409 && error.data && error.data.code === "duplicate")) {
        toast(error.message, error.status === 503 ? "warn" : "error", 10000);
      }
      return null;
    } finally {
      if (button) delete button.dataset.pending;
      refreshBusy();
    }
  }

  // ------------------------------------------------------------------ live AI job cards
  function ensureJobView(job) {
    if (S.jobs.has(job.id)) return S.jobs.get(job.id);
    const box = h("pre", { class: "stream-box mt-3 hidden", "aria-label": "Live AI output" });
    const status = h("p", { class: "text-sm text-zinc-600 dark:text-zinc-300" }, "Queued - waiting for the AI worker…");
    const clock = h("span", { class: "font-mono text-xs text-zinc-500" }, "0:00");
    const notes = h("ul", { class: "mt-2 space-y-1 text-xs text-zinc-500 dark:text-zinc-400" });
    const icon = h("span", { class: "h-5 w-5 shrink-0 animate-spin rounded-full border-2 border-violet-500 border-t-transparent", "aria-hidden": "true" });
    const cancel = h("button", { type: "button", class: "btn btn-ghost btn-sm", onclick: () => cancelJob(job.id) }, "Cancel");
    const el = h(
      "div",
      { class: "card border-violet-200 p-4 dark:border-violet-900" },
      h(
        "div",
        { class: "flex items-center gap-3" },
        icon,
        h("div", { class: "min-w-0 flex-1" }, h("p", { class: "font-medium" }, JOB_LABEL[job.kind] || "Mira is working"), status),
        clock,
        cancel,
      ),
      box,
      notes,
    );
    $("[data-jobs]").append(el);
    const started = Date.now();
    const view = {
      id: job.id,
      kind: job.kind,
      el,
      box,
      status,
      notes,
      icon,
      cancel,
      text: "",
      timer: setInterval(() => {
        clock.textContent = fmt((Date.now() - started) / 1000);
      }, 1000),
    };
    S.jobs.set(job.id, view);
    refreshBusy();
    return view;
  }

  function finishJob(id, ok, message) {
    const view = S.jobs.get(id);
    if (!view) return;
    clearInterval(view.timer);
    view.cancel.remove();
    view.icon.replaceWith(
      h(
        "span",
        {
          class: `flex h-5 w-5 shrink-0 items-center justify-center rounded-full text-xs text-white ${ok ? "bg-emerald-500" : "bg-rose-500"}`,
          "aria-hidden": "true",
        },
        ok ? "✓" : "!",
      ),
    );
    view.status.textContent = message;
    view.status.className = ok ? "text-sm text-emerald-700 dark:text-emerald-400" : "text-sm text-rose-700 dark:text-rose-400";
    S.jobs.delete(id);
    refreshBusy();
    setTimeout(() => view.el.remove(), ok ? 8000 : 20000);
  }

  function finishFromServer(job) {
    const ok = job.status === "succeeded";
    const message = ok ? "Finished." : job.status === "cancelled" ? "Cancelled." : job.error || "Failed.";
    finishJob(job.id, ok, message);
  }

  async function cancelJob(id) {
    try {
      const result = await api("POST", `/api/jobs/${id}/cancel`, {});
      const view = S.jobs.get(id);
      if (result.status === "cancelling") {
        if (view) view.status.textContent = "Cancelling…";
      } else if (result.status === "cancelled") {
        finishJob(id, false, "Cancelled.");
      } else {
        refresh(); // it had already finished
      }
    } catch (error) {
      toast(error.message, "error");
    }
  }

  function note(view, text) {
    if (view) view.notes.append(h("li", {}, text));
  }

  // ------------------------------------------------------------------ run-time budget
  function plan() {
    const questions = S.questions.reduce((sum, q) => sum + (q.est_seconds || 0), 0);
    const intro = Number(S.podcast.intro_seconds) || 0;
    const outro = Number(S.podcast.outro_seconds) || 0;
    const target = (Number(S.podcast.target_minutes) || 30) * 60;
    const planned = intro + questions + outro;
    const status = planned < target * 0.9 ? "under" : planned > target * 1.05 ? "over" : "on_track";
    return { questions, intro, outro, target, planned, status };
  }

  function renderPlan() {
    const p = plan();
    const bar = clear($("[data-plan-bar]"));
    const total = Math.max(p.target, p.planned) || 1;
    [
      [p.intro, "bg-sky-400", "Intro"],
      [p.questions, p.status === "over" ? "bg-rose-500" : "bg-violet-500", "Questions"],
      [p.outro, "bg-indigo-400", "Outro"],
    ].forEach(([seconds, cls, label]) => {
      const segment = h("div", { class: `h-full ${cls}`, title: `${label}: ${fmt(seconds)}` });
      segment.style.width = `${(seconds / total) * 100}%`;
      bar.append(segment);
    });
    const text = $("[data-plan-text]");
    const detail =
      p.status === "over"
        ? `over by ${fmt(p.planned - p.target)} - trim or shorten some questions.`
        : p.status === "under"
          ? `${fmt(p.target - p.planned)} left to fill.`
          : "on track.";
    text.textContent = `${fmt(p.planned)} planned of ${fmt(p.target)} (intro ${fmt(p.intro)}, questions ${fmt(p.questions)}, outro ${fmt(p.outro)}) - ${detail}`;
    text.className =
      "mt-2 text-sm " +
      (p.status === "over"
        ? "text-rose-600 dark:text-rose-400"
        : p.status === "on_track"
          ? "text-emerald-700 dark:text-emerald-400"
          : "text-zinc-600 dark:text-zinc-300");
    const suggestion = clamp(Math.round((p.target - p.planned) / 150), 1, Math.min(8, S.limits.generate));
    const countInput = $("[data-gen-count]");
    if (countInput && !countInput.dataset.touched) countInput.value = String(p.planned >= p.target ? 3 : suggestion);
  }

  let planTimer = 0;
  [
    ["[data-plan-target]", "target_minutes", 5, 240],
    ["[data-plan-intro]", "intro_seconds", 0, 900],
    ["[data-plan-outro]", "outro_seconds", 0, 900],
  ].forEach(([selector, field, low, high]) => {
    const input = $(selector);
    input.addEventListener("input", () => {
      const value = Number(input.value);
      if (!Number.isFinite(value) || value < low || value > high) return;
      S.podcast[field] = value;
      renderPlan();
      clearTimeout(planTimer);
      planTimer = setTimeout(async () => {
        try {
          const result = await api("PATCH", `/api/podcasts/${pid}`, {
            target_minutes: S.podcast.target_minutes,
            intro_seconds: S.podcast.intro_seconds,
            outro_seconds: S.podcast.outro_seconds,
          });
          S.podcast = result.podcast;
        } catch (error) {
          toast(error.message, "error");
        }
      }, 600);
    });
  });
  const genCount = $("[data-gen-count]");
  if (genCount) genCount.addEventListener("input", () => (genCount.dataset.touched = "1"));

  // ------------------------------------------------------------------ questions
  function applyQuestions(list) {
    S.questions = list;
    const ids = new Set(list.map((q) => q.id));
    S.selected.forEach((id) => ids.has(id) || S.selected.delete(id));
    renderQuestions();
    renderPlan();
  }

  function upsertQuestion(question) {
    const index = S.questions.findIndex((q) => q.id === question.id);
    if (index >= 0) S.questions[index] = question;
    else S.questions.push(question);
  }

  async function patchQuestion(id, body) {
    try {
      const result = await api("PATCH", `/api/questions/${id}`, body);
      upsertQuestion(result.question);
      renderQuestions();
      renderPlan();
    } catch (error) {
      toast(error.message, "error");
      await refresh();
    }
  }

  function categoryControl(q) {
    if (!isHost) {
      return h("span", { class: `badge ${CATEGORY_CLASS[q.category] || CATEGORY_CLASS.core}` }, S.categories[q.category] || q.category);
    }
    const select = h("select", {
      class: `badge cursor-pointer border-0 ${CATEGORY_CLASS[q.category] || CATEGORY_CLASS.core}`,
      "aria-label": "Category",
      onchange: (event) => patchQuestion(q.id, { category: event.target.value }),
    });
    Object.entries(S.categories).forEach(([key, label]) => select.append(h("option", { value: key, selected: key === q.category }, label)));
    return select;
  }

  function secondsControl(q) {
    if (!isHost) return h("span", { class: "badge bg-zinc-100 text-zinc-600 dark:bg-zinc-800 dark:text-zinc-400" }, `slot ${fmt(q.est_seconds)}`);
    const input = h("input", {
      type: "number",
      min: "10",
      max: "1800",
      step: "10",
      value: String(q.est_seconds),
      class: "w-20 rounded-md border border-zinc-300 bg-transparent px-1.5 py-0.5 text-xs dark:border-zinc-700",
      "aria-label": "Time slot in seconds",
      title: "Time slot (seconds)",
      onchange: (event) => {
        const value = Number(event.target.value);
        if (value >= 10 && value <= 1800) patchQuestion(q.id, { est_seconds: Math.round(value) });
      },
    });
    return h("label", { class: "flex items-center gap-1 text-zinc-500" }, input, h("span", {}, `s (${fmt(q.est_seconds)})`));
  }

  function textEditor(q) {
    const area = h("textarea", {
      class: "mt-2 w-full resize-none rounded-lg border border-transparent bg-transparent p-1 text-sm font-medium hover:border-zinc-300 focus:border-violet-500 focus:outline-none dark:hover:border-zinc-700",
      rows: "1",
      maxlength: "500",
      "aria-label": "Question text",
    });
    area.value = q.text;
    const fit = () => {
      area.style.height = "auto";
      area.style.height = `${area.scrollHeight}px`;
    };
    requestAnimationFrame(fit);
    area.addEventListener("input", fit);
    area.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) area.blur();
      if (event.key === "Escape") {
        area.value = q.text;
        area.blur();
      }
    });
    area.addEventListener("blur", () => {
      const value = area.value.trim();
      if (value && value !== q.text) patchQuestion(q.id, { text: value });
      else area.value = q.text;
    });
    return area;
  }

  function answerBadge(answer) {
    if (!answer) return h("span", { class: "badge bg-zinc-100 text-zinc-500 dark:bg-zinc-800" }, "Not practised");
    if (answer.score !== null && answer.score !== undefined) {
      const cls =
        answer.score >= 8
          ? "bg-emerald-100 text-emerald-800 dark:bg-emerald-500/15 dark:text-emerald-200"
          : answer.score >= 5
            ? "bg-amber-100 text-amber-800 dark:bg-amber-500/15 dark:text-amber-200"
            : "bg-rose-100 text-rose-800 dark:bg-rose-500/15 dark:text-rose-200";
      return h("span", { class: `badge ${cls}` }, `${answer.score}/10`);
    }
    return h("span", { class: "badge bg-zinc-100 text-zinc-600 dark:bg-zinc-800 dark:text-zinc-300" }, answer.status === "failed" ? "Feedback failed" : "Answered");
  }

  function iconButton(label, glyph, onclick, disabled = false) {
    return h("button", { type: "button", class: "btn-icon", "aria-label": label, title: label, onclick, disabled }, glyph);
  }

  function questionCard(q, index, total) {
    const flags = q.flags || {};
    const issues = [];
    if (flags.match) {
      const repeat = flags.match.kind === "duplicate";
      const how =
        flags.match.method === "ai"
          ? "Mira checked: the same question in other words"
          : `${flags.match.method === "meaning" ? "same meaning" : "same wording"}, ${Math.round((flags.match.similarity || 0) * 100)}%`;
      issues.push(
        h(
          "p",
          { class: repeat ? "text-rose-700 dark:text-rose-300" : "text-amber-700 dark:text-amber-300" },
          `${repeat ? "Repeats" : "Similar to"} another question (${how}): “${flags.match.text}”`,
        ),
      );
    }
    if (flags.asked_before) {
      issues.push(
        h("p", { class: "text-amber-700 dark:text-amber-300" }, `Asked before in “${flags.asked_before.podcast_title}”: “${flags.asked_before.text}”`),
      );
    }
    (flags.quality || []).forEach((flag) => issues.push(h("p", { class: "text-zinc-600 dark:text-zinc-400" }, `Tip: ${S.flagHelp[flag] || flag}`)));

    const actions = [
      iconButton("Move up", "↑", () => move(q.id, -1), index === 0),
      iconButton("Move down", "↓", () => move(q.id, 1), index === total - 1),
      iconButton("Read aloud", "🔊", () => voice.speak(q.text)),
    ];
    if (isHost) {
      actions.push(
        iconButton("Rewrite with Mira", "✨", () => {
          S.selected = new Set([q.id]);
          renderQuestions();
          const input = $("[data-rewrite-instruction]");
          input.focus();
          input.scrollIntoView({ behavior: "smooth", block: "center" });
        }),
      );
    }
    actions.push(iconButton("Undo last change", "↶", () => undo(q.id)), iconButton("Delete", "✕", () => removeQuestion(q.id)));

    const highlighted = !isHost && S.currentId === q.id;
    const li = h("li", {
      class: `rounded-xl border p-3 transition ${highlighted ? "border-violet-400 ring-2 ring-violet-500/40 dark:border-violet-500" : "border-zinc-200 dark:border-zinc-800"}`,
      "data-qid": q.id,
    });
    const header = h(
      "div",
      { class: "flex flex-wrap items-center gap-2 text-xs" },
      isHost
        ? h("input", {
            type: "checkbox",
            class: "h-4 w-4 accent-violet-600",
            "aria-label": `Select question ${index + 1}`,
            checked: S.selected.has(q.id),
            onchange: (event) => {
              if (event.target.checked) S.selected.add(q.id);
              else S.selected.delete(q.id);
              updateSelectedCount();
            },
          })
        : null,
      h("span", { class: "font-semibold text-zinc-500" }, `Q${index + 1}`),
      categoryControl(q),
      secondsControl(q),
      h("span", { class: "badge bg-zinc-100 text-zinc-600 dark:bg-zinc-800 dark:text-zinc-400" }, SOURCE_LABEL[q.source] || q.source),
      flags.match
        ? h(
            "span",
            {
              class:
                flags.match.kind === "duplicate"
                  ? "badge bg-rose-100 text-rose-800 dark:bg-rose-500/15 dark:text-rose-200"
                  : "badge bg-amber-100 text-amber-800 dark:bg-amber-500/15 dark:text-amber-200",
            },
            flags.match.kind === "duplicate" ? "Repeat" : "Similar",
          )
        : null,
      flags.asked_before ? h("span", { class: "badge bg-amber-100 text-amber-800 dark:bg-amber-500/15 dark:text-amber-200" }, "Asked before") : null,
      !isHost ? answerBadge(S.answers[q.id]) : null,
      h("span", { class: "ml-auto flex items-center" }, actions),
    );
    const body = isHost
      ? textEditor(q)
      : h(
          "button",
          { type: "button", class: "mt-2 block w-full text-left text-sm font-medium hover:text-violet-700 dark:hover:text-violet-300", onclick: () => selectQuestion(q.id, true) },
          q.text,
        );
    li.append(header, body);
    if (issues.length) li.append(h("div", { class: "mt-2 space-y-1 text-xs" }, issues));
    return li;
  }

  function updateSelectedCount() {
    const label = $("[data-selected-count]");
    if (label) label.textContent = `${S.selected.size} selected`;
    const all = $("[data-select-all]");
    if (all) all.checked = S.questions.length > 0 && S.selected.size === S.questions.length;
  }

  function renderQuestions() {
    const list = clear($("[data-q-list]"));
    const questions = ordered();
    $("[data-q-count]").textContent = `(${questions.length}/${S.limits.questions})`;
    $("[data-q-empty]").classList.toggle("hidden", questions.length > 0);
    questions.forEach((q, i) => list.append(questionCard(q, i, questions.length)));
    updateSelectedCount();
    if (!isHost) renderPractice();
  }

  async function move(id, delta) {
    const questions = ordered();
    const i = questions.findIndex((q) => q.id === id);
    const j = i + delta;
    if (i < 0 || j < 0 || j >= questions.length) return;
    [questions[i], questions[j]] = [questions[j], questions[i]];
    questions.forEach((q, k) => (q.position = k + 1));
    renderQuestions();
    try {
      const result = await api("POST", `/api/podcasts/${pid}/questions/reorder`, { ids: questions.map((q) => q.id) });
      applyQuestions(result.questions);
    } catch (error) {
      toast(error.message, "error");
      await refresh();
    }
  }

  async function undo(id) {
    try {
      const result = await api("POST", `/api/questions/${id}/undo`, {});
      upsertQuestion(result.question);
      renderQuestions();
      renderPlan();
      toast("Change undone.", "success", 2500);
    } catch (error) {
      toast(error.message, "info");
    }
  }

  async function removeQuestion(id) {
    if (!window.confirm("Delete this question?")) return;
    try {
      const result = await api("DELETE", `/api/questions/${id}`);
      S.selected.delete(id);
      applyQuestions(result.questions);
    } catch (error) {
      toast(error.message, "error");
    }
  }

  async function addQuestion(text, category = "core", seconds = 120) {
    try {
      const result = await api("POST", `/api/podcasts/${pid}/questions`, { text, category, est_seconds: seconds }, { idem: true });
      upsertQuestion(result.question);
      renderQuestions();
      renderPlan();
      const flags = result.question.flags || {};
      if (flags.match && flags.match.kind === "duplicate") toast("Added - but it repeats an existing question.", "warn");
      else toast("Question added.", "success", 2500);
      return true;
    } catch (error) {
      if (!(error.status === 409 && error.data && error.data.code === "duplicate")) toast(error.message, "error");
      return false;
    }
  }

  $("[data-add-form]").addEventListener("submit", async (event) => {
    event.preventDefault();
    const textArea = $("[data-add-text]");
    const text = textArea.value.trim();
    if (!text) return textArea.focus();
    const button = event.submitter;
    if (button) button.disabled = true;
    const ok = await addQuestion(text, $("[data-add-category]").value, clamp(Number($("[data-add-seconds]").value) || 120, 10, 1800));
    if (button) button.disabled = false;
    if (ok) textArea.value = "";
  });

  // ------------------------------------------------------------------ AI actions
  $("[data-generate]").addEventListener("click", (event) => {
    const count = clamp(Number($("[data-gen-count]").value) || 5, 1, S.limits.generate);
    startJob(event.currentTarget, `/api/podcasts/${pid}/ai/generate`, { count, focus: $("[data-gen-focus]").value.trim() });
  });

  $$("[data-preset]").forEach((button) =>
    button.addEventListener("click", () => {
      $("[data-rewrite-instruction]").value = button.dataset.preset;
    }),
  );

  const selectAll = $("[data-select-all]");
  selectAll.addEventListener("change", () => {
    S.selected = selectAll.checked ? new Set(S.questions.map((q) => q.id)) : new Set();
    renderQuestions();
  });

  $("[data-rewrite]").addEventListener("click", async (event) => {
    const ids = ordered()
      .filter((q) => S.selected.has(q.id))
      .map((q) => q.id);
    const instruction = $("[data-rewrite-instruction]").value.trim();
    if (!ids.length) return toast("Select at least one question (or click ✨ on a question).", "info");
    if (ids.length > S.limits.rewrite) return toast(`Select at most ${S.limits.rewrite} questions at once.`, "info");
    if (instruction.length < 2) return toast("Tell Mira how to change them, e.g. “make them shorter”.", "info");
    const result = await startJob(event.currentTarget, `/api/podcasts/${pid}/ai/rewrite`, {
      question_ids: ids,
      instruction,
      remember: $("[data-remember]").checked,
    });
    if (result && result.standing_instructions) {
      S.podcast.standing_instructions = result.standing_instructions;
      renderInstructions();
    }
  });

  $("[data-quality]").addEventListener("click", async (event) => {
    const button = event.currentTarget;
    button.disabled = true;
    try {
      const result = await api("POST", `/api/podcasts/${pid}/quality-check`, {});
      applyQuestions(result.questions);
      const s = result.summary;
      const found = [
        s.duplicates && `${s.duplicates} repeat${s.duplicates === 1 ? "" : "s"}`,
        s.similar && `${s.similar} similar`,
        s.asked_before && `${s.asked_before} asked before`,
        s.quality && `${s.quality} with tips`,
      ].filter(Boolean);
      const unsure = s.to_review || 0;
      const busy = [...S.jobs.values()].some((view) => JOB_GROUP[view.kind] === "questions");
      if (unsure && !busy) {
        // Pairs too close to call by meaning alone: Mira double-checks them as an AI task.
        startJob(null, `/api/podcasts/${pid}/ai/review-repeats`, {});
      }
      const pending = unsure
        ? ` Mira ${busy ? "will double-check" : "is double-checking"} ${unsure} possible repeat${unsure === 1 ? "" : "s"}${busy ? " when she's free - check again then" : ""}.`
        : "";
      toast(
        (found.length ? `Found: ${found.join(", ")}.` : unsure ? "No clear repeats or weak questions." : "All clear - no repeats or weak questions found.") + pending,
        found.length ? "warn" : "success",
      );
    } catch (error) {
      toast(error.message, "error");
    } finally {
      button.disabled = false;
    }
  });

  $("[data-read-all]").addEventListener("click", () => {
    const text = ordered()
      .map((q, i) => `Question ${i + 1}. ${q.text}`)
      .join(" ");
    if (text) voice.speak(text);
  });

  // ------------------------------------------------------------------ socket events
  bus.on("job:update", (event) => {
    if (!mine(event)) return;
    const view = ensureJobView({ id: event.job_id, kind: event.kind });
    view.status.textContent = event.provider ? `Working with ${event.provider}…` : "Starting…";
  });
  bus.on("job:delta", (event) => {
    if (!mine(event)) return;
    const view = ensureJobView({ id: event.job_id, kind: event.kind });
    view.box.classList.remove("hidden");
    view.text = (view.text + event.text).slice(-20000);
    view.box.textContent = view.text;
    view.box.scrollTop = view.box.scrollHeight;
  });
  bus.on("job:reset", (event) => {
    if (!mine(event)) return;
    const view = ensureJobView({ id: event.job_id, kind: event.kind });
    view.text = "";
    view.box.textContent = "";
    note(view, event.reason);
  });
  bus.on("job:question", (event) => {
    if (!mine(event)) return;
    upsertQuestion(event.question);
    renderQuestions();
    renderPlan();
    if (event.ask && !isHost) selectQuestion(event.question.id, true);
  });
  bus.on("job:skipped", (event) => {
    if (!mine(event)) return;
    note(S.jobs.get(event.job_id), `Skipped: “${event.text}” - ${event.reason}`);
  });
  bus.on("job:flags", (event) => {
    if (mine(event)) applyQuestions(event.questions);
  });
  bus.on("job:feedback", (event) => {
    if (!mine(event)) return;
    const previous = S.answers[event.answer.question_id];
    S.answers[event.answer.question_id] = { ...event.answer, attempts: previous ? previous.attempts : 1 };
    renderQuestions();
    if (S.currentId === event.answer.question_id) {
      renderFeedback(event.answer);
      loadAttempts();
    }
  });
  bus.on("job:done", (event) => {
    if (!mine(event)) return;
    const result = event.result || {};
    let message = "Done.";
    if (event.kind === "generate_questions") {
      const skipped = (result.skipped || []).length;
      message = result.added
        ? `Added ${result.added} question${result.added === 1 ? "" : "s"}${skipped ? `, skipped ${skipped} repeat${skipped === 1 ? "" : "s"}` : ""}.`
        : result.message || "No new questions.";
    } else if (event.kind === "rewrite_questions") {
      message = `Rewrote ${result.updated} of ${result.requested} question${result.requested === 1 ? "" : "s"}. Use ↶ to undo any of them.`;
    } else if (event.kind === "answer_feedback") {
      message = result.score !== null && result.score !== undefined ? `Feedback ready: ${result.score}/10.` : "Feedback ready.";
    } else if (event.kind === "next_question") {
      message = "New question added.";
    } else if (event.kind === "review_repeats") {
      const checked = result.checked || 0;
      const repeats = result.repeats || 0;
      message = checked
        ? `Mira double-checked ${checked} pair${checked === 1 ? "" : "s"}: ${
            repeats ? `${repeats} ${repeats === 1 ? "is the same question" : "are the same questions"} in other words (marked Similar).` : "no repeats."
          }`
        : "Nothing new to double-check.";
    }
    finishJob(event.job_id, true, message + (event.provider ? ` (${event.provider})` : ""));
    toast(message, "success");
    refresh();
  });
  bus.on("job:error", (event) => {
    if (!mine(event)) return;
    finishJob(event.job_id, false, event.message || "Failed.");
    if (event.status !== "cancelled") toast(event.message || "The AI task failed.", "error", 12000);
    refresh();
  });
  bus.on("socket:connect", () => refresh()); // catch anything that happened before the socket joined
  bus.on("socket:reconnect", () => refresh());
  bus.on("ai:status", renderAIStatus);

  // ------------------------------------------------------------------ AI provider status
  function renderAIStatus(summary) {
    if (!summary) return;
    const list = clear($("[data-ai-status]"));
    (summary.providers || []).forEach((p) =>
      list.append(
        h(
          "li",
          { class: "flex items-start gap-2" },
          h("span", { class: `mt-1.5 h-2 w-2 shrink-0 rounded-full ${DOT_CLASS[p.status] || "bg-zinc-400"}` }),
          h(
            "span",
            { class: "min-w-0" },
            h("span", { class: "block font-medium" }, p.label),
            h("span", { class: "block truncate text-xs text-zinc-500 dark:text-zinc-400", title: p.detail || "" }, p.status_label + (p.detail ? ` - ${p.detail}` : "")),
          ),
        ),
      ),
    );
    if (summary.worker_alive === false) {
      list.append(h("li", { class: "text-xs font-medium text-rose-600 dark:text-rose-400" }, "AI worker is not running."));
    }
    if (summary.voice && summary.voice.status === "offline") voice.setStatus("Voice: browser voice (Kokoro offline)");
    const banner = $("#ai-banner");
    const none = summary.any_available === false;
    banner.classList.toggle("hidden", !none);
    banner.textContent = none
      ? summary.worker_alive === false
        ? "AI features are paused: the AI worker isn't running. Start the app with the installer (or `docker compose up -d worker`)."
        : "No AI is available right now. " +
          (summary.providers || []).map((p) => `${p.label}: ${p.status_label.toLowerCase()}`).join(" · ") +
          ". Check the AI gateway, wait for the local Gemma download, or add a Claude/ChatGPT API key."
      : "";
  }

  // ------------------------------------------------------------------ standing instructions ("Mira remembers")
  function renderInstructions() {
    const list = clear($("[data-instructions]"));
    const items = S.podcast.standing_instructions || [];
    if (!items.length) list.append(h("li", { class: "text-xs text-zinc-500" }, "Nothing yet."));
    items.forEach((text, i) =>
      list.append(
        h(
          "li",
          { class: "flex items-start gap-2 rounded-lg bg-zinc-50 px-2 py-1.5 dark:bg-zinc-800/60" },
          h("span", { class: "min-w-0 flex-1 break-words" }, text),
          h("button", { type: "button", class: "btn-icon h-6 w-6", "aria-label": `Forget: ${text}`, onclick: () => saveInstructions(items.filter((_, k) => k !== i)) }, "×"),
        ),
      ),
    );
  }

  async function saveInstructions(list) {
    try {
      const result = await api("PATCH", `/api/podcasts/${pid}`, { standing_instructions: list });
      S.podcast = result.podcast;
      renderInstructions();
      return true;
    } catch (error) {
      toast(error.message, "error");
      return false;
    }
  }

  $("[data-instruction-form]").addEventListener("submit", async (event) => {
    event.preventDefault();
    const input = $("[data-instruction-input]");
    const text = input.value.trim();
    if (!text) return;
    const items = S.podcast.standing_instructions || [];
    if (items.length >= S.limits.instructions) return toast(`Mira can remember up to ${S.limits.instructions} instructions per podcast.`, "info");
    if (await saveInstructions([...items, text])) input.value = "";
  });

  // ------------------------------------------------------------------ exports
  function renderExports() {
    const list = clear($("[data-exports]"));
    if (!S.exports.length) list.append(h("li", { class: "text-xs text-zinc-500" }, "No PDFs yet."));
    S.exports.forEach((item) =>
      list.append(
        h(
          "li",
          { class: "flex items-center justify-between gap-2" },
          h("a", { href: item.url, class: "link min-w-0 truncate" }, item.filename),
          h("span", { class: "shrink-0 text-xs text-zinc-500" }, `${bytes(item.size_bytes)} · ${shortDate(item.created_at)}`),
        ),
      ),
    );
  }

  $$("[data-export]").forEach((button) =>
    button.addEventListener("click", async () => {
      const buttons = $$("[data-export]");
      buttons.forEach((b) => (b.disabled = true));
      toast("Preparing your PDF…", "info", 2500);
      try {
        const result = await api("POST", `/api/podcasts/${pid}/export`, {});
        S.exports = [result.export, ...S.exports].slice(0, 10);
        renderExports();
        window.location.href = result.export.url;
      } catch (error) {
        toast(error.message, "error");
      } finally {
        buttons.forEach((b) => (b.disabled = false));
      }
    }),
  );

  // ------------------------------------------------------------------ guest practice
  const answerBox = $("[data-answer]");
  let stopwatch = { running: false, start: 0, elapsed: 0, timer: 0 };

  function current() {
    return S.questions.find((q) => q.id === S.currentId) || null;
  }

  function rehearsedSeconds() {
    const ms = stopwatch.elapsed + (stopwatch.running ? Date.now() - stopwatch.start : 0);
    return Math.round(ms / 1000);
  }

  function updateMeter() {
    if (!answerBox) return;
    const q = current();
    const words = countWords(answerBox.value);
    const seconds = Math.round((words * 60) / (S.prefs.wpm || 150));
    const slot = Math.max(20, ((q && q.est_seconds) || 120) - 10);
    const ratio = seconds / slot;
    $("[data-answer-meter]").textContent =
      `${words} words · about ${fmt(seconds)} spoken · fits ${fmt(slot)}` + (ratio > 1.15 ? " - too long" : ratio < 0.4 && words ? " - quite short" : "");
    const bar = $("[data-answer-bar]");
    bar.style.width = `${Math.min(100, ratio * 100)}%`;
    bar.className = "h-full rounded-full transition-all " + (ratio > 1.15 ? "bg-rose-500" : ratio > 0.9 ? "bg-amber-500" : "bg-emerald-500");
    if (S.currentId) S.drafts[S.currentId] = answerBox.value;
  }

  function resetStopwatch() {
    clearInterval(stopwatch.timer);
    stopwatch = { running: false, start: 0, elapsed: 0, timer: 0 };
    const display = $("[data-stopwatch-display]");
    if (display) display.textContent = "0:00";
    const button = $("[data-stopwatch]");
    if (button) button.textContent = "Start rehearsal timer";
  }

  function selectQuestion(id, speak = false) {
    if (S.currentId !== id) resetStopwatch();
    S.currentId = id;
    renderQuestions();
    loadAttempts();
    const q = current();
    if (speak && q) voice.speak(q.text);
    const panel = $("[data-practice]");
    if (panel && speak) panel.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  function renderPractice() {
    if (isHost) return;
    const questions = ordered();
    const empty = questions.length === 0;
    $("[data-practice-empty]").classList.toggle("hidden", !empty);
    $("[data-practice-body]").classList.toggle("hidden", empty);
    if (empty) return;
    if (!current()) {
      const firstOpen = questions.find((q) => !S.answers[q.id]) || questions[0];
      S.currentId = firstOpen.id;
    }
    const q = current();
    const index = questions.indexOf(q);
    $("[data-practice-meta]").textContent = `Question ${index + 1} of ${questions.length} · ${S.categories[q.category] || q.category} · slot ${fmt(q.est_seconds)}`;
    $("[data-practice-question]").textContent = q.text;
    if (S.draftFor !== q.id) {
      S.draftFor = q.id;
      answerBox.value = S.drafts[q.id] || "";
    }
    updateMeter();
    const latest = S.answers[q.id];
    if (latest) renderFeedback(latest);
    else $("[data-feedback]").classList.add("hidden");
  }

  function feedbackList(title, items, cls) {
    return h(
      "div",
      {},
      h("p", { class: `text-sm font-semibold ${cls}` }, title),
      h("ul", { class: "mt-1 list-disc space-y-1 pl-5 text-sm" }, items.map((item) => h("li", {}, item))),
    );
  }

  function renderFeedback(answer) {
    const box = clear($("[data-feedback]"));
    if (!box) return;
    box.classList.remove("hidden");
    if (answer.status === "failed") {
      box.append(h("div", { class: "alert alert-error" }, "Feedback failed for this attempt. Change something and check it again."));
      return;
    }
    if (!answer.feedback) {
      if (answer.status === "queued" || answer.status === "reviewing") {
        box.append(h("div", { class: "alert alert-info" }, "Mira is checking your answer - the live output is shown above."));
      } else {
        box.append(h("div", { class: "alert alert-info" }, `Saved ${shortDate(answer.created_at)} without feedback.`));
      }
      return;
    }
    const fb = answer.feedback;
    const score = fb.score;
    const scoreClass =
      score === null || score === undefined
        ? "bg-zinc-200 text-zinc-700 dark:bg-zinc-700 dark:text-zinc-100"
        : score >= 8
          ? "bg-emerald-500 text-white"
          : score >= 5
            ? "bg-amber-500 text-white"
            : "bg-rose-500 text-white";
    const parts = [
      h(
        "div",
        { class: "flex items-center gap-4" },
        h(
          "div",
          { class: `flex h-16 w-16 shrink-0 flex-col items-center justify-center rounded-full ${scoreClass}` },
          h("span", { class: "text-xl leading-none font-bold" }, score === null || score === undefined ? "?" : String(score)),
          h("span", { class: "text-[10px] uppercase" }, "/ 10"),
        ),
        h(
          "div",
          {},
          h("p", { class: "font-semibold" }, "Mira's feedback"),
          h(
            "p",
            { class: "text-xs text-zinc-500 dark:text-zinc-400" },
            `${answer.word_count} words · about ${fmt(answer.est_seconds)} spoken` +
              (answer.rehearsed_seconds ? ` · rehearsed ${fmt(answer.rehearsed_seconds)}` : "") +
              (answer.provider ? ` · by ${answer.provider}` : ""),
          ),
        ),
      ),
    ];
    if (answer.redundant_with) {
      parts.push(h("div", { class: "alert alert-warn" }, `This overlaps with your answer to “${answer.redundant_with.question}”. Try a fresh story or angle.`));
    }
    if (fb.raw) parts.push(h("p", { class: "text-sm whitespace-pre-line" }, fb.raw));
    if (fb.worked && fb.worked.length) parts.push(feedbackList("What worked", fb.worked, "text-emerald-700 dark:text-emerald-400"));
    if (fb.improve && fb.improve.length) parts.push(feedbackList("To improve", fb.improve, "text-amber-700 dark:text-amber-400"));
    if (fb.stronger_answer) {
      parts.push(
        h(
          "div",
          { class: "rounded-xl bg-violet-50 p-4 dark:bg-violet-950/40" },
          h(
            "div",
            { class: "flex flex-wrap items-center justify-between gap-2" },
            h("p", { class: "text-sm font-semibold" }, "A stronger version"),
            h(
              "div",
              { class: "flex gap-2" },
              h("button", { type: "button", class: "btn btn-ghost btn-sm", onclick: () => voice.speak(fb.stronger_answer) }, "🔊 Hear it"),
              h(
                "button",
                {
                  type: "button",
                  class: "btn btn-secondary btn-sm",
                  onclick: () => {
                    answerBox.value = fb.stronger_answer;
                    updateMeter();
                    answerBox.focus();
                  },
                },
                "Use as my draft",
              ),
            ),
          ),
          h("p", { class: "mt-2 text-sm whitespace-pre-line" }, fb.stronger_answer),
        ),
      );
    }
    if (fb.timing) parts.push(h("p", { class: "text-sm" }, h("strong", {}, "Timing: "), fb.timing));
    if (fb.follow_up) {
      parts.push(
        h(
          "div",
          { class: "flex flex-wrap items-center gap-2 text-sm" },
          h("span", { class: "min-w-0" }, h("strong", {}, "Likely follow-up: "), fb.follow_up),
          h("button", { type: "button", class: "btn btn-secondary btn-sm", onclick: () => addQuestion(fb.follow_up, "follow_up", 120) }, "Add to my questions"),
          h(
            "button",
            {
              type: "button",
              class: "btn btn-ghost btn-sm",
              "data-ai-action": "questions",
              onclick: (event) => startJob(event.currentTarget, `/api/podcasts/${pid}/ai/next`, { answer_id: answer.id }),
            },
            "Have Mira ask a follow-up",
          ),
        ),
      );
    }
    box.append(h("div", { class: "space-y-4" }, parts));
    refreshBusy();
  }

  async function loadAttempts() {
    const list = $("[data-attempts-list]");
    if (!list || !S.currentId) return;
    const id = S.currentId;
    try {
      const result = await api("GET", `/api/questions/${id}/answers`);
      if (id !== S.currentId) return;
      clear(list);
      if (!result.answers.length) list.append(h("li", { class: "text-xs text-zinc-500" }, "No attempts yet."));
      result.answers.forEach((attempt) =>
        list.append(
          h(
            "li",
            { class: "flex items-center justify-between gap-2 rounded-lg bg-zinc-50 px-3 py-2 dark:bg-zinc-800/60" },
            h("span", { class: "min-w-0 truncate" }, attempt.text),
            h(
              "span",
              { class: "flex shrink-0 items-center gap-2 text-xs text-zinc-500" },
              attempt.score !== null && attempt.score !== undefined ? `${attempt.score}/10` : attempt.status,
              `· ${attempt.word_count} words`,
              h(
                "button",
                {
                  type: "button",
                  class: "btn btn-ghost btn-sm",
                  onclick: () => {
                    answerBox.value = attempt.text;
                    updateMeter();
                    renderFeedback(attempt);
                  },
                },
                "Open",
              ),
            ),
          ),
        ),
      );
    } catch {
      /* non-critical */
    }
  }

  async function submitAnswer(feedback, button) {
    const q = current();
    if (!q) return;
    const text = answerBox.value.trim();
    if (!text) return toast("Write your answer first.", "info");
    button.dataset.pending = "1";
    button.disabled = true;
    try {
      const seconds = rehearsedSeconds();
      const result = await api(
        "POST",
        `/api/questions/${q.id}/answers`,
        { text, rehearsed_seconds: seconds > 0 ? seconds : null, feedback },
        { idem: true },
      );
      const previous = S.answers[q.id];
      S.answers[q.id] = { ...result.answer, attempts: (previous ? previous.attempts : 0) + 1 };
      if (result.job) ensureJobView(result.job);
      else if (result.job_error) toast(`Saved, but feedback isn't available right now: ${result.job_error}`, "warn", 12000);
      else toast("Answer saved.", "success", 2500);
      renderQuestions();
      renderFeedback(S.answers[q.id]);
      loadAttempts();
    } catch (error) {
      if (!(error.status === 409 && error.data && error.data.code === "duplicate")) toast(error.message, "error", 9000);
    } finally {
      delete button.dataset.pending;
      button.disabled = false;
      refreshBusy();
    }
  }

  if (!isHost) {
    answerBox.addEventListener("input", updateMeter);
    $("[data-submit-answer]").addEventListener("click", (event) => submitAnswer(true, event.currentTarget));
    $("[data-save-answer]").addEventListener("click", (event) => submitAnswer(false, event.currentTarget));
    $("[data-ask-aloud]").addEventListener("click", () => {
      const q = current();
      if (q) voice.speak(q.text);
    });
    $("[data-next-q]").addEventListener("click", () => {
      const questions = ordered();
      const i = questions.findIndex((q) => q.id === S.currentId);
      if (questions.length) selectQuestion(questions[(i + 1) % questions.length].id, true);
    });
    $("[data-prev-q]").addEventListener("click", () => {
      const questions = ordered();
      const i = questions.findIndex((q) => q.id === S.currentId);
      if (questions.length) selectQuestion(questions[(i - 1 + questions.length) % questions.length].id, true);
    });
    $("[data-ai-next]").addEventListener("click", (event) => startJob(event.currentTarget, `/api/podcasts/${pid}/ai/next`, {}));
    $("[data-stopwatch]").addEventListener("click", (event) => {
      const button = event.currentTarget;
      const display = $("[data-stopwatch-display]");
      if (stopwatch.running) {
        stopwatch.elapsed += Date.now() - stopwatch.start;
        stopwatch.running = false;
        clearInterval(stopwatch.timer);
        button.textContent = "Resume timer";
      } else {
        stopwatch.start = Date.now();
        stopwatch.running = true;
        stopwatch.timer = setInterval(() => {
          display.textContent = fmt(rehearsedSeconds());
        }, 250);
        button.textContent = "Stop timer";
      }
    });
    $("[data-stopwatch-reset]").addEventListener("click", resetStopwatch);
  }

  // ------------------------------------------------------------------ sync
  let refreshing = null;
  async function refresh() {
    if (refreshing) return refreshing;
    refreshing = (async () => {
      try {
        const state = await api("GET", `/api/podcasts/${pid}`);
        S.podcast = state.podcast;
        S.answers = state.answers;
        S.exports = state.exports;
        applyQuestions(state.questions);
        renderInstructions();
        renderExports();
        state.jobs.forEach((job) => ensureJobView(job));
        const active = new Set(state.jobs.map((job) => job.id));
        // Jobs that ended while we weren't listening: show their real outcome.
        const ended = [...S.jobs.keys()].filter((id) => !active.has(id));
        await Promise.all(
          ended.map(async (id) => {
            try {
              finishFromServer((await api("GET", `/api/jobs/${id}`)).job);
            } catch {
              finishJob(id, false, "This task is no longer available.");
            }
          }),
        );
      } catch {
        /* keep the current view */
      } finally {
        refreshing = null;
      }
    })();
    return refreshing;
  }

  // ------------------------------------------------------------------ boot
  renderPlan();
  renderQuestions();
  renderInstructions();
  renderExports();
  if (!isHost) loadAttempts();
  (data.state.jobs || []).forEach(async (job) => {
    const view = ensureJobView(job);
    try {
      const result = await api("GET", `/api/jobs/${job.id}`);
      if (["succeeded", "failed", "cancelled"].includes(result.job.status)) {
        finishFromServer(result.job); // it ended before our socket joined
        refresh();
        return;
      }
      if (result.partial) {
        view.text = result.partial;
        view.box.textContent = result.partial;
        view.box.classList.remove("hidden");
      }
    } catch {
      /* ignore */
    }
  });
  api("GET", "/api/ai/status")
    .then(renderAIStatus)
    .catch(() => {});
  if (data.autostart) {
    window.history.replaceState(null, "", window.location.pathname);
    setTimeout(() => $("[data-generate]").click(), 300);
  }
}
