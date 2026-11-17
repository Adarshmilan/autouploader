/* forms.js — the two ways to post: the tabs, the "YouTube link" form and the
   "Upload my video" drag & drop (preview, title/caption, upload progress). */
/* ---------- tabs ---------- */
document.querySelectorAll(".tab").forEach(btn => btn.addEventListener("click", () => {
  document.querySelectorAll(".tab").forEach(b => b.classList.toggle("on", b === btn));
  $("#tab-link").hidden = btn.dataset.tab !== "link";
  $("#tab-file").hidden = btn.dataset.tab !== "file";
  try { localStorage.setItem("splitter-tab", btn.dataset.tab); } catch { }
}));
try { const t = localStorage.getItem("splitter-tab"); if (t) $(`.tab[data-tab="${t}"]`)?.click(); } catch { }

/* ---------- link ---------- */
$("#linkForm").addEventListener("submit", async e => {
  e.preventDefault();
  const url = $("#linkInput").value.trim();
  if (!/^(https?:\/\/)?([\w-]+\.)*(youtube\.com|youtu\.be)\//i.test(url)) return toast("That doesn't look like a YouTube link.", true);
  const btn = $("#linkBtn");
  btn.disabled = true; btn.innerHTML = '<span class="spin"></span>Starting…';
  try {
    await api("/api/run", { url: /^https?:/i.test(url) ? url : "https://" + url });
    $("#linkInput").value = "";
    toast("Started! The clips will appear below for review.");
    refresh();
  } catch (err) { toast(err.message, true); }
  finally { btn.disabled = false; btn.textContent = "Make clips"; }
});

/* ---------- drag & drop upload ---------- */
let picked = null;
const drop = $("#drop");
drop.addEventListener("click", () => $("#fileInput").click());
drop.addEventListener("keydown", e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); $("#fileInput").click(); } });
$("#fileInput").addEventListener("change", e => e.target.files[0] && pickFile(e.target.files[0]));
$("#changeFile").addEventListener("click", () => { if (!uploading) $("#fileInput").click(); });
["dragenter", "dragover"].forEach(ev => drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.add("over"); }));
["dragleave", "drop"].forEach(ev => drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.remove("over"); }));
drop.addEventListener("drop", e => e.dataTransfer.files[0] && pickFile(e.dataTransfer.files[0]));
// dropping anywhere on the page also works (and never opens the file in the browser)
window.addEventListener("dragover", e => e.preventDefault());
window.addEventListener("drop", e => {
  e.preventDefault();
  if (e.target.closest && e.target.closest("#drop")) return;
  const f = e.dataTransfer && e.dataTransfer.files[0];
  if (f && !uploading) { $('.tab[data-tab="file"]').click(); pickFile(f); }
});

function pickFile(file) {
  const ext = (file.name.match(/\.[^.]+$/) || [""])[0].toLowerCase();
  if (![".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi", ".3gp"].includes(ext)) return toast("Please choose a video file (MP4, MOV, MKV, WEBM…).", true);
  const max = (S ? S.max_upload_mb : 1024) * 1048576;
  if (file.size > max) return toast(`That file is ${mb(file.size)} — the limit is ${mb(max)}.`, true);
  picked = file;
  const v = $("#pickedVideo");
  if (v.src) URL.revokeObjectURL(v.src);
  v.src = URL.createObjectURL(file);
  $("#pickedName").textContent = file.name;
  $("#pickedInfo").textContent = mb(file.size);
  v.onloadedmetadata = () => {
    const vertical = v.videoHeight > v.videoWidth;
    $("#pickedInfo").textContent = `${mb(file.size)} · ${mmss(v.duration)} · ${v.videoWidth}×${v.videoHeight}` +
      (vertical ? " · vertical ✓" : " · horizontal") + (v.duration > 180 ? " · over 3 min: YouTube posts it as a normal video, not a Short" : "");
    $("#fVertical").checked = !vertical && v.duration <= 180;
  };
  if (!$("#fTitle").value) $("#fTitle").value = file.name.replace(/\.[^.]+$/, "").replace(/[_-]+/g, " ").slice(0, 100);
  $("#drop").hidden = true; $("#picked").hidden = false;
  $("#upHint").textContent = "";
}
function resetFile() {
  picked = null;
  const v = $("#pickedVideo");
  if (v.src) { URL.revokeObjectURL(v.src); v.removeAttribute("src"); v.load(); }
  $("#fileInput").value = ""; $("#fTitle").value = ""; $("#fCaption").value = ""; $("#fTags").value = "";
  $("#picked").hidden = true; $("#drop").hidden = false;
}

$("#upBtn").addEventListener("click", () => {
  if (!picked || uploading) return;
  const plats = chosenPlats($('[data-plats="file"]'));
  if (!plats.length) return toast("Pick YouTube, Instagram or both.", true);
  const q = new URLSearchParams({
    name: picked.name, title: $("#fTitle").value.trim(), caption: $("#fCaption").value.trim(),
    hashtags: $("#fTags").value.trim(), platforms: plats.join(","), vertical: $("#fVertical").checked ? "1" : "0",
  });
  const xhr = new XMLHttpRequest();
  const bar = $("#upProg"), btn = $("#upBtn");
  uploading = true; btn.disabled = true; $("#changeFile").disabled = true;
  btn.innerHTML = '<span class="spin"></span>Sending to server…';
  bar.style.display = "block"; bar.firstElementChild.style.width = "0";
  xhr.upload.onprogress = e => {
    if (!e.lengthComputable) return;
    const pct = e.loaded / e.total * 100;
    bar.firstElementChild.style.width = pct + "%";
    $("#upHint").textContent = `${mb(e.loaded)} of ${mb(e.total)} (${pct.toFixed(0)}%)`;
  };
  const done = () => { uploading = false; btn.disabled = false; $("#changeFile").disabled = false; btn.textContent = "Upload & post"; bar.style.display = "none"; };
  xhr.onload = () => {
    done();
    let data = {}; try { data = JSON.parse(xhr.responseText); } catch { }
    if (xhr.status === 401) { relogin(); return toast("Logged out — reload the page and log in again.", true); }
    if (xhr.status >= 200 && xhr.status < 300) {
      toast(`Got it! Posting to ${plats.map(p => PLAT[p].name).join(" + ")} — follow it in Activity.`);
      resetFile(); refresh();
    } else { toast(data.error || `Upload failed (${xhr.status})`, true); $("#upHint").textContent = ""; }
  };
  xhr.onerror = () => { done(); toast("Upload failed — check your connection and try again.", true); $("#upHint").textContent = ""; };
  xhr.open("POST", location.origin + "/api/upload?" + q);
  xhr.setRequestHeader("Content-Type", "application/octet-stream");
  xhr.send(picked);
});
