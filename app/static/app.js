"use strict";
// Clip Factory dashboard: plain JS, no build step. Polls the local API; everything stays on this PC.

const $ = (sel) => document.querySelector(sel);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

async function api(path, opts = {}) {
  const init = { ...opts, headers: { "Content-Type": "application/json", ...(opts.headers || {}) } };
  if (init.body && typeof init.body !== "string") init.body = JSON.stringify(init.body);
  const res = await fetch(path, init);
  if (res.status === 204) return null;
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(typeof data.detail === "string" ? data.detail : `request failed (${res.status})`);
  return data;
}

// -- time helpers ------------------------------------------------------------------------------
const toMs = (iso) => (iso ? Date.parse(iso) : NaN);
function ago(iso) {
  const s = (Date.now() - toMs(iso)) / 1000;
  if (!isFinite(s)) return "";
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} d ago`;
}
function span(seconds) {
  const m = Math.max(0, Math.round(seconds / 60));
  return m < 60 ? `${m} min` : `${Math.floor(m / 60)} h ${m % 60} min`;
}
const clock = (iso) => (iso ? new Date(iso).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "");
const num = (n) => Number(n ?? 0).toLocaleString();

// -- state ------------------------------------------------------------------------------------------
const state = { overview: null, clips: [], filter: "all", clipSig: "", channelSig: "", vodSig: "", openClip: null };

const STATUS_LABEL = {
  pending: "Waiting for footage", cutting: "Getting footage", cut: "Waiting for transcript", captioning: "Transcribing",
  transcribed: "Waiting for AI review", captioned: "Waiting for AI review", evaluating: "AI reviewing",
  passed: "Passed · waiting to render", rendering: "Rendering the Short", approved: "Approved", rejected: "Rejected",
  published: "Published", failed: "Failed", on_hold: "On hold",
};
const FILTERS = [
  ["all", "All", () => true],
  ["needs", "Needs you", (c) => c.status === "on_hold" || c.status === "failed"],
  ["working", "In progress", (c) => ["pending", "cutting", "cut", "captioning", "transcribed", "captioned", "evaluating", "passed", "rendering"].includes(c.status)],
  ["approved", "Approved", (c) => c.status === "approved"],
  ["published", "Published", (c) => c.status === "published"],
  ["rejected", "Rejected", (c) => c.status === "rejected"],
];
const CRITERIA = { excitement: "Excitement", reaction: "Reaction", gameplay: "Gameplay",
  standalone: "Makes sense alone", visual_quality: "Picture quality", clean_start_end: "Clean start & end" };

// -- masthead gauges -------------------------------------------------------------------------------
function renderGauges(o) {
  const g = [];
  const live = o.channels.filter((c) => c.session?.status === "live").length;
  g.push([live ? "good" : "", `<b>${live}</b> live now`]);
  const q = o.system;
  const qPct = q.quota_used / q.quota_budget;
  g.push([q.api_key_configured ? (qPct > 0.8 ? "warn" : "") : "bad",
    q.api_key_configured ? `API quota <b>${num(q.quota_used)}</b> / ${num(q.quota_budget)}` : "No YouTube API key in .env"]);
  g.push([q.disk_free_gb < q.min_free_gb ? "bad" : "", `Disk <b>${q.disk_free_gb} GB</b> free`]);
  const yt = o.publishing.youtube;
  if (!yt.signed_in) g.push(["bad", "YouTube not connected"]);
  else g.push([yt.renew_soon ? "warn" : "", `YouTube <b>${esc(yt.channel || "connected")}</b>${yt.expires_around ? ` · sign in again by ${clock(yt.expires_around)}` : ""}`]);
  const p = o.publishing;
  g.push([p.enabled ? "good" : "", p.enabled ? `Uploads on · ${esc(p.privacy)} · <b>${p.published_today}</b>/${p.max_per_day} today` : "Uploads off"]);
  $("#gauges").innerHTML = g.map(([cls, html]) => `<li class="gauge ${cls}">${html}</li>`).join("");
  if (yt.channel) $("#brand-sub").textContent = `GTA VI livestreams → Shorts on ${yt.channel}`;
}

// -- on air: seismograph strips ---------------------------------------------------------------
const TRACE_SECONDS = 30 * 60;

function median(xs) {
  if (!xs.length) return 0;
  const s = [...xs].sort((a, b) => a - b);
  const m = s.length >> 1;
  return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2;
}

function traceSvg(card, now, width) {
  const { bucket_seconds: bs, points } = card.trace;
  const W = Math.max(240, width), H = 78, top = 16, bottom = 60;
  const end = Math.floor(now / bs) * bs + bs, start = end - TRACE_SECONDS;
  const x = (t) => ((t - start) / TRACE_SECONDS) * W;
  const peak = Math.max(10, ...points.map((p) => p[1]));
  const y = (v) => bottom - (v / peak) * (bottom - top);
  const bw = Math.max(1, (W * bs) / TRACE_SECONDS - 1);
  const hot = (t) => card.spikes.some((k) => t >= k.start_bucket && t <= k.end_bucket);
  const parts = [];
  for (let m = 0; m <= 30; m += 5) {
    const tx = x(end - m * 60);
    parts.push(`<line class="axis" x1="${tx}" x2="${tx}" y1="${bottom}" y2="${bottom + 4}"/>`,
      `<text class="tick" x="${tx}" y="${H - 2}" text-anchor="${m === 0 ? "end" : m === 30 ? "start" : "middle"}">${m === 0 ? "now" : `−${m}m`}</text>`);
  }
  parts.push(`<line class="axis" x1="0" x2="${W}" y1="${bottom}" y2="${bottom}"/>`);
  for (const k of card.spikes) {
    const x0 = Math.max(0, x(k.start_bucket)), x1 = Math.min(W, x(k.end_bucket + bs));
    if (x1 > 0) parts.push(`<rect class="band-spike" x="${x0}" y="${top}" width="${Math.max(2, x1 - x0)}" height="${bottom - top}"/>`);
    const mx = x(k.event_utc);
    if (mx >= 0) {
      const label = k.clip_id ? `clip ${k.clip_id}` : k.status === "open" ? "spike…" : "spike";
      parts.push(`<line class="moment" x1="${mx}" x2="${mx}" y1="${top - 4}" y2="${bottom}"/>`,
        `<text class="flag" x="${mx + 3}" y="${top - 6}">${label}</text>`);
    }
  }
  const history = [];
  let base = "";
  for (const [t, authors] of points) {
    const bx = x(t);
    if (bx < 0) continue;
    parts.push(`<rect class="bar${hot(t) ? " hot" : ""}" x="${bx}" y="${y(authors)}" width="${bw}" height="${bottom - y(authors)}"/>`);
    if (history.length >= 6) base += `${base ? "L" : "M"}${bx + bw / 2},${y(median(history.slice(-30)))}`;
    history.push(authors);
  }
  if (base) parts.push(`<path class="base" d="${base}"/>`);
  return `<svg class="trace" viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" role="img"
    aria-label="Chat activity, last 30 minutes: distinct chatters per ${bs} seconds; highlighted bursts are spikes">${parts.join("")}</svg>`;
}

function stripHtml(c, o, width) {
  const s = c.session;
  const live = s?.status === "live";
  let tally = "off", stateHtml;
  if (!c.enabled) stateHtml = "Not watched";
  else if (live && s.recording) { tally = "rec"; stateHtml = `<span class="on">Recording</span> · live ${span((Date.now() - toMs(s.started_at)) / 1000)}`; }
  else if (live) { tally = "live"; stateHtml = `<span class="on">Live</span> · ${c.record_enabled ? "recorder starting…" : "recording off"}`; }
  else if (s) stateHtml = `Offline · last live ${ago(s.ended_at || s.started_at)}`;
  else stateHtml = "Offline · not seen live yet";

  let right;
  if (live && c.trace && c.trace.points.length) {
    const pts = c.trace.points;
    const perMin = Math.round(pts.slice(-6).reduce((a, p) => a + p[2], 0) * (60 / (6 * c.trace.bucket_seconds)));
    const cam = s.facecam_method === "none" ? "no facecam found (blur layout)" : s.facecam_rect ? "facecam found" : "looking for facecam…";
    right = `${traceSvg(c, o.now, width)}
      <div class="trace-meta"><span><b>${pts[pts.length - 1][1]}</b> chatters in the last ${c.trace.bucket_seconds}s</span>
      <span><b>${num(perMin)}</b> messages/min</span><span>chat via ${esc(s.chat_source || "—")}</span>
      <span>${cam}</span><span><b>${s.clips}</b> clips this stream</span></div>`;
  } else if (live) {
    right = `<div class="flatline"><span>${s.chat_source ? `Listening to chat via ${esc(s.chat_source)}…` : "Waiting for chat…"}</span></div>`;
  } else {
    right = `<div class="flatline"><span>${s ? `${esc(s.title || "")}${s.clips ? ` · ${s.clips} clips` : ""}` : ""}</span></div>`;
  }
  return `<article class="strip ${live ? "live" : ""}">
    <div class="who"><span class="tally ${tally}" aria-hidden="true"></span>
      <span class="who-name" title="${esc(c.title)}">${esc(c.title || c.handle || c.channel_id)}</span>
      <span class="who-state">${stateHtml}</span></div>
    <div class="trace-box">${right}</div></article>`;
}

function renderStrips(o) {
  const box = $("#strips");
  if (!o.channels.length) { box.innerHTML = `<p class="empty">No channels yet. Add one in Channels below.</p>`; return; }
  const live = (c) => (c.session?.status === "live" ? 0 : 1);
  const sorted = [...o.channels].sort((a, b) => live(a) - live(b));
  const width = Math.round((box.clientWidth || 900) - (box.clientWidth > 700 ? 280 : 40));
  box.innerHTML = sorted.map((c) => stripHtml(c, o, width)).join("");
  const checked = o.channels.map((c) => c.last_checked_at).filter(Boolean).sort().pop();
  $("#poll-note").textContent = `Every ${o.system.poll_seconds}s. Last check ${checked ? ago(checked) : "not yet"}.`;
}

// -- clips -------------------------------------------------------------------------------------------
function clipLabel(c) {
  if (c.status === "approved" && !c.permission_confirmed) return ["blocked", "Approved · needs creator permission"];
  if (c.status === "approved" && c.publish_status === "queued") return ["approved", "Approved · in upload queue"];
  return [c.status, STATUS_LABEL[c.status] || c.status];
}
// The finished Short when there is one; otherwise the raw 16:9 footage, started at the clip's moment (clips are
// only edited once the AI passes them, so rejected or waiting clips have no render of their own).
function videoUrl(c, forThumb = false) {
  if (c.final_path || c.vertical_path) {
    const url = c.final_path ? `/clips/${c.id}/final.mp4` : `/clips/${c.id}/vertical.mp4`;
    return forThumb ? `${url}#t=2` : url;
  }
  if (!c.source_path || c.source_start_pts == null) return null;
  const t0 = Math.max(0, c.window_start_pts - c.source_start_pts), t1 = t0 + (c.window_end_pts - c.window_start_pts);
  return forThumb ? `/clips/${c.id}/source.mp4#t=${(t0 + 2).toFixed(1)}` : `/clips/${c.id}/source.mp4#t=${t0.toFixed(1)},${t1.toFixed(1)}`;
}
const isRawPreview = (c) => !c.final_path && !c.vertical_path && !!c.source_path;

