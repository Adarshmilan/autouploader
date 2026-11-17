/* render.js — draws what the server reports: quota cards, Activity (job progress),
   the clips waiting for review (with the Post button) and the posted history. */
/* ---------- rendering ---------- */
function renderStats() {
  const yt = S.youtube, ig = S.instagram;
  const dot = $("#workerDot");
  dot.classList.toggle("busy", S.busy);
  $("#workerText").textContent = S.busy ? "Working…" : "Ready";

  if (!yt.configured) { $("#ytLeft").textContent = "Not set up"; $("#ytBar").style.width = "0"; }
  else {
    $("#ytLeft").innerHTML = `${yt.left} <small>/ ${yt.limit}</small>`;
    $("#ytBar").style.width = (yt.limit ? yt.left / yt.limit * 100 : 0) + "%";
  }

  if (!ig.configured) { $("#igLeft").textContent = "Not set up"; $("#igSub").textContent = "Add INSTAGRAM_ACCESS_TOKEN to .env"; $("#igBar").style.width = "0"; }
  else if (ig.limit) {
    const left = Math.max(0, ig.limit.total - ig.limit.used);
    $("#igLeft").innerHTML = `${left} <small>/ ${ig.limit.total}</small>`;
    $("#igSub").textContent = "Rolling 24-hour limit set by Instagram";
    $("#igBar").style.width = left / ig.limit.total * 100 + "%";
  } else { $("#igLeft").textContent = "Connected"; $("#igSub").textContent = "Limit: 50 posts per 24 h"; $("#igBar").style.width = "100%"; }

  const w = S.waiting.youtube + S.waiting.instagram;
  $("#waitCount").innerHTML = w ? `${w} <small>upload${w === 1 ? "" : "s"}</small>` : "0";
  $("#waitBar").style.width = w ? "35%" : "100%";
  $("#waitBar").parentElement.className = w ? "bar" : "bar ok";
  $("#keepNote").textContent = `Only the newest ${S.keep_videos} videos are kept on disk`;
  $("#maxMb").textContent = S.max_upload_mb;
  tick();
}

function tick() {
  if (!S) return;
  const gone = (Date.now() - fetchedAt) / 1000;
  const reset = S.youtube.reset_in - gone;
  if (S.youtube.configured) {
    $("#ytReset").textContent = (S.youtube.left ? "Cap resets in " : "Cap reached · next uploads in ") + dur(reset);
  } else $("#ytReset").textContent = "Run `splitter --login` once";
  const w = S.waiting.youtube + S.waiting.instagram;
  if (!w) $("#waitSub").textContent = "Everything approved is posted";
  else {
    const parts = [];
    if (S.waiting.youtube) parts.push(`${S.waiting.youtube} YouTube`);
    if (S.waiting.instagram) parts.push(`${S.waiting.instagram} Instagram`);
    let when;
    if (S.busy) when = "uploading now";
    else if (S.waiting.youtube && !S.youtube.left) when = `YouTube resumes in ${dur(reset)}`;
    else if (S.next_retry_in != null) when = `auto-retry in ${dur(S.next_retry_in - gone)}`;
    else when = "will retry on the next post";
    $("#waitSub").textContent = `${parts.join(" · ")} — ${when}`;
  }
}

