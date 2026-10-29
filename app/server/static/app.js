// SSE-driven partial refresh (DESIGN §13): the server sends {area, fid}; every
// element with data-area="<area>" re-fetches its data-src and swaps its contents.
"use strict";

const TOKEN = document.querySelector('meta[name="planner-token"]').content;

async function post(url, body) {
  return fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Planner-Token": TOKEN },
    body: JSON.stringify(body || {}),
  });
}

// A swap must not eat what the user is doing. Refreshes the game pushes wait while focus is
// inside a form or field of the area, and run once it leaves; every swap carries over
// fields the user changed but hasn't saved, the focused field and which <details> are open.
const pending = new Set();

function editing(node) {
  const a = document.activeElement;
  return !!a && node.contains(a) && !!a.closest("form, input, select, textarea");
}

function fieldKey(el) {
  const form = el.form;
  const scope = form ? (form.dataset.apiForm || "") : "";
  const id = el.name || (el.dataset.changePost && `${el.dataset.changePost}#${el.dataset.key || ""}`) ||
    el.dataset.planSetting || el.dataset.flag ||
    el.dataset.pinEngineer || el.dataset.stepGoal || el.id;
  if (!id) return null;
  return `${scope}|${id}` + (el.type === "checkbox" || el.type === "radio" ? `=${el.value}` : "");
}

function dirty(el) {
  if (el.type === "checkbox" || el.type === "radio") return el.checked !== el.defaultChecked;
  if (el.tagName === "SELECT") {
    const def = [...el.options].findIndex((o) => o.defaultSelected);   // none marked: the first one
    return el.selectedIndex !== Math.max(def, 0);
  }
  return el.value !== el.defaultValue;
}

function detailsKeys(node) {
  const seen = {};
  return [...node.querySelectorAll("details")].map((d) => {
    const base = d.id || d.className || "details";
    seen[base] = (seen[base] || 0) + 1;
    return [`${base}#${seen[base]}`, d];
  });
}

function snapshot(node) {
  const fields = new Map();
  node.querySelectorAll("input, select, textarea").forEach((el) => {
    if (el.type === "hidden" || (el.form && el.form.dataset.saved)) return;
    const key = fieldKey(el);
    if (key && dirty(el)) fields.set(key, el.type === "checkbox" || el.type === "radio" ? el.checked : el.value);
  });
  const a = document.activeElement;
  const focus = a && node.contains(a) && a.matches("input, select, textarea") ? {
    key: fieldKey(a),
    range: (() => { try { return [a.selectionStart, a.selectionEnd]; } catch { return null; } })(),
  } : null;
  return { fields, focus, open: new Map(detailsKeys(node).map(([k, d]) => [k, d.open])) };
}

function restore(node, keep) {
  node.querySelectorAll("input, select, textarea").forEach((el) => {
    const key = fieldKey(el);
    if (!key || !keep.fields.has(key)) return;
    if (el.type === "checkbox" || el.type === "radio") el.checked = keep.fields.get(key);
    else el.value = keep.fields.get(key);
    if (el.matches("[data-kind-select]")) showKindFields(el.closest("form"));
  });
  detailsKeys(node).forEach(([k, d]) => { if (keep.open.has(k)) d.open = keep.open.get(k); });
  if (keep.focus && keep.focus.key) {
    const el = [...node.querySelectorAll("input, select, textarea")].find((x) => fieldKey(x) === keep.focus.key);
    if (el) {
      el.focus({ preventScroll: true });
      try { if (keep.focus.range && keep.focus.range[0] != null) el.setSelectionRange(...keep.focus.range); } catch { /* not a text field */ }
    }
  }
}

async function refreshNode(node, user) {
  const res = await fetch(node.dataset.src, { headers: { "Accept": "text/html" } });
  if (!res.ok) return;
  const html = await res.text();
  if (!user && editing(node)) { pending.add(node); return; }
  pending.delete(node);
  const keep = snapshot(node);
  node.innerHTML = html;
  restore(node, keep);
}

// user: the refresh follows the user's own save, so it runs even while they're in a field.
async function refreshArea(area, user = false) {
  const nodes = document.querySelectorAll(`[data-area~="${area}"][data-src]`);
  await Promise.all([...nodes].map((node) => {
    if (!user && editing(node)) { pending.add(node); return null; }
    return refreshNode(node, user);
  }));
}

document.addEventListener("focusout", () => setTimeout(() => {
  for (const node of [...pending]) if (node.isConnected && !editing(node)) refreshNode(node, false);
}, 0));

async function refreshPill() {
  const pill = document.getElementById("import-pill");
  if (!pill) return;
  const res = await fetch("/api/import/status");
  if (!res.ok) return;
  const st = await res.json();
  pill.classList.toggle("pill-live", st.state === "running");
  pill.textContent =
    st.state === "running" ? `Importing ${st.files_done}/${st.files_total}` :
    st.state === "error" ? "Import error" : "Journals up to date";
  return st;
}

let wasRunning = false;

function connect() {
  const source = new EventSource("/events/stream");
  source.onmessage = async (msg) => {
    let data;
    try { data = JSON.parse(msg.data); } catch { return; }
    if (data.area === "display") { await syncDisplay(); return; }
    const reloadAreas = (document.body.dataset.reloadAreas || "").split(/\s+/).filter(Boolean);
    if (reloadAreas.includes(data.area)) { location.reload(); return; }
    await refreshArea(data.area);
    if (data.area === "import") {
      const st = await refreshPill();
      // A finished import changes counts all over the page: reload once.
      if (wasRunning && st && st.state === "done" && st.events_added > 0) location.reload();
      wasRunning = !!st && st.state === "running";
    }
  };
}

