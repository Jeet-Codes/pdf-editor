"use strict";
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const el = (tag, cls, parent) => {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (parent) parent.appendChild(e);
  return e;
};

const icon = name => `<svg class="ic" aria-hidden="true"><use href="#i-${name}"/></svg>`;

const S = {
  doc: null, zoom: 1, tool: "edit", pages: [], // pages: {index, w, h, rev, node, img, layer, loadedRev, layout}
  editor: null, selected: null, savedRange: null, currentPage: 0, pendingImagePoint: null,
  replaceImage: null, fonts: ["Arial"],
  tabs: [], active: null, view: "home", library: [],
};

// Every call that edits or reads a PDF says which tab it belongs to.
const docParam = () => `doc=${S.active.id}`;
const wait = ms => new Promise(r => setTimeout(r, ms));
async function idle() { while (busyCount > 0) await wait(60); }

// ------------------------------------------------------------------ server
async function api(url, options) {
  const res = await fetch(url, options);
  const type = res.headers.get("content-type") || "";
  const body = type.includes("json") ? await res.json() : null;
  if (!res.ok) {
    const err = new Error((body && body.error) || "Something went wrong.");
    err.status = res.status; err.body = body;
    throw err;
  }
  return body;
}

let busyCount = 0;
async function withBusy(text, fn) {
  busyCount++;
  $("#busyText").textContent = text;
  const timer = setTimeout(() => { if (busyCount) $("#busy").hidden = false; }, 180);
  try { return await fn(); }
  catch (e) { toast(e.message, true); return null; }
  finally {
    clearTimeout(timer);
    if (--busyCount === 0) $("#busy").hidden = true;
  }
}

function op(body, text = "Applying…") {
  return withBusy(text, async () => {
    const state = await api(`/api/op?${docParam()}`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
    applyState(state);
    return state;
  });
}

function toast(message, isError) {
  const t = el("div", "toast" + (isError ? " err" : ""), $("#toasts"));
  t.innerHTML = icon(isError ? "alert" : "check");
  el("span", "", t).textContent = message;
  setTimeout(() => t.remove(), isError ? 7000 : 4500);
}

// --------------------------------------------------------------- document
function applyState(state) {
  (state.warnings || []).forEach(w => toast(w));
  $("#undoBtn").disabled = !state.canUndo;
  $("#redoBtn").disabled = !state.canRedo;
  S.fonts = state.fonts || S.fonts;
  if (!$("#fFamily").options.length) {
    $("#fFamily").replaceChildren(...S.fonts.map(f => {
      const o = document.createElement("option"); o.textContent = f; o.value = f; return o;
    }));
  }
  const structure = !S.doc || S.pages.length !== state.pages.length
    || state.pages.some((p, i) => p.w !== S.pages[i].w || p.h !== S.pages[i].h);
  S.doc = state;
  S.active.state = state;
  S.active.name = state.name;
  if (structure) buildPages();
  else state.pages.forEach((p, i) => {
    if (S.pages[i].rev !== p.rev) {
      S.pages[i].rev = p.rev;
      S.pages[i].thumbImg.src = imageUrl(S.pages[i], 0.25);
    }
  });
  deselect();
  refreshVisible();
  showSaveState();
  if (!$("#findpanel").hidden) runFind(true);
  renderOrganizer();
}

function showSaveState() {
  const saved = S.doc && S.doc.saved;
  const b = $("#saveBtn");
  $("#saveLabel").textContent = saved ? "Saved" : "Save";
  $("#saveIcon").setAttribute("href", saved ? "#i-check" : "#i-save");
  b.dataset.tip = saved ? "In Saved PDFs. Your changes are kept automatically." : "Keep this PDF under Saved PDFs";
  updateStatus();
}

const UNLOCK_NOTE = {
  password: name => `${name} is unlocked. Edit freely: downloads open without a password.`,
  restrictions: name => `${name} limited editing and copying. Those limits are removed, so you can edit it freely.`,
};
const SECURITY_LABEL = { password: "Password removed", restrictions: "Restrictions removed" };

const TOOL_HINTS = {
  edit: "Click any text or photo to change it",
  text: "Click on the page to add text, or drag to set its width",
  select: "Drag around anything to move or copy it",
  erase: "Drag over anything to remove it for good",
  place: "Click on the page where it should go. Esc cancels.",
};

// The status bar and the Document panel only report what the editor already knows.
function updateStatus() {
  if (S.view !== "editor" || !S.doc) return;
  const total = S.doc.pages.length, page = Math.min(S.currentPage, total - 1), info = S.doc.pages[page];
  const saved = !!S.doc.saved;
  const label = saved ? "All changes saved" : "Not in Saved PDFs yet";
  $("#statusPage").textContent = `Page ${page + 1} of ${total}`;
  $("#statusSave").textContent = label;
  $("#statusSave").classList.toggle("good", saved);
  $("#statusHint").textContent = TOOL_HINTS[S.tool] || "";
  $("#dName").textContent = S.doc.name;
  $("#dPages").textContent = total;
  $("#dSize").textContent = `${(info.w / CM).toFixed(1)} × ${(info.h / CM).toFixed(1)} cm`;
  $("#dSaved").textContent = label;
  $("#dSecurity").textContent = SECURITY_LABEL[S.doc.unlocked] || "Not locked";
  $("#dSecurity").classList.toggle("good", !!S.doc.unlocked);
}

function fitZoom(pages) {
  const avail = $("#scroller").clientWidth - 64;
  const widest = Math.max(...pages.map(p => p.w));
  return Math.max(0.5, Math.min(1.5, avail / widest));
}

// ------------------------------------------------------------------- tabs
function addTab(state) {
  const tab = {
    id: state.id, name: state.name, first: state, state: null, pages: [], zoom: 1, currentPage: 0, scrollTop: 0,
    holder: el("div", "pagesdoc", $("#pages")), thumbHolder: el("div", "", $("#thumbs")), observer: null,
  };
  tab.holder.hidden = tab.thumbHolder.hidden = true;
  S.tabs.push(tab);
  return tab;
}

function rememberActiveTab() {
  const tab = S.active;
  if (!tab) return;
  tab.pages = S.pages; tab.zoom = S.zoom; tab.currentPage = S.currentPage;
  if (S.view === "editor") tab.scrollTop = $("#scroller").scrollTop;
}

async function activateTab(tab) {
  await commitEditor();
  await idle();
  if (!S.tabs.includes(tab)) return;
  deselect();
  rememberActiveTab();
  S.tabs.forEach(t => { t.holder.hidden = t.thumbHolder.hidden = t !== tab; });
  S.active = tab;
  closeOrganizer();
  clearHits();
  S.doc = tab.state; S.pages = tab.pages; S.zoom = tab.zoom; S.currentPage = tab.currentPage;
  showView("editor");
  setTool("edit");
  if (!tab.state) {
    S.zoom = fitZoom(tab.first.pages);
    applyState(tab.first);
    if (tab.first.signed) toast(`${tab.name} has a digital signature. Any edit will invalidate it.`);
    if (tab.first.unlocked && !tab.first.saved) toast(UNLOCK_NOTE[tab.first.unlocked](tab.name));
  } else {
    $("#undoBtn").disabled = !tab.state.canUndo;
    $("#redoBtn").disabled = !tab.state.canRedo;
    $("#zoomLabel").textContent = Math.round(S.zoom * 100) + "%";
    showSaveState();
    refreshVisible();
    if (!$("#findpanel").hidden) runFind(false);
  }
  $("#scroller").scrollTop = tab.scrollTop;
  renderTabs();
}

function openInTab(state) {
  const tab = S.tabs.find(t => t.id === state.id) || addTab(state);
  return activateTab(tab);
}

function dropTab(tab) {
  const index = S.tabs.indexOf(tab);
  if (index < 0) return null;
  if (tab.observer) tab.observer.disconnect();
  tab.holder.remove();
  tab.thumbHolder.remove();
  S.tabs.splice(index, 1);
  if (S.active === tab) {
    S.active = null; S.doc = null; S.pages = [];
    return S.tabs[index] || S.tabs[index - 1] || null;
  }
  return S.active;
}

async function closeTab(tab) {
  if (S.active === tab) await commitEditor();
  await idle();
  if (!S.tabs.includes(tab)) return;
  const state = await withBusy("Closing…", () => api(`/api/close?doc=${tab.id}`, { method: "POST" }));
  if (!state) return;
  const wasActive = S.active === tab;
  const next = dropTab(tab);
  renderTabs();
  if (wasActive) { if (next) activateTab(next); else goTo("home"); }
}

function renderTabs() {
  const strip = $("#tabstrip");
  strip.replaceChildren(...S.tabs.map(tab => {
    const isActive = tab === S.active && S.view === "editor";
    const t = el("div", "tab" + (isActive ? " active" : ""));
    t.draggable = true;
    t.insertAdjacentHTML("beforeend", icon("file"));
    const name = el("button", "tabname", t);
    name.textContent = tab.name;
    name.title = tab.name;
    name.setAttribute("role", "tab");
    name.setAttribute("aria-selected", isActive ? "true" : "false");
    name.onclick = () => activateTab(tab);
    const x = el("button", "tabclose", t);
    x.innerHTML = icon("x");
    x.title = "Close tab";
    x.setAttribute("aria-label", `Close ${tab.name}`);
    x.onclick = ev => { ev.stopPropagation(); closeTab(tab); };
    t.onauxclick = ev => { if (ev.button === 1) closeTab(tab); };
    t.ondragstart = () => { S.dragTab = tab; };
    t.ondragend = () => { S.dragTab = null; $$(".tab").forEach(n => n.classList.remove("dropbefore")); };
    t.ondragover = ev => { if (S.dragTab) { ev.preventDefault(); ev.stopPropagation(); t.classList.add("dropbefore"); } };
    t.ondragleave = () => t.classList.remove("dropbefore");
    t.ondrop = ev => {
      if (!S.dragTab) return;
      ev.preventDefault(); ev.stopPropagation();
      S.tabs.splice(S.tabs.indexOf(S.dragTab), 1);
      S.tabs.splice(S.tabs.indexOf(tab), 0, S.dragTab);
      S.dragTab = null;
      renderTabs();
    };
    if (isActive) requestAnimationFrame(() => t.scrollIntoView({ block: "nearest", inline: "nearest" }));
    return t;
  }));
}

// ------------------------------------------------------------------ views
function showView(view) {
  S.view = view;
  $("#home").hidden = view !== "home";
  $("#saved").hidden = view !== "saved";
  const editing = view === "editor";
  $("#topbar").hidden = !editing;
  $("#workspace").hidden = !editing;
  $("#statusbar").hidden = !editing;
  if (!editing) { $("#formatbar").hidden = true; $("#imagebar").hidden = true; $("#areabar").hidden = true; closeOrganizer(); }
  $("#homeBtn").classList.toggle("active", view === "home");
  $("#savedBtn").classList.toggle("active", view === "saved");
  renderTabs();
}

async function goTo(view) {
  await commitEditor();
  await idle();
  deselect();
  rememberActiveTab();
  showView(view);
  await refreshLibrary();
}

async function refreshLibrary() {
  try { S.library = (await api("/api/library")).items; }
  catch (e) { toast(e.message, true); return; }
  renderHome();
  renderSaved();
}

function whenLabel(seconds) {
  return new Date(seconds * 1000).toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
}

function libraryCard(item, manage) {
  const card = el("article", "card");
  const go = el("button", "cardopen", card);
  const img = el("img", "cardthumb", go);
  img.alt = ""; img.loading = "lazy";
  img.src = `/api/library/${item.id}/thumb.png?t=${item.time}`;
  const info = el("span", "cardinfo", go);
  el("b", "", info).textContent = item.name;
  el("span", "", info).textContent = `${item.pages} page${item.pages === 1 ? "" : "s"} · ${whenLabel(item.time)}${item.unlocked ? " · Unlocked" : ""}`;
  if (item.openTab) el("span", "", info).textContent = "Open in a tab";
  else if (!manage && item.saved) el("span", "", info).textContent = "In Saved PDFs";
  go.title = `Open ${item.name}`;
  go.onclick = () => openLibraryItem(item);
  const actions = el("div", "cardactions", card);
  const act = (label, fn, cls) => { const b = el("button", cls || "", actions); b.textContent = label; b.onclick = fn; };
  if (manage) {
    act("Download", () => downloadFrom(`/api/library/${item.id}/download`));
    act("Rename", () => renameItem(item));
    act("Delete", () => deleteItem(item), "danger");
  } else if (!item.saved) {
    act("Remove", () => deleteItem(item));
  }
  return card;
}

function renderHome() {
  const recent = S.library.slice(0, 8);
  $("#homeRecent").hidden = !recent.length;
  $("#recentGrid").replaceChildren(...recent.map(i => libraryCard(i, false)));
}

function renderSaved() {
  const all = S.library.filter(i => i.saved);
  const q = $("#savedSearch").value.trim().toLowerCase();
  const shown = all.filter(i => i.name.toLowerCase().includes(q));
  $("#savedEmpty").hidden = !!all.length;
  $("#savedNoMatch").hidden = !all.length || !!shown.length;
  $("#savedSearch").hidden = !all.length;
  $("#savedGrid").replaceChildren(...shown.map(i => libraryCard(i, true)));
}

async function openLibraryItem(item) {
  const state = await withBusy("Opening…", () => api(`/api/library/${item.id}/open`, { method: "POST" }));
  if (state) await openInTab(state);
}

async function renameItem(item) {
  const name = await ask({ title: "Rename this PDF", value: item.name.replace(/\.pdf$/i, ""), ok: "Rename" });
  if (name === null) return;
  const res = await withBusy("Renaming…", () => api(`/api/library/${item.id}/rename`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name }),
  }));
  if (!res) return;
  const tab = S.tabs.find(t => t.id === res.openTab);
  if (tab) { tab.name = res.name; renderTabs(); }
  refreshLibrary();
}