function renderFilters() {
  $("#clip-filters").innerHTML = FILTERS.map(([key, label, fn]) => {
    const n = state.clips.filter(fn).length;
    return `<button class="chip" role="tab" type="button" data-filter="${key}" aria-selected="${state.filter === key}">${label}<span class="n">${n}</span></button>`;
  }).join("");
}

function renderClips() {
  renderFilters();
  const fn = FILTERS.find((f) => f[0] === state.filter)[2];
  const clips = state.clips.filter(fn);
  const sig = state.filter + JSON.stringify(clips.map((c) => [c.id, c.status, c.score, c.final_path, c.publish_status]));
  if (sig === state.clipSig) return;          // don't reload <video> elements when nothing changed
  state.clipSig = sig;
  const grid = $("#clip-grid");
  if (!clips.length) {
    grid.innerHTML = `<p class="empty">${state.filter === "all" ? "No clips yet. They appear here when chat spikes during a recorded stream or in a past stream's chat replay." : "Nothing here right now."}</p>`;
    return;
  }
  grid.innerHTML = clips.map((c) => {
    const url = videoUrl(c, true);
    const [cls, label] = clipLabel(c);
    return `<button class="card" type="button" data-clip="${c.id}" aria-label="Clip ${c.id} from ${esc(c.channel_title)}: ${esc(label)}">
      <div class="thumb">${url ? `<video muted loop playsinline preload="metadata" src="${url}"></video>` : `<div class="none">${c.files_deleted_at ? (c.status === "published" ? "On YouTube<br>local copy deleted" : "Files deleted") : esc(STATUS_LABEL[c.status] || c.status)}</div>`}
        ${c.score != null ? `<span class="score">${c.score.toFixed(1)}</span>` : ""}</div>
      <div class="card-body"><span class="card-who">${esc(c.channel_title || "Unknown channel")}</span>
        <span class="card-when">Clip ${c.id} · ${c.source_kind === "vod" ? "past stream · " : ""}${clock(c.created_at)}</span>
        <span class="status ${cls}">${esc(label)}</span></div></button>`;
  }).join("");
}