document.addEventListener("click", async (e) => {
  const btn = e.target.closest('[data-action="rescan"]');
  if (!btn) return;
  btn.disabled = true;
  const res = await post("/api/rescan");
  if (!res.ok && res.status !== 409) btn.disabled = false;
});

document.addEventListener("click", async (e) => {
  const btn = e.target.closest('[data-action="rebuild"]');
  if (!btn) return;
  btn.disabled = true;
  const out = document.getElementById("rebuild-result");
  const res = await post("/api/rebuild");
  if (res.ok && out) {
    const c = await res.json();
    out.textContent = `Rebuilt from ${c.events} events: ${c.snapshots} snapshot writes, ${c.unlocks} unlocks.`;
  } else if (out) {
    out.textContent = "Rebuild failed; see the log.";
  }
  btn.disabled = false;
});

document.addEventListener("change", async (e) => {
  const select = e.target.closest('[data-action="select-commander"]');
  if (!select) return;
  const res = await post("/api/commanders/select", { key: select.value });
  if (res.ok) location.reload();
});

refreshPill().then((st) => { wasRunning = !!st && st.state === "running"; });
connect();

// -- Goals (Phase 5a): generic POST buttons, JSON forms, flags and plan settings ----------

async function postAndRefresh(url, body) {
  const res = await post(url, body);
  if (res.ok) await refreshArea("goals", true);
  return res;
}

document.addEventListener("click", async (e) => {
  const btn = e.target.closest("[data-post]");
  if (!btn) return;
  e.preventDefault();
  if (btn.dataset.confirm && !confirm(btn.dataset.confirm)) return;
  btn.disabled = true;
  const res = await postAndRefresh(btn.dataset.post, btn.dataset.body ? JSON.parse(btn.dataset.body) : {});
  if (!res.ok) btn.disabled = false;
  else await followRedirect(res);
});

// Hover cards: an element's title shows in a styled card. With selecting on (Settings ▸ Display),
// the card stays open while the pointer moves into it so its text can be selected; a long one
// (material sources) gets a Copy button and a hint. With it off, the card is for reading only and
// closes as the pointer leaves. Controls keep the browser's own tooltip.
const hoverMode = document.body.dataset.hoverCards || "";      // "select" | "plain"
const hoverSelect = hoverMode === "select";
const CONTROL = "button, input, select, textarea, a[href], summary";
let card = null, cardFor = null, cardPinned = false, showTimer = 0, hideTimer = 0;

function tipOf(el) {
  if (el.hasAttribute("title")) {
    el.dataset.tip = el.getAttribute("title");
    el.removeAttribute("title");                 // or the browser shows its own tooltip as well
    if (!el.hasAttribute("aria-label")) el.setAttribute("aria-description", el.dataset.tip);
  }
  return el.dataset.tip || "";
}

function tipTarget(node) {
  const el = node instanceof Element ? node.closest("[title], [data-tip]") : null;
  return el && !el.matches(CONTROL) && !el.closest(".hovercard") ? el : null;
}

function makeCard() {
  card = document.createElement("div");
  card.className = hoverSelect ? "hovercard" : "hovercard hovercard-plain";
  card.setAttribute("role", "tooltip");
  card.hidden = true;
  card.innerHTML = '<div class="hovercard-text"></div><div class="hovercard-foot">' +
    '<em class="hovercard-hint">Click inside to keep it open and select text</em>' +
    '<button type="button" class="hovercard-copy">Copy</button></div>';
  card.addEventListener("pointerenter", () => clearTimeout(hideTimer));
  card.addEventListener("pointerleave", () => { if (!cardPinned) hideCardSoon(); });
  card.addEventListener("pointerdown", () => { cardPinned = hoverSelect; });   // selecting: stay until a click elsewhere
  card.querySelector(".hovercard-copy").addEventListener("click", async (e) => {
    const btn = e.currentTarget;
    try { await navigator.clipboard.writeText(card.querySelector(".hovercard-text").textContent); btn.textContent = "Copied"; }
    catch { btn.textContent = "Select and Ctrl+C"; }
    setTimeout(() => { btn.textContent = "Copy"; }, 1500);
  });
  document.body.append(card);
}

function showCard(el, x) {
  const text = tipOf(el);
  if (!text) return;
  if (!card) makeCard();
  cardFor = el;
  card.querySelector(".hovercard-text").textContent = text;
  card.querySelector(".hovercard-foot").hidden = !hoverSelect || (!text.includes("\n") && text.length <= 80);
  card.hidden = false;
  const r = el.getBoundingClientRect(), c = card.getBoundingClientRect();
  const left = Math.max(8, Math.min((x ?? r.left) - 12, innerWidth - c.width - 8));
  const below = r.bottom + 6 + c.height <= innerHeight - 8;
  card.style.left = `${left + scrollX}px`;
  card.style.top = `${(below ? r.bottom + 6 : Math.max(8, r.top - 6 - c.height)) + scrollY}px`;
}

function hideCard() {
  clearTimeout(showTimer);
  if (card) card.hidden = true;
  cardFor = null;
  cardPinned = false;
}

function hideCardSoon() {
  clearTimeout(hideTimer);
  hideTimer = setTimeout(hideCard, hoverSelect ? 450 : 0);
}

if (hoverMode) {
  document.addEventListener("pointerover", (e) => {
    if (e.pointerType === "touch" || cardPinned || (card && card.contains(e.target))) return;
    const el = tipTarget(e.target);
    if (!el) return;
    tipOf(el);
    clearTimeout(hideTimer);
    if (el === cardFor) return;
    clearTimeout(showTimer);
    showTimer = setTimeout(() => showCard(el, e.clientX), 350);   // passing over others on the way to a card doesn't swap it
  });
  document.addEventListener("pointerout", (e) => {
    const el = tipTarget(e.target);
    if (!el || cardPinned || (e.relatedTarget && el.contains(e.relatedTarget))) return;
    clearTimeout(showTimer);
    if (el === cardFor) hideCardSoon();
  });
  document.addEventListener("focusin", (e) => { const el = tipTarget(e.target); if (el && !cardPinned) showCard(el); });
  document.addEventListener("focusout", (e) => { if (e.target === cardFor && !cardPinned) hideCardSoon(); });
  document.addEventListener("pointerdown", (e) => { if (card && !card.hidden && !card.contains(e.target)) hideCard(); });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") hideCard(); });
  addEventListener("scroll", () => { if (!cardPinned) hideCard(); }, { passive: true });
}