async function deleteItem(item) {
  const where = item.saved ? "Saved PDFs" : "this list";
  const also = item.openTab ? " Its open tab will close." : "";
  const sure = await ask({
    title: `Delete "${item.name}" from ${where}?`, ok: "Delete", destructive: true,
    text: `This can't be undone.${also} Copies you downloaded are not affected.`,
  });
  if (sure === null) return;
  const res = await withBusy("Deleting…", () => api(`/api/library/${item.id}`, { method: "DELETE" }));
  if (!res) return;
  const tab = S.tabs.find(t => t.id === res.closedTab);
  if (tab) dropTab(tab);
  renderTabs();
  refreshLibrary();
}

async function downloadFrom(url) {
  await withBusy("Preparing your PDF…", async () => {
    const res = await fetch(url);
    if (!res.ok) throw new Error("The PDF could not be downloaded.");
    const blob = await res.blob();
    const match = /filename\*?=(?:UTF-8'')?"?([^";]+)"?/.exec(res.headers.get("content-disposition") || "");
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = match ? decodeURIComponent(match[1]) : "edited.pdf";
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 10000);
    toast(`${a.download} (${sizeLabel(blob.size)}) is in your Downloads folder.`);
  });
}

async function openFile(file, password) {
  const form = new FormData();
  form.append("file", file);
  if (password) form.append("password", password);
  let state = null;
  try {
    busyCount++;
    $("#busyText").textContent = `Opening ${file.name}…`;
    $("#busy").hidden = false;
    state = await api("/api/open", { method: "POST", body: form });
  } catch (e) {
    if (e.status === 401 && e.body && e.body.password) askPassword(file, e.body.wrong);
    else toast(e.message, true);
  } finally {
    if (--busyCount === 0) $("#busy").hidden = true;
  }
  if (state) await openInTab(state);
}

function askPassword(file, wrong, retry = pw => openFile(file, pw)) {
  const d = $("#pwDialog");
  $("#pwTitle").textContent = `${file.name} is password protected`;
  $("#pwWrong").hidden = !wrong;
  $("#pwInput").value = "";
  d.returnValue = "";
  d.showModal();
  d.onclose = () => { if (d.returnValue === "ok" && $("#pwInput").value) retry($("#pwInput").value); };
}

// ------------------------------------------------------------------ pages
function buildPages() {
  const tab = S.active;
  const holder = tab.holder, thumbs = tab.thumbHolder;
  holder.replaceChildren(); thumbs.replaceChildren();
  if (tab.observer) tab.observer.disconnect();
  const observer = new IntersectionObserver(entries => {
    for (const e of entries) {
      const p = tab.pages[+e.target.dataset.page];
      if (!p) continue;
      p.visible = e.isIntersecting;
      if (e.isIntersecting && S.active === tab) loadPage(p);
    }
    const seen = tab.pages.filter(p => p.visible);
    if (seen.length && S.active === tab) markCurrent(seen[0].index);
  }, { root: $("#scroller"), rootMargin: "300px" });
  tab.observer = observer;

  S.pages = S.doc.pages.map((info, index) => {
    const node = el("div", "page", holder);
    node.dataset.page = index;
    const img = el("img", "", node);
    img.alt = `Page ${index + 1}`;
    const layer = el("div", "layer", node);
    const p = { index, docId: tab.id, w: info.w, h: info.h, rev: info.rev, node, img, layer, loadedRev: 0, layout: null };
    bindLayer(p);

    const t = el("div", "thumb", thumbs);
    t.tabIndex = 0;
    const ti = el("img", "", t);
    ti.loading = "lazy"; ti.alt = "";
    ti.dataset.page = index;
    p.thumb = t; p.thumbImg = ti;
    ti.src = imageUrl(p, 0.25);
    el("span", "", t).textContent = index + 1;
    const ops = el("div", "ops", t);
    [["rotate", "Rotate page", () => rotatePage(index)],
     ["plus", "Insert blank page after", () => insertPage(index)],
     ["trash", "Delete page", () => deletePage(index), "danger"]].forEach(([name, title, fn, cls]) => {
      const b = el("button", cls || "", ops);
      b.innerHTML = icon(name); b.title = title; b.setAttribute("aria-label", title);
      b.onclick = ev => { ev.stopPropagation(); fn(); };
    });
    t.onclick = () => node.scrollIntoView({ behavior: "smooth", block: "start" });
    t.onkeydown = ev => { if (ev.key === "Enter") t.click(); };
    observer.observe(node);
    return p;
  });
  tab.pages = S.pages;
  sizePages();
}

const rotatePage = index => op({ type: "rotate_page", page: index });
const insertPage = index => op({ type: "insert_page", page: index });
async function deletePage(index) {
  const sure = await ask({ title: `Delete page ${index + 1}?`, ok: "Delete", destructive: true });
  if (sure !== null) op({ type: "delete_page", page: index });
}

function sizePages() {
  S.pages.forEach(p => {
    p.node.style.width = p.w * S.zoom + "px";
    p.node.style.height = p.h * S.zoom + "px";
    if (p.layout) drawLayer(p);
  });
  $("#zoomLabel").textContent = Math.round(S.zoom * 100) + "%";
  drawHits();
}

function markCurrent(i) {
  if (S.currentPage === i && S.pages[i].thumb.classList.contains("current")) { updateStatus(); return; }
  S.currentPage = i;
  updateStatus();
  S.pages.forEach(p => p.thumb.classList.toggle("current", p.index === i));
  S.pages[i].thumb.scrollIntoView({ block: "nearest" });
}

function refreshVisible() { S.pages.forEach(p => { if (p.visible || p.loadedRev) loadPage(p); }); }

function imageUrl(p, z) { return `/api/page/${p.index}.png?doc=${p.docId}&z=${z.toFixed(2)}&r=${p.rev}`; }

function loadPage(p) {
  const scale = Math.min(4, S.zoom * Math.max(window.devicePixelRatio || 1, 1.5));
  const key = `${p.rev}@${scale.toFixed(2)}`;
  if (p.loadedKey === key) return Promise.resolve();
  if (p.loadingKey === key) return p.loadPromise;
  p.loadingKey = key;
  p.loadPromise = fetchPage(p, key, scale);
  return p.loadPromise;
}

async function fetchPage(p, key, scale) {
  try {
    const url = imageUrl(p, scale);
    const pre = new Image();
    pre.src = url;
    const [layout] = await Promise.all([
      api(`/api/page/${p.index}/layout?doc=${p.docId}`), pre.decode().catch(() => {}),
    ]);
    if (p.loadingKey !== key) return;
    p.img.src = url;
    p.layout = layout;
    p.loadedKey = key;
    p.loadedRev = p.rev;
    p.pixels = null;
    drawLayer(p);
  } catch (e) {
    if (S.tabs.some(t => t.id === p.docId)) toast(e.message, true); // a closed tab needs no message
  }
  finally { if (p.loadingKey === key) p.loadingKey = null; }
}

function place(node, r) {
  const z = S.zoom;
  node.style.left = r.x0 * z + "px"; node.style.top = r.y0 * z + "px";
  node.style.width = (r.x1 - r.x0) * z + "px"; node.style.height = (r.y1 - r.y0) * z + "px";
}

function drawLayer(p) {
  p.layer.replaceChildren();
  const L = p.layout;
  $$(".pagechip", p.node).forEach(n => n.remove());
  L.images.forEach(img => {
    if (img.background) return;
    const n = el("div", "i", p.layer);
    n.dataset.id = img.id;
    place(n, img);
    n.onpointerdown = ev => {
      if (S.tool !== "edit") return;
      ev.stopPropagation();
      commitEditor().then(() => selectImage(p, img, n, ev));
    };
  });
  L.paras.forEach(para => {
    const n = el("div", para.rule ? "t rule" : "t", p.layer);
    place(n, { x0: para.x0, y0: para.y0, x1: para.x1, y1: para.y1 });
    n.title = "Click to edit";
    n.onpointerdown = ev => { if (S.tool === "edit") { ev.stopPropagation(); } };
    n.onclick = ev => {
      if (S.tool !== "edit") return;
      ev.stopPropagation();
      commitEditor().then(() => {
        // The page may have been rebuilt by the commit: find this paragraph afresh.
        const fresh = p.layout.paras.find(q => q.id === para.id);
        if (fresh && JSON.stringify(fresh.runs) === JSON.stringify(para.runs)) openEditor(p, fresh);
      });
    };
  });
  L.widgets.forEach(w => drawWidget(p, w));
  if (!L.paras.length || (L.images.some(i => i.background) && !L.paras.some(q => q.raster))) {
    const chip = el("button", "pagechip", p.node);
    chip.textContent = "Make scanned text editable";
    chip.title = "Reads the words in this page's picture so you can edit them";
    chip.onclick = async ev => {
      ev.stopPropagation();
      await op({ type: "ocr", page: p.index }, "Reading the page… this can take a moment");
    };
  }
}

