const $ = (sel) => document.querySelector(sel);
const PAGE = 60;
const FILTER_IDS = ["q", "view", "sort", "min_price", "max_price", "min_score", "since_hours"];
const KIND_LABEL = { wanted: "Wanted post", business_ad: "Business ad", service: "Service/rental", bait_price: "Bait price", scam: "Likely scam" };
const PRESETS = ["10", "15", "30", "60", "180", "720", "1440"];

const state = { searchId: null, searches: [], offset: 0, editing: null };

function loadPrefs() {
  try {
    const saved = JSON.parse(localStorage.getItem("mkplace-filters") || "{}");
    for (const id of FILTER_IDS) {
      if (!(id in saved)) continue;
      const el = $("#" + id);
      if (el.type === "checkbox") el.checked = saved[id]; else el.value = saved[id];
    }
    document.querySelectorAll('input[name="deal"]').forEach((cb) => { cb.checked = (saved.deal || []).includes(cb.value); });
    if (saved.kind) document.querySelectorAll('input[name="kind"]').forEach((cb) => { cb.checked = saved.kind.includes(cb.value); });
    state.searchId = saved.searchId ?? null;
  } catch {}
  $("#min_score_val").textContent = $("#min_score").value;
}

function savePrefs() {
  const out = { searchId: state.searchId, deal: checked("deal"), kind: checked("kind") };
  for (const id of FILTER_IDS) { const el = $("#" + id); out[id] = el.type === "checkbox" ? el.checked : el.value; }
  try { localStorage.setItem("mkplace-filters", JSON.stringify(out)); } catch {}
}

const checked = (name) => [...document.querySelectorAll(`input[name="${name}"]:checked`)].map((c) => c.value);

async function api(path, opts = {}) {
  const res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);
  return res.status === 204 ? null : res.json();
}

