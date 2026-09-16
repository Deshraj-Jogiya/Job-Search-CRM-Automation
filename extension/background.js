// Career Pilot Autofill -- background service worker.
//
// The one job this file has that content.js/popup.js can't do
// themselves: read the admin_session cookie for the configured Career
// Pilot origin (chrome.cookies is a privileged extension API -- it can
// read an HttpOnly cookie, which page/content-script JS never can, by
// design) and attach it as an explicit header on this extension's own
// fetch() calls. It is never relied on as an ambient, auto-attached
// cookie: the target cookie is SameSite=Strict specifically so a
// THIRD-PARTY WEBSITE can never ride the user's session -- this
// extension is not a website, and reads the value on purpose, once,
// with the user's own browser already logged in. See
// app/routers/extension.py's docstring for the server-side half of this.

const STORAGE_KEY = "careerPilotBaseUrl";

async function getBaseUrl() {
  const stored = await chrome.storage.local.get(STORAGE_KEY);
  return stored[STORAGE_KEY] || null;
}

async function getSessionToken(baseUrl) {
  const cookie = await chrome.cookies.get({ url: baseUrl, name: "admin_session" });
  return cookie ? cookie.value : null;
}

// Shared by callApi (JSON POST) and fetchDocument (binary GET) -- both need
// the exact same "is the URL configured, is the user logged in" checks.
async function resolveAuth() {
  const baseUrl = await getBaseUrl();
  if (!baseUrl) {
    return { error: "Career Pilot URL isn't set yet -- open the extension popup and set it first." };
  }
  const token = await getSessionToken(baseUrl);
  if (!token) {
    return { error: "Not logged in -- log into Career Pilot in this browser (at " + baseUrl + "), then try again." };
  }
  return { baseUrl, token };
}

async function callApi(path, body) {
  const auth = await resolveAuth();
  if (auth.error) return auth;

  let response;
  try {
    response = await fetch(auth.baseUrl.replace(/\/$/, "") + path, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Career-Pilot-Session": auth.token },
      body: JSON.stringify(body),
    });
  } catch (e) {
    return { error: "Couldn't reach " + auth.baseUrl + " -- is it running and is the URL correct?" };
  }

  if (response.status === 401) {
    return { error: "Session expired -- log into Career Pilot again, then retry." };
  }
  if (!response.ok) {
    return { error: "Career Pilot returned an error (HTTP " + response.status + ")." };
  }
  return { data: await response.json() };
}

// Real PDF bytes for content.js's attachDocument to attach to a real
// <input type="file"> via the DataTransfer API. A plain GET (not JSON), so
// kept separate from callApi rather than forcing a binary response through
// the same POST-JSON-body shape.
async function fetchDocument(applicationId, documentType) {
  const auth = await resolveAuth();
  if (auth.error) return auth;

  let response;
  try {
    response = await fetch(
      auth.baseUrl.replace(/\/$/, "") + "/api/extension/documents/" + applicationId + "/" + documentType,
      { headers: { "X-Career-Pilot-Session": auth.token } }
    );
  } catch (e) {
    return { error: "Couldn't reach " + auth.baseUrl + " -- is it running and is the URL correct?" };
  }

  if (response.status === 401) {
    return { error: "Session expired -- log into Career Pilot again, then retry." };
  }
  if (!response.ok) {
    return { error: "No " + documentType.replace("_", " ") + " available to attach for this application yet." };
  }
  const buffer = await response.arrayBuffer();
  const filename = response.headers.get("X-Filename") || documentType + ".pdf";
  return { data: { buffer, filename } };
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message.type === "checkMatch") {
    callApi("/api/extension/match", { url: message.url }).then(sendResponse);
    return true; // keep the message channel open for the async response
  }
  if (message.type === "getAnswers") {
    callApi("/api/extension/answers", { application_id: message.applicationId, fields: message.fields }).then(sendResponse);
    return true;
  }
  if (message.type === "fetchDocument") {
    fetchDocument(message.applicationId, message.documentType).then(sendResponse);
    return true;
  }
  if (message.type === "addJob") {
    // The popup's "+ Add This Job in One Click" action.
    callApi("/api/extension/jobs", {
      url: message.url,
      job_title: message.jobTitle,
      company_name: message.companyName,
      job_description: message.jobDescription,
    }).then(sendResponse);
    return true;
  }
  if (message.type === "tailorApplication") {
    // The popup's "Generate Tailored Resume + Cover Letter" action for
    // an application that's already matched but has no tailored
    // documents yet -- a POST with no body, so reuses callApi as-is.
    callApi("/api/extension/applications/" + message.applicationId + "/tailor", {}).then(sendResponse);
    return true;
  }
  if (message.type === "broadcastFill") {
    // A content script can only ever message the background, never a
    // sibling frame directly -- this relays a confirmed match to every
    // frame of the tab (chrome.tabs.sendMessage reaches all frames when
    // no frameId is given), so a real embedded ATS iframe fills its own
    // DOM too, not just the top-level page. sender.tab is set when this
    // comes from a content script (it already knows its own tab); the
    // popup has no "tab" of its own, so it passes tabId explicitly
    // instead (its "Fill Again" button re-triggers the same broadcast).
    const tabId = sender.tab ? sender.tab.id : message.tabId;
    if (tabId != null) {
      chrome.tabs.sendMessage(tabId, { type: "fillPage", applicationId: message.applicationId });
    }
    sendResponse({ ok: true });
    return false;
  }
  if (message.type === "reportSubFrameResult") {
    // Real gap found live: a sub-frame (the actual Greenhouse iframe on
    // Samsara's page) fills its own DOM correctly, but that success was
    // never reported anywhere -- the main frame's badge only ever
    // reflected its OWN fillThisFrame result, which is genuinely 0 on a
    // page where the real form lives entirely in a different frame.
    // Relayed specifically to frame 0 (the main frame, which owns the
    // one visible badge) so it can fold a sub-frame's real result into
    // what it shows, instead of only ever knowing about itself.
    if (sender.tab) {
      chrome.tabs.sendMessage(
        sender.tab.id,
        { type: "subFrameFilled", filled: message.filled, total: message.total },
        { frameId: 0 }
      );
    }
    return false;
  }
  return false;
});