async function refreshClips() {
  try {
    state.clips = await api("/api/clips?limit=120");
    renderClips();
    if (state.openClip) {
      const fresh = state.clips.find((c) => c.id === state.openClip.id);
      if (fresh && fresh.status !== state.openClip.status) openClip(fresh.id, true);
    }
  } catch (e) { console.warn(e); }
}

// -- clip drawer -------------------------------------------------------------------------------------
function streamLink(c) {
  const offset = Math.max(0, Math.floor(c.window_start_utc - toMs(c.actual_start_at) / 1000));
  return `https://www.youtube.com/watch?v=${encodeURIComponent(c.video_id)}${isFinite(offset) ? `&t=${offset}s` : ""}`;
}

function actionButtons(c) {
  const b = [];
  const busy = ["cutting", "captioning", "evaluating", "rendering"].includes(c.status);
  if (!busy && c.status !== "published") {
    // approving a clip the AI rejected renders it first (the edit only happens for clips that pass)
    if (["rejected", "on_hold", "failed", "transcribed", "captioned"].includes(c.status) && (c.final_path || c.source_path))
      b.push(`<button class="btn go" data-act="approve">Approve</button>`);
    if (c.status !== "rejected") b.push(`<button class="btn stop" data-act="reject">Reject</button>`);
    if (["on_hold", "failed"].includes(c.status)) b.push(`<button class="btn" data-act="requeue">Resume</button>`);
    else if (["approved", "rejected"].includes(c.status)) b.push(`<button class="btn ghost" data-act="requeue">${c.files_deleted_at ? "Fetch again and review" : "Ask the AI again"}</button>`);
    if (c.final_path) b.push(`<button class="btn ghost" data-act="rerender">Re-render captions</button>`);
  }
  b.push(`<button class="btn ghost" data-act="suggest">Suggest a title</button>`);
  if (c.remote_url) b.push(`<a class="btn ghost" href="${esc(c.remote_url)}" target="_blank" rel="noopener">Watch on YouTube</a>`);
  b.push(`<a class="btn ghost" href="${esc(streamLink(c))}" target="_blank" rel="noopener">Open moment in stream</a>`);
  return b.join("");
}