// The accent colour: on <html> (the rest of the palette derives from it) and the tab icon.
function applyAccent(key, hex, live) {
  const root = document.documentElement;
  if (root.dataset.accent === key) return;
  root.style.setProperty("--accent", hex);
  if (live) root.style.setProperty("--live", live);
  else root.style.removeProperty("--live");
  root.dataset.accent = key;
  const icon = document.querySelector('link[rel="icon"]');
  if (icon) icon.href = `/icon.svg?accent=${encodeURIComponent(key)}`;
  document.querySelectorAll("[data-setting-accent]").forEach((b) => {
    const on = b.dataset.settingAccent === key;
    b.setAttribute("aria-checked", on ? "true" : "false");
    if (on) document.querySelectorAll("[data-theme-name]").forEach((n) => { n.textContent = b.textContent.trim(); });
  });
}

// Display settings changed in another tab (or this page came back from the back/forward cache).
async function syncDisplay() {
  let s;
  try { s = await (await fetch("/api/settings/display")).json(); } catch { return; }
  applyAccent(s.accent, s.hex, s.live);
  if ((s.hover_cards ? "select" : "plain") !== hoverMode) location.reload();
}
addEventListener("pageshow", (e) => { if (e.persisted) syncDisplay(); });

// Theme menu (top right): the colour applies at once (page, tab icon), then saves.
document.addEventListener("click", async (e) => {
  const pick = e.target.closest("[data-setting-accent]");
  if (!pick) return;
  const menu = pick.closest("[data-theme-menu]");
  if (menu) menu.open = false;
  applyAccent(pick.dataset.settingAccent, pick.dataset.hex, pick.dataset.live);
  await post("/api/settings/display", { accent: pick.dataset.settingAccent });
});
// It closes on a click elsewhere or Escape; arrow keys move between colours.
document.addEventListener("pointerdown", (e) => {
  document.querySelectorAll("[data-theme-menu][open]").forEach((m) => { if (!m.contains(e.target)) m.open = false; });
});
document.addEventListener("keydown", (e) => {
  const menu = e.target.closest && e.target.closest("[data-theme-menu]");
  if (!menu || !menu.open) return;
  const opts = [...menu.querySelectorAll("[data-setting-accent]")];
  if (e.key === "Escape") { menu.open = false; menu.querySelector("summary").focus(); return; }
  if (e.key !== "ArrowDown" && e.key !== "ArrowUp") return;
  e.preventDefault();
  const i = opts.indexOf(document.activeElement);
  const next = e.key === "ArrowDown" ? (i + 1) % opts.length : (i <= 0 ? opts.length - 1 : i - 1);
  opts[next].focus();
});
document.addEventListener("toggle", (e) => {
  if (!e.target.matches || !e.target.matches("[data-theme-menu]") || !e.target.open) return;
  const on = e.target.querySelector('[aria-checked="true"]');
  if (on) on.focus();
}, true);

document.addEventListener("change", async (e) => {
  const box = e.target.closest("[data-setting-hover-cards]");
  if (!box) return;
  const res = await post("/api/settings/display", { hover_cards: box.checked });
  if (res.ok) location.reload();
});

// Ships: copy a fit's SLEF. If the clipboard refuses, show the text selected for a manual copy.
document.addEventListener("click", async (e) => {
  const btn = e.target.closest("[data-copy]");
  if (!btn) return;
  const box = document.getElementById(btn.dataset.copy);
  if (!box) return;
  try {
    await navigator.clipboard.writeText(box.value);
    const label = btn.textContent;
    btn.textContent = "Copied";
    setTimeout(() => { btn.textContent = label; }, 2000);
  } catch {
    box.hidden = false;
    box.focus();
    box.select();
  }
});

// Ships: saved fits. Each ship's tab is remembered for this browser tab, so a live reload keeps it.
function fitTabKey(ship) { return `fit-tab:${ship}`; }

function showFitTab(tabs, id) {
  const panel = tabs.closest("details");
  if (!panel.querySelector(`[data-fit-panel="${id}"]`)) id = "current";
  tabs.querySelectorAll("[data-fit-tab]").forEach((t) => {
    const on = t.dataset.fitTab === String(id);
    t.classList.toggle("active", on);
    t.setAttribute("aria-selected", on ? "true" : "false");
  });
  panel.querySelectorAll("[data-fit-panel]").forEach((p) => { p.hidden = p.dataset.fitPanel !== String(id); });
  panel.querySelectorAll("[data-fit-actions]").forEach((p) => { p.hidden = p.dataset.fitActions !== String(id); });
  try { sessionStorage.setItem(fitTabKey(tabs.dataset.fitTabs), String(id)); } catch { /* storage blocked */ }
}

document.querySelectorAll("[data-fit-tabs]").forEach((tabs) => {
  let id = null;
  try { id = sessionStorage.getItem(fitTabKey(tabs.dataset.fitTabs)); } catch { /* storage blocked */ }
  if (id) showFitTab(tabs, id);
});

