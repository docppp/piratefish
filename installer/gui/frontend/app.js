'use strict';

// ---- bridge helpers --------------------------------------------------------
const api = () => window.pywebview.api;
let REPORT = null;
let ADDED_TRACKERS = [];
let SELECTED_PROVIDERS = {};   // id -> {id, fields:{}}

const STEP_ORDER = ["welcome","config","install","done","trackers","subtitles","extras","finish"];

function show(step) {
  STEP_ORDER.forEach(s => {
    const pg = document.getElementById("page-" + s);
    if (pg) pg.classList.toggle("hidden", s !== step);
  });
  document.querySelectorAll(".steps .step").forEach(el => {
    const s = el.dataset.step;
    el.classList.toggle("active", s === step);
  });
  markDone(step);
}
function markDone(current) {
  const idx = STEP_ORDER.indexOf(current);
  document.querySelectorAll(".steps .step").forEach(el => {
    const i = STEP_ORDER.indexOf(el.dataset.step);
    el.classList.toggle("done", i < idx);
  });
}

function toast(msg, isErr) {
  const t = document.getElementById("toast");
  t.textContent = msg;
  t.classList.toggle("err", !!isErr);
  t.classList.remove("hidden");
  setTimeout(() => t.classList.add("hidden"), 3500);
}

function modal(html) {
  document.getElementById("modal").innerHTML = html;
  document.getElementById("modal-bg").classList.remove("hidden");
}
function closeModal() { document.getElementById("modal-bg").classList.add("hidden"); }

function resetInstallProgress() {
  const wrap = document.getElementById("pull-progress-wrap");
  wrap.classList.add("hidden");
  document.getElementById("pull-progress-label").textContent = "Downloading Docker images…";
  document.getElementById("pull-progress-pct").textContent = "0%";
  document.getElementById("pull-progress-fill").style.width = "0%";
  document.getElementById("pull-progress-detail").textContent = "Waiting to start…";
}

function handleInstallProgress(data) {
  if (!data || data.phase !== "docker_pull") return;
  const wrap = document.getElementById("pull-progress-wrap");
  wrap.classList.remove("hidden");

  const total = Number(data.total) || 0;
  const current = Number(data.current) || 0;
  const index = Number(data.index) || 0;
  const rawPercent = Number(data.percent);
  const percent = Number.isFinite(rawPercent)
    ? Math.max(0, Math.min(100, Math.round(rawPercent)))
    : (total ? Math.round((current * 100) / total) : 0);

  document.getElementById("pull-progress-pct").textContent = percent + "%";
  document.getElementById("pull-progress-fill").style.width = percent + "%";
  document.getElementById("pull-progress-label").textContent =
    data.message || "Downloading Docker images…";

  const detail = document.getElementById("pull-progress-detail");
  if (data.status === "start") {
    detail.textContent = "Preparing image downloads…";
  } else if (data.status === "pulling" && data.label) {
    detail.textContent = `Pulling ${data.label} image (${index}/${total})…`;
  } else if (data.status === "pulled" && data.label) {
    detail.textContent = `${data.label} image ready (${current}/${total})`;
  } else if (data.status === "done") {
    detail.textContent = "All Docker images are ready.";
  } else if (data.status === "error") {
    detail.textContent = data.message || "Docker image download failed.";
  }
}

// ---- logging (called from Python) -----------------------------------------
window.appLog = function (entry) {
  const log = document.getElementById("log");
  if (!log) return;
  const line = document.createElement("div");
  line.className = entry.level || "info";
  const prefix = { ok:"[OK] ", warn:"[!] ", fail:"[X] ", step:"-> ", info:"", header:"" }[entry.level] || "";
  line.textContent = (entry.level === "header" ? "\n== " + entry.msg + " ==" : "  " + prefix + entry.msg);
  log.appendChild(line);
  log.scrollTop = log.scrollHeight;
};

window.appEvent = function (event, data) {
  if (event === "install_progress") {
    handleInstallProgress(data);
    return;
  }
  if (event === "install_done") {
    document.getElementById("inst-spin").style.display = "none";
    if (data.ok) {
      REPORT = data.report;
      renderReport();
      show("done");
    } else {
      toast("Install failed: " + (data.error || "unknown"), true);
    }
  }
};

