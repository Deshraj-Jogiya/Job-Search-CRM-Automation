const STORAGE_KEY = "careerPilotBaseUrl";

const setupEl = document.getElementById("setup");
const mainEl = document.getElementById("main");
const statusEl = document.getElementById("status");
const fillBtn = document.getElementById("fill-btn");
const baseUrlInput = document.getElementById("base-url");

let currentApplicationId = null;

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

async function checkCurrentPage() {
  const tab = await getActiveTab();
  if (!tab || !tab.url) {
    showStatus("error", "Can't read this tab's URL.");
    return;
  }
  showStatus("ok", "Checking this page against your applications...");
  const response = await chrome.runtime.sendMessage({ type: "checkMatch", url: tab.url });

  if (response.error) {
    showStatus("error", response.error);
    fillBtn.hidden = true;
    return;
  }
  if (!response.data.matched) {
    showStatus(
      "ok",
      "No matching Approved application for this page. This only works for applications you've already Approved in Career Pilot."
    );
    fillBtn.hidden = true;
    return;
  }
  currentApplicationId = response.data.application_id;
  showStatus("ok", "Found: " + response.data.job_title + " at " + response.data.company_name);
  fillBtn.hidden = false;
}

async function fillCurrentPage() {
  const tab = await getActiveTab();
  fillBtn.disabled = true;
  fillBtn.textContent = "Filling...";

  await chrome.scripting.executeScript({ target: { tabId: tab.id, allFrames: true }, files: ["content.js"] });
  // Broadcasts to every frame in the tab (chrome.tabs.sendMessage's
  // documented default with no frameId given) -- each frame's own copy
  // of content.js (just injected above) independently detects and fills
  // its own fields. Only one frame's response comes back from this
  // call even though all of them ran, so this deliberately doesn't try
  // to report a precise fill count -- that would risk showing a number
  // that's quietly wrong for a multi-frame page (the exact Samsara-
  // style embedded-iframe case this extension exists for).
  await chrome.tabs.sendMessage(tab.id, { type: "fillPage", applicationId: currentApplicationId });

  showStatus("ok", "Filled what it could. Green outline = filled from your profile; orange dashed = needs your input.");
  fillBtn.disabled = false;
  fillBtn.textContent = "Fill This Page";
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
    await checkCurrentPage();
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
  await checkCurrentPage();
});

document.getElementById("change-url").addEventListener("click", () => {
  mainEl.hidden = true;
  setupEl.hidden = false;
});

fillBtn.addEventListener("click", fillCurrentPage);

init();