async function openClip(id, keepVideo = false) {
  const c = state.clips.find((x) => x.id === id);
  if (!c) return;
  state.openClip = c;
  const dlg = $("#clip-dialog");
  const [cls, label] = clipLabel(c);
  $("#d-eyebrow").innerHTML = `<span class="status ${cls}">${esc(label)}</span> &nbsp;Clip ${c.id} · ${c.source_kind === "vod" ? "from a past stream · " : ""}${clock(c.created_at)}${isRawPreview(c) ? " · raw footage (the 9:16 edit is made once the clip passes)" : ""}`;
  $("#d-title").textContent = c.channel_title || "Clip";
  const llm = c.llm_result ? JSON.parse(c.llm_result) : null;
  $("#d-summary").textContent = llm?.summary || (c.error ? c.error : "");
  $("#d-actions").innerHTML = actionButtons(c);
  // A note for the AI: for clips it has judged (or that failed), not while busy or once on YouTube
  const noteBox = $("#d-note-box");
  noteBox.hidden = !["rejected", "approved", "on_hold", "failed"].includes(c.status);
  if (!keepVideo || document.activeElement !== $("#d-note")) $("#d-note").value = c.review_note || "";
  $("#d-msg").textContent = "";
  const url = videoUrl(c);
  const v = $("#d-video");
  if (!keepVideo || v.dataset.src !== url) { v.dataset.src = url || ""; if (url) v.src = url; else v.removeAttribute("src"); }
  $("#d-scores").innerHTML = ""; $("#d-meta").innerHTML = ""; $("#d-transcript").textContent = "…";
  const chat = c.chat_sample ? JSON.parse(c.chat_sample) : [];
  $("#d-chat").innerHTML = chat.length ? chat.map((m) => `<li><span class="a">+${m.t}s ${esc(m.a)}</span>${esc(m.m)}</li>`).join("")
    : `<li class="muted">No chat captured for this clip.</li>`;
  if (!dlg.open) dlg.showModal();

  const [ev, tr] = await Promise.all([api(`/api/clips/${id}/evaluation`).catch(() => null), api(`/api/clips/${id}/transcript`).catch(() => null)]);
  if (state.openClip?.id !== id) return;
  $("#d-transcript").textContent = tr?.text?.trim() || "No transcript yet.";
  if (ev) {
    const scores = ev.llm_result?.scores || {};
    const bars = Object.entries(CRITERIA).filter(([k]) => k in scores).map(([k, name]) =>
      `<div class="bar-row"><span>${name}</span><div class="meter"><span style="width:${scores[k] * 10}%"></span></div><b>${scores[k]}</b></div>`).join("");
    $("#d-scores").innerHTML = `<h3>AI review · ${esc(ev.decision)} at ${ev.final_score.toFixed(2)}</h3>
      ${bars ? `<div class="bars">${bars}</div>` : `<p class="muted">The vision model didn't run (a hard check failed first).</p>`}
      ${ev.reasons?.length ? `<ul class="reasons">${ev.reasons.map((r) => `<li>${esc(r)}</li>`).join("")}</ul>` : ""}`;
    const sig = ev.signals || {};
    $("#d-meta").innerHTML = `<h3>Signals</h3><dl class="kv">
      <dt>Stream</dt><dd>${esc(c.title || "")}</dd>
      <dt>Length</dt><dd>${(c.window_end_pts - c.window_start_pts).toFixed(0)} s · ${esc(c.layout || "")} layout</dd>
      <dt>Chat spike</dt><dd>${sig.spike_ratio ? `${sig.spike_ratio.toFixed(1)}× normal (z ${sig.spike_z.toFixed(1)})` : "—"}</dd>
      <dt>Scores</dt><dd>AI ${ev.llm_score ?? "—"} · measured ${ev.objective_score.toFixed(2)} · ${esc(ev.llm_model || "no model")}</dd>
      <dt>Loudness</dt><dd>${sig.media ? `${sig.media.mean_volume_db} dB mean · ${sig.media.silent_seconds}s silent · ${sig.media.black_seconds}s black` : "—"}</dd></dl>`;
  } else {
    $("#d-meta").innerHTML = `<h3>Details</h3><dl class="kv"><dt>Stream</dt><dd>${esc(c.title || "")}</dd>
      <dt>Length</dt><dd>${(c.window_end_pts - c.window_start_pts).toFixed(0)} s</dd></dl>`;
  }
}

async function clipAct(act, btn) {
  const c = state.openClip;
  const msg = $("#d-msg");
  msg.className = "form-msg";
  btn.disabled = true;
  try {
    if (act === "suggest") {
      msg.textContent = "Writing a title with the local model (can take a minute)…";
      const md = await api(`/api/clips/${c.id}/metadata/preview`, { method: "POST" });
      const alts = (md.candidates || []).map((x) => `${x.score.toFixed(1).padStart(5)}  ${esc(x.title)}`).join("\n");
      msg.innerHTML = `<div class="md-preview"><b>${esc(md.title)}</b>\n\n${esc(md.description)}\n\n${esc(md.tags.join(", "))}${alts ? `\n\nAll titles considered (score):\n${alts}` : ""}</div>
        <span class="muted">Preview only (${esc(md.source)}): nothing was saved or uploaded.</span>`;
    } else if (act === "renote") {
      const note = $("#d-note").value.trim();
      if (!note) { msg.className = "form-msg bad"; msg.textContent = "Write what's on screen first."; return; }
      await api(`/api/clips/${c.id}/action`, { method: "POST", body: { action: "requeue", note } });
      await refreshClips();
      openClip(c.id, true);
      $("#d-msg").textContent = "Sent back to the AI with your note. The new verdict shows here in a few minutes.";
    } else if (act === "rerender") {
      msg.textContent = "Re-rendering…";
      await api(`/api/clips/${c.id}/rerender`, { method: "POST" });
      msg.textContent = "Re-rendered with the current layout and caption settings.";
      $("#d-video").src = `/clips/${c.id}/final.mp4?v=${Date.now()}`;
    } else {
      const row = await api(`/api/clips/${c.id}/action`, { method: "POST", body: { action: act } });
      msg.textContent = { approve: row.status === "passed" ? "Approved. Rendering the Short now." : "Approved.",
        reject: "Rejected.", requeue: "Back in the pipeline." }[act];
      await refreshClips();
      openClip(row.id, true);
      $("#d-msg").textContent = msg.textContent;
    }
  } catch (e) { msg.className = "form-msg bad"; msg.textContent = e.message; }
  finally { btn.disabled = false; }
}

// -- publishing --------------------------------------------------------------------------------
async function refreshPublish() {
  try {
    const [p, hist] = await Promise.all([api("/api/publish"), api("/api/publish/history?limit=30")]);
    const notes = [];
    if (!p.enabled) notes.push("Uploads are off. Set PUBLISH_ENABLED=true in .env to upload automatically.");
    else if (p.waiting_because) notes.push(`Waiting: ${p.waiting_because}.`);
    if (p.privacy === "private") notes.push("Uploads go up as private.");
    const locked = p.items.filter((i) => i.privacy_actual === "private" && i.privacy !== "private").length;
    if (locked) notes.push(`YouTube locked ${locked} upload(s) to private: the Google project needs YouTube's API audit (see docs/setup-youtube-upload.md). Until then, make them public in YouTube Studio.`);
    if (p.blocked_no_permission.length) notes.push(`${p.blocked_no_permission.length} approved clip(s) can't be uploaded until the creator gives permission.`);
    $("#publish-note").textContent = notes.join(" ");
    const upcoming = p.items.filter((i) => ["queued", "uploading", "failed"].includes(i.status));
    $("#queue").innerHTML = upcoming.length ? upcoming.map((i) => `<li><div class="row-top">
        <span class="row-title">${esc(i.title || "Title is written right before upload")}</span>
        <span class="status ${i.status === "failed" ? "failed" : ""}">${esc(i.status)}</span></div>
        <div class="row-sub">clip ${i.clip_id} · score ${i.score?.toFixed(1) ?? "—"}${i.next_attempt_at ? ` · next try ${clock(i.next_attempt_at)}` : ""}${i.error ? ` · ${esc(i.error)}` : ""}</div>
        <button class="btn ghost" data-cancel="${i.id}" type="button" style="margin-top:.4rem">Remove from queue</button></li>`).join("")
      : `<li class="muted">Nothing waiting to upload.</li>`;
    $("#history").innerHTML = hist.length ? hist.map((a) => `<li><div class="row-top">
        <span class="row-title">${a.remote_url && a.outcome === "published" ? `<a href="${esc(a.remote_url)}" target="_blank" rel="noopener">${esc(a.title || "Untitled")}</a>` : esc(a.title || "Untitled")}</span>
        <span class="status ${a.outcome === "published" ? "published" : a.outcome ? "failed" : ""}">${esc(a.outcome || "uploading")}</span></div>
        <div class="row-sub">clip ${a.clip_id} · ${clock(a.started_at)}${a.error ? ` · ${esc(a.error)}` : ""}</div></li>`).join("")
      : `<li class="muted">No uploads yet.</li>`;
  } catch (e) { console.warn(e); }
}