// ---- welcome ---------------------------------------------------------------
async function loadEnv() {
  const env = await api().get_environment();
  document.getElementById("envline").textContent =
    `${env.os} · ${env.arch} · ${env.lan_ip}`;
  const dockerTxt = env.docker_ok ? "Docker running ✓" :
    "Docker not detected — the installer will set it up.";
  const composeTxt = env.compose_ok ? "Compose ✓" : "Compose will be installed.";
  document.getElementById("envcard").innerHTML =
    `<div class="svc-links">
       <div class="svc"><b>Operating system</b><span>${env.os} (${env.arch})</span></div>
       <div class="svc"><b>Administrator</b><span>${env.is_admin ? "yes" : "no"}</span></div>
       <div class="svc"><b>Network address</b><span>${env.lan_ip}</span></div>
       <div class="svc"><b>Docker</b><span>${dockerTxt}</span></div>
       <div class="svc"><b>Compose</b><span>${composeTxt}</span></div>
     </div>`;
}

async function loadDefaults() {
  const d = await api().guess_defaults();
  document.getElementById("f-tz").value = d.tz || "Etc/UTC";
  document.getElementById("f-subnet").value = d.lan_subnet || "";
  document.getElementById("f-user").value = d.qbit_user || "admin";
  if (d.data_path) {
    const el = document.getElementById("f-path");
    el.value = d.data_path;
    if (d.data_path_locked) {
      // Chosen with the Windows folder picker: lock it so it can't be changed
      // to a path inside WSL2 by accident.
      el.readOnly = true;
      el.classList.add("locked");
      el.title = "Chosen on Windows and locked to keep media on your Windows drive.";
      const b = document.getElementById("btn-browse");
      if (b) b.style.display = "none";
      const lbl = document.getElementById("path-label");
      if (lbl) lbl.textContent =
        "Data path — chosen on Windows (locked to keep media on your Windows drive)";
    }
    checkPath(d.data_path);
  }
}

// ---- config ----------------------------------------------------------------
document.getElementById("btn-browse").addEventListener("click", async () => {
  const p = await api().pick_folder();
  if (p) {
    document.getElementById("f-path").value = p;
    checkPath(p);
  }
});

document.getElementById("f-path").addEventListener("change", (e) => {
  const hint = document.getElementById("path-hint");
  const p = e.target.value.trim();
  if (!p) {
    hint.textContent = "";
    return;
  }
  checkPath(p);
});

async function checkPath(p) {
  const hint = document.getElementById("path-hint");
  hint.textContent = "Checking filesystem…";
  try {
    const r = await api().check_path(p);
    if (!r.safe) {
      hint.innerHTML = "<span style='color:#e5534b'>⚠ " + (r.fatal.join("; ") || "not usable") + "</span>";
    } else {
      let msg = `Filesystem ${r.fs_type} · hardlinks ${r.hardlink_ok ? "OK ✓" : "unavailable"}`;
      if (r.caveats.length) msg += " · " + r.caveats.length + " note(s)";
      hint.textContent = msg;
    }
  } catch (e) { hint.textContent = ""; }
}

document.getElementById("btn-config-next").addEventListener("click", () => {
  const form = {
    data_path: document.getElementById("f-path").value.trim(),
    tz: document.getElementById("f-tz").value.trim(),
    lan_subnet: document.getElementById("f-subnet").value.trim(),
    qbit_user: document.getElementById("f-user").value.trim(),
    qbit_pass: document.getElementById("f-pass").value,
  };
  if (!form.data_path) return toast("Please choose a data path.", true);
  if (!form.qbit_pass) return toast("Please set a Web UI password.", true);
  show("install");
  resetInstallProgress();
  document.getElementById("log").innerHTML = "";
  document.getElementById("inst-spin").style.display = "inline-block";
  api().start_install(form);
});