// /ships?fit=<id> (an imported build that became a fit): open that ship on that tab.
{
  const want = new URLSearchParams(location.search).get("fit");
  const tab = want && document.querySelector(`[data-fit-tab="${CSS.escape(want)}"]`);
  if (tab) {
    const panel = tab.closest("details");
    panel.open = true;
    showFitTab(tab.closest("[data-fit-tabs]"), want);
    history.replaceState(null, "", location.pathname);
    panel.scrollIntoView({ block: "start" });
  }
}

function fitError(el, res, body) {
  const out = el.closest("details").querySelector(".fit-tabs .form-error");
  out.textContent = body.error || body.detail || `Failed (${res.status})`;
  out.hidden = false;
}

document.addEventListener("click", async (e) => {
  const tab = e.target.closest("[data-fit-tab]");
  if (tab) { showFitTab(tab.closest("[data-fit-tabs]"), tab.dataset.fitTab); return; }
  const save = e.target.closest("[data-fit-save]");
  const del = e.target.closest("[data-fit-delete]");
  if (!save && !del) return;
  if (del && !confirm(`Delete the saved fit “${del.dataset.name}”?`)) return;
  const btn = save || del;
  btn.disabled = true;
  const res = await post(save ? `/api/ships/${save.dataset.fitSave}/fits` : `/api/fits/${del.dataset.fitDelete}/delete`);
  const body = await res.json().catch(() => ({}));
  if (!res.ok) { fitError(btn, res, body); btn.disabled = false; return; }
  const tabs = btn.closest("details").querySelector("[data-fit-tabs]");
  try { sessionStorage.setItem(fitTabKey(tabs.dataset.fitTabs), save ? String(body.id) : "current"); } catch { /* storage blocked */ }
  location.reload();
});

document.addEventListener("change", async (e) => {
  const box = e.target.closest("[data-fit-all]");
  if (box) { box.closest("[data-fit-panel]").querySelector(".fit-compare").classList.toggle("only-changes", !box.checked); return; }
  const input = e.target.closest("[data-fit-rename]");
  if (!input) return;
  const res = await post(`/api/fits/${input.dataset.fitRename}/rename`, { name: input.value });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) { fitError(input, res, body); input.value = input.defaultValue; return; }
  input.defaultValue = input.value;
  const tab = input.closest("details").querySelector(`[data-fit-tab="${input.dataset.fitRename}"]`);
  if (tab) tab.querySelector("[data-fit-label]").textContent = input.value;
  input.closest("details").querySelector(`[data-fit-delete="${input.dataset.fitRename}"]`).dataset.name = input.value;
});

// A save the server wants confirmed answers 409 {confirm}: ask, and on yes send it again with
// retarget. Returns the final response, or null when the user said no.
async function postConfirmed(url, body, send) {
  const res = await send(url, body);
  if (res.status !== 409) return res;
  const out = await res.clone().json().catch(() => ({}));
  if (!out.confirm) return res;
  return confirm(out.confirm) ? send(url, { ...body, retarget: true }) : null;
}

function revertField(el) {
  if (el.tagName === "SELECT") {
    const i = [...el.options].findIndex((o) => o.defaultSelected);
    el.selectedIndex = Math.max(i, 0);
  } else if (el.type === "checkbox" || el.type === "radio") el.checked = el.defaultChecked;
  else el.value = el.defaultValue;
}

// A save may answer {redirect: url} (Notes): go there, in place when the page allows it.
async function followRedirect(res) {
  const body = await res.clone().json().catch(() => ({}));
  if (!body.redirect) return;
  document.querySelectorAll("form[data-guard]").forEach((f) => { f.dataset.saved = "1"; });
  const url = new URL(body.redirect, location.href);
  if (document.querySelector("main[data-swap]") && url.pathname === location.pathname) await swapTo(url.href);
  else location.href = url.href;
}

function formPayload(form) {
  // Only fields the user can see: inputs for other goal kinds are hidden, not removed.
  const visible = [...form.elements].filter((el) => el.name && !el.closest("[hidden]"));
  const data = {};
  const flags = [];
  let reqRank = "", rankPick = "", hasRequires = false;
  for (const el of visible) {
    if (el.type === "checkbox") {
      if (el.name === "flag") { hasRequires = true; if (el.checked) flags.push({ flag: el.value }); }
      continue;
    }
    if (el.name === "req_rank") { hasRequires = true; reqRank = el.value; continue; }
    if (el.name === "rank_pick") { rankPick = el.value; continue; }
    data[el.name] = el.value;
  }
  if (hasRequires) {
    if (reqRank) { const [rank, min] = reqRank.split(":"); flags.push({ rank, min: Number(min) }); }
    data.requires = flags;
  }
  if (rankPick && data.kind === "rank") { const [k, v] = rankPick.split(":"); data.target_key = k; data.target_value = v; }
  return data;
}

document.addEventListener("submit", async (e) => {
  const form = e.target.closest("[data-api-form]");
  if (!form) return;
  e.preventDefault();
  const err = form.querySelector(".form-error");
  const res = await postConfirmed(form.dataset.apiForm, formPayload(form), post);
  if (!res) return;                                    // the user said no to the question
  if (res.ok) {
    if (form.hasAttribute("data-reset")) form.reset();
    else form.dataset.saved = "1";                     // saved: the swap takes the server's values
    await refreshArea("goals", true);
    await followRedirect(res);
  } else if (err) {
    const body = await res.json().catch(() => ({}));
    err.textContent = body.error || body.detail || "Couldn't save.";
    err.hidden = false;
  }
});