function drawWidget(p, w) {
  const z = S.zoom;
  if (w.kind === "check" || w.kind === "radio") {
    const n = el("button", "w toggle", p.layer);
    place(n, w);
    n.setAttribute("aria-label", "Toggle form field");
    n.setAttribute("aria-pressed", w.value ? "true" : "false");
    n.onclick = () => op({ type: "set_form", page: p.index, xref: w.xref, value: !w.value });
    return;
  }
  let n;
  if (w.kind === "choice") {
    n = el("select", "w", p.layer);
    w.options.forEach(o => { const opt = el("option", "", n); opt.textContent = o; opt.value = o; });
  } else {
    n = el(w.multiline ? "textarea" : "input", "w", p.layer);
  }
  place(n, w);
  n.value = w.value || "";
  n.setAttribute("aria-label", "Form field");
  n.style.fontSize = Math.max(9, (w.size || Math.min(12, (w.y1 - w.y0) * 0.7)) * z) + "px";
  n.onchange = () => op({ type: "set_form", page: p.index, xref: w.xref, value: n.value });
}

// ------------------------------------------------------------- page clicks
function pagePoint(p, ev) {
  const r = p.node.getBoundingClientRect();
  return { x: (ev.clientX - r.left) / S.zoom, y: (ev.clientY - r.top) / S.zoom };
}

function bindLayer(p) {
  p.layer.onpointerdown = ev => {
    if (ev.button !== 0) return;
    if (S.editor) { commitEditor(); return; }
    deselect();
    if (S.tool === "place") { ev.preventDefault(); placeStamp(p, pagePoint(p, ev)); return; }
    if (S.tool === "text" || S.tool === "erase" || S.tool === "select") startMarquee(p, ev);
  };
}

function startMarquee(p, ev) {
  ev.preventDefault();
  const a = pagePoint(p, ev);
  const box = el("div", "marquee" + (S.tool === "text" ? " add" : ""), p.node);
  let b = a;
  const draw = () => place(box, rectOf(a, b));
  draw();
  const move = e => { b = pagePoint(p, e); draw(); };
  const up = () => {
    window.removeEventListener("pointermove", move);
    window.removeEventListener("pointerup", up);
    box.remove();
    const r = rectOf(a, b);
    if (S.tool === "text") {
      const wide = r.x1 - r.x0 > 20;
      openNewText(p, r.x0, r.y0, wide ? r.x1 - r.x0 : null);
    } else if (r.x1 - r.x0 > 3 && r.y1 - r.y0 > 3) {
      if (S.tool === "select") startArea(p, r);
      else op({ type: "erase", page: p.index, rect: [r.x0, r.y0, r.x1, r.y1] }, "Erasing…");
    }
  };
  window.addEventListener("pointermove", move);
  window.addEventListener("pointerup", up);
}

const rectOf = (a, b) => ({
  x0: Math.min(a.x, b.x), y0: Math.min(a.y, b.y), x1: Math.max(a.x, b.x), y1: Math.max(a.y, b.y),
});

// ------------------------------------------------------------ text editing
function cssFamily(name) { return `"${name}", Arial, sans-serif`; }

function backgroundAt(p, para) {
  // Hide the rendered text under the editor using the colour right next to it.
  try {
    if (!p.pixels) {
      const c = document.createElement("canvas");
      c.width = p.img.naturalWidth; c.height = p.img.naturalHeight;
      const ctx = c.getContext("2d", { willReadFrequently: true });
      ctx.drawImage(p.img, 0, 0);
      p.pixels = ctx;
    }
    const k = p.img.naturalWidth / p.w;
    const spots = [[para.x0 - 2, para.y0 - 1], [para.x1 + 2, para.y0 - 1], [para.x0 - 2, para.y1 + 1], [para.x1 + 2, para.y1 + 1]];
    const seen = spots.map(([x, y]) => {
      const d = p.pixels.getImageData(Math.max(0, x * k), Math.max(0, y * k), 1, 1).data;
      return [d[0], d[1], d[2]];
    });
    const mid = i => seen.map(s => s[i]).sort((a, b) => a - b)[1];
    return `rgb(${mid(0)},${mid(1)},${mid(2)})`;
  } catch { return "#fff"; }
}

function runSpan(run) {
  const s = document.createElement("span");
  s.textContent = run.text;
  s.dataset.font = run.font || run.family || "Arial";
  s.dataset.family = run.family || "Arial";
  s.dataset.xref = run.xref || 0;
  s.dataset.exact = run.exact === false ? "0" : "1";
  s.dataset.bold = run.bold ? "1" : "0";
  s.dataset.italic = run.italic ? "1" : "0";
  s.dataset.sup = run.sup ? "1" : "0";
  s.style.fontFamily = cssFamily(run.family || "Arial");
  s.style.fontSize = run.size * S.zoom + "px";
  s.style.fontWeight = run.bold ? "700" : "400";
  s.style.fontStyle = run.italic ? "italic" : "normal";
  s.style.color = run.color || "#000";
  return s;
}

function buildEditor(p, opts) {
  const z = S.zoom;
  const wrap = el("div", "editwrap", p.node);
  wrap.style.left = opts.x * z + "px";
  wrap.style.top = opts.y * z + "px";
  const grip = el("div", "grip", wrap);
  grip.title = "Drag to move";
  const ed = el("div", "editor", wrap);
  ed.contentEditable = "true";
  ed.spellcheck = true;
  ed.setAttribute("role", "textbox");
  ed.setAttribute("aria-multiline", "true");
  if (opts.width) ed.style.width = opts.width * z + "px"; else ed.classList.add("nowrap");
  ed.style.lineHeight = opts.lineHeight * z + "px";
  ed.style.background = opts.background || "transparent";
  ed.style.textAlign = opts.align || "left";
  if (opts.firstIndent) ed.style.textIndent = opts.firstIndent * z + "px";
  opts.runs.forEach(r => ed.appendChild(runSpan(r)));
  S.editor = { ...opts, p, wrap, ed, dx: 0, dy: 0, align: opts.align || "left", startAlign: opts.align || "left" };
  S.editor.initial = JSON.stringify(serialize());
  bindEditor();
  $("#formatbar").hidden = false;
  syncFormatBar();
  $$(".fmt[data-align]").forEach(b => b.classList.toggle("on", b.dataset.align === S.editor.align));
  return ed;
}

function openEditor(p, para) {
  if (para.angle) {
    // Vertical or slanted text is typed level, then written back at its own angle.
    para = { ...para, wrap: false, width: null };
  }
  const ed = buildEditor(p, {
    id: para.id, x: para.angle ? para.x0 : para.left, y: para.y0,
    width: para.wrap ? para.width : null, lineHeight: para.lineHeight, align: para.align,
    firstIndent: para.firstIndent, runs: para.runs, background: backgroundAt(p, para),
  });
  ed.focus();
  const sel = getSelection();
  sel.selectAllChildren(ed);
  sel.collapseToEnd();
  saveRange();
}

function openNewText(p, x, y, width, text) {
  const size = S.lastSize || 14;
  const family = S.lastFamily || (S.fonts.includes("Arial") ? "Arial" : S.fonts[0]);
  const ed = buildEditor(p, {
    id: null, x, y, width, lineHeight: size * 1.2, align: "left", background: "transparent",
    runs: [{ text: text || "​", font: family, family, xref: 0, size, color: S.lastColor || "#000000", bold: false, italic: false }],
  });
  if (!text) ed.classList.add("empty-hint");
  ed.addEventListener("input", () => ed.classList.toggle("empty-hint", !ed.textContent.replace(/​/g, "")), { passive: true });
  ed.focus();
  const sel = getSelection();
  sel.selectAllChildren(ed);
  sel.collapseToEnd();
  saveRange();
}

function bindEditor() {
  const { ed, wrap } = S.editor;
  ed.addEventListener("keydown", ev => {
    if (ev.key === "Enter" && (ev.ctrlKey || ev.metaKey)) { ev.preventDefault(); commitEditor(); }
    else if (ev.key === "Enter") { ev.preventDefault(); document.execCommand("insertLineBreak"); }
    else if (ev.key === "Escape") { ev.preventDefault(); closeEditor(); }
    else if ((ev.ctrlKey || ev.metaKey) && ev.key.toLowerCase() === "b") { ev.preventDefault(); toggle("fontWeight"); }
    else if ((ev.ctrlKey || ev.metaKey) && ev.key.toLowerCase() === "i") { ev.preventDefault(); toggle("fontStyle"); }
    ev.stopPropagation();
  });
  ed.addEventListener("paste", ev => {
    ev.preventDefault();
    document.execCommand("insertText", false, (ev.clipboardData || window.clipboardData).getData("text/plain"));
  });
  ed.addEventListener("pointerdown", ev => ev.stopPropagation());
  ed.addEventListener("keyup", () => { saveRange(); syncFormatBar(); });
  ed.addEventListener("pointerup", () => { saveRange(); syncFormatBar(); });
  const grip = $(".grip", wrap);
  grip.onpointerdown = ev => {
    ev.preventDefault(); ev.stopPropagation();
    const start = { x: ev.clientX, y: ev.clientY, dx: S.editor.dx, dy: S.editor.dy };
    const left = parseFloat(wrap.style.left), top = parseFloat(wrap.style.top);
    const move = e => {
      const ox = e.clientX - start.x, oy = e.clientY - start.y;
      wrap.style.left = left + ox + "px"; wrap.style.top = top + oy + "px";
      S.editor.dx = start.dx + ox / S.zoom; S.editor.dy = start.dy + oy / S.zoom;
      // Once moved, the patch that hid the original text would travel with it.
      S.editor.ed.style.background = "transparent";
    };
    const up = () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
  };
}

function saveRange() {
  const sel = getSelection();
  if (S.editor && sel.rangeCount && S.editor.ed.contains(sel.anchorNode)) S.savedRange = sel.getRangeAt(0).cloneRange();
}

function selectionRange() {
  const r = S.savedRange;
  return r && !r.collapsed && S.editor && S.editor.ed.contains(r.commonAncestorContainer) ? r : null;
}

function applyStyle(prop, value) {
  if (!S.editor) return;
  const range = selectionRange();
  if (!range) {
    $$("*", S.editor.ed).forEach(n => { n.style && (n.style[prop] = ""); });
    $$("span[data-font]", S.editor.ed).forEach(n => { n.style[prop] = value; });
  } else {
    const frag = range.extractContents();
    $$("*", frag).forEach(n => { n.style[prop] = ""; });
    const w = document.createElement("span");
    w.style[prop] = value;
    w.appendChild(frag);
    range.insertNode(w);
    const sel = getSelection();
    const nr = document.createRange();
    nr.selectNodeContents(w);
    sel.removeAllRanges(); sel.addRange(nr);
    S.savedRange = nr.cloneRange();
  }
  S.editor.ed.focus({ preventScroll: true });
  syncFormatBar();
}

function toggle(prop) {
  const node = focusNode();
  if (!node) return;
  const cs = getComputedStyle(node);
  if (prop === "fontWeight") applyStyle(prop, parseInt(cs.fontWeight, 10) >= 600 ? "400" : "700");
  else applyStyle(prop, cs.fontStyle === "italic" ? "normal" : "italic");
}

function focusNode() {
  if (!S.editor) return null;
  const r = S.savedRange;
  let n = r && S.editor.ed.contains(r.startContainer) ? r.startContainer : null;
  if (n && n.nodeType === 3) n = n.parentElement;
  if (!n || n === S.editor.ed) n = $("span[data-font]", S.editor.ed) || S.editor.ed;
  return n;
}

function rgbToHex(c) {
  const m = c.match(/\d+(\.\d+)?/g) || [0, 0, 0];
  return "#" + m.slice(0, 3).map(v => (+v | 0).toString(16).padStart(2, "0")).join("");
}