/* jobs: keyed, so open logs stay open while it refreshes */
const jobEls = new Map();
function lastStep(log) {
  const m = [...log.matchAll(/\[(\d)\/(\d)\]\s*([^\n]*)/g)];
  return m.length ? { n: +m[m.length - 1][1], total: +m[m.length - 1][2], text: m[m.length - 1][3] } : null;
}
function lastLine(log) {
  const lines = log.split("\n").map(l => l.trim()).filter(l => l && !l.startsWith("==") && !/^Traceback|^File "/.test(l));
  return lines[lines.length - 1] || "";
}
function renderJobs() {
  const card = $("#jobsCard"), box = $("#jobs");
  card.hidden = !S.jobs.length;
  const running = S.jobs.filter(j => j.status === "running" || j.status === "queued").length;
  $("#jobsSub").textContent = running ? `${running} in progress` : "";
  const keep = new Set();
  S.jobs.forEach((j, i) => {
    keep.add(j.id);
    let el = jobEls.get(j.id);
    if (!el) {
      el = h("div", { class: "job" },
        h("div", { class: "job-top" }, h("div", { class: "job-title" }), h("span", { class: "badge" })),
        h("div", { class: "steps" }), h("div", { class: "job-msg" }),
        h("details", {}, h("summary", {}, "Log"), h("pre")));
      jobEls.set(j.id, el);
    }
    const kindIcon = { link: "🔗", file: "⬆️", post: "🚀" }[j.kind] || "";
    $(".job-title", el).textContent = `${kindIcon} ${j.label}`;
    $(".job-title", el).title = j.label;
    const badge = $(".badge", el);
    badge.className = "badge " + j.status;
    badge.textContent = j.status === "done" && j.kind === "link" ? "clips ready" : j.status;

    const labels = STEPS[j.kind] || [];
    const st = lastStep(j.log);
    const stepsEl = $(".steps", el);
    stepsEl.replaceChildren(...labels.map((label, k) => {
      const n = k + 1;
      const totalSteps = st ? st.total : labels.length;
      let cur = st ? Math.round(st.n * labels.length / totalSteps) : 0;
      if (j.kind === "post") cur = 1;
      let cls = "";
      if (j.status === "done") cls = "done";
      else if (j.status === "failed") cls = n < cur ? "done" : n === Math.max(cur, 1) ? "fail" : "";
      else if (j.status === "running") cls = n < cur ? "done" : n === Math.max(cur, 1) ? "now" : "";
      return h("div", { class: "step " + cls }, h("i"), h("b", {}, label));
    }));
    stepsEl.hidden = j.status === "queued";

    const msg = $(".job-msg", el);
    msg.classList.toggle("err", j.status === "failed");
    if (j.status === "queued") msg.textContent = "Waiting for the job before it to finish…";
    else if (j.status === "done" && j.kind === "link") msg.textContent = "Clips are ready — review them below and press Post.";
    else msg.textContent = lastLine(j.log) || "Starting…";

    const pre = $("pre", el);
    const atBottom = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 8;
    if (pre.textContent !== j.log) { pre.textContent = j.log || "…"; if (atBottom) pre.scrollTop = pre.scrollHeight; }
    if (box.children[i] !== el) box.insertBefore(el, box.children[i] || null);
  });
  for (const [id, el] of jobEls) if (!keep.has(id)) { el.remove(); jobEls.delete(id); }
}

/* review cards: created once per video and never re-rendered (your edits and the players stay put) */
const reviewEls = new Map();
function renderReviews() {
  const box = $("#reviews");
  const keep = new Set(S.reviews.map(r => r.video_id));
  for (const [id, el] of reviewEls) if (!keep.has(id)) { el.remove(); reviewEls.delete(id); }
  S.reviews.forEach((r, i) => {
    if (!reviewEls.has(r.video_id)) reviewEls.set(r.video_id, reviewCard(r));
    const el = reviewEls.get(r.video_id);
    if (box.children[i] !== el) box.insertBefore(el, box.children[i] || null);
  });
}
function reviewCard(r) {
  const plats = h("div", { class: "plats" });
  platToggles(plats, "rp-" + r.video_id);
  const postBtn = h("button", { class: "btn primary", type: "button" });
  const discardBtn = h("button", { class: "btn danger sm", type: "button" }, "Discard all");
  const grid = h("div", { class: "clips" });

  const update = () => {
    const n = grid.querySelectorAll(".clip.on").length, p = chosenPlats(plats);
    postBtn.textContent = n ? `Post ${n} clip${n === 1 ? "" : "s"} to ${p.map(x => PLAT[x].name).join(" + ") || "…"}` : "Select a clip";
    postBtn.disabled = !n || !p.length;
  };
  plats.addEventListener("change", update);

  r.clips.forEach((c, k) => {
    const cb = h("input", { type: "checkbox", checked: true });
    const card = h("div", { class: "clip on", "data-file": c.file },
      h("video", { src: c.src, controls: true, playsinline: true, preload: "metadata" }),
      h("div", { class: "body" },
        h("div", { class: "pick" }, h("label", {}, cb, `Clip ${k + 1}`), h("span", { class: "time" }, `${mmss(c.start)}–${mmss(c.end)} · ${Math.round(c.end - c.start)}s`)),
        h("label", { class: "f" }, "Title"), h("input", { class: "input", "data-k": "title", maxlength: 100, value: c.title }),
        h("label", { class: "f" }, "Caption"), h("textarea", { class: "input", "data-k": "caption", maxlength: 2000 }, c.caption),
        h("label", { class: "f" }, "Hashtags"), h("input", { class: "input", "data-k": "hashtags", value: c.hashtags.join(" ") })));
    cb.addEventListener("change", () => { card.classList.toggle("on", cb.checked); update(); });
    // only one preview plays at a time
    $("video", card).addEventListener("play", e => grid.querySelectorAll("video").forEach(v => v !== e.target && v.pause()));
    grid.append(card);
  });

  const busy = on => { postBtn.disabled = discardBtn.disabled = on; if (on) postBtn.innerHTML = '<span class="spin"></span>Posting…'; else update(); };

  postBtn.addEventListener("click", async () => {
    const clips = [...grid.querySelectorAll(".clip.on")].map(card => ({
      file: card.dataset.file,
      title: $('[data-k="title"]', card).value, caption: $('[data-k="caption"]', card).value, hashtags: $('[data-k="hashtags"]', card).value,
    }));
    if (clips.some(c => !c.title.trim())) return toast("Every selected clip needs a title.", true);
    const platforms = chosenPlats(plats);
    if (S.youtube && platforms.includes("youtube") && S.youtube.left < clips.length) {
      toast(`YouTube has ${S.youtube.left} upload(s) left today — the rest will post automatically after the reset.`);
    }
    busy(true);
    grid.querySelectorAll("video").forEach(v => v.pause());
    try {
      const res = await api("/api/post", { video_id: r.video_id, clips, platforms });
      toast(`Posting ${res.count} clip${res.count === 1 ? "" : "s"}… follow it in Activity.`);
      refresh();
    } catch (err) { toast(err.message, true); busy(false); }
  });
  discardBtn.addEventListener("click", async () => {
    if (!confirm("Delete these clips without posting them?")) return;
    busy(true);
    try { await api("/api/discard", { video_id: r.video_id }); toast("Clips discarded."); refresh(); }
    catch (err) { toast(err.message, true); busy(false); }
  });
  update();

  const src = r.url ? h("a", { href: r.url, target: "_blank", rel: "noopener" }, "source video ↗") : h("span", {}, "your upload");
  return h("section", { class: "card review" },
    h("div", { class: "card-h" },
      h("div", { style: "min-width:0" }, h("h2", { style: "overflow:hidden;text-overflow:ellipsis;white-space:nowrap" }, "Review: ", r.title),
        h("div", { class: "muted small" }, `${r.clips.length} clip${r.clips.length === 1 ? "" : "s"} · `, src, " · untick the ones you don't want, edit the text, then post")),
      discardBtn),
    h("div", { class: "card-b" }, grid),
    h("div", { class: "review-foot" }, h("div", {}, h("div", { class: "muted small", style: "margin-bottom:6px" }, "Post to"), plats), postBtn));
}

function chip(platform, st) {
  const p = PLAT[platform];
  const map = { done: "✓", pending: "⏳", error: "✗", skipped: "—", review: "…" };
  const s = st.status === "error" && (st.attempts || 0) < 3 ? "pending" : st.status;
  const label = { done: "Posted", pending: "Waiting", error: "Failed", skipped: "Skipped", review: "Review" }[s] || s;
  const inner = `${map[st.status] || ""} ${p.name}: ${label}`;
  if (st.status === "done" && st.url) return h("a", { class: "chip done", href: st.url, target: "_blank", rel: "noopener" }, inner + " ↗");
  return h("span", { class: "chip " + s, title: st.error || "" }, inner);
}
function renderHistory() {
  const box = $("#history");
  const items = S.history.filter(v => v.clips.some(c => c.youtube.status !== "skipped" || c.instagram.status !== "skipped"));
  if (!items.length) return box.replaceChildren(h("div", { class: "empty" }, "Nothing posted yet."));
  box.replaceChildren(...items.map(v => h("div", { class: "hist" },
    h("div", { class: "hist-h" },
      h("b", { title: v.title }, (v.source === "upload" ? "⬆️ " : "🔗 ") + v.title),
      h("span", { class: "muted small" }, v.url ? h("a", { href: v.url, target: "_blank", rel: "noopener" }, "source") : "", v.url ? " · " : "", ago(v.created))),
    ...v.clips.filter(c => c.youtube.status !== "skipped" || c.instagram.status !== "skipped").map(c =>
      h("div", { class: "hclip" }, h("span", { class: "t", title: c.title }, c.title),
        ...["youtube", "instagram"].filter(p => c[p].status !== "skipped" || (c[p].error || "").includes("free space")).map(p => chip(p, c[p])))))));
}
