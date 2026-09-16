// Career Pilot Autofill -- content script. Declared statically in
// manifest.json (all_urls, all_frames: true) so it runs automatically
// on every page load, in every frame including cross-origin embedded
// ones -- this is the actual fix for the employer-wrapped ATS case
// (e.g. Samsara: the real form lives in an iframe on a different origin
// than the employer's own careers page, which the server-side
// Playwright automation couldn't reliably reach and got bot-blocked on
// from the VM's datacenter IP besides). Running here, in the user's own
// real browser, sidesteps both problems at once.
//
// Real bug found live 2026-09-16 on this exact Samsara page: it has a
// hidden, unrelated third-party iframe (an Intellimize A/B-testing
// script). With all_frames:true, this script ran there too, correctly
// found no match for ITS OWN location.href (a tracking script's URL,
// obviously not a real job posting), and chrome.tabs.sendMessage's
// documented behavior -- "if several frames respond, the promise
// resolves to one of the answers" -- meant that irrelevant frame's
// negative result could just as easily win the race as the real page's
// positive one, showing a false "no match" for a page that really did
// have one. Fix: ONLY the main frame (window === window.top) ever
// decides match/no-match at all -- a posting's stored job_url is always
// the top-level page's URL, never some unrelated iframe's own URL, so a
// sub-frame trying to match itself was never meaningful in the first
// place. Once the main frame confirms a match, it tells every frame
// (itself and any real embedded ATS iframe, via the background relay)
// to fill whatever it finds in its own DOM -- that part still needs
// all_frames, since the real form can genuinely live in a sub-frame.
//
// Runs automatically on load, zero clicks -- matching the existing
// server-side autofill's own precedent (evaluate_and_enqueue
// auto-launches and fills immediately for a clean, Approved
// application, no manual trigger there either). A real, earlier version
// of this needed a popup click to check plus a separate click to fill;
// real use flagged that as an extra step this app exists to remove.
//
// Also adds an on-page floating button (main frame only) -- real
// feedback comparing this to JobRight's own extension, which shows one
// directly on the page instead of requiring a toolbar-icon hunt.
// Positioned bottom-LEFT specifically so it doesn't stack on top of
// JobRight's own bottom-right one if both are installed.
//
// Deliberately does ONLY generic field detection (label text + input
// type) -- no per-ATS knowledge. The actual answer for each label comes
// from the real backend (extension_service.py), which reuses the exact
// same mechanical_common_answer/contact-field logic Playwright autofill
// already has. A field this can't confidently answer is left blank, on
// purpose -- never a guess.