function syncFormatBar() {
  const n = focusNode();
  if (!n) return;
  const cs = getComputedStyle(n);
  const family = cs.fontFamily.split(",")[0].replace(/["']/g, "").trim();
  const sel = $("#fFamily");
  if (![...sel.options].some(o => o.value === family)) {
    const o = document.createElement("option");
    o.value = family; o.textContent = family;
    sel.prepend(o);
  }
  sel.value = family;
  if (document.activeElement !== $("#fSize")) {
    $("#fSize").value = Math.round(parseFloat(cs.fontSize) / S.zoom * 10) / 10;
  }
  $("#fBold").classList.toggle("on", parseInt(cs.fontWeight, 10) >= 600);
  $("#fItalic").classList.toggle("on", cs.fontStyle === "italic");
  $("#fColor").value = rgbToHex(cs.color);
}

function serialize() {
  const { ed } = S.editor;
  const runs = [];
  const add = (text, style) => {
    if (!text) return;
    const last = runs[runs.length - 1];
    if (last && JSON.stringify({ ...last, text: "" }) === JSON.stringify({ ...style, text: "" })) last.text += text;
    else runs.push({ ...style, text });
  };
  let lastStyle = null;
  const styleOf = parent => {
    const cs = getComputedStyle(parent);
    const host = parent.closest("span[data-font]");
    const d = host ? host.dataset : {};
    const family = cs.fontFamily.split(",")[0].replace(/["']/g, "").trim();
    const bold = parseInt(cs.fontWeight, 10) >= 600;
    const italic = cs.fontStyle === "italic";
    const same = family === d.family && bold === (d.bold === "1") && italic === (d.italic === "1");
    return {
      font: same ? d.font : family, family,
      xref: same ? +(d.xref || 0) : 0,
      exact: same ? d.exact !== "0" : true,
      size: Math.round(parseFloat(cs.fontSize) / S.zoom * 100) / 100,
      color: rgbToHex(cs.color), bold, italic, sup: d.sup === "1",
    };
  };
  const walk = node => {
    for (const child of node.childNodes) {
      if (child.nodeType === 3) {
        lastStyle = styleOf(child.parentElement);
        add(child.nodeValue.replace(/​/g, ""), lastStyle);
      } else if (child.nodeName === "BR") {
        add("\n", lastStyle || styleOf(child.parentElement));
      } else if (child.nodeType === 1) {
        const block = /^(DIV|P)$/.test(child.nodeName);
        if (block && runs.length) add("\n", lastStyle || styleOf(child));
        walk(child);
      }
    }
  };
  walk(ed);
  return runs;
}

let committing = null;
function commitEditor() {
  if (!S.editor) return committing || Promise.resolve();
  const e = S.editor;
  const runs = serialize();
  const text = runs.map(r => r.text).join("");
  // New text counts even when untouched, so a placed date is kept as it is.
  const unchanged = e.id !== null && JSON.stringify(runs) === e.initial && !e.dx && !e.dy && e.align === e.startAlign;
  closeEditor();
  if (e.id === null && runs[0]) { S.lastSize = runs[0].size; S.lastFamily = runs[0].family; S.lastColor = runs[0].color; }
  if (unchanged) return Promise.resolve();
  if (e.id === null) {
    if (!text.trim()) return Promise.resolve();
    committing = op({
      type: "add_text", page: e.p.index, x: e.x + e.dx, y: e.y + e.dy,
      width: e.width, runs, align: e.align,
    }).finally(() => { committing = null; }).then(() => loadPage(e.p));
  } else {
    committing = op({
      type: "replace_text", page: e.p.index, id: e.id, runs: text.trim() ? runs : [],
      align: e.align, dx: e.dx, dy: e.dy,
    }).finally(() => { committing = null; }).then(() => loadPage(e.p));
  }
  if (S.tool === "text") setTool("edit");
  return committing;
}

function closeEditor() {
  if (!S.editor) return;
  S.editor.wrap.remove();
  S.editor = null;
  S.savedRange = null;
  $("#formatbar").hidden = true;
}

// ------------------------------------------------------------- select area
function startArea(p, rect) {
  cancelArea();
  const a = { src: p, rect, dst: p, x: rect.x0, y: rect.y0 };
  a.outline = el("div", "areasrc", p.node);
  place(a.outline, rect);
  a.ghost = el("div", "areaghost", p.node);
  a.ghost.title = "Drag to position";
  a.ghost.setAttribute("aria-label", "Selected area. Drag to position it, or use the arrow keys.");
  a.ghost.tabIndex = 0;
  const z = S.zoom;
  a.ghost.style.backgroundImage = `url("${p.img.src}")`;
  a.ghost.style.backgroundSize = `${p.w * z}px ${p.h * z}px`;
  a.ghost.style.backgroundPosition = `${-rect.x0 * z}px ${-rect.y0 * z}px`;
  a.ghost.onpointerdown = dragArea;
  S.area = a;
  $("#aPage").replaceChildren(...S.pages.map(q => {
    const o = document.createElement("option");
    o.value = q.index; o.textContent = q.index + 1;
    return o;
  }));
  $("#aPage").value = p.index;
  placeGhost();
  $("#areabar").hidden = false;
}

function placeGhost() {
  const a = S.area;
  const w = a.rect.x1 - a.rect.x0, h = a.rect.y1 - a.rect.y0;
  a.x = Math.max(0, Math.min(a.dst.w - w, a.x));
  a.y = Math.max(0, Math.min(a.dst.h - h, a.y));
  if (a.ghost.parentNode !== a.dst.node) a.dst.node.appendChild(a.ghost);
  place(a.ghost, { x0: a.x, y0: a.y, x1: a.x + w, y1: a.y + h });
  $("#aPage").value = a.dst.index;
}

function cancelArea() {
  if (!S.area) return;
  S.area.outline.remove();
  S.area.ghost.remove();
  S.area = null;
  $("#areabar").hidden = true;
}

function dragArea(ev) {
  ev.preventDefault();
  ev.stopPropagation();
  const a = S.area;
  const box = a.ghost.getBoundingClientRect();
  const grab = { x: (ev.clientX - box.left) / S.zoom, y: (ev.clientY - box.top) / S.zoom };
  a.ghost.style.pointerEvents = "none"; // lets the page under the pointer be found
  const move = e => {
    const over = document.elementsFromPoint(e.clientX, e.clientY).find(n => n.classList && n.classList.contains("page"));
    if (over) a.dst = S.pages[+over.dataset.page];
    const pt = pagePoint(a.dst, e);
    a.x = pt.x - grab.x;
    a.y = pt.y - grab.y;
    placeGhost();
  };
  const up = () => {
    a.ghost.style.pointerEvents = "";
    window.removeEventListener("pointermove", move);
    window.removeEventListener("pointerup", up);
  };
  window.addEventListener("pointermove", move);
  window.addEventListener("pointerup", up);
}

function alignArea(ax, ay) {
  const a = S.area;
  if (!a) return;
  const margin = 36; // half an inch from the edge of the paper
  const w = a.rect.x1 - a.rect.x0, h = a.rect.y1 - a.rect.y0;
  if (ax === "left") a.x = margin;
  if (ax === "center") a.x = (a.dst.w - w) / 2;
  if (ax === "right") a.x = a.dst.w - margin - w;
  if (ay === "top") a.y = margin;
  if (ay === "middle") a.y = (a.dst.h - h) / 2;
  if (ay === "bottom") a.y = a.dst.h - margin - h;
  placeGhost();
}

async function applyArea(move) {
  const a = S.area;
  if (!a) return;
  const unmoved = a.dst === a.src && Math.abs(a.x - a.rect.x0) < 0.5 && Math.abs(a.y - a.rect.y0) < 0.5;
  if (unmoved) { toast("Drag the box or choose a page or alignment first."); return; }
  const w = a.rect.x1 - a.rect.x0, h = a.rect.y1 - a.rect.y0;
  const dst = a.dst, x = a.x, y = a.y;
  const state = await op({
    type: "area", page: a.src.index, rect: [a.rect.x0, a.rect.y0, a.rect.x1, a.rect.y1],
    toPage: dst.index, x, y, move,
  }, move ? "Moving…" : "Copying…");
  if (!state) return;
  dst.node.scrollIntoView({ block: "nearest" });
  // Page numbers are unchanged by this, so keep the result selected for further adjusting.
  const page = S.pages[dst.index];
  await loadPage(page);
  if (S.tool === "select") startArea(page, { x0: x, y0: y, x1: x + w, y1: y + h });
}

// ------------------------------------------------------------------ images
function deselect() {
  cancelArea();
  if (!S.selected) return;
  const n = S.selected.node;
  if (n && n.isConnected) { n.classList.remove("sel"); n.replaceChildren(); place(n, S.selected.img); }
  S.selected = null;
  $("#imagebar").hidden = true;
}

function selectImage(p, img, node, ev) {
  deselect();
  S.selected = { p, img, node };
  node.classList.add("sel");
  ["nw", "n", "ne", "e", "se", "s", "sw", "w"].forEach(h => {
    const d = el("div", "h " + h, node);
    d.onpointerdown = e => { e.stopPropagation(); dragImage(e, h); };
  });
  $("#imagebar").hidden = false;
  syncImageBar(img);
  if (ev) dragImage(ev, null);
}

const CM = 72 / 2.54;
const round1 = v => Math.round(v * 10) / 10;

function syncImageBar(r) {
  $("#iWidth").value = round1((r.x1 - r.x0) / CM);
  $("#iHeight").value = round1((r.y1 - r.y0) / CM);
}

// Selects the image at rect after the page refreshes, so it can be adjusted further.
async function reselect(p, rect) {
  await loadPage(p);
  const images = p.layout.images.filter(i => !i.background);
  if (!images.length) return;
  const score = i => Math.abs(i.x0 - rect[0]) + Math.abs(i.y0 - rect[1]) + Math.abs(i.x1 - rect[2]) + Math.abs(i.y1 - rect[3]);
  const best = rect ? images.reduce((a, b) => (score(b) < score(a) ? b : a)) : images[images.length - 1];
  const node = $(`.i[data-id="${best.id}"]`, p.layer);
  if (node) selectImage(p, best, node);
}

async function setImageRect(rect) {
  const sel = S.selected;
  if (!sel) return;
  const state = await op({ type: "move_image", page: sel.p.index, id: sel.img.id, rect });
  await reselect(sel.p, state ? rect : [sel.img.x0, sel.img.y0, sel.img.x1, sel.img.y1]);
}

function resizeSelected(changed) {
  const sel = S.selected;
  if (!sel) return;
  const { img } = sel;
  const oldW = img.x1 - img.x0, oldH = img.y1 - img.y0;
  let w = parseFloat($("#iWidth").value) * CM, h = parseFloat($("#iHeight").value) * CM;
  if (!(w > 0) || !(h > 0)) { syncImageBar(img); return; }
  if ($("#iLock").checked) {
    if (changed === "w") h = w * oldH / oldW; else w = h * oldW / oldH;
  }
  // The box grows from its centre, so a resized photo stays where it was.
  const cx = (img.x0 + img.x1) / 2, cy = (img.y0 + img.y1) / 2;
  setImageRect([cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2]);
}

function alignSelected(ax, ay) {
  const sel = S.selected;
  if (!sel) return;
  const { img, p } = sel;
  const margin = 36; // half an inch, so a photo doesn't touch the edge of the paper
  const w = img.x1 - img.x0, h = img.y1 - img.y0;
  let x = img.x0, y = img.y0;
  if (ax === "left") x = margin;
  if (ax === "center") x = (p.w - w) / 2;
  if (ax === "right") x = p.w - margin - w;
  if (ay === "top") y = margin;
  if (ay === "middle") y = (p.h - h) / 2;
  if (ay === "bottom") y = p.h - margin - h;
  setImageRect([x, y, x + w, y + h]);
}

function dragImage(ev, handle) {
  const sel = S.selected;
  if (!sel) return;
  ev.preventDefault();
  const { node, img, p } = sel;
  const start = { x: ev.clientX, y: ev.clientY };
  const r0 = { x0: img.x0, y0: img.y0, x1: img.x1, y1: img.y1 };
  const ratio = (r0.x1 - r0.x0) / (r0.y1 - r0.y0);
  let r = { ...r0 };
  const move = e => {
    const dx = (e.clientX - start.x) / S.zoom, dy = (e.clientY - start.y) / S.zoom;
    r = { ...r0 };
    if (!handle) { r.x0 += dx; r.x1 += dx; r.y0 += dy; r.y1 += dy; }
    else {
      if (handle.includes("w")) r.x0 += dx;
      if (handle.includes("e")) r.x1 += dx;
      if (handle.includes("n")) r.y0 += dy;
      if (handle.includes("s")) r.y1 += dy;
      if (handle.length === 2) { // corners keep the proportions
        const w = Math.max(8, r.x1 - r.x0), h = w / ratio;
        if (handle.includes("w")) r.x0 = r.x1 - w; else r.x1 = r.x0 + w;
        if (handle.includes("n")) r.y0 = r.y1 - h; else r.y1 = r.y0 + h;
      }
      if (r.x1 - r.x0 < 8) r.x1 = r.x0 + 8;
      if (r.y1 - r.y0 < 8) r.y1 = r.y0 + 8;
    }
    place(node, r);
    syncImageBar(r);
  };
  const up = () => {
    window.removeEventListener("pointermove", move);
    window.removeEventListener("pointerup", up);
    const moved = Math.abs(r.x0 - r0.x0) + Math.abs(r.y0 - r0.y0) + Math.abs(r.x1 - r0.x1) + Math.abs(r.y1 - r0.y1);
    if (moved > 0.5) setImageRect([r.x0, r.y0, r.x1, r.y1]);
  };
  window.addEventListener("pointermove", move);
  window.addEventListener("pointerup", up);
}

async function toPdfImage(file) {
  // Everything the browser can open becomes a PNG or JPEG, which PDFs store natively.
  const bitmap = await createImageBitmap(file).catch(() => null);
  if (!bitmap) throw new Error("That image format can't be opened. Please use JPG, PNG or WebP.");
  const size = { w: bitmap.width, h: bitmap.height };
  if (/^image\/(png|jpeg)$/.test(file.type)) return { blob: file, ...size };
  const c = document.createElement("canvas");
  c.width = size.w; c.height = size.h;
  c.getContext("2d").drawImage(bitmap, 0, 0);
  const blob = await new Promise(r => c.toBlob(r, "image/png"));
  return { blob, ...size };
}

async function addImageFile(file, page, point) {
  let info;
  try { info = await toPdfImage(file); } catch (e) { toast(e.message, true); return; }
  const form = new FormData();
  form.append("file", info.blob, "image");
  const replacing = !!S.replaceImage;
  if (S.replaceImage) {
    form.append("page", S.replaceImage.page);
    form.append("id", S.replaceImage.id);
    page = S.pages[S.replaceImage.page];
    S.replaceImage = null;
  } else {
    page = page || S.pages[S.currentPage];
    const maxW = page.w * 0.5, maxH = page.h * 0.5;
    const k = Math.min(1, maxW / info.w, maxH / info.h);
    const w = info.w * k, h = info.h * k;
    const cx = point ? point.x : page.w / 2, cy = point ? point.y : page.h / 3;
    const x0 = Math.max(0, Math.min(page.w - w, cx - w / 2));
    const y0 = Math.max(0, Math.min(page.h - h, cy - h / 2));
    form.append("page", page.index);
    [["x0", x0], ["y0", y0], ["x1", x0 + w], ["y1", y0 + h]].forEach(([k2, v]) => form.append(k2, v));
  }
  await withBusy("Adding photo…", async () => {
    applyState(await api(`/api/image?${docParam()}`, { method: "POST", body: form }));
    setTool("edit");
    await reselect(page, null);
    if (!replacing) page.node.scrollIntoView({ behavior: "smooth", block: "nearest" });
  });
}

// ------------------------------------------------------------------ actions
function setTool(tool) {
  S.tool = tool;
  $$(".tool[data-tool]").forEach(b => {
    b.classList.toggle("active", b.dataset.tool === tool);
    b.setAttribute("aria-pressed", b.dataset.tool === tool ? "true" : "false");
  });
  document.body.classList.toggle("tool-text", tool === "text");
  document.body.classList.toggle("tool-erase", tool === "erase");
  document.body.classList.toggle("tool-select", tool === "select");
  document.body.classList.toggle("tool-place", tool === "place");
  if (tool !== "place") S.stamp = null;
  deselect();
  updateStatus();
}

function setZoom(z) {
  commitEditor();
  cancelArea();
  S.zoom = Math.max(0.3, Math.min(4, z));
  sizePages();
  refreshVisible();
}

async function save() {
  if (S.view !== "editor" || !S.active) return;
  await commitEditor();
  const state = await withBusy("Saving…", () => api(`/api/save?${docParam()}`, { method: "POST" }));
  if (!state) return;
  applyState(state);
  toast("Saved. Find it any time under Saved PDFs. Later changes are kept automatically.");
}

const sizeLabel = n => n < 1024 * 1024 ? `${Math.max(1, Math.round(n / 1024))} KB` : `${(n / 1024 / 1024).toFixed(1)} MB`;

async function download() {
  await commitEditor();
  const d = $("#exportDialog");
  let kind = "standard";
  try { kind = localStorage.getItem("pdfeditor-export") || "standard"; } catch {}
  $$('input[name="exp"]', d).forEach(r => { r.checked = r.value === kind; });
  d.returnValue = "";
  d.showModal();
  d.onclose = () => {
    if (d.returnValue !== "ok") return;
    const choice = $('input[name="exp"]:checked', d).value;
    try { localStorage.setItem("pdfeditor-export", choice); } catch {}
    downloadFrom(`/api/export?${docParam()}${choice === "compressed" ? "&compress=1" : ""}`);
  };
}

// ------------------------------------------------------------------ wiring
const keepSelection = e => e.preventDefault();
$$("#formatbar button").forEach(b => b.addEventListener("mousedown", keepSelection));

$("#newTabBtn").onclick = $("#landingOpen").onclick = () => $("#filePdf").click();
$("#homeBtn").onclick = () => goTo("home");
$("#savedBtn").onclick = $("#seeSaved").onclick = () => goTo("saved");
$("#savedSearch").oninput = renderSaved;
$("#filePdf").onchange = async e => {
  const files = [...e.target.files]; e.target.value = "";
  for (const f of files) await openFile(f);
};
$("#imageBtn").onclick = () => { commitEditor(); setTool("edit"); S.replaceImage = null; $("#fileImg").click(); };
$("#iWidth").onchange = () => resizeSelected("w");
$("#iHeight").onchange = () => resizeSelected("h");
$$("#imagebar [data-ax]").forEach(b => { b.onclick = () => alignSelected(b.dataset.ax, null); });
$$("#imagebar [data-ay]").forEach(b => { b.onclick = () => alignSelected(null, b.dataset.ay); });
$("#iReplace").onclick = () => {
  if (!S.selected) return;
  S.replaceImage = { page: S.selected.p.index, id: S.selected.img.id };
  $("#fileImg").click();
};
$("#iDelete").onclick = () => {
  if (S.selected) op({ type: "delete_image", page: S.selected.p.index, id: S.selected.img.id });
};
$("#iDone").onclick = () => deselect();
$("#aCancel").onclick = () => cancelArea();
$("#aMove").onclick = () => applyArea(true);
$("#aCopy").onclick = () => applyArea(false);
$("#aPage").onchange = e => {
  if (!S.area) return;
  S.area.dst = S.pages[+e.target.value];
  placeGhost();
  S.area.dst.node.scrollIntoView({ block: "nearest" });
};
$$("#areabar [data-aax]").forEach(b => { b.onclick = () => alignArea(b.dataset.aax, null); });
$$("#areabar [data-aay]").forEach(b => { b.onclick = () => alignArea(null, b.dataset.aay); });
$("#fileImg").onchange = e => {
  const f = e.target.files[0]; e.target.value = "";
  if (f) addImageFile(f);
  else S.replaceImage = null;
};
$$(".tool[data-tool]").forEach(b => { b.onclick = () => { commitEditor(); setTool(b.dataset.tool); }; });
$("#undoBtn").onclick = () => { closeEditor(); op({ type: "undo" }, "Undoing…"); };
$("#redoBtn").onclick = () => { closeEditor(); op({ type: "redo" }, "Redoing…"); };
$("#zoomIn").onclick = () => setZoom(S.zoom * 1.2);
$("#zoomOut").onclick = () => setZoom(S.zoom / 1.2);
$("#zoomLabel").onclick = () => setZoom(fitZoom(S.pages));
$("#saveBtn").onclick = save;
$("#downloadBtn").onclick = download;

$("#fFamily").onchange = e => applyStyle("fontFamily", cssFamily(e.target.value));
$("#fSize").onchange = e => { const v = parseFloat(e.target.value); if (v > 0) applyStyle("fontSize", v * S.zoom + "px"); };
$("#fColor").oninput = e => applyStyle("color", e.target.value);
$("#fBold").onclick = () => toggle("fontWeight");
$("#fItalic").onclick = () => toggle("fontStyle");
$$(".fmt[data-align]").forEach(b => {
  b.onclick = () => {
    if (!S.editor) return;
    S.editor.align = b.dataset.align;
    S.editor.ed.style.textAlign = b.dataset.align;
    if (!S.editor.width) {
      S.editor.width = S.editor.ed.getBoundingClientRect().width / S.zoom;
      S.editor.ed.style.width = S.editor.width * S.zoom + "px";
      S.editor.ed.classList.remove("nowrap");
    }
    $$(".fmt[data-align]").forEach(x => x.classList.toggle("on", x === b));
  };
});
$("#fDone").onclick = () => commitEditor();
$("#fCancel").onclick = () => closeEditor();
$("#fDelete").onclick = () => {
  if (!S.editor) return;
  const e = S.editor;
  closeEditor();
  if (e.id !== null) op({ type: "replace_text", page: e.p.index, id: e.id, runs: [] }).then(() => loadPage(e.p));
};
document.addEventListener("selectionchange", () => { if (S.editor && document.activeElement === S.editor.ed) saveRange(); });

window.addEventListener("keydown", ev => {
  const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName) || S.editor;
  const ctrl = ev.ctrlKey || ev.metaKey;
  if (ctrl && ev.key.toLowerCase() === "s") { ev.preventDefault(); save(); return; }
  if ($("dialog[open]") || typing || !S.doc || S.view !== "editor") return;
  const k = ev.key.toLowerCase();
  if (S.area && ev.key.startsWith("Arrow")) {
    ev.preventDefault();
    const step = ev.shiftKey ? 10 : 1;
    if (ev.key === "ArrowLeft") S.area.x -= step;
    if (ev.key === "ArrowRight") S.area.x += step;
    if (ev.key === "ArrowUp") S.area.y -= step;
    if (ev.key === "ArrowDown") S.area.y += step;
    placeGhost();
    return;
  }
  if (ctrl && k === "z") { ev.preventDefault(); if (ev.shiftKey) $("#redoBtn").click(); else $("#undoBtn").click(); }
  else if (ctrl && k === "y") { ev.preventDefault(); $("#redoBtn").click(); }
  else if ((ev.key === "Delete" || ev.key === "Backspace") && S.selected) {
    ev.preventDefault();
    op({ type: "delete_image", page: S.selected.p.index, id: S.selected.img.id });
  }
  else if (ev.key === "Escape" && !$("#organizer").hidden) closeOrganizer();
  else if (ev.key === "Escape") { deselect(); setTool("edit"); }
  else if (!ctrl && k === "v") setTool("edit");
  else if (!ctrl && k === "t") setTool("text");
  else if (!ctrl && k === "e") setTool("erase");
  else if (!ctrl && k === "s") setTool("select");
  else if (!ctrl && k === "i") $("#imageBtn").click();
});

window.addEventListener("paste", ev => {
  if (!S.doc || S.editor || S.view !== "editor") return;
  const item = [...(ev.clipboardData?.items || [])].find(i => i.type.startsWith("image/"));
  if (item) addImageFile(item.getAsFile());
});

window.addEventListener("dragover", ev => {
  ev.preventDefault();
  if (!S.dragTab && org.drag === null) document.body.classList.add("dragging");
});
window.addEventListener("dragleave", ev => { if (!ev.relatedTarget) document.body.classList.remove("dragging"); });
window.addEventListener("drop", async ev => {
  ev.preventDefault();
  document.body.classList.remove("dragging");
  const files = [...ev.dataTransfer.files];
  const pdfs = files.filter(f => f.type === "application/pdf" || /\.pdf$/i.test(f.name));
  if (org.drag !== null) return;
  if (pdfs.length) {
    for (const f of pdfs) await openFile(f);
    return;
  }
  const f = files[0];
  if (f && f.type.startsWith("image/") && S.doc && S.view === "editor") {
    const node = ev.target.closest && ev.target.closest(".page");
    const page = node ? S.pages[+node.dataset.page] : null;
    addImageFile(f, page, page ? pagePoint(page, ev) : null);
  }
});
window.addEventListener("resize", () => { if (S.doc && S.view === "editor") refreshVisible(); });

// ------------------------------------------------------- look and feel only
function ask({ title, text, value, ok, destructive }) {
  const d = $("#askDialog"), input = $("#askInput"), isPrompt = value !== undefined;
  $("#askTitle").textContent = title;
  $("#askText").textContent = text || "";
  $("#askText").hidden = !text;
  input.hidden = !isPrompt;
  input.value = value || "";
  $("#askOk").textContent = ok;
  $("#askOk").classList.toggle("destructive", !!destructive);
  $("#askCancel").autofocus = !isPrompt; // a destructive question never defaults to yes
  input.autofocus = isPrompt;
  d.returnValue = "";
  d.showModal();
  if (isPrompt) input.select();
  return new Promise(resolve => {
    d.onclose = () => resolve(d.returnValue === "ok" ? input.value : null);
  });
}

function setTheme(dark) {
  document.documentElement.dataset.theme = dark ? "dark" : "light";
  $("#themeIcon").setAttribute("href", dark ? "#i-sun" : "#i-moon");
  $("#themeLabel").textContent = dark ? "Light" : "Dark";
  try { localStorage.setItem("pdfeditor-theme", dark ? "dark" : "light"); } catch {}
}
setTheme(document.documentElement.dataset.theme === "dark");
const toggleTheme = () => setTheme(document.documentElement.dataset.theme !== "dark");
$("#themeBtn").onclick = toggleTheme;

// Side panels: closed by choice on a wide screen, opened on demand on a narrow one.
const narrow = window.matchMedia("(max-width: 1024px)");
function togglePanel(name) {
  document.body.classList.toggle(narrow.matches ? `${name}-open` : `${name}-closed`);
}
$("#thumbsToggle").onclick = () => togglePanel("thumbs");
$("#panelToggle").onclick = () => togglePanel("panel");
narrow.addEventListener("change", () => document.body.classList.remove("thumbs-open", "panel-open"));

// Tooltips with the keyboard shortcut.
function showTip(target) {
  const tip = $("#tip");
  tip.textContent = target.dataset.tip;
  if (target.dataset.key) el("kbd", "", tip).textContent = target.dataset.key;
  tip.hidden = false;
  const r = target.getBoundingClientRect(), t = tip.getBoundingClientRect();
  const below = r.bottom + 6 + t.height < innerHeight;
  const beside = r.right + 6 + t.width < innerWidth && target.closest("#rail");
  tip.style.left = Math.max(6, Math.min(innerWidth - t.width - 6, beside ? r.right + 6 : r.left + r.width / 2 - t.width / 2)) + "px";
  tip.style.top = (beside ? r.top + r.height / 2 - t.height / 2 : below ? r.bottom + 6 : r.top - t.height - 6) + "px";
}
const hideTip = () => { $("#tip").hidden = true; };
document.addEventListener("pointerover", ev => {
  const target = ev.target.closest && ev.target.closest("[data-tip]");
  if (target) showTip(target); else hideTip();
});
document.addEventListener("focusin", ev => {
  if (ev.target.matches && ev.target.matches("[data-tip]:focus-visible")) showTip(ev.target);
});
document.addEventListener("focusout", hideTip);
document.addEventListener("pointerdown", hideTip);

// Ctrl+K: every action here already exists on screen; this only finds and runs it.
function paletteActions() {
  const click = id => () => $(id).click();
  const list = [
    ["home", "Go to Home", () => goTo("home")],
    ["bookmark", "Go to Saved PDFs", () => goTo("saved")],
    ["upload", "Open a PDF", click("#filePdf")],
    [document.documentElement.dataset.theme === "dark" ? "sun" : "moon", "Switch between light and dark", toggleTheme],
  ];
  S.tabs.filter(t => t !== S.active || S.view !== "editor").forEach(t => list.push(["file", `Go to tab: ${t.name}`, () => activateTab(t)]));
  if (S.view === "editor" && S.doc) {
    const page = S.currentPage;
    list.unshift(
      ["pencil", "Edit", click('[data-tool="edit"]'), "V"],
      ["type", "Add text", click('[data-tool="text"]'), "T"],
      ["image", "Add photo", click("#imageBtn"), "I"],
      ["select", "Select area", click('[data-tool="select"]'), "S"],
      ["eraser", "Erase area", click('[data-tool="erase"]'), "E"],
    );
    if (!$("#undoBtn").disabled) list.push(["undo", "Undo", click("#undoBtn"), "Ctrl+Z"]);
    if (!$("#redoBtn").disabled) list.push(["redo", "Redo", click("#redoBtn"), "Ctrl+Y"]);
    list.push(
      ["save", "Save", save, "Ctrl+S"],
      ["download", "Download", click("#downloadBtn")],
      ["plus", "Zoom in", click("#zoomIn")],
      ["minus", "Zoom out", click("#zoomOut")],
      ["search", "Fit width", click("#zoomLabel")],
      ["rotate", `Rotate page ${page + 1}`, () => rotatePage(page)],
      ["plus", `Insert blank page after page ${page + 1}`, () => insertPage(page)],
      ["trash", `Delete page ${page + 1}`, () => deletePage(page)],
      ["search", "Find and replace", openFind, "Ctrl+F"],
      ["sign", "Sign: draw, type or upload a signature", () => openSign("draw")],
      ["calendar", "Add today's date", () => openSign("date")],
      ["grid", "Organise pages", openOrganizer],
      ["fileplus", "Add pages from another PDF", click("#orgAdd")],
      ["download", "Download compressed copy", () => downloadFrom(`/api/export?${docParam()}&compress=1`)],
      ["pleft", "Show or hide pages", click("#thumbsToggle")],
      ["pright", "Show or hide details", click("#panelToggle")],
      ["x", "Close this tab", () => closeTab(S.active)],
    );
  }
  return list;
}

const palette = { items: [], at: 0 };
function renderPalette() {
  const q = $("#palInput").value.trim().toLowerCase();
  palette.items = paletteActions().filter(a => a[1].toLowerCase().includes(q));
  palette.at = Math.min(palette.at, Math.max(0, palette.items.length - 1));
  const rows = palette.items.map(([name, label, , key], i) => {
    const li = document.createElement("li");
    li.setAttribute("role", "option");
    li.setAttribute("aria-selected", i === palette.at ? "true" : "false");
    li.className = i === palette.at ? "on" : "";
    li.innerHTML = icon(name);
    el("span", "", li).textContent = label;
    if (key) el("kbd", "", li).textContent = key;
    li.onpointerenter = () => { if (palette.at !== i) { palette.at = i; renderPalette(); } };
    li.onclick = () => runPalette(i);
    return li;
  });
  if (!rows.length) {
    const li = document.createElement("li");
    li.className = "none";
    li.textContent = "Nothing matches that.";
    rows.push(li);
  }
  $("#palList").replaceChildren(...rows);
  const on = $("#palList .on");
  if (on) on.scrollIntoView({ block: "nearest" });
}
function runPalette(i) {
  const action = palette.items[i];
  $("#palette").close();
  if (action) action[2]();
}
function openPalette() {
  if ($("dialog[open]")) return;
  hideTip();
  $("#palInput").value = "";
  palette.at = 0;
  renderPalette();
  $("#palette").showModal();
  $("#palInput").focus();
}
$("#paletteBtn").onclick = openPalette;
$("#palInput").oninput = () => { palette.at = 0; renderPalette(); };
$("#palInput").onkeydown = ev => {
  const n = palette.items.length;
  if (ev.key === "ArrowDown" && n) { ev.preventDefault(); palette.at = (palette.at + 1) % n; renderPalette(); }
  else if (ev.key === "ArrowUp" && n) { ev.preventDefault(); palette.at = (palette.at - 1 + n) % n; renderPalette(); }
  else if (ev.key === "Enter") { ev.preventDefault(); runPalette(palette.at); }
};
$("#palette").onclick = ev => { if (ev.target === $("#palette")) $("#palette").close(); };
// Runs first, so it also works while typing in a text box on the page.
window.addEventListener("keydown", ev => {
  if ((ev.ctrlKey || ev.metaKey) && ev.key.toLowerCase() === "k") { ev.preventDefault(); ev.stopPropagation(); openPalette(); }
  if ((ev.ctrlKey || ev.metaKey) && ev.key.toLowerCase() === "f" && S.view === "editor" && S.doc && !$("dialog[open]")) {
    ev.preventDefault(); ev.stopPropagation(); openFind();
  }
}, true);

// ------------------------------------------------------------ find and replace
const find = { hits: [], at: 0, seq: 0, timer: null };

function openFind() {
  if (S.view !== "editor" || !S.doc) return;
  closeOrganizer();
  const sel = S.editor ? String(getSelection()).trim() : "";
  $("#findpanel").hidden = false;
  $("#findBtn").classList.add("active");
  if (sel && !sel.includes("\n")) $("#findInput").value = sel;
  $("#findInput").focus();
  $("#findInput").select();
  runFind(false);
}

function closeFind() {
  $("#findpanel").hidden = true;
  $("#findBtn").classList.remove("active");
  clearHits();
}

function clearHits() {
  find.hits = []; find.at = 0; find.seq++;
  $$(".hit").forEach(n => n.remove());
  syncFind();
}

async function runFind(keep) {
  const q = $("#findInput").value, seq = ++find.seq;
  if (!q.trim() || !S.active) { find.hits = []; drawHits(); return; }
  let res;
  try {
    res = await api(`/api/find?${docParam()}`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ q, case: $("#findCase").checked }),
    });
  } catch (e) { toast(e.message, true); return; }
  if (seq !== find.seq) return; // a newer search has started
  find.hits = res.hits;
  find.at = keep ? Math.min(find.at, Math.max(0, find.hits.length - 1)) : 0;
  drawHits();
  if (!keep) showHit();
}

