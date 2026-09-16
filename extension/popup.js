const STORAGE_KEY = "careerPilotBaseUrl";

const setupEl = document.getElementById("setup");
const mainEl = document.getElementById("main");
const statusEl = document.getElementById("status");
const fillBtn = document.getElementById("fill-btn");
const showBadgeBtn = document.getElementById("show-badge-btn");
const baseUrlInput = document.getElementById("base-url");
const matchCardEl = document.getElementById("match-card");
const matchTitleEl = document.getElementById("match-title");
const matchCompanyEl = document.getElementById("match-company");
const matchScoreBadgeEl = document.getElementById("match-score-badge");
const infoPanelEl = document.getElementById("info-panel");
const infoAlertDotEl = document.getElementById("info-alert-dot");
const infoResumeEl = document.getElementById("info-resume");
const infoCoverLetterEl = document.getElementById("info-cover-letter");
const infoWarningsEl = document.getElementById("info-warnings");
const infoProfileLinkEl = document.getElementById("info-profile-link");
const generateDocsBtn = document.getElementById("generate-docs-btn");
const addJobPanelEl = document.getElementById("add-job-panel");
const addJobBtn = document.getElementById("add-job-btn");
const addJobSubtextEl = document.getElementById("add-job-subtext");

let currentApplication = null; // the full match response (application_id, job_title, company_name, match_score, has_tailored_resume, has_tailored_cover_letter, profile_warnings)
let currentBaseUrl = null;

function showStatus(kind, text) {
  statusEl.innerHTML = "";
  const div = document.createElement("div");
  div.className = kind === "error" ? "status-error" : "status-ok";
  div.textContent = text;
  statusEl.appendChild(div);
}

async function getActiveTab() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  return tab;
}

async function refreshBadgeToggle(tab) {
  let collapsed = false;
  try {
    collapsed = await chrome.tabs.sendMessage(tab.id, { type: "getBadgeCollapsed" }, { frameId: 0 });
  } catch (e) {
    // no content script in this frame -- leave the toggle hidden
  }
  showBadgeBtn.hidden = !collapsed;
}

// "Your Autofill Information" -- researched against JobRight's own
// popup before building (which document is in play, a red-dot
// completeness alert) rather than invented. Every value here already
// existed elsewhere in the app (match_score, TailoredDocument rows,
// profile_service.profile_completeness_warnings) -- this only displays
// it. profile_warnings is deliberately about the PROFILE, not this one
// job -- see application_match_summary's own docstring for why a
// per-job version could never fire here.
function renderInfoPanel(application) {
  infoPanelEl.hidden = false;
  infoResumeEl.innerHTML = application.has_tailored_resume
    ? '<span class="ok-mark">&#10003;</span> Tailored resume ready for this application'
    : '<span class="pending-mark">&#9679;</span> Using your base profile -- no tailored resume for this one yet';
  infoCoverLetterEl.innerHTML = application.has_tailored_cover_letter
    ? '<span class="ok-mark">&#10003;</span> Tailored cover letter ready'
    : '<span class="pending-mark">&#9679;</span> No cover letter generated for this application yet';

  const warnings = application.profile_warnings || [];
  infoAlertDotEl.hidden = warnings.length === 0;
  if (warnings.length > 0) {
    infoWarningsEl.hidden = false;
    infoWarningsEl.innerHTML = "";
    warnings.forEach((w) => {
      const li = document.createElement("li");
      li.textContent = w;
      infoWarningsEl.appendChild(li);
    });
    if (currentBaseUrl) {
      infoProfileLinkEl.hidden = false;
      infoProfileLinkEl.href = currentBaseUrl.replace(/\/$/, "") + "/profile";
    }
  } else {
    infoWarningsEl.hidden = true;
    infoProfileLinkEl.hidden = true;
  }

  // The popup's "Generate Tailored Resume + Cover Letter" action --
  // researched against JobRight/Simplify Copilot's own popups before
  // building (both offer this inline, no navigating to a separate page).
  // Deliberately ONE button, not two independent ones: the real
  // pipeline always generates both documents together in a single pass,
  // so two buttons that both silently ran the whole thing would be
  // misleading about what each one does.
  generateDocsBtn.hidden = application.has_tailored_resume && application.has_tailored_cover_letter;
  generateDocsBtn.disabled = false;
  generateDocsBtn.textContent = "Generate Tailored Resume + Cover Letter";
}