(function () {
  // A static content script only ever loads once per real page
  // navigation, but this guard stays cheap insurance against a stray
  // double-injection (e.g. a same-document history navigation) stacking
  // a second onMessage listener in the same frame.
  if (window.__careerPilotFillLoaded) return;
  window.__careerPilotFillLoaded = true;

  const isMainFrame = window === window.top;
  let lastResult = null;

  // A field that comes back with no answer stays "fillable" forever
  // (still empty) -- without tracking this, a manual re-fill (the badge
  // click, or the popup's "Autofill Again") would re-ask the backend
  // about the exact same already-attempted-and-unanswerable field every
  // time, instead of only asking about genuinely new fields. Tracked
  // once per element for this page's lifetime.
  const attemptedUnanswerable = new WeakSet();

  function findLabelText(el) {
    if (el.id) {
      const byFor = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (byFor && byFor.textContent.trim()) return byFor.textContent.trim();
    }
    const ancestorLabel = el.closest("label");
    if (ancestorLabel && ancestorLabel.textContent.trim()) return ancestorLabel.textContent.trim();
    const ariaLabel = el.getAttribute("aria-label");
    if (ariaLabel && ariaLabel.trim()) return ariaLabel.trim();
    const ariaLabelledBy = el.getAttribute("aria-labelledby");
    if (ariaLabelledBy) {
      const referenced = document.getElementById(ariaLabelledBy);
      if (referenced && referenced.textContent.trim()) return referenced.textContent.trim();
    }
    const placeholder = el.getAttribute("placeholder");
    if (placeholder && placeholder.trim()) return placeholder.trim();
    return null;
  }

  // A select's own "nothing chosen yet" option -- almost always value=""
  // and/or a literal "Select..."-shaped label. Excluded from the real
  // option list sent to the backend so it's never mistaken for a real
  // choice, and used to detect whether the field is still unanswered.
  function isPlaceholderOption(opt) {
    return opt.value === "" || /^(select|choose|please select)/i.test(opt.textContent.trim());
  }

  function realOptions(selectEl) {
    return Array.from(selectEl.options).filter((o) => !isPlaceholderOption(o));
  }

  function isFillable(el) {
    if (attemptedUnanswerable.has(el)) return false;
    if (el.disabled || el.readOnly) return false;
    if (el.offsetParent === null) return false; // hidden
    if (el.tagName === "INPUT") {
      const type = (el.getAttribute("type") || "text").toLowerCase();
      return ["text", "email", "tel", "url"].includes(type) && !el.value;
    }
    if (el.tagName === "TEXTAREA") return !el.value;
    if (el.tagName === "SELECT") {
      const selected = el.options[el.selectedIndex];
      return (!selected || isPlaceholderOption(selected)) && realOptions(el).length > 0;
    }
    return false;
  }

  // Sets the value through the native setter (not the element's own,
  // possibly React-overridden one) then dispatches real input/change
  // events -- required on any React-controlled form, which several
  // modern ATS UIs (including Greenhouse's newer boards) use. Setting
  // .value directly without this makes a field LOOK filled while the
  // page's own state stays empty, so it silently fails to submit --
  // exactly the kind of "looks right, actually broken" bug this app has
  // hunted down all session.
  function setNativeValue(el, value) {
    const protoByTag = {
      TEXTAREA: window.HTMLTextAreaElement.prototype,
      INPUT: window.HTMLInputElement.prototype,
      SELECT: window.HTMLSelectElement.prototype,
    };
    const descriptor = Object.getOwnPropertyDescriptor(protoByTag[el.tagName], "value");
    descriptor.set.call(el, value);
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
  }

  function highlight(el, ok) {
    el.style.outline = ok ? "2px solid #10b981" : "2px dashed #f59e0b";
    el.style.outlineOffset = "1px";
  }

  function fillThisFrame(applicationId) {
    const elements = Array.from(document.querySelectorAll("input, textarea, select"));
    const fields = [];
    const elementById = {};
    let nextId = 0;

    for (const el of elements) {
      if (!isFillable(el)) continue;
      const label = findLabelText(el);
      if (!label) continue;
      const fieldId = "f" + nextId++;
      if (el.tagName === "SELECT") {
        // Real option TEXT, not the option's internal value attribute --
        // the backend matches against what a human actually reads, and
        // never invents a choice that isn't one of these real options.
        fields.push({ field_id: fieldId, label, type: "select", options: realOptions(el).map((o) => o.textContent.trim()) });
      } else {
        fields.push({ field_id: fieldId, label });
      }
      elementById[fieldId] = el;
    }

    if (fields.length === 0) {
      return Promise.resolve({ filled: 0, total: 0 });
    }

    return chrome.runtime.sendMessage({ type: "getAnswers", applicationId, fields }).then((response) => {
      if (response.error) return { error: response.error };
      const answers = response.data;
      let filled = 0;
      for (const [fieldId, el] of Object.entries(elementById)) {
        const answer = answers[fieldId];
        if (!answer) {
          highlight(el, false);
          attemptedUnanswerable.add(el);
          continue;
        }
        if (el.tagName === "SELECT") {
          // The backend only ever returns text that exactly matches one
          // of the real options this sent it -- find that option's
          // actual .value (not necessarily the same as its display
          // text) to set on the element.
          const match = realOptions(el).find((o) => o.textContent.trim() === answer);
          if (!match) {
            highlight(el, false); // shouldn't happen; never silently pick the wrong option
            attemptedUnanswerable.add(el);
            continue;
          }
          setNativeValue(el, match.value);
        } else {
          setNativeValue(el, answer);
        }
        highlight(el, true);
        filled++;
      }
      return { filled, total: fields.length };
    });
  }

  // ---- Main-frame-only: decide match, own the on-page UI ----

  // Position/collapsed state are real user preferences, not just visual
  // detail -- a badge fixed in one corner blocks whatever real content
  // happens to sit there on a given page, and a badge with no way to
  // put away is nagging on a page the user just wants to read. Both are
  // remembered per-site (localStorage is already origin-scoped) so a
  // position/collapse choice on this employer's page sticks across
  // reloads of it, without needing a synced-across-sites concept that
  // would be more machinery than this warrants.
  const POSITION_KEY = "careerPilotBadgePosition";
  const COLLAPSED_KEY = "careerPilotBadgeCollapsed";
  let badgeEl = null;
  let dragged = false; // distinguishes a real drag from a plain click, so dragging never also triggers a re-fill

  function loadJSON(key, fallback) {
    try {
      const raw = localStorage.getItem(key);
      return raw ? JSON.parse(raw) : fallback;
    } catch (e) {
      return fallback;
    }
  }

  function saveJSON(key, value) {
    try {
      localStorage.setItem(key, JSON.stringify(value));
    } catch (e) {
      /* private-browsing or storage-disabled -- position just won't persist, not worth erroring over */
    }
  }

  function makeDraggable(el) {
    let startX, startY, startLeft, startTop;

    function onMouseMove(e) {
      const dx = e.clientX - startX;
      const dy = e.clientY - startY;
      if (Math.abs(dx) > 3 || Math.abs(dy) > 3) dragged = true;
      el.style.left = Math.max(0, startLeft + dx) + "px";
      el.style.top = Math.max(0, startTop + dy) + "px";
      el.style.bottom = "auto";
    }
    function onMouseUp() {
      document.removeEventListener("mousemove", onMouseMove);
      document.removeEventListener("mouseup", onMouseUp);
      const rect = el.getBoundingClientRect();
      saveJSON(POSITION_KEY, { left: rect.left, top: rect.top });
      setTimeout(() => { dragged = false; }, 0); // let the click handler see it first
    }
    el.addEventListener("mousedown", (e) => {
      if (e.button !== 0) return;
      dragged = false;
      startX = e.clientX;
      startY = e.clientY;
      const rect = el.getBoundingClientRect();
      startLeft = rect.left;
      startTop = rect.top;
      document.addEventListener("mousemove", onMouseMove);
      document.addEventListener("mouseup", onMouseUp);
      e.preventDefault();
    });
  }

  function ensureBadge() {
    if (badgeEl) return badgeEl;
    badgeEl = document.createElement("div");
    badgeEl.id = "career-pilot-autofill-badge";
    Object.assign(badgeEl.style, {
      position: "fixed",
      zIndex: "2147483647",
      background: "#ffffff",
      color: "#0f172a",
      border: "1px solid #cbd5e1",
      borderRadius: "999px",
      padding: "8px 10px 8px 14px",
      fontFamily: "-apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif",
      fontSize: "13px",
      fontWeight: "600",
      boxShadow: "0 4px 14px rgba(0,0,0,0.15)",
      cursor: "grab",
      display: "none",
      alignItems: "center",
      gap: "8px",
      userSelect: "none",
    });

    const position = loadJSON(POSITION_KEY, null);
    if (position) {
      badgeEl.style.left = position.left + "px";
      badgeEl.style.top = position.top + "px";
    } else {
      badgeEl.style.left = "20px";
      badgeEl.style.bottom = "20px";
    }

    const label = document.createElement("span");
    label.id = "career-pilot-autofill-badge-label";
    badgeEl.appendChild(label);

    const closeBtn = document.createElement("span");
    closeBtn.textContent = "✕";
    Object.assign(closeBtn.style, { color: "#94a3b8", fontSize: "11px", cursor: "pointer", padding: "2px" });
    closeBtn.title = "Hide (click the extension icon to bring it back)";
    closeBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      setCollapsed(true);
    });
    badgeEl.appendChild(closeBtn);

    badgeEl.addEventListener("click", () => {
      if (!dragged) triggerFill();
    });
    makeDraggable(badgeEl);
    document.body.appendChild(badgeEl);
    return badgeEl;
  }

  function setCollapsed(collapsed) {
    saveJSON(COLLAPSED_KEY, collapsed);
    if (badgeEl) badgeEl.style.display = collapsed ? "none" : "flex";
  }

  function isUserCollapsed() {
    return loadJSON(COLLAPSED_KEY, false);
  }

  function showBadge(text, color) {
    if (isUserCollapsed()) return; // the user explicitly put this away -- respect it, don't pop back up on its own
    const el = ensureBadge();
    el.querySelector("#career-pilot-autofill-badge-label").textContent = text;
    el.style.color = color || "#0f172a";
    el.style.display = "flex";
  }

  function hideBadge() {
    if (badgeEl) badgeEl.style.display = "none";
  }

  async function triggerFill() {
    if (!lastResult || !lastResult.matched) return;
    showBadge("Filling...", "#0f172a");
    const fillResult = await fillThisFrame(lastResult.application.application_id);
    await notifyFrames(lastResult.application.application_id);
    lastResult = { ...lastResult, ...fillResult };
    renderBadge();
  }

  // "URL isn't set yet" is a real, but one-time and non-urgent, setup
  // step -- showing a red on-page badge for it on literally every site
  // visited before that's done would be intrusive, not helpful. Still
  // fully visible in the popup for whenever the user does open it.
  function isSetupPendingError(message) {
    return typeof message === "string" && message.includes("URL isn't set yet");
  }

  function renderBadge() {
    if (!lastResult) return;
    if (lastResult.error) {
      if (isSetupPendingError(lastResult.error)) {
        hideBadge();
        return;
      }
      showBadge("Career Pilot: " + lastResult.error, "#b91c1c");
      return;
    }
    if (!lastResult.matched) {
      hideBadge();
      return;
    }
    const scoreSuffix =
      lastResult.application && typeof lastResult.application.match_score === "number"
        ? " (" + lastResult.application.match_score + "% match)"
        : "";
    if (lastResult.total === 0) {
      showBadge("Career Pilot: no fillable fields found" + scoreSuffix, "#64748b");
      return;
    }
    showBadge(
      "Career Pilot: filled " + lastResult.filled + "/" + lastResult.total + scoreSuffix + " -- click to re-run",
      lastResult.filled === lastResult.total ? "#047857" : "#b45309"
    );
  }

  // Tells every frame of this tab (this one included, plus any real
  // embedded ATS iframe) to fill whatever it finds in its own DOM.
  // Relayed through the background service worker because a content
  // script can only message the background, never a sibling frame
  // directly -- background.js then broadcasts via chrome.tabs.sendMessage
  // (which reaches every frame when no frameId is given).
  function notifyFrames(applicationId) {
    return chrome.runtime.sendMessage({ type: "broadcastFill", applicationId });
  }

  // Real bug found live 2026-09-16, root-caused by actually re-reading
  // this code rather than guessing again: mainFrameCheckAndFill filled
  // this frame directly, THEN called notifyFrames -- which broadcasts to
  // EVERY frame of the tab, including this exact one, so its own
  // "fillPage" listener below fired again immediately afterward. By
  // then every field was either already filled (excluded by isFillable's
  // !el.value check) or already marked unanswerable (excluded via
  // attemptedUnanswerable), so that second pass always found 0 fields
  // and overwrote the correct "filled 4/9" badge with a wrong "no
  // fillable fields found" -- deterministically, every single time, not
  // an intermittent race. This timestamp guard treats a "fillPage"
  // arriving right after this frame's own direct fill as that same
  // self-echo and skips redoing it, while still letting a real later
  // click (the badge, or the popup's "Autofill Again") through.
  let lastFillAt = 0;
  const SELF_ECHO_WINDOW_MS = 1500;

  async function mainFrameCheckAndFill() {
    const match = await chrome.runtime.sendMessage({ type: "checkMatch", url: location.href });
    if (match.error) {
      lastResult = { error: match.error };
      renderBadge();
      return;
    }
    if (!match.data.matched) {
      lastResult = { matched: false };
      renderBadge();
      return;
    }
    const fillResult = await fillThisFrame(match.data.application_id);
    lastFillAt = Date.now();
    lastResult = { matched: true, application: match.data, ...fillResult };
    renderBadge();
    // Sub-frames (a real embedded ATS iframe, e.g. Greenhouse on an
    // employer's own page) never call mainFrameCheckAndFill themselves
    // (isMainFrame is false there) -- this is the only way they learn
    // the applicationId and fill their own DOM. This frame receives its
    // own broadcast too; the guard above is what stops that from
    // clobbering the result just set above.
    notifyFrames(match.data.application_id);
    pollForNewFields(match.data.application_id);
  }

  // Real behavior confirmed live: Samsara's "Apply Now" reveals the
  // actual form via client-side JS, not a real page navigation, so the
  // one-time check above can run before the form even exists in the
  // DOM. A prior version watched for this with a MutationObserver
  // (reacting to every DOM change) -- pulled after finding a real,
  // deterministic bug it caused (see mainFrameCheckAndFill's self-echo
  // comment) and because reacting to every mutation, including ones
  // this script's own fill triggers indirectly (a React re-render after
  // a dispatched input/change event), is inherently harder to reason
  // about correctly than something bounded and event-driven.
  //
  // A first version of THIS replacement used a single fixed 15s timer
  // from page load -- real bug found live immediately: that window is
  // anchored to page load, not to when the user actually gets around to
  // clicking "Apply Now". A real human reading the job description for
  // longer than 15s before clicking it means the form appears AFTER
  // polling already gave up, so it silently never gets filled at all.
  // Fixed by tying re-checks to actual clicks instead of a blind clock:
  // any click anywhere on the page is exactly the kind of real action
  // most likely to reveal new content client-side, and re-arming on
  // every click (not just the first) means this never "expires" for the
  // rest of the page's lifetime, however long the user takes.
  function pollForNewFields(applicationId) {
    let scheduled = [];

    function clearScheduled() {
      scheduled.forEach(clearTimeout);
      scheduled = [];
    }

    async function checkOnce() {
      const fillResult = await fillThisFrame(applicationId);
      // total > 0, not just filled > 0 -- a check that finds real
      // fields but can't answer any of them is still real, new
      // information worth showing (a stale "no fillable fields found"
      // left up once the form has actually appeared is the wrong
      // thing), not just a silent no-op.
      if (fillResult.total > 0) {
        lastFillAt = Date.now();
        lastResult = { matched: true, application: lastResult && lastResult.application, ...fillResult };
        renderBadge();
      }
    }

    function burstCheck() {
      clearScheduled();
      // A few checks spread over the next few seconds after a click --
      // covers typical client-side render/animation time without
      // hammering the backend on every single click (fillThisFrame
      // itself is a cheap no-op, no network call, whenever nothing new
      // is actually found).
      [400, 1000, 2000, 3500].forEach((delay) => scheduled.push(setTimeout(checkOnce, delay)));
    }

    burstCheck(); // covers a form that's already present at load, no click needed
    document.addEventListener("click", burstCheck, { capture: true, passive: true });
  }

  chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    if (message.type === "fillPage") {
      if (isMainFrame && Date.now() - lastFillAt < SELF_ECHO_WINDOW_MS) {
        sendResponse(lastResult ? { filled: lastResult.filled, total: lastResult.total } : { filled: 0, total: 0 });
        return false;
      }
      // Every frame (main + any real embedded ATS sub-frame) fills its
      // own DOM in response to a broadcast (a real embedded sub-frame's
      // first-ever fill, or a genuine later re-run -- the badge click,
      // or the popup's "Autofill Again").
      fillThisFrame(message.applicationId).then((fillResult) => {
        lastFillAt = Date.now();
        if (isMainFrame) {
          lastResult = { matched: true, application: lastResult && lastResult.application, ...fillResult };
          renderBadge();
        }
        sendResponse(fillResult);
      });
      return true;
    }
    if (message.type === "getStatus" && isMainFrame) {
      sendResponse(lastResult);
      return false;
    }
    if (message.type === "getBadgeCollapsed" && isMainFrame) {
      sendResponse(isUserCollapsed());
      return false;
    }
    if (message.type === "setBadgeCollapsed" && isMainFrame) {
      setCollapsed(message.collapsed);
      if (!message.collapsed) renderBadge(); // "bring it back" -- show it again right away, not just on the next status change
      sendResponse({ ok: true });
      return false;
    }
    return false;
  });

  if (isMainFrame) {
    mainFrameCheckAndFill();
  }
})();