// ---- report / done ---------------------------------------------------------
function renderReport() {
  if (!REPORT) return;
  const html = REPORT.services.map(s =>
    `<div class="svc"><b>${s.name}</b><a href="#" onclick="openUrl('${s.url}');return false;">${s.url}</a></div>`
  ).join("");
  document.getElementById("svc-links").innerHTML = html;
  document.getElementById("svc-links-2").innerHTML = html;
  document.getElementById("fin-creds").textContent =
    `${REPORT.qbit_user} / ${REPORT.qbit_pass}`;
}
window.openUrl = (u) => api().open_url(u);

document.getElementById("btn-start-setup").addEventListener("click", () => {
  show("trackers");
  if (!TRACKERS_LOADED) loadAllTrackers();
});

// ---- trackers --------------------------------------------------------------
let ALL_TRACKERS = [];        // full catalog, loaded once
let TRACKERS_LOADED = false;

async function loadAllTrackers() {
  const list = document.getElementById("tracker-list");
  const count = document.getElementById("tracker-count");
  count.textContent = "Loading tracker catalog…";
  list.innerHTML = "<div class='hint'><span class='spinner'></span> Loading…</div>";
  const r = await api().list_all_trackers();
  if (r.error) {
    count.textContent = "";
    list.innerHTML = "<div class='hint'>Could not load catalog: " + r.error + "</div>";
    return;
  }
  ALL_TRACKERS = r.trackers || [];
  // Pre-mark trackers already configured in Prowlarr.
  (r.added || []).forEach(n => { if (!ADDED_TRACKERS.includes(n)) ADDED_TRACKERS.push(n); });
  if (ADDED_TRACKERS.length)
    document.getElementById("added-trackers").textContent = ADDED_TRACKERS.join(", ");
  TRACKERS_LOADED = true;
  renderTrackers("");
}

function renderTrackers(filter) {
  const list = document.getElementById("tracker-list");
  const count = document.getElementById("tracker-count");
  const q = (filter || "").trim().toLowerCase();
  let items = ALL_TRACKERS;
  if (q) items = ALL_TRACKERS.filter(t => (t.name || "").toLowerCase().includes(q));

  count.textContent = q
    ? `${items.length} of ${ALL_TRACKERS.length} trackers match "${filter}"`
    : `${ALL_TRACKERS.length} trackers available — type to filter`;

  // Cap DOM to a reasonable number for performance; refine by typing more.
  const capped = items.slice(0, 200);
  if (!capped.length) {
    list.innerHTML = "<div class='hint'>No trackers match. Try a different term.</div>";
    return;
  }
  list.innerHTML = "";
  capped.forEach(t => {
    const row = document.createElement("div");
    row.className = "tracker-item";
    const added = ADDED_TRACKERS.includes(t.name);
    row.innerHTML =
      `<span class="name">${t.name}</span>
       <span class="badge ${t.privacy}">${t.privacy}</span>
       <span class="grow hint">${t.protocol}${t.language ? " · " + t.language : ""}</span>
       <button class="small ${added ? 'secondary' : ''}">${added ? "Added ✓" : "Add"}</button>`;
    const btn = row.querySelector("button");
    if (!added) btn.addEventListener("click", () => addTracker(t.name));
    list.appendChild(row);
  });
  if (items.length > capped.length) {
    const more = document.createElement("div");
    more.className = "hint";
    more.style.marginTop = "8px";
    more.textContent = `…and ${items.length - capped.length} more — keep typing to narrow down.`;
    list.appendChild(more);
  }
}

let trackerFilterTimer = null;
document.getElementById("tracker-search").addEventListener("input", (e) => {
  clearTimeout(trackerFilterTimer);
  const v = e.target.value;
  trackerFilterTimer = setTimeout(() => renderTrackers(v), 120);
});

