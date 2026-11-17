/* app.js — shared bits used by the other files: constants, page state, small helpers,
   the server API call, and the auto-refresh loop. Loaded first. */
"use strict";
const $ = (s, el = document) => el.querySelector(s);
const PLAT = {
  youtube: { name: "YouTube", cls: "yt", icon: '<svg width="16" height="16" viewBox="0 0 24 24"><rect x="2" y="5" width="20" height="14" rx="4" fill="#ff3d3d"/><path d="M10 9l5 3-5 3z" fill="#fff"/></svg>' },
  instagram: { name: "Instagram", cls: "ig", icon: '<svg width="16" height="16" viewBox="0 0 24 24"><rect x="2" y="2" width="20" height="20" rx="6" fill="#dd2a7b"/><circle cx="12" cy="12" r="4.2" fill="none" stroke="#fff" stroke-width="2"/><circle cx="17.3" cy="6.7" r="1.2" fill="#fff"/></svg>' },
};
const STEPS = {
  link: ["Video info", "Transcript", "AI moments", "Cropping", "Ready"],
  file: ["Preparing", "Uploading"],
  post: ["Uploading"],
};

let S = null;            // last /api/state
let fetchedAt = 0;       // when S arrived (for the countdowns)
let uploading = false;

/* ---------- helpers ---------- */
function h(tag, attrs = {}, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "html") el.innerHTML = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) if (kid != null && kid !== false) el.append(kid);
  return el;
}
function toast(msg, err = false) {
  const t = h("div", { class: "toast" + (err ? " err" : "") }, msg);
  $("#toasts").append(t);
  setTimeout(() => t.remove(), err ? 7000 : 4000);
}
function dur(sec) {
  sec = Math.max(0, Math.round(sec));
  const hh = Math.floor(sec / 3600), mm = Math.floor(sec % 3600 / 60), ss = sec % 60;
  return hh ? `${hh}h ${String(mm).padStart(2, "0")}m ${String(ss).padStart(2, "0")}s` : `${mm}m ${String(ss).padStart(2, "0")}s`;
}
const mmss = s => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`;
const mb = b => (b / 1048576).toFixed(b > 104857600 ? 0 : 1) + " MB";
function ago(ts) {
  const s = Date.now() / 1000 - ts;
  if (s < 60) return "just now";
  if (s < 3600) return Math.floor(s / 60) + " min ago";
  if (s < 86400) return Math.floor(s / 3600) + " h ago";
  return new Date(ts * 1000).toLocaleDateString();
}
function relogin() {
  // ask the browser to show the login box again, but never get stuck reloading
  let last = 0; try { last = +sessionStorage.getItem("splitter-relogin") || 0; } catch { }
  if (Date.now() - last > 30000) { try { sessionStorage.setItem("splitter-relogin", Date.now()); } catch { } location.reload(); }
}
async function api(path, body) {
  const res = await fetch(location.origin + path, body === undefined ? { cache: "no-store" } : {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  if (res.status === 401) { relogin(); throw new Error("Logged out — reload the page and log in again"); }
  let data = null;
  try { data = await res.json(); } catch { }
  if (!res.ok) throw new Error((data && data.error) || `Server error ${res.status}`);
  return data;
}

/* platform toggles: both on by default, unconfigured ones disabled */
function platToggles(container, name) {
  container.replaceChildren();
  for (const [key, p] of Object.entries(PLAT)) {
    const ready = !S || S[key].configured;
    container.append(h("label", { class: `plat ${p.cls}`, title: ready ? "" : `${p.name} isn't set up in .env` },
      h("input", { type: "checkbox", name, value: key, checked: ready, disabled: !ready }),
      h("span", { html: `<i class="tick"></i>${p.icon}${p.name}${ready ? "" : " <small>(not set up)</small>"}` })));
  }
}
const chosenPlats = container => [...container.querySelectorAll("input:checked")].map(i => i.value);

/* ---------- polling ---------- */
let timer = null, inflight = false, firstLoad = true;
async function refresh() {
  if (inflight) return;
  inflight = true;
  clearTimeout(timer);
  try {
    S = await api("/api/state");
    fetchedAt = Date.now();
    renderStats(); renderJobs(); renderReviews(); renderHistory();
    if (firstLoad) { platToggles($('[data-plats="file"]'), "fp"); firstLoad = false; }
  } catch (err) {
    $("#workerText").textContent = "Can't reach the server — retrying…";
    $("#workerDot").className = "dot busy";
  } finally {
    inflight = false;
    timer = setTimeout(refresh, S && S.busy ? 2000 : 8000);
  }
}
// start once every script file is loaded
document.addEventListener("DOMContentLoaded", () => {
  setInterval(tick, 1000);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
  platToggles($('[data-plats="file"]'), "fp");
  refresh();
});