// -- past streams ------------------------------------------------------------------------------
const VOD_LABEL = { queued: "Waiting its turn", analyzing: "Reading chat replay", clipping: "Cutting clips",
  done: "Done", waiting_chat: "Chat replay not ready", no_chat: "No chat replay", skipped: "Skipped", failed: "Failed" };
const VOD_CLASS = { done: "published", failed: "failed", no_chat: "failed", waiting_chat: "on_hold" };

function vodDetail(j) {
  const bits = [esc(j.channel_title || "Unknown channel")];
  if (j.duration_s) bits.push(span(j.duration_s));
  if (j.ended_at) bits.push(`ended ${ago(j.ended_at)}`);
  if (j.chat_messages != null) bits.push(`${num(j.chat_messages)} chat messages · ${j.spikes ?? 0} spikes`);
  if (j.status === "clipping") bits.push(`${j.clips_cut} of ${j.clips} clips cut`);
  else if (j.status === "done") bits.push(j.clips ? `${j.clips} clips` : "no moment stood out");
  if (j.status === "waiting_chat" && j.next_attempt_at) bits.push(`next try ${clock(j.next_attempt_at)}`);
  if (j.error) bits.push(esc(j.error));
  return bits.join(" · ");
}

function renderVod(v) {
  const notes = [];
  if (!v.enabled) notes.push("Off. Set VOD_ENABLED=true in .env to clip uploaded streams.");
  else {
    if (v.paused_because) notes.push(`Paused: ${v.paused_because}.`);
    notes.push(`The ${v.max_clips} biggest chat moments of each finished GTA 6 stream from the last ${v.max_age_hours} h, newest first, while nobody is live. Checked every ${v.scan_minutes} min.`);
    if (v.skipped) notes.push(`${v.skipped} older or off-topic streams skipped.`);
  }
  $("#vod-note").textContent = notes.join(" ");
  const sig = JSON.stringify(v.jobs);
  const list = $("#vod-jobs");
  if (sig === state.vodSig || list.contains(document.activeElement)) return;
  state.vodSig = sig;
  list.innerHTML = v.jobs.length ? v.jobs.map((j) => {
    const acts = [];
    if (["failed", "no_chat", "skipped", "waiting_chat"].includes(j.status)) acts.push(`<button class="btn ghost" type="button" data-vod="${j.id}" data-vod-act="retry">Try again</button>`);
    if (["queued", "waiting_chat"].includes(j.status)) acts.push(`<button class="btn ghost" type="button" data-vod="${j.id}" data-vod-act="skip">Skip</button>`);
    return `<li><div class="row-top">
        <span class="row-title"><a href="https://www.youtube.com/watch?v=${encodeURIComponent(j.video_id)}" target="_blank" rel="noopener">${esc(j.title || j.video_id)}</a></span>
        <span class="status ${VOD_CLASS[j.status] || ""}">${esc(VOD_LABEL[j.status] || j.status)}</span></div>
      <div class="row-sub">${vodDetail(j)}</div>
      ${acts.length ? `<div class="row-acts">${acts.join("")}</div>` : ""}</li>`;
  }).join("") : `<li class="muted">No past streams yet. The first check runs half a minute after the app starts.</li>`;
}