async function addTracker(name) {
  const f = await api().get_tracker_form(name);
  if (f.error) return toast(f.error, true);

  let fieldsHtml = f.fields.map(fl =>
    `<label class="field"><span>${fl.label}</span>
       <input type="${fl.secret ? "password" : "text"}" data-fname="${fl.name}"></label>`
  ).join("");

  const cookieBtn = f.needs_cookie
    ? `<button class="secondary" id="m-login">Log in to capture cookie</button>
       <div class="hint" id="m-cookie-status"></div>` : "";

  modal(
    `<h2>${f.name}</h2>
     <p class="sub">${f.privacy} tracker</p>
     ${fieldsHtml || "<p class='hint'>No credentials needed for this tracker.</p>"}
     ${cookieBtn}
     <div class="actions">
       <button class="secondary" id="m-cancel">Cancel</button>
       <button id="m-add">Test &amp; add</button>
     </div>`
  );

  document.getElementById("m-cancel").addEventListener("click", closeModal);

  let capturedUA = "";
  if (f.needs_cookie) {
    document.getElementById("m-login").addEventListener("click", async () => {
      const url = (f.urls && f.urls[0]) || "";
      if (!url) return toast("No login URL for this tracker.", true);
      document.getElementById("m-cookie-status").textContent = "Opening login window…";
      const r = await api().login_capture(url, "Log in to " + f.name);
      if (r.ok && r.cookie) {
        const inp = document.querySelector('#modal input[data-fname="cookie"]');
        if (inp) inp.value = r.cookie;
        capturedUA = r.user_agent || "";
        document.getElementById("m-cookie-status").innerHTML =
          "<span style='color:#3ddc84'>Cookie captured ✓</span>";
      } else {
        document.getElementById("m-cookie-status").innerHTML =
          "<span style='color:#d9a441'>No cookie captured — paste it manually if needed.</span>";
      }
    });
  }

  document.getElementById("m-add").addEventListener("click", async () => {
    const values = {};
    document.querySelectorAll("#modal input[data-fname]").forEach(i => {
      values[i.dataset.fname] = i.value;
    });
    // Cookie trackers also need the matching browser User-Agent; supply the one
    // captured during login (matched case-insensitively to the schema field).
    if (capturedUA) values["useragent"] = capturedUA;
    document.getElementById("m-add").disabled = true;
    document.getElementById("m-add").textContent = "Testing…";
    const r = await api().add_tracker(name, values);
    if (r.ok) {
      ADDED_TRACKERS.push(name);
      document.getElementById("added-trackers").textContent = ADDED_TRACKERS.join(", ");
      toast(name + " added ✓");
      closeModal();
      renderTrackers(document.getElementById("tracker-search").value);
    } else {
      toast("Failed: " + (r.error || "test failed"), true);
      document.getElementById("m-add").disabled = false;
      document.getElementById("m-add").textContent = "Test & add";
    }
  });
}

document.getElementById("btn-trackers-next").addEventListener("click", () => {
  loadSubtitles();
  show("subtitles");
});

// ---- subtitles -------------------------------------------------------------
let PROVIDER_CATALOG = [];
async function loadSubtitles() {
  const r = await api().get_bazarr_providers();
  const langSel = document.getElementById("sub-language");
  langSel.innerHTML = r.languages.map(l =>
    `<option value="${l.code}">${l.name}</option>`).join("");
  langSel.addEventListener("change", updateFallbackHint);
  updateFallbackHint();

  PROVIDER_CATALOG = r.providers;
  const list = document.getElementById("provider-list");
  list.innerHTML = "";
  r.providers.forEach(p => {
    const row = document.createElement("div");
    row.className = "provider-item";
    row.innerHTML =
      `<input type="checkbox" data-pid="${p.id}">
       <span class="name">${p.name}</span>
       ${p.needs_cookie ? '<span class="badge private">login</span>' : ''}
       ${p.fields.length ? '<span class="badge">account</span>' : '<span class="badge public">no login</span>'}
       <span class="grow"></span>
       <button class="small secondary" style="display:none">Configure</button>`;
    const cb = row.querySelector("input");
    const btn = row.querySelector("button");
    cb.addEventListener("change", () => {
      if (cb.checked) {
        SELECTED_PROVIDERS[p.id] = { id: p.id, fields: {} };
        if (p.fields.length || p.needs_cookie) { btn.style.display = "inline-block"; configureProvider(p); }
      } else {
        delete SELECTED_PROVIDERS[p.id];
        btn.style.display = "none";
      }
    });
    btn.addEventListener("click", () => configureProvider(p));
    list.appendChild(row);
  });
}