function ago(iso) {
  if (!iso) return "never";
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

const every = (m) => (m % 1440 === 0 ? `${m / 1440}d` : m % 60 === 0 ? `${m / 60}h` : `${m}m`);

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

// ---------- searches ----------
async function loadSearches() {
  state.searches = await api("/api/searches");
  if (state.searchId && !state.searches.some((s) => s.id === state.searchId)) state.searchId = null;
  const list = $("#searches");
  const all = `<li class="search-item ${state.searchId ? "" : "active"}" data-id="">
      <span class="dot ok"></span><div class="body"><div class="name">All searches</div></div></li>`;
  list.innerHTML = all + state.searches.map((s) => {
    const err = s.last_status === "error";
    const pending = s.run_requested ? "queued…" : "";
    const meta = err ? esc(s.last_error) : `${pending || "Checked " + ago(s.last_run_at)} · every ${every(s.interval_minutes)}`;
    return `<li class="search-item ${state.searchId === s.id ? "active" : ""}" data-id="${s.id}">
      <span class="dot ${err ? "err" : s.enabled ? "ok" : ""}" title="${s.enabled ? "Enabled" : "Paused"}"></span>
      <div class="body">
        <div class="name">${esc(s.name)}${s.new_count ? `<span class="badge">${s.new_count}</span>` : ""}</div>
        <div class="meta ${err ? "err" : ""}">${meta}</div>
        <div class="meta">${esc(s.location)} · ${s.radius_km} km${s.max_price != null ? ` · ≤ $${s.max_price}` : ""}</div>
      </div>
      <div class="actions">
        <button class="icon-btn" data-act="run" title="Run now">↻</button>
        <button class="icon-btn" data-act="edit" title="Edit">✎</button>
      </div></li>`;
  }).join("");
  const current = state.searches.find((s) => s.id === state.searchId);
  $("#results-title").textContent = current ? current.name : "All listings";
  if (!state.searches.length) showBanner('No saved searches yet. Click <b>+ New</b> and describe what you want.');
}

$("#searches").addEventListener("click", async (e) => {
  const li = e.target.closest(".search-item");
  if (!li) return;
  const id = li.dataset.id ? Number(li.dataset.id) : null;
  const act = e.target.closest("[data-act]")?.dataset.act;
  if (act === "run") {
    await api(`/api/searches/${id}/run`, { method: "POST" });
    showBanner("Queued. The worker picks it up within about a minute.");
    return loadSearches();
  }
  if (act === "edit") return openDialog(state.searches.find((s) => s.id === id));
  state.searchId = id;
  $("#sidebar").classList.remove("open");
  savePrefs();
  loadSearches();
  loadListings();
});

// ---------- dialog ----------
const dialog = $("#search-dialog");
const form = $("#search-form");

function openDialog(search = null) {
  state.editing = search;
  form.reset();
  $("#dialog-title").textContent = search ? "Edit search" : "New search";
  $("#delete-search").hidden = !search;
  let interval = state.settings?.default_interval_minutes ?? 30;
  if (search) {
    for (const k of ["query", "keywords", "name", "min_price", "max_price", "location", "radius_km"]) {
      form.elements[k].value = search[k] ?? "";
    }
    form.elements.enabled.checked = !!search.enabled;
    interval = search.interval_minutes;
  } else {
    const last = state.searches[0];
    if (last) { form.elements.location.value = last.location; form.elements.radius_km.value = last.radius_km; }
  }
  setIntervalField(interval);
  dialog.showModal();
}

function setIntervalField(minutes) {
  const preset = PRESETS.includes(String(minutes));
  form.elements.interval_preset.value = preset ? String(minutes) : "custom";
  form.elements.interval_minutes.value = minutes;
  form.elements.interval_minutes.hidden = preset;
}

form.elements.interval_preset.addEventListener("change", (e) => {
  const custom = e.target.value === "custom";
  form.elements.interval_minutes.hidden = !custom;
  if (custom) form.elements.interval_minutes.focus();
  else form.elements.interval_minutes.value = e.target.value;
});

let parseTimer;
form.elements.query.addEventListener("input", () => {
  clearTimeout(parseTimer);
  parseTimer = setTimeout(async () => {
    const q = form.elements.query.value.trim();
    if (q.length < 3) return;
    const p = await api("/api/parse", { method: "POST", body: JSON.stringify({ query: q }) });
    form.elements.keywords.placeholder = p.keywords || "auto";
    form.elements.min_price.placeholder = p.min_price ?? "none";
    form.elements.max_price.placeholder = p.max_price ?? "none";
  }, 300);
});

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = form.elements;
  const num = (v) => (v === "" ? null : Number(v));
  const body = {
    query: f.query.value.trim(),
    keywords: f.keywords.value.trim() || null,
    name: f.name.value.trim() || null,
    min_price: num(f.min_price.value),
    max_price: num(f.max_price.value),
    location: f.location.value.trim(),
    radius_km: Number(f.radius_km.value || 40),
    interval_minutes: Math.max(5, Number(f.interval_minutes.value) || 30),
    enabled: f.enabled.checked,
  };
  try {
    const saved = state.editing
      ? await api(`/api/searches/${state.editing.id}`, { method: "PUT", body: JSON.stringify(body) })
      : await api("/api/searches", { method: "POST", body: JSON.stringify(body) });
    if (state.editing) await api(`/api/searches/${saved.id}/run`, { method: "POST" });
    dialog.close();
    state.searchId = saved.id;
    savePrefs();
    showBanner(`Saved “${esc(saved.name)}”. First results usually show up within a couple of minutes.`);
    await loadSearches();
    loadListings();
  } catch (err) {
    alert("Could not save: " + err.message);
  }
});

$("#cancel").onclick = () => dialog.close();
$("#new-search").onclick = () => openDialog();
$("#delete-search").onclick = async () => {
  if (!state.editing || !confirm(`Delete “${state.editing.name}” and its listings?`)) return;
  await api(`/api/searches/${state.editing.id}`, { method: "DELETE" });
  dialog.close();
  state.searchId = null;
  await loadSearches();
  loadListings();
};