function drawHits() {
  $$(".hit").forEach(n => n.remove());
  find.hits.forEach((h, i) => {
    const p = S.pages[h.page];
    if (!p) return;
    h.node = el("div", "hit" + (i === find.at ? " on" : ""), p.node);
    place(h.node, { x0: h.x0 - 1, y0: h.y0 - 1, x1: h.x1 + 1, y1: h.y1 + 1 });
  });
  syncFind();
}

function syncFind() {
  const n = find.hits.length, q = $("#findInput").value.trim();
  $("#findCount").textContent = !q ? "Searches every page." : n ? `${find.at + 1} of ${n} match${n === 1 ? "" : "es"}` : "No matches.";
  ["#findPrev", "#findNext", "#replaceOne", "#replaceAll"].forEach(id => { $(id).disabled = !n; });
}

function showHit() {
  find.hits.forEach((h, i) => h.node && h.node.classList.toggle("on", i === find.at));
  const h = find.hits[find.at];
  if (h && h.node) h.node.scrollIntoView({ block: "center", inline: "nearest", behavior: "smooth" });
  syncFind();
}

function stepHit(d) {
  const n = find.hits.length;
  if (!n) return;
  find.at = (find.at + d + n) % n;
  showHit();
}

async function replaceHits(all) {
  const h = find.hits[find.at];
  if (!h) return;
  await commitEditor();
  const body = { type: "replace", query: $("#findInput").value, replacement: $("#replaceInput").value, matchCase: $("#findCase").checked };
  if (!all) body.target = { page: h.page, para: h.para, k: h.k };
  const state = await op(body, all ? "Replacing everywhere…" : "Replacing…");
  if (state && !all) { await runFind(true); showHit(); }
}

