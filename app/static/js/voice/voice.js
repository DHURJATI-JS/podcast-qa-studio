// Mira's voice: Kokoro TTS through the app's /api/tts proxy (soft female voice), split into
// sentence-sized chunks that are fetched in parallel (the next chunk downloads while the
// current one plays). Falls back to the browser's own speech synthesis if Kokoro is offline.
import { csrfToken } from "../lib/api.js";

const FEMALE_HINT =
  /(female|woman|samantha|victoria|karen|moira|tessa|fiona|serena|zira|aria|jenny|libby|sonia|hazel|susan|emma|olivia|ava|allison|google uk english female|google us english)/i;

export function splitForSpeech(text, max = 220) {
  const clean = String(text || "").replace(/\s+/g, " ").trim();
  if (!clean) return [];
  const sentences = clean.match(/[^.!?]+[.!?]+["')\]]*|[^.!?]+$/g) || [clean];
  const chunks = [];
  let current = "";
  const push = (piece) => {
    if (!piece) return;
    if ((current + " " + piece).trim().length > max) {
      if (current) chunks.push(current);
      current = piece;
    } else {
      current = (current + " " + piece).trim();
    }
  };
  for (let sentence of sentences) {
    sentence = sentence.trim();
    while (sentence.length > max) {
      let cut = sentence.lastIndexOf(", ", max);
      if (cut < max * 0.4) cut = sentence.lastIndexOf(" ", max);
      if (cut <= 0) cut = max;
      push(sentence.slice(0, cut + 1).trim());
      sentence = sentence.slice(cut + 1).trim();
    }
    push(sentence);
  }
  if (current) chunks.push(current);
  return chunks;
}

function pickFemaleVoice() {
  if (!("speechSynthesis" in window)) return null;
  const voices = window.speechSynthesis.getVoices();
  return (
    voices.find((v) => /^en/i.test(v.lang) && FEMALE_HINT.test(v.name)) ||
    voices.find((v) => /^en/i.test(v.lang)) ||
    null
  );
}

export class Voice {
  constructor({ avatar, caption, status, getVoice, getSpeed }) {
    this.avatar = avatar;
    this.caption = caption;
    this.status = status;
    this.getVoice = getVoice;
    this.getSpeed = getSpeed;
    this.audio = new Audio();
    this.audio.preload = "auto";
    this.ctx = null;
    this.analyser = null;
    this.session = 0;
    this.kokoroRetryAt = 0;
    this.finishCurrent = null;
    if ("speechSynthesis" in window) window.speechSynthesis.getVoices(); // warm up the voice list
  }

  setStatus(text) {
    if (this.status) this.status.textContent = text;
  }

  setInitialStatus(state) {
    this.setStatus(state === "offline" ? "Voice: browser voice (Kokoro offline)" : "Voice: Kokoro, ready");
  }

  showCaption(text) {
    if (this.caption && text) this.caption.textContent = text;
  }

  ensureAudioGraph() {
    if (this.ctx) {
      if (this.ctx.state === "suspended") this.ctx.resume().catch(() => {});
      return;
    }
    const Ctx = window.AudioContext || window.webkitAudioContext;
    if (!Ctx) return;
    try {
      this.ctx = new Ctx();
      const source = this.ctx.createMediaElementSource(this.audio);
      this.analyser = this.ctx.createAnalyser();
      this.analyser.fftSize = 1024;
      this.analyser.smoothingTimeConstant = 0.6;
      source.connect(this.analyser);
      this.analyser.connect(this.ctx.destination);
    } catch {
      this.ctx = null;
      this.analyser = null;
    }
  }

  async speak(text) {
    this.stop();
    const session = ++this.session;
    const chunks = splitForSpeech(text);
    if (!chunks.length) return;
    this.ensureAudioGraph();
    const pending = new Map();
    const fetchChunk = (i) => {
      if (i >= chunks.length) return null;
      if (!pending.has(i)) pending.set(i, Date.now() < this.kokoroRetryAt ? Promise.resolve(null) : this.fetchAudio(chunks[i]));
      return pending.get(i);
    };
    try {
      for (let i = 0; i < chunks.length; i++) {
        if (session !== this.session) return;
        const current = fetchChunk(i);
        fetchChunk(i + 1); // parallel prefetch of the next sentence
        const url = await current;
        if (session !== this.session) return;
        this.showCaption(chunks[i]);
        if (url) await this.playUrl(url);
        else await this.browserSpeak(chunks[i]);
      }
    } finally {
      if (session === this.session) this.avatar && this.avatar.stop();
      pending.forEach((promise) => promise.then((url) => url && URL.revokeObjectURL(url)).catch(() => {}));
    }
  }

  async fetchAudio(chunk) {
    try {
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), 20000); // then use the browser's voice
      const response = await fetch("/api/tts", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken() },
        body: JSON.stringify({ text: chunk, voice: this.getVoice(), speed: this.getSpeed() }),
        credentials: "same-origin",
        signal: controller.signal,
      }).finally(() => clearTimeout(timer));
      if (response.status === 503) {
        this.kokoroRetryAt = Date.now() + 30000;
        this.setStatus("Voice: browser voice (Kokoro busy or offline)");
        return null;
      }
      if (!response.ok) return null;
      this.setStatus("Voice: Kokoro");
      return URL.createObjectURL(await response.blob());
    } catch {
      return null;
    }
  }

  playUrl(url) {
    return new Promise((resolve) => {
      const audio = this.audio;
      let settled = false;
      const done = () => {
        if (settled) return;
        settled = true;
        audio.onended = null;
        audio.onerror = null;
        this.finishCurrent = null;
        resolve();
      };
      this.finishCurrent = done;
      audio.onended = done;
      audio.onerror = done;
      audio.src = url;
      if (this.avatar) {
        if (this.analyser) this.avatar.useAnalyser(this.analyser);
        else this.avatar.useSimulation(60000);
      }
      const playing = audio.play();
      if (playing && typeof playing.catch === "function") playing.catch(() => done());
    });
  }

  browserSpeak(chunk) {
    return new Promise((resolve) => {
      if (!("speechSynthesis" in window)) return resolve();
      let settled = false;
      const done = () => {
        if (settled) return;
        settled = true;
        this.finishCurrent = null;
        resolve();
      };
      const utterance = new SpeechSynthesisUtterance(chunk);
      const voice = pickFemaleVoice();
      if (voice) utterance.voice = voice;
      utterance.rate = Number(this.getSpeed()) || 1;
      utterance.pitch = 1.1;
      utterance.volume = 0.95;
      const words = chunk.split(/\s+/).length;
      if (this.avatar) this.avatar.useSimulation((words / 2.6) * 1000 / utterance.rate);
      utterance.onboundary = (event) => {
        if (this.avatar && typeof event.charIndex === "number") {
          this.avatar.pulseWord(chunk.slice(event.charIndex, event.charIndex + (event.charLength || 8)));
        }
      };
      utterance.onend = done;
      utterance.onerror = done;
      this.finishCurrent = done;
      window.speechSynthesis.speak(utterance);
    });
  }

  stop() {
    this.session++;
    this.audio.onended = null;
    this.audio.onerror = null;
    this.audio.pause();
    if (this.audio.src) {
      this.audio.removeAttribute("src");
      try {
        this.audio.load();
      } catch {
        /* ignore */
      }
    }
    if ("speechSynthesis" in window) window.speechSynthesis.cancel();
    if (this.finishCurrent) this.finishCurrent();
    if (this.avatar) this.avatar.stop();
  }
}