document.addEventListener("change", async (e) => {
  const t = e.target;
  if (t.matches("[data-step-goal]")) {
    await postAndRefresh(`/api/goals/${t.dataset.stepGoal}/step`, { step: t.value, done: t.checked });
  } else if (t.matches("[data-pin-engineer]")) {
    await postAndRefresh(`/api/engineers/${t.dataset.pinEngineer}/pin`, { pin: t.value });
  } else if (t.matches("[data-change-post]")) {
    t.setCustomValidity("");
    const res = await postConfirmed(t.dataset.changePost, { [t.dataset.key]: t.value }, postAndRefresh);
    if (!res || !res.ok) {
      revertField(t);                                  // the choice didn't save: show what's saved
      if (res) { t.setCustomValidity((await res.json().catch(() => ({}))).error || "Not saved"); t.reportValidity(); }
    }
  } else if (t.matches("[data-flag]")) {
    await postAndRefresh("/api/flags", { key: t.dataset.flag, value: t.value });
  } else if (t.matches("[data-plan-setting]")) {
    await postAndRefresh("/api/plan", { [t.dataset.planSetting]: t.value });
  } else if (t.matches("[data-selling]")) {
    const ids = [...document.querySelectorAll("[data-selling]:checked")].map((x) => Number(x.value));
    await postAndRefresh("/api/plan", { selling: ids });
  } else if (t.matches("[data-kind-select]")) {
    showKindFields(t.closest("form"));
  }
});

function showKindFields(form) {
  if (!form) return;
  const kind = form.querySelector("[data-kind-select]").value;
  form.querySelectorAll("[data-for-kind]").forEach((el) => {
    el.hidden = !el.dataset.forKind.split(" ").includes(kind);
  });
}

new MutationObserver(() => document.querySelectorAll("form [data-kind-select]").forEach((s) => showKindFields(s.closest("form"))))
  .observe(document.body, { childList: true, subtree: true });
document.querySelectorAll("form [data-kind-select]").forEach((s) => showKindFields(s.closest("form")));

// -- In-place navigation (DESIGN §13): on a page whose <main> has data-swap, links to the same
// page and GET forms (range, rank track, material…) fetch the new page and swap <main>, so the
// scroll position stays and nothing reloads. Anything unexpected falls back to a normal load.
async function swapTo(url, push = true) {
  const main = document.getElementById("page-main");
  try {
    const res = await fetch(url, { headers: { "Accept": "text/html" } });
    const fresh = res.ok && new DOMParser().parseFromString(await res.text(), "text/html").getElementById("page-main");
    if (!fresh) throw new Error("no page");
    main.innerHTML = fresh.innerHTML;
    pending.clear();
    if (push) history.pushState(null, "", url);
    filterStats();
  } catch {
    location.href = url;
  }
}

// Forms marked data-guard (a note being edited) ask before their unsaved changes are dropped.
function unsavedGuard() {
  return [...document.querySelectorAll("form[data-guard]")].some((f) =>
    !f.dataset.saved && [...f.elements].some((el) => el.name && dirty(el)));
}

window.addEventListener("beforeunload", (e) => { if (unsavedGuard()) { e.preventDefault(); e.returnValue = ""; } });

document.addEventListener("click", (e) => {
  const a = e.target.closest("a[href]");
  if (a && !e.defaultPrevented && !a.target && unsavedGuard() && !confirm("Leave without saving this note?")) {
    e.preventDefault();
    e.stopImmediatePropagation();
  }
}, true);

document.addEventListener("submit", (e) => {
  const form = e.target.closest("main[data-swap] form[method='get']");
  if (!form) return;
  e.preventDefault();
  const url = new URL(form.getAttribute("action") || location.pathname, location.href);
  url.search = new URLSearchParams(new FormData(form)).toString();
  swapTo(url.href);
});

document.addEventListener("click", (e) => {
  const a = e.target.closest("main[data-swap] a[href]");
  if (!a || e.defaultPrevented || e.button !== 0 || e.ctrlKey || e.metaKey || e.shiftKey || a.target) return;
  const url = new URL(a.href, location.href);
  if (url.origin !== location.origin || url.pathname !== location.pathname || (url.hash && url.search === location.search)) return;
  e.preventDefault();
  swapTo(url.href);
});

// A search box emptied (typed away or its ✕) shows everything again without waiting for Enter.
document.addEventListener("input", async (e) => {
  const box = e.target.matches("input[type='search']") && e.target.closest("main[data-swap] form[method='get']") && e.target;
  if (!box || box.value) return;
  const name = box.name;
  box.form.requestSubmit();
  setTimeout(() => {                                 // the page swaps in place: keep typing where you were
    const again = document.querySelector(`main form[method='get'] input[name='${name}']`);
    if (again) again.focus();
  }, 300);
});

// Statistics: filter rows as you type. Each word must appear in the row (or its section's name);
// "bounty" also finds "bounties" and the other way round.
function searchForms(word) {
  const out = new Set([word]);
  if (word.endsWith("ies")) out.add(word.slice(0, -3) + "y");
  if (word.endsWith("s")) out.add(word.slice(0, -1));
  if (word.endsWith("y")) out.add(word.slice(0, -1) + "ie");
  return [...out];
}

function filterStats() {
  const box = document.querySelector("[data-filter-stats]");
  if (!box) return;
  const words = box.value.toLowerCase().split(/\s+/).filter(Boolean).map(searchForms);
  const hit = (text) => words.every((forms) => forms.some((f) => text.includes(f)));
  let rows = 0;
  document.querySelectorAll(".stat-grid > section").forEach((sec) => {
    const cat = (sec.querySelector("h2")?.textContent || "").toLowerCase();
    let n = 0;
    sec.querySelectorAll("tbody tr").forEach((tr) => {
      const show = !words.length || hit(`${cat} ${tr.cells[0].textContent.toLowerCase()}`);
      tr.hidden = !show;
      if (show) n += 1;
    });
    sec.hidden = n === 0;
    rows += n;
  });
  document.querySelector("[data-filter-count]").textContent = words.length ? `${rows} ${rows === 1 ? "match" : "matches"}` : "";
  document.querySelector("[data-filter-none]").hidden = !words.length || rows > 0;
  const url = new URL(location.href);
  if (box.value.trim()) url.searchParams.set("q", box.value.trim()); else url.searchParams.delete("q");
  history.replaceState(null, "", url);
  document.querySelectorAll("nav.subtabs a[href^='/statistics']").forEach((a) => {   // range links keep the search
    const u = new URL(a.href, location.href);
    if (box.value.trim()) u.searchParams.set("q", box.value.trim()); else u.searchParams.delete("q");
    a.href = u.pathname + u.search;
  });
}