$("#findBtn").onclick = () => ($("#findpanel").hidden ? openFind() : closeFind());
$("#findClose").onclick = closeFind;
$("#findInput").oninput = () => { clearTimeout(find.timer); find.timer = setTimeout(() => runFind(false), 250); };
$("#findCase").onchange = () => runFind(false);
$("#findInput").onkeydown = ev => {
  if (ev.key === "Enter") { ev.preventDefault(); stepHit(ev.shiftKey ? -1 : 1); }
  if (ev.key === "Escape") { ev.preventDefault(); closeFind(); }
};
$("#replaceInput").onkeydown = ev => { if (ev.key === "Enter") { ev.preventDefault(); replaceHits(false); } };
$("#findNext").onclick = () => stepHit(1);
$("#findPrev").onclick = () => stepHit(-1);
$("#replaceOne").onclick = () => replaceHits(false);
$("#replaceAll").onclick = () => replaceHits(true);

// ------------------------------------------------------------------------ sign
const SIG_FONTS = ["Segoe Script", "Lucida Handwriting", "Ink Free", "Brush Script MT", "Segoe Print",
                   "Freestyle Script", "Mistral", "Gabriola", "Kristen ITC", "Comic Sans MS"];
const sig = { tab: "draw", pen: "#111827", width: 4, drawn: false, font: null, upload: null, dateFmt: 0 };