function updateFallbackHint() {
  const code = document.getElementById("sub-language").value;
  const hint = document.getElementById("fallback-hint");
  hint.textContent = code === "en"
    ? "English selected — used as the only language."
    : "If a subtitle isn't found in your language, English will be downloaded as a fallback.";
}

function configureProvider(p) {
  if (!p.fields.length && !p.needs_cookie) return;
  const cur = SELECTED_PROVIDERS[p.id] || { id: p.id, fields: {} };
  let fieldsHtml = p.fields.map(fl =>
    `<label class="field"><span>${fl.label}</span>
       <input type="${fl.secret ? "password" : "text"}" data-fname="${fl.name}" value="${cur.fields[fl.name] || ""}"></label>`
  ).join("");
  const cookieBtn = p.needs_cookie
    ? `<button class="secondary" id="p-login">Log in to capture cookie</button>
       <div class="hint" id="p-cookie-status"></div>` : "";
  modal(
    `<h2>${p.name}</h2>
     ${fieldsHtml || "<p class='hint'>No credentials needed.</p>"}
     ${cookieBtn}
     <div class="actions"><button class="secondary" id="p-cancel">Cancel</button>
       <button id="p-save">Save</button></div>`
  );
  document.getElementById("p-cancel").addEventListener("click", closeModal);
  if (p.needs_cookie) {
    document.getElementById("p-login").addEventListener("click", async () => {
      const url = p.login_url || "";
      document.getElementById("p-cookie-status").textContent = "Opening login…";
      const r = await api().login_capture(url, "Log in to " + p.name);
      const inp = document.querySelector('#modal input[data-fname="cookies"]');
      if (r.ok && r.cookie && inp) {
        inp.value = r.cookie;
        document.getElementById("p-cookie-status").innerHTML = "<span style='color:#3ddc84'>Cookie captured ✓</span>";
      } else {
        document.getElementById("p-cookie-status").innerHTML = "<span style='color:#d9a441'>No cookie captured.</span>";
      }
    });
  }
  document.getElementById("p-save").addEventListener("click", () => {
    const fields = {};
    document.querySelectorAll("#modal input[data-fname]").forEach(i => fields[i.dataset.fname] = i.value);
    SELECTED_PROVIDERS[p.id] = { id: p.id, fields };
    toast(p.name + " configured");
    closeModal();
  });
}

document.getElementById("btn-subtitles-next").addEventListener("click", async () => {
  const lang = document.getElementById("sub-language").value;
  const providers = Object.values(SELECTED_PROVIDERS);
  const btn = document.getElementById("btn-subtitles-next");
  btn.disabled = true; btn.textContent = "Applying…";
  const r = await api().apply_bazarr(providers, lang);
  btn.disabled = false; btn.textContent = "Apply & continue →";
  if (r.ok) { toast("Bazarr configured ✓"); show("extras"); }
  else toast("Bazarr error: " + (r.error || "?"), true);
});

// ---- quality ----------------------------------------------------------------
let QUALITY = {
  resolution: "1080p",
  release_types: ["bluray", "webdl", "webrip", "hdtv"],
  max_bitrate_mbps: 8.0
};

function gbPerHourFromMbps(mbps) {
  return Number(mbps) * 0.45;
}

function renderBitrateHint() {
  const mbps = Number(document.getElementById("quality-bitrate").value);
  QUALITY.max_bitrate_mbps = mbps;
  document.getElementById("quality-bitrate-value").textContent = mbps.toFixed(1) + " Mbps";
  document.getElementById("quality-bitrate-hint").textContent =
    "≈ " + gbPerHourFromMbps(mbps).toFixed(2) + " GB/hour";
}