document.addEventListener("input", (e) => { if (e.target.matches("[data-filter-stats]")) filterStats(); });
filterStats();

document.addEventListener("change", (e) => {
  const form = e.target.matches("select") && e.target.closest("main[data-swap] form[method='get']");
  if (!form) return;                                 // text fields (search) go on Enter, via submit
  const url = new URL(form.getAttribute("action") || location.pathname, location.href);
  url.search = new URLSearchParams(new FormData(form)).toString();
  swapTo(url.href);
});

window.addEventListener("popstate", () => {
  if (document.querySelector("main[data-swap]")) swapTo(location.href, false);
});

// -- Ships: expand / collapse every fit ----------------------------------------------------
document.addEventListener("click", (e) => {
  const btn = e.target.closest("[data-ships-toggle]");
  if (!btn) return;
  const open = btn.dataset.shipsToggle === "open";
  document.querySelectorAll("details.ship-panel").forEach((d) => { d.open = open; });
});

// -- Backup & import (Phase 6): export, upload for review, download, apply ------------------
function showError(scope, text) {
  const err = scope && scope.querySelector(".form-error");
  if (err) { err.textContent = text; err.hidden = !text; }
}

async function errorText(res, fallback) {
  const body = await res.json().catch(() => ({}));
  return body.error || body.detail || fallback;
}

async function pickSaveTarget(name) {
  // Where the browser can ask (Edge, Chrome), the user chooses the folder; else it downloads.
  if (!window.showSaveFilePicker) return null;
  return showSaveFilePicker({ suggestedName: name,
    types: [{ description: "ED Commander Planner file", accept: { "application/octet-stream": [".edplanner"] } }] });
}

async function saveResponse(res, handle) {
  const blob = await res.blob();
  if (handle) {
    const out = await handle.createWritable();
    await out.write(blob);
    await out.close();
    return;
  }
  const m = (res.headers.get("Content-Disposition") || "").match(/filename\*=UTF-8''([^;]+)/);
  const a = Object.assign(document.createElement("a"), {
    href: URL.createObjectURL(blob), download: m ? decodeURIComponent(m[1]) : "planner.edplanner" });
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
}

document.addEventListener("click", async (e) => {
  const btn = e.target.closest('[data-action="backup-export"], [data-action="backup-download"]');
  if (!btn) return;
  const scope = btn.closest(".panel");
  const exporting = btn.dataset.action === "backup-export";
  const keys = [...document.querySelectorAll("[data-export-key]:checked")].map((x) => x.dataset.exportKey);
  if (exporting && !keys.length) { showError(scope, "Pick at least one commander."); return; }
  showError(scope, "");
  const today = new Date();
  const name = exporting
    ? `ED Commander Planner ${today.getFullYear()}-${String(today.getMonth() + 1).padStart(2, "0")}-${String(today.getDate()).padStart(2, "0")}.edplanner`
    : btn.dataset.name;
  let handle = null;
  try { handle = await pickSaveTarget(name); } catch (err) { if (err.name === "AbortError") return; }
  btn.disabled = true;
  try {
    const res = exporting ? await post("/api/backup/export", { commanders: keys })
                          : await post("/api/backup/download", { name: btn.dataset.name });
    if (res.ok) await saveResponse(res, handle);
    else showError(scope, await errorText(res, "Couldn't make the file."));
  } finally {
    btn.disabled = false;
  }
});

document.addEventListener("click", async (e) => {
  const btn = e.target.closest('[data-action="backup-upload"]');
  if (!btn) return;
  const scope = btn.closest(".panel");
  const file = scope.querySelector("[data-backup-file]").files[0];
  if (!file) { showError(scope, "Pick a .edplanner file first."); return; }
  showError(scope, "");
  btn.disabled = true;
  try {
    const res = await fetch("/api/backup/upload", { method: "POST", body: file, headers: {
      "Content-Type": "application/octet-stream", "X-Planner-Token": TOKEN, "X-Filename": encodeURIComponent(file.name) } });
    if (res.ok) await followRedirect(res);
    else showError(scope, await errorText(res, "Couldn't read that file."));
  } finally {
    btn.disabled = false;
  }
});

function showConflicts(select) {
  document.querySelectorAll(`[data-conflicts-for="${select.name}"]`).forEach((el) => { el.hidden = select.value !== "merge"; });
}

document.addEventListener("change", (e) => { if (e.target.matches("select[data-mode]")) showConflicts(e.target); });
document.querySelectorAll("select[data-mode]").forEach(showConflicts);

document.addEventListener("click", (e) => {
  const btn = e.target.closest("[data-conflicts-all]");
  if (!btn) return;
  btn.closest(".conflicts").querySelectorAll(`input[type=radio][value="${btn.dataset.conflictsAll}"]`)
    .forEach((r) => { r.checked = true; });
});

