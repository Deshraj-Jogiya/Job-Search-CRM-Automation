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

  // Real, major gap found live testing against a real SmartRecruiters
  // application: its entire form is built from Shadow DOM web
  // components (<spl-input>, <spl-form-field>, <spl-dropzone>, etc. --
  // SmartRecruiters' own design system), nested multiple levels deep.
  // A plain document.querySelectorAll never crosses a shadow boundary
  // at all, by design (that's the whole point of shadow DOM
  // encapsulation) -- confirmed live this meant the extension detected
  // literally ZERO fields on this platform, not just some. queryAllDeep
  // recursively collects matches from the light DOM AND every shadow
  // root nested anywhere within it; on every other platform tested
  // this session (none of which use shadow DOM), it behaves exactly
  // like a plain querySelectorAll, so this is purely additive.
  function queryAllDeep(root, selector) {
    let results = Array.from(root.querySelectorAll(selector));
    const all = root.querySelectorAll("*");
    for (const el of all) {
      if (el.shadowRoot) {
        results = results.concat(queryAllDeep(el.shadowRoot, selector));
      }
    }
    return results;
  }

  // A label and the element it describes (via for=/id) are only ever
  // meaningfully connected within the SAME shadow root scope -- an id
  // reference can't reach across a shadow boundary. el.getRootNode()
  // returns the element's own ShadowRoot when it's inside one, or the
  // top-level document otherwise, so scoping every for=/id lookup to
  // the target element's own root (instead of always the top-level
  // document) is what makes this work correctly inside SmartRecruiters'
  // nested components without needing to know anything about them
  // specifically.
  function rootOf(el) {
    return el.getRootNode();
  }

  // A plain node.parentElement walk stops dead at a ShadowRoot boundary
  // (a ShadowRoot's own .parentElement is always null) -- real bug
  // caught live testing SmartRecruiters' real resume upload: its real
  // "Resume *" heading lives in the LIGHT DOM, one level above the
  // <spl-dropzone> custom element whose shadow root contains the actual
  // <input type="file">, so a plain parentElement walk from the input
  // never reaches it at all. Jumping to the shadow root's own host
  // element when parentElement is null is what lets an ancestor walk
  // continue past a shadow boundary into the real surrounding page.
  function parentOrHost(node) {
    if (node.parentElement) return node.parentElement;
    const root = node.getRootNode();
    return root && root.host ? root.host : null;
  }

  // Real bug found live testing against a real Workable application:
  // a real field's label (e.g. "Address") sits alongside a real SVG
  // help/info icon, and that icon's own accessibility fallback text
  // ("SVGs not supported by this browser.") is a real text node inside
  // the <svg>, included in .textContent even though no real browser in
  // use today actually falls back to it. Left in, this polluted both
  // regular field labels and, worse, real select/radio OPTION text
  // (e.g. "SVGs not supported by this browser.Yes" instead of "Yes"),
  // which only happened to still match the backend's exact answer via
  // a lucky word-boundary next to a trailing period -- fragile, not a
  // real fix. Strips any <svg> descendant's text before reading a
  // label, generically (no Workable-specific knowledge), by reading
  // from a clone with all <svg> descendants removed rather than the
  // live element.
  function visibleText(el) {
    const clone = el.cloneNode(true);
    clone.querySelectorAll("svg").forEach((svg) => svg.remove());
    return clone.textContent.trim();
  }

  function findLabelText(el) {
    if (el.id) {
      const byFor = rootOf(el).querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (byFor && visibleText(byFor)) return visibleText(byFor);
    }
    const ancestorLabel = el.closest("label");
    if (ancestorLabel && visibleText(ancestorLabel)) return visibleText(ancestorLabel);
    const ariaLabel = el.getAttribute("aria-label");
    if (ariaLabel && ariaLabel.trim()) return ariaLabel.trim();
    const ariaLabelledBy = el.getAttribute("aria-labelledby");
    if (ariaLabelledBy) {
      const referenced = rootOf(el).getElementById(ariaLabelledBy);
      if (referenced && visibleText(referenced)) return visibleText(referenced);
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

  // Real, serious bug found live 2026-09-16 testing against a real
  // Salesforce Workday application: a genuine anti-bot honeypot field
  // (Workday's own data-automation-id="beecatcher", name="website",
  // labeled "Enter website. This input is for robots only, do not
  // enter if you're human.") has offsetParent !== null -- the existing
  // hidden check never caught it -- because it's hidden via the classic
  // clip-to-1px-and-clip-rect technique (width/height ~1px), not
  // display:none. Confirmed live this would have been genuinely
  // filled: the label contains the word "website", which matches
  // _PORTFOLIO_RE on the backend, and it would have returned Deshraj's
  // real portfolio URL -- exactly the kind of thing that gets a real
  // application flagged as a bot submission.
  //
  // A near-zero-size INPUT alone is not enough to call it hidden,
  // though -- a real bug caught live testing against a real Recruitee
  // application immediately after shipping the naive version of this
  // check: Recruitee's own real radio/checkbox inputs are THEMSELVES
  // genuinely ~1x1px (a completely different custom-styling technique
  // than Ashby's, which keeps the native input full-sized), with a
  // real, visible, normally-sized <label> as the actual control a
  // human sees and clicks. The distinguishing signal, confirmed
  // against both real cases: Workday's honeypot has NO real visible
  // label either (its own label is ALSO ~1x1px, genuinely invisible to
  // any human) -- Recruitee's real radio's label is a real, visible
  // 105x48px element. Only when BOTH the element and its own label (if
  // any) are near-zero-sized is this actually invisible to a human.
  function isEffectivelyHidden(el) {
    if (el.offsetParent === null) return true;
    const rect = el.getBoundingClientRect();
    if (rect.width > 2 && rect.height > 2) return false;

    const label = (el.id && rootOf(el).querySelector(`label[for="${CSS.escape(el.id)}"]`)) || el.closest("label");
    if (label) {
      const labelRect = label.getBoundingClientRect();
      if (labelRect.width > 2 && labelRect.height > 2) return false;
    }
    return true;
  }

  function isFillable(el) {
    if (attemptedUnanswerable.has(el)) return false;
    if (el.disabled || el.readOnly) return false;
    if (isEffectivelyHidden(el)) return false;
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
      // "number" added after finding real, live Recruitee fields this
      // excluded entirely -- "How many years of professional
      // experience do you have?" and an hourly-rate field are both
      // genuinely <input type="number">, and years-of-experience is
      // exactly the kind of field the backend already has real answer
      // logic for (_years_experience_answer returns a plain number
      // string for a non-select field, which a real number input
      // accepts the same as any text value).
      return ["text", "email", "tel", "url", "number"].includes(type) && !el.value;
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

  // Real gap found live 2026-09-16 while testing against Ashby -- the
  // first ATS this whole extension had been tested against besides
  // Greenhouse. EEO fields (gender, race, veteran/disability status)
  // and real yes/no questions are commonly rendered as native
  // <input type="radio">/"checkbox"> groups there, a shape this
  // extension had ZERO detection for from its very first version. This
  // predates the extension entirely -- the older Playwright-based
  // autofill_service.py never handled radio/checkbox either (grep for
  // "radio"/"checkbox" there returns nothing) -- it was never caught
  // because every live test this whole session used Samsara's
  // Greenhouse form, which happens not to use this shape at all. The
  // backend's mechanical_common_answer already has real EEO-answering
  // logic (gender/race/veteran/disability, sourced from the profile's
  // own eeo fields) -- confirmed live it just needed a real caller: the
  // real stored profile value ("No, I do not have a disability and
  // have not had one in the past") is a literal, exact match for
  // Ashby's own real rendered option text.
  //
  // Never auto-checks a consent/legal-acknowledgment checkbox ("I
  // acknowledge...", "I agree...", "I certify...") -- those are a
  // categorically different kind of question from a factual EEO
  // default, same reasoning already applied to Samsara's "AI Policy"/
  // "Processing of Personal Data" fields.
  const CONSENT_CHECKBOX_RE = /\b(acknowledge|agree|certif|consent|have read|confirm)\b/i;

  // Real bug caught live testing against a second real ATS (Lever)
  // before shipping: Lever's own radio/checkbox options have no id at
  // all -- each option's real text lives in a <label> that WRAPS the
  // input directly (`<label><input>...<span>He/him</span></label>`),
  // never a separate `label[for=id]`. Ashby's own options DO use
  // for=id. Checking both, in the same order findLabelText already
  // does for every other field type, covers both real shapes.
  function optionLabelFor(el) {
    if (el.id) {
      const byFor = rootOf(el).querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (byFor && visibleText(byFor)) return visibleText(byFor);
    }
    const ancestorLabel = el.closest("label");
    if (ancestorLabel && visibleText(ancestorLabel)) return visibleText(ancestorLabel);
    return null;
  }

  // The group's own real question text -- three real shapes confirmed
  // live, neither Greenhouse-specific knowledge, all from genuinely
  // different real fields (two different ATSes, and two different
  // real Lever field shapes from each other):
  // 1. Ashby: a <fieldset> spanning the whole group, with its own
  //    heading <label> alongside each option's separate label[for=id]
  //    (excluded here since it's for= one of the group's own ids).
  // 2. Lever's own standard fields (e.g. Pronouns): no <fieldset>, no
  //    heading <label> either -- the real question text is a plain
  //    sibling <div>, some bounded number of levels above whatever
  //    div/ul wraps all the real options, that doesn't itself contain
  //    any of the group's own inputs.
  // 3. Lever's EMPLOYER-CUSTOMIZED questions (a real Allegiant one:
  //    "This position requires working at our Las Vegas, NV
  //    headquarters..."): the heading isn't a separate element at all
  //    -- it's a plain text node typed directly inside the SAME
  //    container that also holds the real options, ahead of them in
  //    DOM order.
  function headingOwnText(node) {
    const ownText = Array.from(node.childNodes)
      .filter((n) => n.nodeType === Node.TEXT_NODE)
      .map((n) => n.textContent.trim())
      .join(" ")
      .trim();
    return ownText || null;
  }

  function groupQuestionLabel(groupEls) {
    let node = groupEls[0];
    while (node && !groupEls.every((el) => node.contains(el))) {
      node = node.parentElement;
    }
    if (!node) return null;
    const smallestContainer = node;

    const optionIds = new Set(groupEls.map((el) => el.id).filter(Boolean));
    const labels = queryAllDeep(smallestContainer, "label");
    const labelHeading = labels.find((l) => {
      const forId = l.getAttribute("for");
      if (forId && optionIds.has(forId)) return false; // an option's own label via for=
      if (groupEls.some((el) => l.contains(el))) return false; // an option's own label via wrapping
      return visibleText(l);
    });
    if (labelHeading) return visibleText(labelHeading);

    let ancestor = smallestContainer;
    for (let depth = 0; ancestor && depth < 5; depth++, ancestor = ancestor.parentElement) {
      if (ancestor.children) {
        const headingChild = Array.from(ancestor.children).find(
          (c) => !groupEls.some((el) => c.contains(el)) && visibleText(c)
        );
        if (headingChild) return visibleText(headingChild);
      }
      const ownText = headingOwnText(ancestor);
      if (ownText) return ownText;
    }
    return null;
  }

  function collectRadioCheckboxGroups() {
    const elements = queryAllDeep(document, 'input[type="radio"], input[type="checkbox"]');
    const groups = new Map(); // shared name -> elements[]; an unnamed/standalone checkbox gets its own single-element group
    let anonIndex = 0;
    for (const el of elements) {
      if (attemptedUnanswerable.has(el)) continue;
      if (el.disabled) continue;
      if (isEffectivelyHidden(el)) continue;
      const key = el.name || `__anon${anonIndex++}`;
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(el);
    }
    return groups;
  }

  async function fillRadioAndCheckboxGroups(applicationId) {
    const groups = collectRadioCheckboxGroups();
    const fields = [];
    const groupById = {};
    let nextId = 0;

    for (const els of groups.values()) {
      if (els.some((el) => el.checked)) continue; // already answered (by this extension or the human) -- never re-touch

      if (els.length > 1) {
        // A real multi-option group (radio, or occasionally checkboxes
        // sharing a name for a "select several" pattern) -- represented
        // to the backend exactly like a native <select>, reusing its
        // existing exact-match/phrase-cascade matching, not a second
        // copy of that logic.
        const label = groupQuestionLabel(els);
        if (!label) continue;
        const options = els.map((el) => optionLabelFor(el)).filter(Boolean);
        if (options.length === 0) continue;
        const fieldId = "r" + nextId++;
        fields.push({ field_id: fieldId, label, type: "select", options });
        groupById[fieldId] = els;
      } else {
        // A single standalone checkbox -- a real yes/no toggle (e.g.
        // "Current role"), never a multi-choice group. Represented as a
        // synthetic Yes/No "select" so it reuses the exact same
        // resolution path a real 2-option select already has, rather
        // than a third, separate answer shape.
        const el = els[0];
        const label = optionLabelFor(el) || findLabelText(el);
        if (!label || CONSENT_CHECKBOX_RE.test(label)) continue;
        const fieldId = "r" + nextId++;
        fields.push({ field_id: fieldId, label, type: "select", options: ["Yes", "No"] });
        groupById[fieldId] = els;
      }
    }

    if (fields.length === 0) return { filled: 0, total: 0 };

    const response = await chrome.runtime.sendMessage({ type: "getAnswers", applicationId, fields });
    if (response.error) return { error: response.error };
    const answers = response.data;
    let filled = 0;
    for (const [fieldId, els] of Object.entries(groupById)) {
      const answer = answers[fieldId];
      if (!answer) {
        els.forEach((el) => attemptedUnanswerable.add(el)); // never keep re-asking every poll -- same posture as any other unanswerable field
        continue;
      }
      if (els.length > 1) {
        const match = els.find((el) => optionLabelFor(el) === answer);
        if (!match) {
          els.forEach((el) => attemptedUnanswerable.add(el)); // shouldn't happen; never guess
          continue;
        }
        match.checked = true;
        match.dispatchEvent(new Event("input", { bubbles: true }));
        match.dispatchEvent(new Event("change", { bubbles: true }));
        highlight(match, true);
        filled++;
      } else if (answer === "Yes") {
        els[0].checked = true;
        els[0].dispatchEvent(new Event("input", { bubbles: true }));
        els[0].dispatchEvent(new Event("change", { bubbles: true }));
        highlight(els[0], true);
        filled++;
      } else {
        // "No" -- already correctly unchecked, nothing to click. Marked
        // resolved anyway so this doesn't keep re-appearing (and
        // re-inflating the running total) on every later poll.
        attemptedUnanswerable.add(els[0]);
      }
    }
    return { filled, total: fields.length };
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
    // Deliberately no visibility/hidden check of any kind here -- real
    // bug found live testing against a real Personio application: its
    // real resume file input (id="doc-input-cv") is display:none
    // entirely (offsetParent === null, width/height 0), triggered only
    // through a separate, visible "Upload CV" button that calls the
    // real input's own .click() under the hood. An offsetParent check
    // (matching every other field type) would have excluded it
    // completely -- file attachment would never have worked on
    // Personio at all. This is safe specifically for file inputs
    // because attachDocument sets .files via DataTransfer directly on
    // the element, which needs no real visibility or user-driven click
    // to register -- unlike a text/select/radio field, there's no
    // "the user needs to be able to see and interact with this"
    // requirement here. The real safety net against ever attaching to
    // an unrelated hidden input stays documentTypeForFileInput's own
    // requirement of an unambiguous resume/cover-letter label match --
    // not visibility.
    if (!document.contains(el)) return false;
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

    let node = parentOrHost(el);
    for (let depth = 0; node && depth < 8; depth++, node = parentOrHost(node)) {
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
    const fileInputs = queryAllDeep(document, 'input[type="file"]').filter(isFillableFileInput);
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
        const listbox = listboxId && rootOf(el).getElementById(listboxId);
        if (!listbox) return null;
        const found = queryAllDeep(listbox, '[role="option"]');
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
    const elements = queryAllDeep(document, "input, textarea, select");
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

    // File inputs and radio/checkbox groups both run alongside the text/
    // select batch, not through it -- each has its own detection shape
    // (which document goes where is decided client-side from the label;
    // radio/checkbox groups need their own DOM grouping-by-name pass)
    // that doesn't fit the plain input/textarea/select scan above.
    return Promise.all([textFill, fillFileInputs(applicationId), fillRadioAndCheckboxGroups(applicationId)]).then(
      ([textResult, fileResult, radioResult]) => {
        if (textResult.error) return textResult;
        if (radioResult.error) return radioResult;
        return {
          filled: textResult.filled + fileResult.filled + radioResult.filled,
          total: textResult.total + fileResult.total + radioResult.total,
        };
      }
    );
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
