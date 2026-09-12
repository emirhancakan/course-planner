/*
 * Native-feeling scroll views: a draggable knob inside a knob slot, drawn over
 * scroll containers whose own scrollbars are hidden.
 *
 * Two things are deliberate about the design:
 *
 * 1. The native scroller stays the engine. Wheel, trackpad, touch, keyboard and
 *    momentum are never intercepted - the only scroll listener is passive, and
 *    overscroll-behaviour is left alone. That is what preserves macOS scroll
 *    elasticity (rubber-banding) at the content boundaries: it belongs to the
 *    OS/browser and cannot be reimplemented, only left intact. Setting
 *    overscroll-behavior:none or hijacking wheel events would kill it.
 *
 * 2. NSScroller.Style is not readable from a web page, so the overlay/legacy
 *    distinction is taken from the behaviour that actually matters: whether the
 *    browser reserves layout width for its scrollbars. macOS overlay scrollers
 *    float above content and reserve 0px; the legacy style (and Windows/Linux)
 *    reserves a gutter. See detectScrollerStyle().
 *
 * Native scrollbars are only hidden once a custom one has been built, so if this
 * file fails to load the platform scrollbars are still there.
 */
(function () {
  "use strict";

  const MIN_KNOB_PX = 24;          // a knob shorter than this is hard to grab
  const OVERLAY_FADE_DELAY_MS = 900;
  const PAGE_OVERLAP_PX = 40;      // keep a little context when paging
  const EDGE_GLOW_MS = 340;

  /* macOS (and iOS) rubber-band at the boundaries in the compositor; nothing in
     a web page can reproduce that. Elsewhere - Windows and most of Linux - a
     scroll simply stops dead, so those platforms get a brief edge glow instead
     so hitting a boundary still reads as hitting a boundary. Platform sniffing
     is a heuristic, but the only signal available, and the downside of guessing
     wrong is a glow that is slightly redundant. */
  const HAS_NATIVE_ELASTICITY =
    /Mac|iPhone|iPad|iPod/i.test(navigator.platform || "") ||
    /Mac OS X|iPhone|iPad/i.test(navigator.userAgent || "");

  const views = [];
  let uid = 0;

  function clamp(value, lo, hi) {
    return value < lo ? lo : value > hi ? hi : value;
  }

  function prefersReducedMotion() {
    return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  }

  /* Width the browser's own scrollbars take out of the layout. 0 means the
     platform draws them as overlays. */
  function nativeScrollbarWidth() {
    const probe = document.createElement("div");
    probe.setAttribute("aria-hidden", "true");
    probe.style.cssText = "position:absolute;top:-9999px;left:-9999px;" +
      "width:100px;height:100px;overflow-y:scroll;visibility:hidden";
    (document.body || document.documentElement).appendChild(probe);
    const width = probe.offsetWidth - probe.clientWidth;
    probe.remove();
    return width;
  }

  function detectScrollerStyle() {
    // data-scroller-force on <html> pins the style; used for testing both paths
    // on a platform that only exhibits one of them.
    const forced = document.documentElement.getAttribute("data-scroller-force");
    if (forced === "overlay" || forced === "legacy") return forced;
    return nativeScrollbarWidth() > 0 ? "legacy" : "overlay";
  }

  function createScrollView(viewport) {
    const isDocument =
      viewport === document.documentElement ||
      viewport === document.scrollingElement ||
      viewport === document.body;

    const scrollHost = isDocument ? document.documentElement : viewport;

    const metrics = isDocument
      ? {
          top: () => window.scrollY || document.documentElement.scrollTop || 0,
          client: () => window.innerHeight,
          scroll: () => document.documentElement.scrollHeight,
          setTop: (v) => window.scrollTo(0, v),
          scrollTo: (opts) => window.scrollTo(opts),
          events: window,
        }
      : {
          top: () => viewport.scrollTop,
          client: () => viewport.clientHeight,
          scroll: () => viewport.scrollHeight,
          setTop: (v) => { viewport.scrollTop = v; },
          scrollTo: (opts) => viewport.scrollTo(opts),
          events: viewport,
        };

    if (!viewport.id) viewport.id = "sv-viewport-" + (++uid);

    const rail = document.createElement("div");
    rail.className = "sv-rail" + (isDocument ? " sv-rail-fixed" : "");

    const slot = document.createElement("div");
    slot.className = "sv-slot";           // NSScroller's knob slot

    const knob = document.createElement("div");
    knob.className = "sv-knob";
    knob.setAttribute("role", "scrollbar");
    knob.setAttribute("aria-orientation", "vertical");
    knob.setAttribute("aria-controls", viewport.id);
    knob.setAttribute("aria-label", "Scroll");

    slot.appendChild(knob);
    rail.appendChild(slot);

    // Boundary feedback for platforms with no native elasticity.
    let edgeTop = null;
    let edgeBottom = null;
    if (!HAS_NATIVE_ELASTICITY) {
      edgeTop = document.createElement("div");
      edgeTop.className = "sv-edge sv-edge-top";
      edgeBottom = document.createElement("div");
      edgeBottom.className = "sv-edge sv-edge-bottom";
      rail.appendChild(edgeTop);
      rail.appendChild(edgeBottom);
    }

    if (isDocument) {
      document.body.appendChild(rail);
    } else {
      // A zero-height sticky rail stays glued to the top of the visible area,
      // which means no wrapper element is needed and the existing flex layout
      // and sticky search field are left untouched.
      //
      // It has to be the FIRST child: sticky can hold an element at top:0 but
      // cannot lift it above its position in the flow, so as a last child the
      // rail would sit at the bottom of the content and the knob would only
      // appear once you had scrolled all the way down.
      viewport.insertBefore(rail, viewport.firstChild);
      // Element scrollers reserve a gutter for the knob (see the CSS) so cards
      // and their buttons stop before it rather than running underneath.
      viewport.classList.add("sv-viewport-element");
    }
    scrollHost.classList.add("sv-viewport");

    let dragging = false;
    let fadeTimer = 0;

    function reveal() {
      rail.classList.add("sv-active");
      clearTimeout(fadeTimer);
      fadeTimer = setTimeout(function () {
        if (!dragging) rail.classList.remove("sv-active");
      }, OVERLAY_FADE_DELAY_MS);
    }

    function sync() {
      const client = metrics.client();

      if (!isDocument) {
        // The slot is absolutely positioned, and absolute descendants do count
        // towards a scroll container's overflow. Anchored at the rail (which
        // sits below the viewport's top padding) a full-height slot would push
        // scrollHeight past clientHeight and make the panel scrollable by the
        // padding amount even when its content fits. Pulling it up by the top
        // padding makes it span exactly the visible box, and overflow above the
        // top edge doesn't grow scrollHeight.
        const cs = getComputedStyle(viewport);
        const padTop = parseFloat(cs.paddingTop) || 0;
        const padRight = parseFloat(cs.paddingRight) || 0;
        slot.style.top = -padTop + "px";
        // The rail is a block element, so its right edge is the content edge -
        // right where a card's Add/Remove button sits. Push the slot out into
        // the reserved gutter instead, keeping 2px off the panel's rounded
        // corner.
        slot.style.right = -Math.max(0, padRight - 2) + "px";
        slot.style.height = client + "px";
        if (edgeTop) {
          edgeTop.style.top = -padTop + "px";
          edgeBottom.style.top = (-padTop + client) + "px";
        }
      }

      const scroll = metrics.scroll();
      const max = scroll - client;

      if (max <= 1) {
        rail.classList.add("sv-not-scrollable");
        knob.setAttribute("aria-disabled", "true");
        return;
      }
      rail.classList.remove("sv-not-scrollable");
      knob.removeAttribute("aria-disabled");

      const slotH = slot.clientHeight || client;
      const knobH = Math.max(MIN_KNOB_PX, Math.round(slotH * (client / scroll)));
      // While macOS is rubber-banding, scrollTop can report a value outside
      // [0, max]; clamp so the knob stays in its slot instead of overshooting.
      const progress = clamp(metrics.top() / max, 0, 1);

      knob.style.height = knobH + "px";
      knob.style.transform = "translateY(" + Math.round((slotH - knobH) * progress) + "px)";
      knob.setAttribute("aria-valuemin", "0");
      knob.setAttribute("aria-valuemax", "100");
      knob.setAttribute("aria-valuenow", String(Math.round(progress * 100)));
    }

    // Passive: this listener must never be able to delay or cancel scrolling.
    metrics.events.addEventListener("scroll", function () {
      sync();
      reveal();
    }, { passive: true });

    (isDocument ? document.documentElement : viewport)
      .addEventListener("pointerenter", reveal);

    /* --- boundary feedback where the platform has no elasticity ----------- */
    if (!HAS_NATIVE_ELASTICITY) {
      let glowTimer = 0;
      const bump = function (edge) {
        rail.classList.remove("sv-glow-top", "sv-glow-bottom");
        // Force a reflow so a repeated bump restarts the animation.
        void rail.offsetWidth;
        rail.classList.add(edge === "top" ? "sv-glow-top" : "sv-glow-bottom");
        clearTimeout(glowTimer);
        glowTimer = setTimeout(function () {
          rail.classList.remove("sv-glow-top", "sv-glow-bottom");
        }, EDGE_GLOW_MS);
      };

      // Passive: a passive listener cannot call preventDefault, so this can
      // observe the gesture without ever standing between it and the scroller.
      metrics.events.addEventListener("wheel", function (event) {
        const max = metrics.scroll() - metrics.client();
        if (max <= 1) return;
        const top = metrics.top();
        if (event.deltaY < 0 && top <= 0) bump("top");
        else if (event.deltaY > 0 && top >= max - 1) bump("bottom");
      }, { passive: true });
    }

    /* --- dragging the knob ------------------------------------------------ */
    knob.addEventListener("pointerdown", function (event) {
      if (event.button !== 0) return;
      // Only suppresses text selection on the knob itself; scrolling input is
      // untouched.
      event.preventDefault();
      event.stopPropagation();

      const client = metrics.client();
      const max = metrics.scroll() - client;
      const runway = slot.clientHeight - knob.offsetHeight;
      if (max <= 0 || runway <= 0) return;

      const startY = event.clientY;
      const startTop = metrics.top();

      dragging = true;
      rail.classList.add("sv-active", "sv-dragging");
      document.documentElement.classList.add("sv-drag-active");
      try { knob.setPointerCapture(event.pointerId); } catch (e) { /* not fatal */ }

      function onMove(moveEvent) {
        const delta = ((moveEvent.clientY - startY) / runway) * max;
        metrics.setTop(clamp(startTop + delta, 0, max));
      }

      function onRelease(releaseEvent) {
        dragging = false;
        rail.classList.remove("sv-dragging");
        document.documentElement.classList.remove("sv-drag-active");
        try { knob.releasePointerCapture(releaseEvent.pointerId); } catch (e) { /* ignore */ }
        knob.removeEventListener("pointermove", onMove);
        knob.removeEventListener("pointerup", onRelease);
        knob.removeEventListener("pointercancel", onRelease);
        reveal();
      }

      knob.addEventListener("pointermove", onMove);
      knob.addEventListener("pointerup", onRelease);
      knob.addEventListener("pointercancel", onRelease);
    });

    /* --- clicking the knob slot pages, as an NSScroller does -------------- */
    slot.addEventListener("pointerdown", function (event) {
      if (event.button !== 0 || event.target === knob) return;
      const client = metrics.client();
      const max = metrics.scroll() - client;
      if (max <= 0) return;

      const direction = event.clientY < knob.getBoundingClientRect().top ? -1 : 1;
      const page = Math.max(1, client - PAGE_OVERLAP_PX);
      metrics.scrollTo({
        top: clamp(metrics.top() + direction * page, 0, max),
        behavior: prefersReducedMotion() ? "auto" : "smooth",
      });
      reveal();
    });

    sync();
    return { sync: sync, viewport: viewport, rail: rail, knob: knob, slot: slot };
  }

  function refresh() {
    for (let i = 0; i < views.length; i++) views[i].sync();
  }

  function applyStyle(style) {
    document.documentElement.setAttribute("data-scroller-style", style);
    refresh();
  }

  function init() {
    const root = document.documentElement;
    root.style.setProperty("--sv-native-width", nativeScrollbarWidth() + "px");
    root.setAttribute("data-scroller-style", detectScrollerStyle());

    const targets = [document.scrollingElement || document.documentElement];
    const opted = document.querySelectorAll("[data-scrollview]");
    for (let i = 0; i < opted.length; i++) targets.push(opted[i]);

    for (let i = 0; i < targets.length; i++) {
      if (targets[i]) views.push(createScrollView(targets[i]));
    }

    window.addEventListener("resize", refresh);
    // Web fonts and late layout can change content height after first paint.
    window.addEventListener("load", refresh);

    window.ScrollViews = {
      refresh: refresh,
      style: function () { return root.getAttribute("data-scroller-style"); },
      // Force a style at runtime; mainly so both code paths can be exercised on
      // a platform that only exhibits one of them.
      setStyle: applyStyle,
      nativeScrollbarWidth: nativeScrollbarWidth,
      views: views,
    };
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