// -- channels -----------------------------------------------------------------------------------
function renderChannels(o) {
  const sig = JSON.stringify(o.channels.map((c) => [c.channel_id, c.enabled, c.record_enabled, c.vod_enabled, c.permission_confirmed, c.layout, c.chat_delay_auto, c.chat_delay_seconds]));
  const body = $("#channel-rows");
  if (sig === state.channelSig || body.contains(document.activeElement)) return;
  state.channelSig = sig;
  const sw = (c, field, label) => `<input type="checkbox" class="switch" data-field="${field}" data-ch="${esc(c.channel_id)}" ${c[field] ? "checked" : ""} aria-label="${label} for ${esc(c.title)}">`;
  body.innerHTML = o.channels.map((c) => `<tr>
    <td><div class="ch-name">${esc(c.title)}</div><div class="ch-handle">${esc(c.handle || c.channel_id)}</div></td>
    <td>${sw(c, "enabled", "Watch")}</td><td>${sw(c, "record_enabled", "Record and clip")}</td>
    <td>${sw(c, "vod_enabled", "Clip past streams")}</td>
    <td>${sw(c, "permission_confirmed", "Upload allowed")}</td>
    <td><select data-field="layout" data-ch="${esc(c.channel_id)}" aria-label="Layout for ${esc(c.title)}">
      ${[["auto", "Auto (find facecam)"], ["split", "Camera on top"], ["blur", "Full frame"]].map(([v, l]) => `<option value="${v}" ${c.layout === v ? "selected" : ""}>${l}</option>`).join("")}</select></td>
    <td>${sw(c, "chat_delay_auto", "Automatic chat delay")} <span class="muted">auto</span>
      <input type="number" min="0" max="120" step="1" data-field="chat_delay_seconds" data-ch="${esc(c.channel_id)}" value="${c.chat_delay_seconds}" ${c.chat_delay_auto ? "disabled" : ""} aria-label="Chat delay in seconds"> s</td>
    <td><button class="btn ghost" type="button" data-remove="${esc(c.channel_id)}">Remove</button></td></tr>`).join("");
}

