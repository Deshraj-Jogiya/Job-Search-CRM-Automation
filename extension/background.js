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

async function callApi(path, body) {
  const baseUrl = await getBaseUrl();
  if (!baseUrl) {
    return { error: "Career Pilot URL isn't set yet -- open the extension popup and set it first." };
  }
  const token = await getSessionToken(baseUrl);
  if (!token) {
    return { error: "Not logged in -- log into Career Pilot in this browser (at " + baseUrl + "), then try again." };
  }

  let response;
  try {
    response = await fetch(baseUrl.replace(/\/$/, "") + path, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Career-Pilot-Session": token },
      body: JSON.stringify(body),
    });
  } catch (e) {
    return { error: "Couldn't reach " + baseUrl + " -- is it running and is the URL correct?" };
  }

  if (response.status === 401) {
    return { error: "Session expired -- log into Career Pilot again, then retry." };
  }
  if (!response.ok) {
    return { error: "Career Pilot returned an error (HTTP " + response.status + ")." };
  }
  return { data: await response.json() };
}

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message.type === "checkMatch") {
    callApi("/api/extension/match", { url: message.url }).then(sendResponse);
    return true; // keep the message channel open for the async response
  }
  if (message.type === "getAnswers") {
    callApi("/api/extension/answers", { application_id: message.applicationId, fields: message.fields }).then(sendResponse);
    return true;
  }
  return false;
});