document.addEventListener("submit", async (e) => {
  const form = e.target.closest("[data-backup-apply]");
  if (!form) return;
  e.preventDefault();
  const modes = {}, choices = {};
  form.querySelectorAll("select[data-mode]").forEach((s) => { modes[s.name] = s.value; });
  form.querySelectorAll("input[type=radio]:checked").forEach((r) => { choices[r.name] = r.value; });
  const overrides = Object.values(modes).filter((m) => m === "override").length;
  if (overrides && !confirm(`Override replaces yours with the file's for ${overrides} part${overrides === 1 ? "" : "s"}. A backup is taken first. Go ahead?`)) return;
  const btn = form.querySelector("[type=submit]");
  btn.disabled = true;
  showError(form, "");
  try {
    const res = await post("/api/backup/apply", { file: form.dataset.backupApply, modes, choices });
    if (res.ok) await followRedirect(res);
    else showError(form, await errorText(res, "The import didn't apply; nothing was changed."));
  } finally {
    btn.disabled = false;
  }
});

// -- Notes: edit in place, save as you type, revert, preview, formatting, ticking to-dos ---------
// The editor saves 0.8 s after typing stops. "Revert" goes back to the note as it was when the
// editor opened, then offers "Undo revert". Toolbar edits go through insertText, so Ctrl+Z
// undoes them like typing.
const noteState = new WeakMap();

function noteField(form, name) { return form.elements.namedItem(name); }

function noteSt(form) {
  if (!noteState.has(form)) {
    noteState.set(form, { orig: { title: noteField(form, "title").defaultValue, body: noteField(form, "body").defaultValue },
                          redo: null, timer: 0, saving: null, again: false });
  }
  return noteState.get(form);
}

function noteDirty(form) {
  return ["title", "body"].some((n) => noteField(form, n).value !== noteField(form, n).defaultValue);
}

function noteStatus(form, text) { form.querySelector(".note-status").textContent = text; }

function noteRevertButton(form) {
  const st = noteSt(form), btn = form.querySelector("[data-note-revert]");
  if (st.redo) { btn.textContent = "Undo revert"; btn.disabled = false; return; }
  btn.textContent = "Revert to before editing";
  btn.disabled = noteField(form, "title").value === st.orig.title && noteField(form, "body").value === st.orig.body;
}

async function saveNote(form) {
  const st = noteSt(form);
  clearTimeout(st.timer);
  st.timer = 0;
  if (st.saving) { st.again = true; return st.saving; }
  const sent = { title: noteField(form, "title").value, body: noteField(form, "body").value };
  noteStatus(form, "Saving…");
  st.saving = (async () => {
    const res = await post(`/api/notes/${form.dataset.noteEditor}/update`, sent).catch(() => null);
    const out = res ? await res.json().catch(() => ({})) : {};
    const err = form.querySelector(".form-error");
    if (res && res.ok) {
      noteField(form, "title").defaultValue = sent.title;
      noteField(form, "body").defaultValue = sent.body;
      err.hidden = true;
      noteStatus(form, `Saved ${new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}`);
      const link = form.isConnected && document.querySelector(".note-link.active .note-title-text");
      if (link && out.title) link.textContent = out.title;
    } else {
      err.textContent = out.error || out.detail || "Couldn't save. Your text is still here, and saving tries again as you type.";
      err.hidden = false;
      noteStatus(form, "Not saved");
    }
  })();
  await st.saving;
  st.saving = null;
  if (st.again) { st.again = false; await saveNote(form); }
}

function openNoteEditor(view) {
  const form = view && view.querySelector("[data-note-editor]");
  if (!form) return false;
  view.querySelector(".note-read").hidden = true;
  form.hidden = false;
  noteSt(form);
  noteRevertButton(form);
  history.replaceState(null, "", `/notes?n=${form.dataset.noteEditor}&edit=1`);
  noteField(form, "body").focus();
  return true;
}

function replaceSelection(ta, text, selStart = null, selEnd = null) {
  ta.focus();
  const ok = text ? document.execCommand("insertText", false, text) : document.execCommand("delete");
  if (!ok) {                                     // no execCommand: still edits, without undo history
    ta.setRangeText(text, ta.selectionStart, ta.selectionEnd, "end");
    ta.dispatchEvent(new Event("input", { bubbles: true }));
  }
  if (selStart !== null) ta.setSelectionRange(selStart, selEnd);
}

function wrapSelection(ta, before, after, placeholder) {
  const s = ta.selectionStart, sel = ta.value.slice(s, ta.selectionEnd) || placeholder;
  replaceSelection(ta, before + sel + after, s + before.length, s + before.length + sel.length);
}

