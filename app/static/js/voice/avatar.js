// Mira's lip-sync. With Kokoro audio, a Web Audio AnalyserNode drives the mouth:
// loudness (RMS) opens it, the spectral centroid makes it wider ("ee") or rounder ("oo").
// With the browser's speech synthesis (no audio access), word-boundary events drive a
// syllable-rate approximation. Eyes blink at random intervals.

export class Avatar {
  constructor(wrap) {
    this.wrap = wrap;
    this.svg = wrap ? wrap.querySelector("[data-mira]") : null;
    this.ready = Boolean(this.svg);
    if (!this.ready) return;
    const q = (selector) => this.svg.querySelector(selector);
    this.mouth = q("[data-mira-mouth]");
    this.lips = q("[data-mira-lips]");
    this.teeth = q("[data-mira-teeth]");
    this.talk = q("[data-mira-talk]");
    this.smile = q("[data-mira-smile]");
    this.head = q("[data-mira-head]");
    this.eyes = Array.from(this.svg.querySelectorAll("[data-mira-eye]"));
    this.open = 0;
    this.wide = 0.5;
    this.target = { open: 0, wide: 0.5 };
    this.mode = "idle";
    this.analyser = null;
    this.simUntil = 0;
    this.raf = 0;
    this.reduced = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    this.scheduleBlink();
  }

  scheduleBlink() {
    setTimeout(() => {
      this.blink();
      this.scheduleBlink();
    }, 2500 + Math.random() * 3500);
  }

  blink() {
    if (!this.ready || document.hidden) return;
    this.eyes.forEach((eye) => eye.setAttribute("ry", "0.6"));
    setTimeout(() => this.eyes.forEach((eye) => eye.setAttribute("ry", "5.4")), 130);
  }

  useAnalyser(analyser) {
    if (!this.ready) return;
    this.analyser = analyser;
    this.timeData = new Uint8Array(analyser.fftSize);
    this.freqData = new Uint8Array(analyser.frequencyBinCount);
    this.mode = "audio";
    this.start();
  }

  useSimulation(estimatedMs = 0) {
    if (!this.ready) return;
    this.analyser = null;
    this.mode = "sim";
    this.simUntil = performance.now() + estimatedMs;
    this.start();
  }

  pulseWord(word) {
    const syllables = Math.max(1, ((word || "").match(/[aeiouy]+/gi) || []).length);
    this.simUntil = Math.max(this.simUntil, performance.now() + syllables * 190);
  }

  start() {
    this.wrap.classList.add("mira-speaking");
    if (!this.raf) this.raf = requestAnimationFrame((t) => this.tick(t));
  }

  stop() {
    if (!this.ready) return;
    this.mode = "idle";
    this.analyser = null;
    this.wrap.classList.remove("mira-speaking");
  }

  tick(time) {
    this.raf = 0;
    if (this.mode === "audio" && this.analyser) {
      this.analyser.getByteTimeDomainData(this.timeData);
      let sum = 0;
      for (let i = 0; i < this.timeData.length; i++) {
        const x = (this.timeData[i] - 128) / 128;
        sum += x * x;
      }
      const rms = Math.sqrt(sum / this.timeData.length);
      this.analyser.getByteFrequencyData(this.freqData);
      let weighted = 0;
      let total = 0;
      const bins = Math.min(this.freqData.length, 96);
      for (let i = 1; i < bins; i++) {
        weighted += i * this.freqData[i];
        total += this.freqData[i];
      }
      const centroid = total ? weighted / total / bins : 0.3;
      this.target.open = Math.min(1, Math.max(0, (rms - 0.012) * 7.5));
      this.target.wide = Math.min(1, Math.max(0, (centroid - 0.1) * 3.2));
    } else if (this.mode === "sim") {
      const active = performance.now() < this.simUntil;
      this.target.open = active ? 0.3 + 0.5 * Math.abs(Math.sin(time / 85)) : 0.04;
      this.target.wide = 0.45 + 0.3 * Math.sin(time / 230);
    } else {
      this.target.open = 0;
      this.target.wide = 0.5;
    }
    this.open += (this.target.open - this.open) * 0.45;
    this.wide += (this.target.wide - this.wide) * 0.3;
    this.render(time);
    if (this.mode !== "idle" || this.open > 0.02) {
      this.raf = requestAnimationFrame((t) => this.tick(t));
    } else {
      this.open = 0;
      this.render(time);
    }
  }

  render(time) {
    const open = this.open;
    const talking = open > 0.06;
    this.talk.setAttribute("opacity", talking ? "1" : "0");
    this.smile.setAttribute("opacity", talking ? "0" : "1");
    const ry = 1.2 + open * 8;
    const rx = 6.5 + this.wide * 4 - open * 1.2;
    this.mouth.setAttribute("ry", ry.toFixed(2));
    this.mouth.setAttribute("rx", rx.toFixed(2));
    this.lips.setAttribute("ry", (ry + 1.8).toFixed(2));
    this.lips.setAttribute("rx", (rx + 1.6).toFixed(2));
    this.teeth.setAttribute("opacity", open > 0.25 ? "0.85" : "0");
    this.teeth.setAttribute("cy", (123 - ry + 1.6).toFixed(2));
    if (this.head && !this.reduced) {
      const tilt = talking ? Math.sin(time / 420) * 1.6 : 0;
      this.head.setAttribute("transform", `rotate(${tilt.toFixed(2)} 100 110)`);
    }
  }
}