function renderQualityControls(options) {
  const resolutions = options.resolutions || [];
  const releaseTypes = options.release_types || [];
  const defaults = options.defaults || {};

  QUALITY = {
    resolution: defaults.resolution || "1080p",
    release_types: (defaults.release_types || []).slice(),
    max_bitrate_mbps: Number(defaults.max_bitrate_mbps || 8.0)
  };

  const resWrap = document.getElementById("quality-options");
  resWrap.innerHTML = resolutions.map(r =>
    `<span class="pill ${r.id === QUALITY.resolution ? "sel" : ""}" data-q="${r.id}">${r.label}</span>`
  ).join("");
  resWrap.querySelectorAll(".pill").forEach(pill => {
    pill.addEventListener("click", () => {
      QUALITY.resolution = pill.dataset.q;
      resWrap.querySelectorAll(".pill").forEach(p => p.classList.remove("sel"));
      pill.classList.add("sel");
    });
  });

  const relWrap = document.getElementById("quality-release-types");
  relWrap.innerHTML = releaseTypes.map(t => {
    const checked = QUALITY.release_types.includes(t.id) ? "checked" : "";
    return `<label><input type="checkbox" data-rel="${t.id}" ${checked}><span>${t.label}</span></label>`;
  }).join("");
  relWrap.querySelectorAll("input[data-rel]").forEach(cb => {
    cb.addEventListener("change", () => {
      QUALITY.release_types = Array.from(
        relWrap.querySelectorAll("input[data-rel]:checked")
      ).map(x => x.dataset.rel);
    });
  });

  const slider = document.getElementById("quality-bitrate");
  slider.value = String(QUALITY.max_bitrate_mbps);
  slider.addEventListener("input", renderBitrateHint);
  renderBitrateHint();
}

async function loadQualityOptions() {
  try {
    const options = await api().get_quality_options();
    renderQualityControls(options || {});
  } catch (_) {
    renderQualityControls({
      resolutions: [
        { id: "720p", label: "HD 720p" },
        { id: "1080p", label: "HD 1080p" },
        { id: "2160p", label: "Ultra-HD 4K 2160p" }
      ],
      release_types: [
        { id: "bluray", label: "BluRay" },
        { id: "webdl", label: "WEB-DL" },
        { id: "webrip", label: "WEBRip" },
        { id: "hdtv", label: "HDTV" },
        { id: "remux", label: "Remux" },
        { id: "dvd", label: "DVD" }
      ],
      defaults: QUALITY
    });
  }
}

document.getElementById("btn-extras-next").addEventListener("click", async () => {
  const btn = document.getElementById("btn-extras-next");
  const status = document.getElementById("quality-status");
  if (!QUALITY.release_types.length) {
    return toast("Select at least one release type.", true);
  }
  btn.disabled = true; btn.textContent = "Applying…";
  status.textContent = "Configuring Sonarr & Radarr quality profiles…";
  const r = await api().set_quality({
    resolution: QUALITY.resolution,
    release_types: QUALITY.release_types,
    max_bitrate_mbps: QUALITY.max_bitrate_mbps
  });
  btn.disabled = false; btn.textContent = "Apply & finish →";
  if (r.ok) {
    status.innerHTML = "<span style='color:#3ddc84'>Quality profile 'piratefish_default' applied ✓</span>";
    show("finish");
  } else {
    status.innerHTML = "<span style='color:#e5534b'>Failed: " + (r.error || "?") + "</span>";
    toast("Quality setup failed: " + (r.error || "?"), true);
  }
});

document.getElementById("btn-open-dash").addEventListener("click", () => {
  if (!REPORT) return;
  const url = REPORT.dashboard_url ||
    (REPORT.services.find(s => /homepage|dashboard/i.test(s.name)) || {}).url ||
    (REPORT.services[0] || {}).url;
  if (url) api().open_url(url);
});

// ---- generic nav -----------------------------------------------------------
document.querySelectorAll("[data-goto]").forEach(b =>
  b.addEventListener("click", () => show(b.dataset.goto)));
document.getElementById("btn-welcome-next").addEventListener("click", () => show("config"));
document.getElementById("modal-bg").addEventListener("click", (e) => {
  if (e.target.id === "modal-bg") closeModal();
});

// ---- boot ------------------------------------------------------------------
function boot() {
  loadEnv();
  loadDefaults();
  loadQualityOptions();
  show("welcome");
}
if (window.pywebview) boot();
else window.addEventListener("pywebviewready", boot);
