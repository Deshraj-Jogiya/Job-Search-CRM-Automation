// Career Pilot Autofill -- content script. Injected on demand (via
// chrome.scripting.executeScript, allFrames: true, triggered only by
// the user clicking "Fill this page" in the popup) into every frame of
// the tab, including cross-origin embedded ones -- this is the actual
// fix for the employer-wrapped ATS case (e.g. Samsara: the real form
// lives in an iframe on a different origin than the employer's own
// careers page, which the server-side Playwright automation couldn't
// reliably reach and got bot-blocked on from the VM's datacenter IP
// besides). Running here, in the user's own real browser, sidesteps
// both problems at once.
//
// Deliberately does ONLY generic field detection (label text + input
// type) -- no per-ATS knowledge. The actual answer for each label comes
// from the real backend (extension_service.py), which reuses the exact
// same mechanical_common_answer/contact-field logic Playwright autofill
// already has. A field this can't confidently answer is left blank, on
// purpose -- never a guess.

(function () {
  // Clicking "Fill This Page" twice re-injects this file -- without this
  // guard, each injection would stack another onMessage listener in the
  // same frame (executeScript doesn't dedupe), so a single click would
  // eventually trigger N redundant fill passes. Harmless in effect
  // (isFillable skips anything already non-empty) but wasteful; skip
  // re-registering entirely instead.
  if (window.__careerPilotFillLoaded) return;
  window.__careerPilotFillLoaded = true;

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

  function isFillable(el) {
    if (el.disabled || el.readOnly) return false;
    if (el.offsetParent === null) return false; // hidden
    if (el.tagName === "INPUT") {
      const type = (el.getAttribute("type") || "text").toLowerCase();
      return ["text", "email", "tel", "url"].includes(type) && !el.value;
    }
    if (el.tagName === "TEXTAREA") return !el.value;
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
    const proto = el.tagName === "TEXTAREA" ? window.HTMLTextAreaElement.prototype : window.HTMLInputElement.prototype;
    const descriptor = Object.getOwnPropertyDescriptor(proto, "value");
    descriptor.set.call(el, value);
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
  }

  function highlight(el, ok) {
    el.style.outline = ok ? "2px solid #10b981" : "2px dashed #f59e0b";
    el.style.outlineOffset = "1px";
  }

  async function run(applicationId) {
    const elements = Array.from(document.querySelectorAll("input, textarea"));
    const fields = [];
    const elementById = {};
    let nextId = 0;

    for (const el of elements) {
      if (!isFillable(el)) continue;
      const label = findLabelText(el);
      if (!label) continue;
      const fieldId = "f" + nextId++;
      fields.push({ field_id: fieldId, label });
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
      if (answers[fieldId]) {
        setNativeValue(el, answers[fieldId]);
        highlight(el, true);
        filled++;
      } else {
        highlight(el, false);
      }
    }
    return { filled, total: fields.length };
  }

  chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    if (message.type === "fillPage") {
      run(message.applicationId).then(sendResponse);
      return true;
    }
    return false;
  });
})();