// Heading, list and to-do apply to whole lines, and come off again when every line has them.
function prefixLines(ta, kind) {
  const v = ta.value, s = ta.selectionStart;
  const ls = v.lastIndexOf("\n", s - 1) + 1;
  let le = v.indexOf("\n", Math.max(ta.selectionEnd - 1, s));
  if (le === -1) le = v.length;
  const lines = v.slice(ls, le).split("\n");
  const bare = (l) => l.replace(/^(\s*)(?:#{1,3}\s+|(?:[-*]|\d+[.)])\s+(?:\[[ xX]\]\s+)?)/, "$1");
  const marks = { heading: /^#{1,3}\s/, list: /^\s*[-*]\s+(?!\[[ xX]\])/, task: /^\s*[-*]\s+\[[ xX]\]\s/ };
  const add = { heading: "## ", list: "- ", task: "- [ ] " };
  const filled = lines.filter((l) => l.trim());
  if (!filled.length) {                          // a blank line: start one, ready to type
    ta.setSelectionRange(ls, le);
    replaceSelection(ta, add[kind]);
    return;
  }
  const on = filled.every((l) => marks[kind].test(l));
  const out = lines.map((l) => (!l.trim() ? l : on ? bare(l) : bare(l).replace(/^(\s*)/, `$1${add[kind]}`))).join("\n");
  ta.setSelectionRange(ls, le);
  replaceSelection(ta, out, ls, ls + out.length);
}

function applyMarkdown(form, kind) {
  const ta = noteField(form, "body");
  if (kind === "bold") wrapSelection(ta, "**", "**", "bold text");
  else if (kind === "italic") wrapSelection(ta, "*", "*", "italic text");
  else if (kind === "code") wrapSelection(ta, "`", "`", "code");
  else if (kind === "link") {
    const s = ta.selectionStart, text = ta.value.slice(s, ta.selectionEnd) || "link text";
    const at = s + text.length + 3;
    replaceSelection(ta, `[${text}](https://)`, at, at + 8);
  } else prefixLines(ta, kind);
}

function continueList(ta, e) {
  const s = ta.selectionStart;
  if (s !== ta.selectionEnd) return;
  const ls = ta.value.lastIndexOf("\n", s - 1) + 1;
  const m = ta.value.slice(ls, s).match(/^(\s*)([-*]|\d+[.)])\s+(\[[ xX]\]\s+)?(.*)$/);
  if (!m) return;
  e.preventDefault();
  if (!m[4].trim()) {                            // Enter on an empty item ends the list
    ta.setSelectionRange(ls, s);
    replaceSelection(ta, "");
    return;
  }
  const marker = /^\d/.test(m[2]) ? `${parseInt(m[2], 10) + 1}${m[2].slice(-1)}` : m[2];
  replaceSelection(ta, `\n${m[1]}${marker} ${m[3] ? "[ ] " : ""}`);
}

document.addEventListener("input", (e) => {
  const form = e.target.closest("[data-note-editor]");
  if (!form) return;
  const st = noteSt(form);
  st.redo = null;
  noteRevertButton(form);
  noteStatus(form, "Editing…");
  clearTimeout(st.timer);
  st.timer = setTimeout(() => saveNote(form), 800);
});

document.addEventListener("keydown", (e) => {
  const form = e.target.closest("[data-note-editor]");
  if (!form) return;
  const mod = e.ctrlKey || e.metaKey;
  const key = e.key.toLowerCase();
  const shortcut = { b: "bold", i: "italic", k: "link" }[key];
  if (mod && key === "s") {
    e.preventDefault();
    saveNote(form);
  } else if (e.target.name === "body" && mod && !e.shiftKey && !e.altKey && shortcut) {
    e.preventDefault();
    applyMarkdown(form, shortcut);
  } else if (e.target.name === "body" && e.key === "Enter" && !mod && !e.shiftKey && !e.isComposing) {
    continueList(e.target, e);
  }
});

document.addEventListener("click", async (e) => {
  const edit = e.target.closest("[data-note-edit]");
  if (edit && openNoteEditor(edit.closest(".notes-view"))) { e.preventDefault(); return; }
  const form = e.target.closest("[data-note-editor]");
  if (!form) {
    // Leaving through a link: send a pending save now; the page swaps in place, so it completes.
    if (e.target.closest("a[href]")) {
      document.querySelectorAll("[data-note-editor]").forEach((f) => { if (noteDirty(f)) saveNote(f); });
    }
    return;
  }
  const md = e.target.closest("[data-md]");
  if (md) { applyMarkdown(form, md.dataset.md); return; }
  const mode = e.target.closest("[data-note-mode]");
  if (mode) {
    const preview = mode.dataset.noteMode === "preview";
    form.querySelectorAll("[data-note-mode]").forEach((b) => {
      b.classList.toggle("active", b === mode);
      b.setAttribute("aria-selected", b === mode ? "true" : "false");
    });
    form.querySelectorAll("[data-md]").forEach((b) => { b.disabled = preview; });
    const out = form.querySelector(".note-preview");
    if (preview) {
      const res = await post("/api/notes/preview", { body: noteField(form, "body").value });
      out.innerHTML = res.ok ? ((await res.json()).html || '<p class="muted">Nothing to preview yet.</p>')
                             : '<p class="warn">Preview failed.</p>';
    }
    out.hidden = !preview;
    noteField(form, "body").hidden = preview;
    return;
  }
  if (e.target.closest("[data-note-revert]")) {
    const st = noteSt(form), title = noteField(form, "title"), body = noteField(form, "body");
    if (st.redo) {
      [title.value, body.value] = [st.redo.title, st.redo.body];
      st.redo = null;
    } else {
      st.redo = { title: title.value, body: body.value };
      [title.value, body.value] = [st.orig.title, st.orig.body];
    }
    noteRevertButton(form);
    await saveNote(form);
    return;
  }
  if (e.target.closest("[data-note-done]")) {
    if (noteSt(form).timer || noteDirty(form)) await saveNote(form);
    noteState.delete(form);
    await swapTo(`/notes?n=${form.dataset.noteEditor}`);
  }
});

document.addEventListener("dblclick", (e) => {
  const body = e.target.closest(".note-read .note-body");
  if (body && !e.target.closest("a, input")) {
    getSelection().removeAllRanges();
    openNoteEditor(body.closest(".notes-view"));
  }
});

// Closing the tab or the app mid-sentence still saves.
addEventListener("pagehide", () => {
  document.querySelectorAll("[data-note-editor]").forEach((form) => {
    if (!noteDirty(form)) return;
    fetch(`/api/notes/${form.dataset.noteEditor}/update`, {
      method: "POST", keepalive: true, headers: { "Content-Type": "application/json", "X-Planner-Token": TOKEN },
      body: JSON.stringify({ title: noteField(form, "title").value, body: noteField(form, "body").value }),
    });
  });
});

// Ticking a to-do while reading saves it straight away.
document.addEventListener("change", async (e) => {
  const box = e.target.closest("[data-note-tasks] input[data-task]");
  if (!box) return;
  box.disabled = true;
  const res = await post(`/api/notes/${box.closest("[data-note-tasks]").dataset.noteTasks}/task`,
                         { index: Number(box.dataset.task), done: box.checked });
  if (!res.ok) { box.checked = !box.checked; box.disabled = false; return; }
  await swapTo(location.pathname + location.search, false);   // so the editor's copy of the text is fresh too
});