function installedFonts() {
  const ctx = document.createElement("canvas").getContext("2d");
  const width = font => { ctx.font = font; return ctx.measureText("Signature mmwwiil 123").width; };
  return SIG_FONTS.filter(f => ["monospace", "serif"].some(base => width(`40px "${f}", ${base}`) !== width(`40px ${base}`)));
}

function dateChoices() {
  const now = new Date();
  const two = n => String(n).padStart(2, "0");
  return [
    `${two(now.getDate())}/${two(now.getMonth() + 1)}/${now.getFullYear()}`,
    now.toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" }),
    now.toLocaleDateString("en-GB", { day: "numeric", month: "long", year: "numeric" }),
    `${now.getFullYear()}-${two(now.getMonth() + 1)}-${two(now.getDate())}`,
  ];
}

function savedSignatures() {
  try { return JSON.parse(localStorage.getItem("pdfeditor-signatures") || "[]"); } catch { return []; }
}
function storeSignatures(list) {
  try { localStorage.setItem("pdfeditor-signatures", JSON.stringify(list.slice(0, 6))); } catch {}
}

function renderSavedSignatures() {
  const box = $("#savedSigs"), list = savedSignatures();
  box.hidden = !list.length || sig.tab === "date";
  box.replaceChildren(Object.assign(document.createElement("span"), { className: "muted", textContent: "Saved:" }));
  list.forEach((url, i) => {
    const b = el("button", "savedsig", box);
    b.setAttribute("aria-label", "Use this saved signature");
    const img = el("img", "", b); img.src = url; img.alt = "";
    b.onclick = async () => {
      const c = document.createElement("canvas");
      const bmp = await createImageBitmap(await (await fetch(url)).blob());
      c.width = bmp.width; c.height = bmp.height;
      c.getContext("2d").drawImage(bmp, 0, 0);
      useSignature(c, false);
    };
    const rm = el("span", "rm", b);
    rm.innerHTML = icon("x"); rm.setAttribute("role", "button"); rm.setAttribute("aria-label", "Forget this signature"); rm.tabIndex = 0;
    rm.onclick = ev => { ev.stopPropagation(); const l = savedSignatures(); l.splice(i, 1); storeSignatures(l); renderSavedSignatures(); };
  });
}

function showSignTab(tab) {
  sig.tab = tab;
  $$("[data-stab]").forEach(b => b.setAttribute("aria-selected", b.dataset.stab === tab ? "true" : "false"));
  $$("[data-spane]").forEach(p => { p.hidden = p.dataset.spane !== tab; });
  $("#sigColourRow").hidden = tab === "date" || tab === "upload";
  $("#sigRememberRow").hidden = tab === "date";
  $("#sigPlace").textContent = tab === "date" ? "Place date on page" : "Place on page";
  renderSavedSignatures();
  if (tab === "type") { renderFonts(); $("#sigName").focus(); }
}

function renderFonts() {
  const fonts = installedFonts();
  if (!sig.font || !fonts.includes(sig.font)) sig.font = fonts[0] || "cursive";
  const name = $("#sigName").value.trim() || "Your signature";
  $("#sigFonts").replaceChildren(...(fonts.length ? fonts : ["cursive"]).map(f => {
    const b = document.createElement("button");
    b.setAttribute("role", "radio");
    b.setAttribute("aria-checked", f === sig.font ? "true" : "false");
    b.style.fontFamily = `"${f}", cursive`;
    b.style.color = sig.pen;
    b.textContent = name;
    b.title = f;
    b.onclick = () => { sig.font = f; renderFonts(); };
    return b;
  }));
}

function renderDates() {
  $("#dateFormats").replaceChildren(...dateChoices().map((d, i) => {
    const b = document.createElement("button");
    b.className = "datefmt";
    b.setAttribute("role", "radio");
    b.setAttribute("aria-checked", i === sig.dateFmt ? "true" : "false");
    b.textContent = d;
    b.onclick = () => { sig.dateFmt = i; renderDates(); };
    return b;
  }));
}

function openSign(tab = "draw") {
  if (S.view !== "editor" || !S.doc) return;
  commitEditor();
  closeOrganizer();
  renderDates();
  showSignTab(tab);
  $("#signDialog").showModal();
}

// Only the inked part, with a little margin.
function trimmed(canvas) {
  const ctx = canvas.getContext("2d");
  const { width: w, height: h } = canvas;
  const data = ctx.getImageData(0, 0, w, h).data;
  let x0 = w, y0 = h, x1 = -1, y1 = -1;
  for (let y = 0; y < h; y++) for (let x = 0; x < w; x++) {
    if (data[(y * w + x) * 4 + 3] > 16) { if (x < x0) x0 = x; if (x > x1) x1 = x; if (y < y0) y0 = y; if (y > y1) y1 = y; }
  }
  if (x1 < 0) return null;
  const pad = 6, out = document.createElement("canvas");
  out.width = x1 - x0 + 1 + pad * 2; out.height = y1 - y0 + 1 + pad * 2;
  out.getContext("2d").drawImage(canvas, x0, y0, x1 - x0 + 1, y1 - y0 + 1, pad, pad, x1 - x0 + 1, y1 - y0 + 1);
  return out;
}

function typedSignature() {
  const text = $("#sigName").value.trim();
  if (!text) return null;
  const c = document.createElement("canvas");
  const ctx = c.getContext("2d");
  const font = `120px "${sig.font}", cursive`;
  ctx.font = font;
  c.width = Math.ceil(ctx.measureText(text).width + 80); c.height = 240;
  ctx.font = font; ctx.fillStyle = sig.pen; ctx.textBaseline = "middle";
  ctx.fillText(text, 40, 120);
  return trimmed(c);
}

async function useSignature(canvas, remember) {
  const ratio = canvas.width / canvas.height;
  let h = 42, w = h * ratio;
  if (w > 200) { w = 200; h = w / ratio; }
  if (remember) {
    const url = canvas.toDataURL("image/png"), list = savedSignatures().filter(u => u !== url);
    storeSignatures([url, ...list]);
  }
  const blob = await new Promise(r => canvas.toBlob(r, "image/png"));
  $("#signDialog").close();
  setTool("place");
  S.stamp = { kind: "image", blob, w, h };
  toast("Click on the page where the signature should go.");
}

async function placeStamp(p, pt) {
  const stamp = S.stamp;
  if (!stamp) { setTool("edit"); return; }
  if (stamp.kind === "date") {
    setTool("edit");
    const size = S.lastSize || 12;
    openNewText(p, pt.x, Math.max(0, pt.y - size * 0.6), null, stamp.text);
    return;
  }
  const x0 = Math.max(0, Math.min(p.w - stamp.w, pt.x - stamp.w / 2));
  const y0 = Math.max(0, Math.min(p.h - stamp.h, pt.y - stamp.h / 2));
  const rect = [x0, y0, x0 + stamp.w, y0 + stamp.h];
  const form = new FormData();
  form.append("file", stamp.blob, "signature.png");
  form.append("page", p.index);
  ["x0", "y0", "x1", "y1"].forEach((k, i) => form.append(k, rect[i]));
  setTool("edit");
  await withBusy("Adding signature…", async () => {
    applyState(await api(`/api/image?${docParam()}`, { method: "POST", body: form }));
    await reselect(p, rect);
  });
}

async function placeFromDialog() {
  if (sig.tab === "date") {
    S.stamp = null;
    $("#signDialog").close();
    setTool("place");
    S.stamp = { kind: "date", text: dateChoices()[sig.dateFmt] };
    return;
  }
  const remember = $("#sigRemember").checked;
  const canvas = sig.tab === "draw" ? (sig.drawn ? trimmed($("#sigCanvas")) : null)
    : sig.tab === "type" ? typedSignature() : sig.upload;
  if (!canvas) {
    toast(sig.tab === "draw" ? "Draw your signature in the box first." : sig.tab === "type" ? "Type your name or initials first." : "Choose an image first.", true);
    return;
  }
  useSignature(canvas, remember);
}