function renderResult(result) {
  if (!result) {
    showStatus("ok", "Still checking this page...");
    fillBtn.hidden = true;
    showBadgeBtn.hidden = true;
    matchCardEl.hidden = true;
    infoPanelEl.hidden = true;
    addJobPanelEl.hidden = true;
    return;
  }
  if (result.error) {
    showStatus("error", result.error);
    fillBtn.hidden = true;
    showBadgeBtn.hidden = true;
    matchCardEl.hidden = true;
    infoPanelEl.hidden = true;
    addJobPanelEl.hidden = true;
    return;
  }
  if (!result.matched) {
    currentApplication = null;
    showStatus(
      "ok",
      "No matching Approved application for this page yet -- add it below, or this only works for applications you've already Approved in Career Pilot."
    );
    fillBtn.hidden = true;
    showBadgeBtn.hidden = true;
    matchCardEl.hidden = true;
    infoPanelEl.hidden = true;
    // Real gap Deshraj pointed at directly and researched against
    // JobRight/Simplify Copilot's own popups: a job that isn't
    // recognized yet shouldn't be a dead end -- offer to add it right
    // here instead of sending the user to the Jobs page.
    addJobPanelEl.hidden = false;
    addJobBtn.disabled = false;
    addJobBtn.textContent = "+ Add This Job in One Click";
    addJobSubtextEl.textContent = "See your match score and get a tailored resume + cover letter, right from this page.";
    return;
  }

  addJobPanelEl.hidden = true;
  currentApplication = result.application;
  matchCardEl.hidden = false;
  matchTitleEl.textContent = result.application.job_title;
  matchCompanyEl.textContent = result.application.company_name;
  if (typeof result.application.match_score === "number") {
    matchScoreBadgeEl.hidden = false;
    matchScoreBadgeEl.textContent = result.application.match_score + "% match";
  } else {
    matchScoreBadgeEl.hidden = true;
  }
  renderInfoPanel(result.application);

  if (result.total === 0) {
    showStatus("ok", "No fillable fields found on this page.");
  } else {
    showStatus(
      "ok",
      "Filled " + result.filled + " of " + result.total + " fields automatically -- " +
        "orange-dashed fields need your input. Review everything before submitting."
    );
  }
  fillBtn.hidden = false;
  fillBtn.textContent = "Autofill Again";
  getActiveTab().then(refreshBadgeToggle);
}

// The content script runs automatically on page load (see content.js) --
// this asks it what already happened rather than re-triggering a second
// check-and-fill pass just for opening the popup. A fresh page load's
// async work might not be done yet by the time the popup opens, so this
// retries once, briefly, instead of showing a permanent "still checking".
async function loadStatus(tab, isRetry) {
  let result;
  try {
    result = await chrome.tabs.sendMessage(tab.id, { type: "getStatus" });
  } catch (e) {
    // No content script in this tab yet (e.g. a chrome:// page it can't
    // run on) -- not an error state to alarm the user with.
    showStatus("ok", "This page can't be checked (browser-internal page, or the extension was just installed -- try reloading the tab).");
    fillBtn.hidden = true;
    return;
  }
  if (!result && !isRetry) {
    setTimeout(() => loadStatus(tab, true), 600);
    return;
  }
  renderResult(result);
}

async function fillCurrentPage() {
  if (!currentApplication) return; // fillBtn is hidden whenever this is null; extra guard against a stray click
  const tab = await getActiveTab();
  fillBtn.disabled = true;
  fillBtn.textContent = "Filling...";
  // Broadcasts to every frame (so a real embedded ATS sub-frame gets
  // re-filled too, exactly like content.js's own automatic run does via
  // notifyFrames) -- fire-and-forget, since this popup only displays the
  // main frame's own result below. Matching itself is never re-decided
  // here, only in content.js's mainFrameCheckAndFill.
  chrome.runtime.sendMessage({ type: "broadcastFill", tabId: tab.id, applicationId: currentApplication.application_id });
  const fillResult = await chrome.tabs.sendMessage(
    tab.id, { type: "fillPage", applicationId: currentApplication.application_id }, { frameId: 0 }
  );
  renderResult({ matched: true, application: currentApplication, ...fillResult });
  fillBtn.disabled = false;
}

// The popup's "+ Add This Job in One Click" action -- real gap Deshraj
// pointed at directly, researched against JobRight/Simplify Copilot's
// own popups before building (both let a user add an unrecognized
// posting and generate tailored documents right there, no navigating
// away). Extraction is best-effort (see content.js's
// extractJobPostingInfo -- there's no structured DOM contract for an
// arbitrary employer page), so the real backend error (a real title/
// description couldn't be found) is shown as-is rather than a generic
// failure message.
async function addThisJob() {
  addJobBtn.disabled = true;
  addJobBtn.textContent = "Adding...";
  addJobSubtextEl.textContent = "Reading this page...";

  const tab = await getActiveTab();
  let pageInfo;
  try {
    pageInfo = await chrome.tabs.sendMessage(tab.id, { type: "getPageInfo" }, { frameId: 0 });
  } catch (e) {
    addJobSubtextEl.textContent = "Couldn't read this page -- try reloading the tab first.";
    addJobBtn.disabled = false;
    addJobBtn.textContent = "+ Add This Job in One Click";
    return;
  }

  addJobSubtextEl.textContent = "Adding & starting to tailor...";
  const result = await chrome.runtime.sendMessage({
    type: "addJob",
    url: tab.url,
    jobTitle: pageInfo.title,
    companyName: pageInfo.company,
    jobDescription: pageInfo.description,
  });

  if (result.error) {
    addJobSubtextEl.textContent = result.error;
    addJobBtn.disabled = false;
    addJobBtn.textContent = "+ Add This Job in One Click";
    return;
  }

  addJobBtn.textContent = "Added";
  addJobSubtextEl.textContent = "Scoring & tailoring now -- usually 30-90s.";
  pollForMatchAfterAdd(tab, 15); // ~90s total at 6s intervals
}