async function patchChannel(el) {
  const id = el.dataset.ch, field = el.dataset.field;
  let value = el.type === "checkbox" ? el.checked : el.type === "number" ? Number(el.value) : el.value;
  if (field === "permission_confirmed" && value &&
      !confirm("Only turn this on if the creator has agreed to have clips of their streams uploaded. Continue?")) {
    el.checked = false; return;
  }
  try { await api(`/api/channels/${encodeURIComponent(id)}`, { method: "PATCH", body: { [field]: value } }); }
  catch (e) { alert(e.message); }
  state.channelSig = "";
  el.blur();
  refreshOverview();
}

// -- system -------------------------------------------------------------------------------------------
function renderSystem(o) {
  const s = o.system;
  const used = s.recordings_gb, free = s.disk_free_gb;
  $("#storage").innerHTML = `<div class="meter ${free < s.min_free_gb ? "bad" : ""}"><span style="width:${Math.min(100, (used / Math.max(0.01, used + free)) * 100)}%"></span></div>
    <p class="storage-line">Recordings: <b>${used} GB</b> · clips: <b>${s.clips_gb} GB</b> · free on disk: <b>${free} GB</b>. New recordings pause below ${s.min_free_gb} GB free.
    Live footage keeps a rolling ${s.buffer_minutes}-minute buffer and is deleted after a stream ends.
    ${s.purge_after_publish ? `A clip's local files are deleted ${s.purge_grace_minutes ? `${s.purge_grace_minutes} min after` : "as soon as"} it's posted.` : "Posted clips are kept on this PC."}</p>`;
  $("#errors").innerHTML = o.errors.length ? o.errors.map((e) => `<li><div class="row-top"><span class="row-title">${esc(e.message)}</span>
      <span class="row-sub">${ago(e.at)}</span></div><div class="row-sub">${esc(e.component)}${e.details ? ` · ${esc(e.details)}` : ""}</div></li>`).join("")
    : `<li class="muted">No problems recorded.</li>`;
}

async function refreshLog() {
  if (!$("#log-box").open) return;
  try {
    const { lines } = await api("/api/logs?lines=200");
    const pre = $("#log");
    const atBottom = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 20;
    pre.innerHTML = lines.map((l) => { const m = l.match(/ (ERROR|WARNING) /); return m ? `<span class="lvl-${m[1]}">${esc(l)}</span>` : esc(l); }).join("\n");
    if (atBottom) pre.scrollTop = pre.scrollHeight;
  } catch (e) { console.warn(e); }
}

// -- overview loop -----------------------------------------------------------------------------
async function refreshOverview() {
  try {
    const o = await api("/api/overview");
    state.overview = o;
    renderGauges(o); renderStrips(o); renderVod(o.vod); renderChannels(o); renderSystem(o);
  } catch (e) {
    $("#gauges").innerHTML = `<li class="gauge bad">Can't reach the app: ${esc(e.message)}</li>`;
  }
}