(function bindSignPad() {
  const c = $("#sigCanvas"), ctx = c.getContext("2d");
  let last = null;
  const at = e => { const r = c.getBoundingClientRect(); return { x: (e.clientX - r.left) * c.width / r.width, y: (e.clientY - r.top) * c.height / r.height }; };
  c.onpointerdown = e => {
    c.setPointerCapture(e.pointerId);
    last = at(e);
    ctx.fillStyle = sig.pen;
    ctx.beginPath(); ctx.arc(last.x, last.y, sig.width / 2, 0, Math.PI * 2); ctx.fill();
    sig.drawn = true;
  };
  c.onpointermove = e => {
    if (!last) return;
    const pt = at(e);
    ctx.strokeStyle = sig.pen; ctx.lineWidth = sig.width; ctx.lineCap = ctx.lineJoin = "round";
    ctx.beginPath(); ctx.moveTo(last.x, last.y);
    ctx.quadraticCurveTo(last.x, last.y, (last.x + pt.x) / 2, (last.y + pt.y) / 2);
    ctx.lineTo(pt.x, pt.y); ctx.stroke();
    last = pt;
  };
  c.onpointerup = c.onpointercancel = () => { last = null; };
  $("#sigClear").onclick = () => { ctx.clearRect(0, 0, c.width, c.height); sig.drawn = false; };
})();

async function loadUploadedSignature(file) {
  const bmp = await createImageBitmap(file).catch(() => null);
  if (!bmp) { toast("That image can't be opened. Please use JPG, PNG or WebP.", true); return; }
  const k = Math.min(1, 1200 / bmp.width, 600 / bmp.height);
  const c = document.createElement("canvas");
  c.width = Math.round(bmp.width * k); c.height = Math.round(bmp.height * k);
  const ctx = c.getContext("2d");
  ctx.drawImage(bmp, 0, 0, c.width, c.height);
  if ($("#sigClean").checked) {
    const img = ctx.getImageData(0, 0, c.width, c.height), d = img.data;
    for (let i = 0; i < d.length; i += 4) {
      const light = (d[i] + d[i + 1] + d[i + 2]) / 3;
      if (light > 225) d[i + 3] = 0;
      else if (light > 180) d[i + 3] = Math.round(d[i + 3] * (225 - light) / 45);
    }
    ctx.putImageData(img, 0, 0);
  }
  sig.upload = trimmed(c) || c;
  sig.uploadFile = file;
  const prev = $("#sigUpload"), pctx = prev.getContext("2d");
  pctx.clearRect(0, 0, prev.width, prev.height);
  const s = Math.min((prev.width - 40) / sig.upload.width, (prev.height - 70) / sig.upload.height, 3);
  pctx.drawImage(sig.upload, (prev.width - sig.upload.width * s) / 2, (prev.height - 50 - sig.upload.height * s) / 2, sig.upload.width * s, sig.upload.height * s);
  $(".sigupload").classList.add("has");
  $("#sigPick").lastChild.textContent = "Choose another image";
}

$("#signBtn").onclick = () => openSign("draw");
$$("[data-stab]").forEach(b => { b.onclick = () => showSignTab(b.dataset.stab); });
$$("[data-penw]").forEach(b => { b.onclick = () => { sig.width = +b.dataset.penw; $$("[data-penw]").forEach(x => x.classList.toggle("on", x === b)); }; });
$$("[data-pen]").forEach(b => { b.onclick = () => { sig.pen = b.dataset.pen; $$("[data-pen]").forEach(x => x.classList.toggle("on", x === b)); if (sig.tab === "type") renderFonts(); }; });
$("#sigName").oninput = renderFonts;
$("#sigName").onkeydown = ev => { if (ev.key === "Enter") { ev.preventDefault(); placeFromDialog(); } };
$("#sigPick").onclick = () => $("#fileSign").click();
$("#fileSign").onchange = e => { const f = e.target.files[0]; e.target.value = ""; if (f) loadUploadedSignature(f); };
$("#sigClean").onchange = () => { if (sig.uploadFile) loadUploadedSignature(sig.uploadFile); };
$("#sigPlace").onclick = placeFromDialog;
$("#sigCancel").onclick = () => $("#signDialog").close();

// -------------------------------------------------------------- organise pages
const org = { sel: new Set(), last: null, drag: null };

function openOrganizer() {
  if (S.view !== "editor" || !S.doc) return;
  commitEditor();
  deselect();
  org.sel.clear(); org.last = null;
  $("#organizer").hidden = false;
  $("#pagesBtn").classList.add("active");
  renderOrganizer();
}

function closeOrganizer() {
  if ($("#organizer").hidden) return;
  $("#organizer").hidden = true;
  $("#pagesBtn").classList.remove("active");
}

function syncOrganizer() {
  const n = S.pages.length, k = org.sel.size;
  $("#orgCount").textContent = `${n} page${n === 1 ? "" : "s"}`;
  $("#orgSel").textContent = k ? `${k} page${k === 1 ? "" : "s"} selected` : "Click pages to select them.";
  ["#orgRotate", "#orgDup", "#orgExtract", "#orgSplit", "#orgDelete"].forEach(id => { $(id).disabled = !k; });
  $("#orgAll").textContent = k === n ? "Select none" : "Select all";
  $$(".orgcard").forEach(c => {
    const on = org.sel.has(+c.dataset.page);
    c.classList.toggle("sel", on);
    $(".orgcheck", c).checked = on;
  });
}

function toggleOrg(i, range) {
  if (range && org.last !== null) {
    const [a, b] = [Math.min(org.last, i), Math.max(org.last, i)];
    for (let j = a; j <= b; j++) org.sel.add(j);
  } else if (org.sel.has(i)) org.sel.delete(i);
  else org.sel.add(i);
  org.last = i;
  syncOrganizer();
}

function renderOrganizer() {
  if ($("#organizer").hidden) return;
  org.sel = new Set([...org.sel].filter(i => i < S.pages.length));
  $("#orgGrid").replaceChildren(...S.pages.map(p => {
    const card = document.createElement("div");
    card.className = "orgcard";
    card.dataset.page = p.index;
    card.draggable = true;
    const img = el("img", "", card);
    img.src = imageUrl(p, 0.35); img.alt = `Page ${p.index + 1}`; img.loading = "lazy";
    const box = el("input", "orgcheck", card);
    box.type = "checkbox"; box.setAttribute("aria-label", `Select page ${p.index + 1}`);
    box.onclick = ev => { ev.stopPropagation(); toggleOrg(p.index, ev.shiftKey); };
    el("span", "orgnum", card).textContent = p.index + 1;
    const add = el("button", "orgins", card);
    add.innerHTML = icon("plus"); add.title = "Insert a blank page after this one";
    add.setAttribute("aria-label", `Insert a blank page after page ${p.index + 1}`);
    add.onclick = ev => { ev.stopPropagation(); insertPage(p.index); };
    card.onclick = ev => toggleOrg(p.index, ev.shiftKey);
    card.ondragstart = ev => {
      org.drag = p.index;
      ev.dataTransfer.effectAllowed = "move";
      ev.dataTransfer.setData("text/plain", String(p.index));
      requestAnimationFrame(() => card.classList.add("dragging"));
    };
    card.ondragend = () => { org.drag = null; $$(".orgcard").forEach(c => c.classList.remove("dragging", "dropbefore", "dropafter")); };
    card.ondragover = ev => {
      if (org.drag === null) return;
      ev.preventDefault(); ev.stopPropagation();
      const r = card.getBoundingClientRect(), after = ev.clientX > r.left + r.width / 2;
      card.classList.toggle("dropafter", after); card.classList.toggle("dropbefore", !after);
    };
    card.ondragleave = () => card.classList.remove("dropbefore", "dropafter");
    card.ondrop = ev => {
      if (org.drag === null) return;
      ev.preventDefault(); ev.stopPropagation();
      const r = card.getBoundingClientRect();
      movePages(org.drag, p.index, ev.clientX > r.left + r.width / 2);
      org.drag = null;
    };
    return card;
  }));
  syncOrganizer();
}

const allPages = () => S.pages.map(p => p.index);
const selected = () => [...org.sel].sort((a, b) => a - b);

async function arrange(order, newSel) {
  const state = await op({ type: "arrange", order }, "Rearranging pages…");
  if (state) { org.sel = new Set(newSel || []); org.last = null; renderOrganizer(); }
}

function movePages(dragged, target, after) {
  const moving = org.sel.has(dragged) ? selected() : [dragged];
  if (moving.includes(target)) return;
  const rest = allPages().filter(i => !moving.includes(i));
  const at = rest.indexOf(target) + (after ? 1 : 0);
  const order = [...rest.slice(0, at), ...moving, ...rest.slice(at)];
  if (order.every((v, i) => v === i)) return;
  arrange(order, moving.map((_, i) => at + i));
}

async function mergeFile(file, password) {
  const sel = selected();
  const at = sel.length ? sel[sel.length - 1] + 1 : S.pages.length;
  const form = new FormData();
  form.append("file", file);
  form.append("at", at);
  if (password) form.append("password", password);
  busyCount++;
  $("#busyText").textContent = `Adding ${file.name}…`;
  $("#busy").hidden = false;
  try {
    const before = S.pages.length;
    const state = await api(`/api/merge?${docParam()}`, { method: "POST", body: form });
    const added = state.pages.length - before;
    org.sel = new Set(Array.from({ length: added }, (_, i) => at + i)); // the new pages show as selected
    org.last = null;
    applyState(state);
  } catch (e) {
    if (e.status === 401 && e.body && e.body.password) askPassword(file, e.body.wrong, pw => mergeFile(file, pw));
    else toast(e.message, true);
  } finally {
    if (--busyCount === 0) $("#busy").hidden = true;
  }
}

$("#pagesBtn").onclick = () => ($("#organizer").hidden ? openOrganizer() : closeOrganizer());
$("#orgDone").onclick = closeOrganizer;
$("#orgAll").onclick = () => {
  if (org.sel.size === S.pages.length) org.sel.clear(); else org.sel = new Set(allPages());
  syncOrganizer();
};
$("#orgRotate").onclick = async () => {
  const keep = selected();
  if (await op({ type: "rotate_pages", pages: keep }, "Rotating…")) { org.sel = new Set(keep); renderOrganizer(); }
};
$("#orgDup").onclick = () => {
  const sel = org.sel, order = [];
  allPages().forEach(i => { order.push(i); if (sel.has(i)) order.push(i); });
  arrange(order, []);
};
$("#orgDelete").onclick = async () => {
  const sel = selected();
  if (sel.length >= S.pages.length) { toast("A PDF must keep at least one page.", true); return; }
  const sure = await ask({ title: `Delete ${sel.length} page${sel.length === 1 ? "" : "s"}?`, text: "You can bring them back with Undo.", ok: "Delete", destructive: true });
  if (sure !== null) arrange(allPages().filter(i => !org.sel.has(i)), []);
};
$("#orgExtract").onclick = () => downloadFrom(`/api/export?${docParam()}&pages=${selected().join(",")}`);
$("#orgSplit").onclick = () => downloadFrom(`/api/export?${docParam()}&pages=${selected().join(",")}&split=1`);
$("#orgAdd").onclick = () => { if (S.view === "editor" && S.doc) $("#fileMerge").click(); };
$("#fileMerge").onchange = e => { const f = e.target.files[0]; e.target.value = ""; if (f) mergeFile(f); };

// Coming back to the page keeps whatever PDFs are still open.
showView("home");
api("/api/tabs").then(async ({ tabs }) => {
  tabs.forEach(addTab);
  if (S.tabs.length) await activateTab(S.tabs[S.tabs.length - 1]);
  else await refreshLibrary();
}).catch(e => toast(e.message, true));
