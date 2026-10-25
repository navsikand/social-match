/* Social Match — pitch deck navigation.
 * Keyboard, wheel, touch and dots. Deep-linkable by slide number. No dependencies.
 */
(() => {
  "use strict";

  const slides = Array.from(document.querySelectorAll(".slide"));
  const dotsNav = document.getElementById("dots");
  const fill = document.getElementById("progress-fill");
  const nowEl = document.getElementById("slide-now");
  const totalEl = document.getElementById("slide-total");
  const notes = document.getElementById("notes");
  const notesText = notes ? notes.querySelector("p") : null;
  const hint = document.getElementById("hint");

  let index = 0;
  let notesOn = false;
  let wheelLock = 0;

  totalEl.textContent = String(slides.length);

  /* ── dots ─────────────────────────────────────────────── */
  const dots = slides.map((slide, i) => {
    const b = document.createElement("button");
    b.type = "button";
    b.setAttribute("aria-label", `Slide ${i + 1}: ${label(slide)}`);
    b.addEventListener("click", () => go(i));
    dotsNav.appendChild(b);
    return b;
  });

  function label(slide) {
    const e = slide.querySelector(".eyebrow");
    return (e ? e.textContent : slide.querySelector("h1,h2,blockquote")?.textContent || "")
      .replace(/\s+/g, " ").trim().slice(0, 48) || `Slide ${slides.indexOf(slide) + 1}`;
  }

  /* ── counting stats (once per visit to the slide) ─────── */
  const counted = new WeakSet();
  function countUp(slide) {
    if (counted.has(slide)) return;
    counted.add(slide);
    slide.querySelectorAll("[data-count]").forEach((el) => {
      const target = parseInt(el.dataset.count, 10) || 0;
      if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
        el.textContent = String(target);
        return;
      }
      const start = performance.now();
      const dur = 700;
      const step = (now) => {
        const t = Math.min(1, (now - start) / dur);
        const eased = 1 - Math.pow(1 - t, 3);
        el.textContent = String(Math.round(target * eased));
        if (t < 1) requestAnimationFrame(step);
      };
      el.textContent = "0";
      requestAnimationFrame(step);
    });
  }

  /* ── navigation ───────────────────────────────────────── */
  function go(next) {
    const clamped = Math.max(0, Math.min(slides.length - 1, next));
    if (clamped === index && slides[index].classList.contains("active")) return;

    slides[index].classList.remove("active");
    index = clamped;
    const slide = slides[index];
    slide.classList.add("active");
    slide.scrollTop = 0;

    dots.forEach((d, i) => d.setAttribute("aria-current", String(i === index)));
    fill.style.width = `${((index + 1) / slides.length) * 100}%`;
    nowEl.textContent = String(index + 1);

    if (notesText) notesText.textContent = slide.dataset.notes || "(no notes for this slide)";
    countUp(slide);

    if (index > 0 && hint && !hint.classList.contains("gone")) hint.classList.add("gone");

    // keep the file deep-linkable: index.html#7
    const hash = `#${index + 1}`;
    if (location.hash !== hash) history.replaceState(null, "", hash);
  }

  const next = () => go(index + 1);
  const prev = () => go(index - 1);

  function toggleNotes() {
    notesOn = !notesOn;
    notes.hidden = !notesOn;
    if (notesOn && notesText) notesText.textContent = slides[index].dataset.notes || "(no notes)";
  }

  async function toggleFullscreen() {
    try {
      if (!document.fullscreenElement) await document.documentElement.requestFullscreen();
      else await document.exitFullscreen();
    } catch (err) {
      /* the browser may refuse without a gesture; nothing to report */
    }
  }

  /* ── input ────────────────────────────────────────────── */
  document.addEventListener("keydown", (e) => {
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    switch (e.key) {
      case "ArrowRight":
      case "ArrowDown":
      case "PageDown":
      case " ":
      case "Enter":
        e.preventDefault(); next(); break;
      case "ArrowLeft":
      case "ArrowUp":
      case "PageUp":
      case "Backspace":
        e.preventDefault(); prev(); break;
      case "Home": e.preventDefault(); go(0); break;
      case "End": e.preventDefault(); go(slides.length - 1); break;
      case "n": case "N": e.preventDefault(); toggleNotes(); break;
      case "f": case "F": e.preventDefault(); toggleFullscreen(); break;
      case "?": e.preventDefault(); if (hint) hint.classList.toggle("gone"); break;
      default: break;
    }
  });

  // wheel: one slide per gesture, with a short lock so a trackpad flick is one slide
  window.addEventListener("wheel", (e) => {
    if (Math.abs(e.deltaY) < 24) return;
    const target = e.target.closest?.(".slide");
    // inside a scrollable slide, only page when that slide is already at its end
    if (target && target.scrollHeight > target.clientHeight + 4) {
      const atEnd = target.scrollTop + target.clientHeight >= target.scrollHeight - 2;
      const atTop = target.scrollTop <= 2;
      if (!(e.deltaY > 0 ? atEnd : atTop)) return;
    }
    const now = Date.now();
    if (now < wheelLock) return;
    wheelLock = now + 520;
    if (e.deltaY > 0) next(); else prev();
  }, { passive: true });

  // touch: horizontal swipe, or vertical when the slide cannot scroll
  let touchX = 0, touchY = 0, touching = false;
  window.addEventListener("touchstart", (e) => {
    const t = e.changedTouches[0];
    touchX = t.clientX; touchY = t.clientY; touching = true;
  }, { passive: true });
  window.addEventListener("touchend", (e) => {
    if (!touching) return;
    touching = false;
    const t = e.changedTouches[0];
    const dx = t.clientX - touchX;
    const dy = t.clientY - touchY;
    const slide = slides[index];
    const scrollable = slide.scrollHeight > slide.clientHeight + 4;
    if (Math.abs(dx) > 56 && Math.abs(dx) > Math.abs(dy)) {
      if (dx < 0) next(); else prev();
      return;
    }
    if (scrollable) return;
    if (Math.abs(dy) > 56) { if (dy < 0) next(); else prev(); }
  }, { passive: true });

  // click the far edges of the deck to page (walls of a projector-friendly deck)
  document.getElementById("deck").addEventListener("click", (e) => {
    if (e.target.closest("a,button,pre,code")) return;
    const x = e.clientX / window.innerWidth;
    if (x > 0.86) next();
    else if (x < 0.14) prev();
  });

  window.addEventListener("hashchange", () => {
    const n = parseInt(location.hash.replace("#", ""), 10);
    if (Number.isFinite(n) && n >= 1 && n <= slides.length) go(n - 1);
  });

  /* ── boot ─────────────────────────────────────────────── */
  const initial = parseInt(location.hash.replace("#", ""), 10);
  index = Number.isFinite(initial) && initial >= 1 && initial <= slides.length ? initial - 1 : 0;
  slides[index].classList.add("active");
  dots.forEach((d, i) => d.setAttribute("aria-current", String(i === index)));
  fill.style.width = `${((index + 1) / slides.length) * 100}%`;
  nowEl.textContent = String(index + 1);
  if (notesText) notesText.textContent = slides[index].dataset.notes || "(no notes for this slide)";
  countUp(slides[index]);
})();
