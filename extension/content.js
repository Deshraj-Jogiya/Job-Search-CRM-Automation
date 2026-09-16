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
// Runs the check-and-fill automatically on load, with zero click
// required -- matching the existing server-side autofill's own
// precedent (evaluate_and_enqueue auto-launches a real browser and
// fills it immediately for a clean, autofill-supported, well-matched
// application, no manual trigger needed there either). A real, earlier
// version of this required opening the popup to check, then a separate
// click to fill -- real feedback from using it live: that's an extra
// step this app's whole point is to remove, not add. The popup is still
// available for a manual re-run (see the fillPage listener at the
// bottom), not as the primary path.
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

  let lastResult = null;

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

  async function run(applicationId) {
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
      return { filled: 0, total: 0 };
    }

    const response = await chrome.runtime.sendMessage({ type: "getAnswers", applicationId, fields });
    if (response.error) {
      return { error: response.error };
    }

    const answers = response.data;
    let filled = 0;
    for (const [fieldId, el] of Object.entries(elementById)) {
      const answer = answers[fieldId];
      if (!answer) {
        highlight(el, false);
        continue;
      }
      if (el.tagName === "SELECT") {
        // The backend only ever returns text that exactly matches one of
        // the real options this sent it -- find that option's actual
        // .value (not necessarily the same string as its display text)
        // to set on the element.
        const match = realOptions(el).find((o) => o.textContent.trim() === answer);
        if (!match) {
          highlight(el, false); // shouldn't happen; never silently pick the wrong option
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
  }

  // The one thing that actually runs on every page load, zero clicks.
  // Re-checks match fresh each time (never trusts a stale applicationId
  // from a previous call) -- cheap (one small POST to the user's own
  // server), and the real safety gate is still server-side anyway
  // (find_fillable_application only ever matches a real "Approved"
  // application).
  async function autoCheckAndFill() {
    const match = await chrome.runtime.sendMessage({ type: "checkMatch", url: location.href });
    if (match.error || !match.data.matched) {
      lastResult = match.error ? { error: match.error } : { matched: false };
      return;
    }
    const fillResult = await run(match.data.application_id);
    lastResult = { matched: true, application: match.data, ...fillResult };
  }

  chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    if (message.type === "fillPage") {
      autoCheckAndFill().then(() => sendResponse(lastResult));
      return true;
    }
    if (message.type === "getStatus") {
      sendResponse(lastResult);
      return false;
    }
    return false;
  });

  autoCheckAndFill();
})();
