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

  // The main frame's badge has to reflect the WHOLE tab's real progress,
  // not just this one frame's own DOM -- the real Samsara case is a
  // form that lives entirely in a different (Greenhouse) frame, where
  // this frame's own fillThisFrame legitimately always finds 0. Tracked
  // as two separate running totals (this frame's own work, and whatever
  // sub-frames have reported in) so a later report from either source
  // adds to the real picture instead of overwriting it.
  let ownFillResult = { filled: 0, total: 0 };
  let subFrameFillResult = { filled: 0, total: 0 };

  function updateBadgeResult(application) {
    lastResult = {
      matched: true,
      application: application || (lastResult && lastResult.application),
      filled: ownFillResult.filled + subFrameFillResult.filled,
      total: ownFillResult.total + subFrameFillResult.total,
    };
    renderBadge();
  }

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
      // Real bug found live 2026-09-16: a react-select combobox's own
      // .value is EMPTY even right after a real, successful selection --
      // confirmed directly (selecting "Master's" for education level left
      // el.value === "" while the widget's own select__single-value
      // display correctly showed "Master's"). The plain !el.value check
      // below therefore NEVER recognizes one of these fields as already
      // answered, so every later click-triggered poll re-detected it as
      // still-blank, re-selected the same already-correct option, and
      // re-counted it as newly filled every time -- exactly the "keeps
      // changing the answer and inflating the filled count" behavior
      // reported live. hasReactSelectValue reads the widget's own real
      // selected-value display instead of the input's own (unreliable,
      // for this widget shape) value attribute.
      if (isReactSelectCombobox(el)) return !hasReactSelectValue(el);
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

  // File attachment: real, sanctioned browser API -- constructing a real
  // in-memory File from bytes this extension already legitimately fetched
  // (via the authenticated /api/extension/documents route) and assigning
  // it through DataTransfer. Different from, and not blocked like, setting
  // a file input's .value to a path string (blocked everywhere, for real
  // security reasons -- a page/script should never be able to point a file
  // input at an arbitrary path on disk it didn't pick itself). This is the
  // same technique real extensions like JobRight's use.
  const RESUME_LABEL_RE = /(resume|\bcv\b)/i;
  const COVER_LETTER_LABEL_RE = /cover\s*letter/i;

  function isFillableFileInput(el) {
    if (attemptedUnanswerable.has(el)) return false;
    if (el.disabled || el.readOnly) return false;
    if (el.offsetParent === null) return false; // hidden
    if (el.tagName !== "INPUT" || (el.getAttribute("type") || "").toLowerCase() !== "file") return false;
    return el.files.length === 0;
  }

  // Real bug found live 2026-09-16, root cause of "still no file upload
  // is being done": findLabelText's normal label[for=...] lookup finds a
  // REAL label on the real Samsara/Greenhouse file inputs -- but its text
  // is just "Attach" (the upload button's own caption), not "Resume/CV" /
  // "Cover Letter". The field's actual identifying text sits a few DOM
  // levels up, in a heading div that has no for=/aria-labelledby
  // connection to the input at all (confirmed live -- Greenhouse's own
  // accessibility gap on this widget, not something fixable from here,
  // only worked around). Never guesses: only walks up as far as the
  // FIRST ancestor whose own text unambiguously names one document type
  // and not the other; confirmed live that the real containers ARE that
  // clean (resume and cover-letter widgets are separate sibling
  // containers) well before any shared ancestor's text would start
  // matching both and force this to stop.
  function findFileInputLabelText(el) {
    const direct = findLabelText(el);
    if (direct && (RESUME_LABEL_RE.test(direct) || COVER_LETTER_LABEL_RE.test(direct))) return direct;

    let node = el.parentElement;
    for (let depth = 0; node && depth < 8; depth++, node = node.parentElement) {
      const text = node.textContent;
      const isResume = RESUME_LABEL_RE.test(text);
      const isCoverLetter = COVER_LETTER_LABEL_RE.test(text);
      if (isResume && !isCoverLetter) return text;
      if (isCoverLetter && !isResume) return text;
      if (isResume && isCoverLetter) return null; // walked too far -- now spans more than one real field, never guess
    }
    return null;
  }

  // Cover-letter check first -- more specific than the resume check, so a
  // label that somehow matched both would resolve to the more precise one.
  function documentTypeForFileInput(el) {
    const label = findFileInputLabelText(el) || "";
    if (COVER_LETTER_LABEL_RE.test(label)) return "cover_letter";
    if (RESUME_LABEL_RE.test(label)) return "resume";
    return null; // a file field this can't confidently identify -- never guess which document goes here
  }

  async function attachDocument(fileInput, applicationId, documentType) {
    const response = await chrome.runtime.sendMessage({ type: "fetchDocument", applicationId, documentType });
    if (!response || response.error) return false;
    const file = new File([response.data.buffer], response.data.filename, { type: "application/pdf" });
    const dataTransfer = new DataTransfer();
    dataTransfer.items.add(file);
    fileInput.files = dataTransfer.files;
    fileInput.dispatchEvent(new Event("input", { bubbles: true }));
    fileInput.dispatchEvent(new Event("change", { bubbles: true }));
    return true;
  }

  async function fillFileInputs(applicationId) {
    const fileInputs = Array.from(document.querySelectorAll('input[type="file"]')).filter(isFillableFileInput);
    let filled = 0;
    let total = 0;
    for (const el of fileInputs) {
      const documentType = documentTypeForFileInput(el);
      if (!documentType) continue;
      total++;
      const ok = await attachDocument(el, applicationId, documentType);
      if (ok) {
        highlight(el, true);
        filled++;
      } else {
        highlight(el, false); // no tailored document available yet, or a network/auth error -- left blank on purpose, never a guess
        attemptedUnanswerable.add(el);
      }
    }
    return { filled, total };
  }

  // Real bug found live 2026-09-16 while inspecting the still-open
  // "Where have you learned about Samsara" multi-select gap: Greenhouse's
  // newer job-boards.greenhouse.io UI builds several fields (Country
  // included -- not just that one multi-select) as a react-select-style
  // widget: a plain <input type="text" role="combobox">, not a native
  // <select>. setNativeValue's plain value-set makes it LOOK filled (the
  // typed text visibly sits in the box) but never actually registers a
  // selection in the widget's own internal state -- confirmed directly by
  // setting Country's input value to "United States" this way and finding
  // no select__single-value element (react-select's own real selected-
  // value display) ever appears. A field that looks filled but was never
  // really selected is exactly the "looks right, actually broken" bug
  // class this whole extension exists to avoid -- so this needs a real
  // click-based selection (open the menu, click the real rendered option),
  // the same way a human fills it, not a text write. Detected generically
  // (role="combobox" + aria-haspopup="true") since this is a react-select
  // shape, not something specific to Greenhouse -- any ATS built on the
  // same, very common library matches the same way.
  function isReactSelectCombobox(el) {
    return el.tagName === "INPUT" && el.getAttribute("role") === "combobox" && el.getAttribute("aria-haspopup") === "true";
  }

  // react-select's own real "a value is selected" signal -- confirmed
  // live: once selected (by this extension OR by the human clicking it
  // themselves), a select__single-value / select__multi-value sibling
  // appears next to the search input inside the shared "value container"
  // wrapper (react-select's own default class-naming convention, not
  // Greenhouse-specific). Used instead of the input's own .value, which
  // stays empty either way -- see isFillable's comment above.
  function hasReactSelectValue(el) {
    const valueContainer = el.closest('[class*="value-container"], [class*="valueContainer"]');
    return !!(
      valueContainer &&
      valueContainer.querySelector('[class*="single-value"], [class*="singleValue"], [class*="multi-value"], [class*="multiValue"]')
    );
  }

  function fireMouseSequence(target) {
    ["mousedown", "mouseup", "click"].forEach((type) =>
      target.dispatchEvent(new MouseEvent(type, { bubbles: true, cancelable: true, view: window, button: 0 }))
    );
  }

  function waitFor(predicate, attempts, intervalMs) {
    return new Promise((resolve) => {
      let count = 0;
      (function check() {
        const result = predicate();
        if (result) return resolve(result);
        count += 1;
        if (count >= attempts) return resolve(null);
        setTimeout(check, intervalMs);
      })();
    });
  }

  // Real bug caught live before shipping the click-selection fix this
  // supports: a plain, page-wide document.querySelectorAll('[role=
  // "option"]') is not safely scoped to THIS field -- if a previous
  // field's menu failed to close cleanly (the real Samsara form has a
  // phone-country-code combobox, labeled just "Country", whose menu
  // stayed open after a failed match in testing), its stale options stay
  // in the DOM and get mixed in with whatever field is being processed
  // next. React-select reliably sets the input's aria-controls to its own
  // listbox's real id once open (confirmed live) -- scoping the option
  // search to that exact listbox is what actually makes this safe.
  async function openReactSelectMenu(el) {
    fireMouseSequence(el.closest('[class*="control"]') || el);
    return waitFor(
      () => {
        const listboxId = el.getAttribute("aria-controls");
        const listbox = listboxId && document.getElementById(listboxId);
        if (!listbox) return null;
        const found = Array.from(listbox.querySelectorAll('[role="option"]'));
        return found.length > 0 ? found : null;
      },
      10,
      50
    );
  }

  // Real bug caught live before shipping this: a dispatched Escape
  // keydown (and blur(), and a synthetic mousedown on document.body --
  // all tried live against the real widget) does NOT actually close this
  // menu; aria-expanded stayed "true" every time. The one thing confirmed
  // live to actually work is a second click on the control, the same
  // toggle a real user's second click would do -- only ever called right
  // after openReactSelectMenu has confirmed the menu is actually open, so
  // this can't accidentally toggle a closed menu back open instead.
  function closeReactSelectMenu(el) {
    fireMouseSequence(el.closest('[class*="control"]') || el);
  }

  // Exact match only, same convention as the existing native-<select>
  // matching just below -- never silently pick the closest-sounding
  // option instead of the real one the backend actually resolved.
  async function selectReactSelectOption(el, answer) {
    const options = await openReactSelectMenu(el);
    if (!options) return false;
    const match = options.find((o) => o.textContent.trim() === answer);
    if (!match) {
      closeReactSelectMenu(el); // leave it closed rather than stuck open with unmatched search text
      return false;
    }
    fireMouseSequence(match);
    return true;
  }

  // Real root cause of education level / years-of-experience staying
  // permanently blank, found live 2026-09-16: both ARE react-select
  // comboboxes (confirmed against the real Samsara form), so the field
  // scan below never gave them a real "options" list the way it already
  // does for a native <select> -- react-select's real option text is
  // only ever knowable once its menu is actually open, unlike a native
  // <select>'s .options. Without that, the backend's exact-match/phrase-
  // cascade matching (_best_option_match) had nothing real to compare
  // against, so a correct canonical answer like "Master's Degree" could
  // never line up with this employer's own shorter real wording
  // ("Master's"). Opens the menu just to read the real rendered text,
  // then closes it again without selecting anything -- the actual
  // selection still only happens later, through the normal answer-
  // resolution path, once the backend has matched the real answer
  // against these real options exactly the way it already does for a
  // native <select>.
  async function harvestReactSelectOptions(el) {
    const options = await openReactSelectMenu(el);
    if (options) closeReactSelectMenu(el); // only when it's actually open -- see closeReactSelectMenu's own note
    return options ? options.map((o) => o.textContent.trim()) : [];
  }

  async function fillThisFrame(applicationId) {
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
      } else if (isReactSelectCombobox(el)) {
        // See harvestReactSelectOptions's docstring -- gives the backend
        // the same real option text a native <select> already provides,
        // instead of leaving these fields permanently unmatchable.
        const options = await harvestReactSelectOptions(el);
        if (options.length > 0) {
          fields.push({ field_id: fieldId, label, type: "select", options });
        } else {
          fields.push({ field_id: fieldId, label }); // couldn't open/read the menu this pass -- still safe, just falls back to an exact-text attempt
        }
      } else {
        fields.push({ field_id: fieldId, label });
      }
      elementById[fieldId] = el;
    }

    const textFill =
      fields.length === 0
        ? Promise.resolve({ filled: 0, total: 0 })
        : chrome.runtime.sendMessage({ type: "getAnswers", applicationId, fields }).then(async (response) => {
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
                // The backend only ever returns text that exactly matches
                // one of the real options this sent it -- find that
                // option's actual .value (not necessarily the same as its
                // display text) to set on the element.
                const match = realOptions(el).find((o) => o.textContent.trim() === answer);
                if (!match) {
                  highlight(el, false); // shouldn't happen; never silently pick the wrong option
                  attemptedUnanswerable.add(el);
                  continue;
                }
                setNativeValue(el, match.value);
              } else if (isReactSelectCombobox(el)) {
                // A plain value-set here would only LOOK filled -- see
                // selectReactSelectOption's docstring above. Real click-
                // based selection instead; a failed match (the backend's
                // answer text isn't one of this widget's real rendered
                // options) is left honestly unanswered, same as any other
                // field the backend can't confidently answer.
                const ok = await selectReactSelectOption(el, answer);
                if (!ok) {
                  highlight(el, false);
                  attemptedUnanswerable.add(el);
                  continue;
                }
              } else {
                setNativeValue(el, answer);
              }
              highlight(el, true);
              filled++;
            }
            return { filled, total: fields.length };
          });

    // File inputs run alongside the text/select batch, not through it --
    // which document goes where is decided client-side from the label
    // (see documentTypeForFileInput), never sent to the backend as a text
    // field to be "answered".
    return Promise.all([textFill, fillFileInputs(applicationId)]).then(([textResult, fileResult]) => {
      if (textResult.error) return textResult;
      return { filled: textResult.filled + fileResult.filled, total: textResult.total + fileResult.total };
    });
  }

  // Best-effort extraction for the popup's "+ Add This Job in One Click"
  // action -- there's no structured DOM contract to rely on here (unlike
  // ATS form fields, which is why the rest of this file only ever does
  // generic label/type detection), since this runs on an arbitrary
  // employer careers page the extension has never seen before. Real,
  // honest fallbacks at each step, never a fabricated guess: Open Graph
  // meta tags first (most real career pages set these for link
  // previews), then a plausible on-page heading, then the page's own
  // real URL/title as a last resort -- every fallback is still real text
  // that exists on/derived from the actual page, never invented.
  function extractJobTitle() {
    const ogTitle = document.querySelector('meta[property="og:title"]');
    if (ogTitle && ogTitle.content && ogTitle.content.trim()) return ogTitle.content.trim();
    const h1 = document.querySelector("h1");
    if (h1 && h1.textContent.trim()) return h1.textContent.trim();
    return document.title.trim();
  }

  // Real bug caught live before shipping: the real Samsara posting page
  // has no og:site_name at all, and its title is "Data Engineer - Remote
  // - Canada" -- a title-dash-split heuristic (originally tried first,
  // before the hostname) confidently picked "Canada" as the company.
  // That title shape (Title - Location - Location) is at least as
  // common as "Title - Company" in practice, so guessing from it is
  // unreliable. The page's own real hostname is always available and
  // never wrong in this particular way -- tried before the guess-prone
  // title split, which is now only a last-ditch fallback for the rare
  // case a hostname produces nothing usable at all (e.g. a bare IP).
  function extractCompanyName() {
    const ogSiteName = document.querySelector('meta[property="og:site_name"]');
    if (ogSiteName && ogSiteName.content && ogSiteName.content.trim()) return ogSiteName.content.trim();
    const host = (location.hostname || "").replace(/^www\./, "").split(".")[0];
    if (host && !/^\d+$/.test(host)) return host.charAt(0).toUpperCase() + host.slice(1);
    const titleParts = document.title.split(/[-|–—]/).map((p) => p.trim()).filter(Boolean);
    return titleParts.length > 1 ? titleParts[titleParts.length - 1] : "";
  }

  // The whole page's own visible text -- crude (includes nav/footer
  // noise a structured ATS-API scrape wouldn't have), but real, and
  // matches what a human would get copy-pasting the page themselves.
  // The downstream scoring/tailoring pipeline is LLM-driven and already
  // tolerant of real-world JD text quality from other sources; this is
  // the honest, generic-detection-only equivalent for a page with no
  // known structure at all.
  function extractJobPostingInfo() {
    return {
      title: extractJobTitle(),
      company: extractCompanyName(),
      description: document.body ? document.body.innerText.trim() : "",
    };
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
    const applicationId = lastResult.application.application_id;
    const fillResult = await fillThisFrame(applicationId);
    lastFillAt = Date.now();
    // Accumulate into this frame's own running total (fillThisFrame only
    // ever returns the delta for fields it just processed) and recompute
    // through the shared helper, so a sub-frame's already-reported
    // result is preserved rather than discarded by a direct click here.
    ownFillResult = { filled: ownFillResult.filled + fillResult.filled, total: ownFillResult.total + fillResult.total };
    updateBadgeResult();
    notifyFrames(applicationId);
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
    ownFillResult = await fillThisFrame(match.data.application_id);
    lastFillAt = Date.now();
    updateBadgeResult(match.data);
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
      // thing), not just a silent no-op. fillThisFrame only ever returns
      // the DELTA for fields it just processed (already-filled/marked-
      // unanswerable ones are excluded from its own scan) -- accumulate
      // into this frame's running total, never overwrite it, or a later
      // poll with a smaller delta would erase the count from an earlier
      // one.
      if (fillResult.total > 0) {
        lastFillAt = Date.now();
        ownFillResult = { filled: ownFillResult.filled + fillResult.filled, total: ownFillResult.total + fillResult.total };
        updateBadgeResult();
      }
      // The actual root cause of the Samsara case this whole extension
      // exists for: "Apply Now" doesn't reveal anything in THIS frame's
      // own DOM at all -- it creates a brand-new cross-origin iframe
      // (job-boards.greenhouse.io) that didn't exist when notifyFrames
      // was first called from mainFrameCheckAndFill, so that one-time
      // broadcast never reached it. Chrome does inject this content
      // script into that new iframe automatically (all_frames: true
      // applies to frames created after initial load too), but a
      // sub-frame only ever fills itself in response to a "fillPage"
      // message -- it never decides to on its own. Re-broadcasting on
      // every click-triggered check (not just the very first one) is
      // what actually reaches a sub-frame created after the page
      // originally loaded.
      notifyFrames(applicationId);
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
          // Accumulate (fillThisFrame only ever returns the delta for
          // fields it just processed), never overwrite.
          ownFillResult = { filled: ownFillResult.filled + fillResult.filled, total: ownFillResult.total + fillResult.total };
          updateBadgeResult();
        } else if (fillResult.total > 0) {
          // This frame IS a sub-frame (the real Samsara case: the whole
          // form lives in a different, Greenhouse, frame) -- the main
          // frame owns the one visible badge and has no way to see this
          // frame's own DOM, so report a real result back through the
          // background relay instead of it just vanishing here.
          chrome.runtime.sendMessage({ type: "reportSubFrameResult", filled: fillResult.filled, total: fillResult.total });
        }
        sendResponse(fillResult);
      });
      return true;
    }
    if (message.type === "subFrameFilled" && isMainFrame) {
      // A real sub-frame's real result (see background.js's
      // reportSubFrameResult relay) -- added to this frame's own
      // running total, never replacing it, since a page can genuinely
      // have both real fields itself AND a real embedded ATS iframe.
      subFrameFillResult = {
        filled: subFrameFillResult.filled + message.filled,
        total: subFrameFillResult.total + message.total,
      };
      updateBadgeResult();
      return false;
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
    if (message.type === "getPageInfo" && isMainFrame) {
      sendResponse(extractJobPostingInfo());
      return false;
    }
    if (message.type === "recheckMatch" && isMainFrame) {
      // The popup's "+ Add This Job" flow just created a real application
      // for this exact page -- re-running the normal check-and-fill pass
      // is what lets the on-page badge notice it too, instead of staying
      // stuck on a stale "no match" until the next full page reload.
      mainFrameCheckAndFill();
      sendResponse({ ok: true });
      return false;
    }
    return false;
  });

  if (isMainFrame) {
    mainFrameCheckAndFill();
  }
})();