// ---------- listings ----------
function query(offset) {
  const p = new URLSearchParams({ view: $("#view").value, sort: $("#sort").value, min_score: $("#min_score").value,
    limit: PAGE, offset });
  if (state.searchId) p.set("search_id", state.searchId);
  for (const id of ["q", "min_price", "max_price", "since_hours"]) if ($("#" + id).value) p.set(id, $("#" + id).value);
  const deals = checked("deal");
  if (deals.length) p.set("deal", deals.join(","));
  // No type ticked means show everything rather than nothing.
  const kinds = checked("kind");
  if (kinds.length) p.set("kind", kinds.join(","));
  return p;
}

function card(l) {
  const score = l.ai_score == null ? null : Math.round(l.ai_score);
  const cls = score == null ? "" : score >= 70 ? "good" : score >= 45 ? "ok" : "";
  const img = l.image_url ? `<img loading="lazy" src="${esc(l.image_url)}" alt="" referrerpolicy="no-referrer">` : "";
  return `<a class="card ${l.status === "hidden" ? "hidden-item" : ""}" href="${esc(l.url)}" target="_blank" rel="noopener" data-id="${l.id}">
    <div class="thumb">${img}
      ${score == null ? `<span class="score" title="Waiting for classification">…</span>` : `<span class="score ${cls}" title="${esc(l.ai_source)}">${score}</span>`}
      ${l.status === "new" ? `<span class="unseen">New</span>` : ""}
      ${KIND_LABEL[l.kind] ? `<span class="kind">${KIND_LABEL[l.kind]}</span>` : ""}
      <div class="card-actions">
        <button data-act="star" class="${l.status === "starred" ? "on" : ""}" title="Star">★</button>
        <button data-act="hide" title="${l.status === "hidden" ? "Unhide" : "Hide"}">${l.status === "hidden" ? "↺" : "✕"}</button>
      </div>
    </div>
    <div class="price">${esc(l.price_text || (l.price != null ? "$" + l.price : "—"))}</div>
    <div class="title">${esc(l.title)}</div>
    <div class="loc">${esc(l.location || "")} · ${ago(l.first_seen_at)}</div>
    ${l.ai_reason ? `<div class="reason">${esc(l.ai_reason)}</div>` : ""}
  </a>`;
}

async function loadListings(append = false) {
  state.offset = append ? state.offset + PAGE : 0;
  const data = await api("/api/listings?" + query(state.offset));
  const grid = $("#grid");
  const html = data.items.map(card).join("");
  if (append) grid.insertAdjacentHTML("beforeend", html);
  else grid.innerHTML = html || `<div class="empty">No listings match these filters yet.</div>`;
  $("#count").textContent = `${data.total} listing${data.total === 1 ? "" : "s"}`;
  $("#more").hidden = state.offset + PAGE >= data.total;
}

$("#grid").addEventListener("click", async (e) => {
  const a = e.target.closest(".card");
  if (!a) return;
  const act = e.target.closest("[data-act]")?.dataset.act;
  if (act) {
    e.preventDefault();
    const btn = e.target.closest("button");
    let status;
    if (act === "star") status = btn.classList.contains("on") ? "seen" : "starred";
    else status = a.classList.contains("hidden-item") ? "seen" : "hidden";
    await api(`/api/listings/${a.dataset.id}`, { method: "PATCH", body: JSON.stringify({ status }) });
    if (status === "hidden" && $("#view").value !== "all" && $("#view").value !== "hidden") a.remove();
    else loadListings();
    return;
  }
  const badge = a.querySelector(".unseen");
  if (badge) {
    badge.remove();
    api(`/api/listings/${a.dataset.id}`, { method: "PATCH", body: JSON.stringify({ status: "seen" }) });
  }
});

// ---------- status ----------
function showBanner(html, err = false) {
  const b = $("#banner");
  b.innerHTML = html;
  b.className = "banner" + (err ? " err" : "");
  b.hidden = false;
}