// -- events ---------------------------------------------------------------------------------------------
document.addEventListener("click", async (ev) => {
  const t = ev.target.closest("button, a");
  if (!t) return;
  if (t.dataset.filter) { state.filter = t.dataset.filter; state.clipSig = ""; renderClips(); }
  else if (t.dataset.clip) openClip(Number(t.dataset.clip));
  else if (t.dataset.act) clipAct(t.dataset.act, t);
  else if (t.id === "d-close") $("#clip-dialog").close();
  else if (t.dataset.cancel) {
    try { await api(`/api/publish/${t.dataset.cancel}/cancel`, { method: "POST" }); } catch (e) { alert(e.message); }
    refreshPublish();
  } else if (t.dataset.remove) {
    if (!confirm("Remove this channel? Clips already made are kept.")) return;
    try { await api(`/api/channels/${encodeURIComponent(t.dataset.remove)}`, { method: "DELETE" }); } catch (e) { alert(e.message); }
    state.channelSig = ""; refreshOverview();
  } else if (t.id === "add-streamer") {
    $("#add-channel").scrollIntoView({ block: "center" });
    $("#channel-url").focus({ preventScroll: true });
  } else if (t.dataset.vodAct) {
    t.disabled = true;
    try { await api(`/api/vods/${t.dataset.vod}/action`, { method: "POST", body: { action: t.dataset.vodAct } }); } catch (e) { alert(e.message); }
    state.vodSig = ""; refreshOverview();
  } else if (t.id === "vod-scan") {
    t.disabled = true; t.textContent = "Looking…";
    const msg = $("#vod-msg");
    try {
      const r = await api("/api/vods/scan", { method: "POST" });
      msg.className = "form-msg";
      msg.textContent = r.new ? `Found ${r.new} new stream(s).` : "No new finished streams.";
    } catch (e) { msg.className = "form-msg bad"; msg.textContent = e.message; }
    t.disabled = false; t.textContent = "Look for new streams"; state.vodSig = ""; refreshOverview();
  } else if (t.id === "poll-now") {
    t.disabled = true; t.textContent = "Checking…";
    try { await api("/api/monitor/poll", { method: "POST" }); } catch (e) { $("#poll-note").textContent = e.message; }
    t.disabled = false; t.textContent = "Check now"; refreshOverview();
  }
});
document.addEventListener("change", (ev) => { if (ev.target.dataset.field) patchChannel(ev.target); });
$("#clip-dialog").addEventListener("close", () => { state.openClip = null; const v = $("#d-video"); v.pause(); });
$("#clip-dialog").addEventListener("click", (ev) => { if (ev.target === ev.currentTarget) ev.currentTarget.close(); });
$("#clip-grid").addEventListener("mouseover", (ev) => { const v = ev.target.closest(".card")?.querySelector("video"); if (v && v.paused) v.play().catch(() => {}); });
$("#clip-grid").addEventListener("mouseout", (ev) => {
  const card = ev.target.closest(".card");
  if (card && !card.contains(ev.relatedTarget)) card.querySelector("video")?.pause();
});
$("#log-box").addEventListener("toggle", refreshLog);
$("#add-channel").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const input = $("#channel-url"), msg = $("#add-msg"), btn = ev.target.querySelector("button");
  btn.disabled = true; msg.className = "form-msg"; msg.textContent = "Looking up the channel…";
  try {
    const c = await api("/api/channels", { method: "POST", body: { url: input.value.trim() } });
    msg.textContent = `Added ${c.title}. Their next live stream is recorded and clipped automatically; uploads stay off until you allow them.`;
    input.value = ""; state.channelSig = ""; refreshOverview();
  } catch (e) { msg.className = "form-msg bad"; msg.textContent = e.message; }
  finally { btn.disabled = false; }
});
$("#add-vod").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const input = $("#vod-url"), msg = $("#vod-msg"), btn = ev.target.querySelector("button");
  btn.disabled = true; msg.className = "form-msg"; msg.textContent = "Checking the stream…";
  try {
    const j = await api("/api/vods", { method: "POST", body: { url: input.value.trim() } });
    msg.textContent = `Added "${j.title || j.video_id}". It's next in line${state.overview?.vod?.paused_because ? " once the live stream is done" : ""}.`;
    input.value = ""; state.vodSig = ""; refreshOverview();
  } catch (e) { msg.className = "form-msg bad"; msg.textContent = e.message; }
  finally { btn.disabled = false; }
});
let resizeTimer;
window.addEventListener("resize", () => { clearTimeout(resizeTimer); resizeTimer = setTimeout(() => state.overview && renderStrips(state.overview), 150); });

refreshOverview(); refreshClips(); refreshPublish();
setInterval(refreshOverview, 5000);
setInterval(refreshClips, 10000);
setInterval(refreshPublish, 15000);
setInterval(refreshLog, 10000);
