const STORAGE_KEY = "careerPilotBaseUrl";

const setupEl = document.getElementById("setup");
const mainEl = document.getElementById("main");
const statusEl = document.getElementById("status");
const fillBtn = document.getElementById("fill-btn");
const showBadgeBtn = document.getElementById("show-badge-btn");
const baseUrlInput = document.getElementById("base-url");

let currentApplication = null; // {application_id, job_title, company_name} from the last matched result

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

function renderResult(result) {
  if (!result) {
    showStatus("ok", "Still checking this page...");
    fillBtn.hidden = true;
    showBadgeBtn.hidden = true;
    return;
  }
  if (result.error) {
    showStatus("error", result.error);
    fillBtn.hidden = true;
    showBadgeBtn.hidden = true;
    return;
  }
  if (!result.matched) {
    currentApplication = null;
    showStatus(
      "ok",
      "No matching Approved application for this page. This only works for applications you've already Approved in Career Pilot."
    );
    fillBtn.hidden = true;
    showBadgeBtn.hidden = true;
    return;
  }
  currentApplication = result.application;
  const label = result.application.job_title + " at " + result.application.company_name;
  if (result.total === 0) {
    showStatus("ok", "Found: " + label + ". No fillable fields found on this page.");
  } else {
    showStatus(
      "ok",
      "Found: " + label + ". Filled " + result.filled + " of " + result.total + " fields automatically -- " +
        "orange-dashed fields need your input. Review everything before submitting."
    );
  }
  fillBtn.hidden = false;
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
  fillBtn.textContent = "Fill Again";
}

async function loadBaseUrl() {
  const stored = await chrome.storage.local.get(STORAGE_KEY);
  return stored[STORAGE_KEY] || null;
}

async function init() {
  const baseUrl = await loadBaseUrl();
  if (baseUrl) {
    baseUrlInput.value = baseUrl;
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

showBadgeBtn.addEventListener("click", async () => {
  const tab = await getActiveTab();
  await chrome.tabs.sendMessage(tab.id, { type: "setBadgeCollapsed", collapsed: false }, { frameId: 0 });
  showBadgeBtn.hidden = true;
});

init();