async function loadStatus() {
  const s = await api("/api/status");
  const pill = $("#status-pill");
  const crawl = !s.worker_alive ? "Worker offline" : s.paused ? "Crawling paused" : !s.in_active_hours ? "Outside active hours" : "Worker running";
  const spent = s.jev_cost_usd ? ` · $${s.jev_cost_usd.toFixed(s.jev_cost_usd < 1 ? 4 : 2)} spent` : "";
  pill.textContent = `${crawl} · ${s.classifier}${spent}`;
  pill.className = "pill " + (s.worker_alive && !s.paused ? "ok" : "bad");
  $("#cookie-state").textContent = s.cookies_present ? "A cookie file is installed. Paste a new export to replace it." : "No cookie file installed yet.";
  if (!s.cookies_present) showBanner("No Facebook cookies yet. Open ⚙ Settings and paste a Cookie-Editor export of facebook.com.", true);
}

// ---------- wiring ----------
let debounce;
const refresh = () => { clearTimeout(debounce); debounce = setTimeout(() => { savePrefs(); loadListings(); }, 250); };
for (const id of FILTER_IDS) $("#" + id).addEventListener("input", refresh);
document.querySelectorAll('input[name="deal"], input[name="kind"]').forEach((cb) => cb.addEventListener("change", refresh));
$("#min_score").addEventListener("input", () => { $("#min_score_val").textContent = $("#min_score").value; });
$("#more").onclick = () => loadListings(true);
$("#toggle-sidebar").onclick = () => $("#sidebar").classList.toggle("open");
$("#reset").onclick = () => {
  for (const id of ["q", "min_price", "max_price", "since_hours"]) $("#" + id).value = "";
  $("#view").value = "active"; $("#sort").value = "score"; $("#min_score").value = 0; $("#min_score_val").textContent = "0";
  document.querySelectorAll('input[name="deal"]').forEach((cb) => (cb.checked = false));
  document.querySelectorAll('input[name="kind"]').forEach((cb) => (cb.checked = cb.value === "genuine"));
  refresh();
};

// ---------- settings ----------
const settingsDialog = $("#settings-dialog");
const settingsForm = $("#settings-form");
for (const name of ["active_start_hour", "active_end_hour"]) {
  settingsForm.elements[name].innerHTML = `<option value="">any time</option>` +
    Array.from({ length: 24 }, (_, h) => `<option value="${h}">${String(h).padStart(2, "0")}:00</option>`).join("");
}

async function loadSettings() {
  state.settings = await api("/api/settings");
}

$("#open-settings").onclick = async () => {
  await loadSettings();
  const f = settingsForm.elements, s = state.settings;
  f.paused.checked = s.paused;
  f.active_start_hour.value = s.active_start_hour ?? "";
  f.active_end_hour.value = s.active_end_hour ?? "";
  f.default_interval_minutes.value = s.default_interval_minutes;
  f.cookies.value = "";
  settingsDialog.showModal();
};
$("#settings-cancel").onclick = () => settingsDialog.close();

settingsForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = settingsForm.elements;
  const hour = (v) => (v === "" ? null : Number(v));
  try {
    if (f.cookies.value.trim()) {
      await api("/api/cookies", { method: "PUT", body: f.cookies.value.trim() });
    }
    state.settings = await api("/api/settings", { method: "PUT", body: JSON.stringify({
      paused: f.paused.checked,
      active_start_hour: hour(f.active_start_hour.value),
      active_end_hour: hour(f.active_end_hour.value),
      default_interval_minutes: Number(f.default_interval_minutes.value) || 30,
    }) });
    settingsDialog.close();
    $("#banner").hidden = true;
    loadStatus();
  } catch (err) {
    alert("Could not save: " + err.message);
  }
});

loadPrefs();
loadSettings();
loadSearches().then(() => loadListings());
loadStatus();
// Pick up new results from the background worker without a manual reload.
setInterval(() => { loadSearches(); loadStatus(); if (state.offset === 0 && !document.hidden) loadListings(); }, 60000);