// Best-effort while the popup happens to stay open -- a real multi-pass
// LLM tailoring run keeps going server-side (same background-thread
// pattern as the Jobs page's own "Tailor Now" button) even if the user
// closes the popup immediately after adding, so a lost poll here is
// never a lost result, only a missed live update.
async function pollForMatchAfterAdd(tab, attemptsLeft) {
  if (attemptsLeft <= 0) {
    addJobSubtextEl.textContent =
      "Still tailoring in the background -- reopen this popup in a bit, or check it on the Jobs page.";
    return;
  }
  let result;
  try {
    result = await chrome.runtime.sendMessage({ type: "checkMatch", url: tab.url });
  } catch (e) {
    return; // the popup is very likely closing -- nothing more to update
  }
  if (result.error) {
    addJobSubtextEl.textContent = result.error;
    return;
  }
  if (result.data && result.data.matched) {
    addJobPanelEl.hidden = true;
    currentApplication = result.data;
    // Lets the on-page badge notice the newly-approved application too,
    // instead of staying stuck on a stale "no match" until a reload.
    chrome.tabs.sendMessage(tab.id, { type: "recheckMatch" }, { frameId: 0 }).catch(() => {});
    await fillCurrentPage(); // completes the one-click promise: added, tailored, AND filled
    return;
  }
  setTimeout(() => pollForMatchAfterAdd(tab, attemptsLeft - 1), 6000);
}

// The popup's "Generate Tailored Resume + Cover Letter" action for an
// application that's already matched but has no tailored documents yet.
async function generateTailoredDocuments() {
  if (!currentApplication) return;
  generateDocsBtn.disabled = true;
  generateDocsBtn.textContent = "Tailoring... (usually 30-90s)";

  const result = await chrome.runtime.sendMessage({
    type: "tailorApplication",
    applicationId: currentApplication.application_id,
  });
  if (result.error) {
    showStatus("error", result.error);
    generateDocsBtn.disabled = false;
    generateDocsBtn.textContent = "Generate Tailored Resume + Cover Letter";
    return;
  }
  pollForTailoredDocuments(15);
}

async function pollForTailoredDocuments(attemptsLeft) {
  if (attemptsLeft <= 0) {
    generateDocsBtn.textContent = "Still tailoring -- reopen this popup in a bit";
    return;
  }
  const tab = await getActiveTab();
  let result;
  try {
    result = await chrome.runtime.sendMessage({ type: "checkMatch", url: tab.url });
  } catch (e) {
    return;
  }
  if (result.data && result.data.matched && (result.data.has_tailored_resume || result.data.has_tailored_cover_letter)) {
    currentApplication = result.data;
    renderInfoPanel(result.data);
    return;
  }
  setTimeout(() => pollForTailoredDocuments(attemptsLeft - 1), 6000);
}

async function loadBaseUrl() {
  const stored = await chrome.storage.local.get(STORAGE_KEY);
  return stored[STORAGE_KEY] || null;
}

async function init() {
  currentBaseUrl = await loadBaseUrl();
  if (currentBaseUrl) {
    baseUrlInput.value = currentBaseUrl;
    setupEl.hidden = true;
    mainEl.hidden = false;
    const tab = await getActiveTab();
    await loadStatus(tab, false);
  } else {
    setupEl.hidden = false;
    mainEl.hidden = true;
  }
}

document.getElementById("save-url").addEventListener("click", async () => {
  const url = baseUrlInput.value.trim().replace(/\/$/, "");
  if (!url) return;
  currentBaseUrl = url;
  await chrome.storage.local.set({ [STORAGE_KEY]: url });
  setupEl.hidden = true;
  mainEl.hidden = false;
  showStatus("ok", "Saved. Reload the job application tab for this to take effect there.");
  fillBtn.hidden = true;
});

document.getElementById("change-url").addEventListener("click", () => {
  mainEl.hidden = true;
  setupEl.hidden = false;
});

fillBtn.addEventListener("click", fillCurrentPage);
addJobBtn.addEventListener("click", addThisJob);
generateDocsBtn.addEventListener("click", generateTailoredDocuments);

showBadgeBtn.addEventListener("click", async () => {
  const tab = await getActiveTab();
  await chrome.tabs.sendMessage(tab.id, { type: "setBadgeCollapsed", collapsed: false }, { frameId: 0 });
  showBadgeBtn.hidden = true;
});

init();
