const app = document.querySelector("#app"),
  modalRoot = document.querySelector("#modalRoot"),
  toast = document.querySelector("#toast");
const state = {
  user: null,
  vehicles: [],
  services: [],
  reminders: [],
  fuel: [],
  receipts: [],
  mods: [],
  tokens: [],
  users: [],
  settings: { garage_name: "Your Garage" },
  specs: {},
  selected: null,
  tab: "log",
};
const esc = (s) =>
  String(s ?? "").replace(
    /[&<>'"]/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" })[
        c
      ],
  );
const money = (n) =>
  new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" }).format(
    Number(n) || 0,
  );
const miles = (n) => new Intl.NumberFormat("en-US").format(Number(n) || 0);
const metric = () => !!state.settings?.use_kilometers,
  dist = (n) => miles(metric() ? Number(n) * 1.609344 : n),
  unit = () => (metric() ? "km" : "mi"),
  toStored = (n) => (metric() ? Math.round(Number(n) / 1.609344) : Number(n)),
  fromStored = (n) => (metric() ? Math.round(Number(n) * 1.609344) : Number(n)),
  efficiency = (mpg) =>
    metric()
      ? `${(235.214583 / mpg).toFixed(1)} L/100km`
      : `${mpg.toFixed(1)} mpg`;
const date = (s) =>
  s
    ? new Date(`${s}T12:00:00`).toLocaleDateString(undefined, {
        year: "numeric",
        month: "short",
        day: "numeric",
      })
    : "—";
const today = () => new Date().toISOString().slice(0, 10);
const canEdit = (v) =>
  !!state.user && (state.user.is_admin || v.owner_id === state.user.id);
function notify(msg) {
  toast.textContent = msg;
  toast.classList.add("show");
  setTimeout(() => toast.classList.remove("show"), 2200);
}
async function api(path, opts = {}) {
  const isForm = opts.body instanceof FormData,
    r = await fetch("/api" + path, {
      headers: {
        ...(isForm ? {} : { "Content-Type": "application/json" }),
        ...(opts.headers || {}),
      },
      ...opts,
    });
  if (!r.ok) {
    let d = {};
    try {
      d = await r.json();
    } catch {}
    throw new Error(d.detail || `Request failed (${r.status})`);
  }
  return r.status === 204 ? null : r.json();
}
async function boot() {
  const status = await api("/status");
  if (status.setup_required) return authView(true);
  try {
    state.user = await api("/me");
    await load();
    render();
  } catch {
    authView(false);
  }
}
async function load() {
  state.specs = {};
  [
    state.vehicles,
    state.services,
    state.reminders,
    state.fuel,
    state.notes,
    state.mods,
    state.receipts,
    state.settings,
  ] = await Promise.all([
    api("/vehicles"),
    api("/services"),
    api("/reminders"),
    api("/fuel"),
    api("/notes"),
    api("/mods"),
    api("/receipts"),
    api("/settings"),
  ]);
  if (state.user?.is_admin) state.users = await api("/users");
}
function chrome() {
  const name = state.settings?.garage_name || "Your Garage";
  document.querySelector("#garageName").textContent = name;
  document.title = name;
  document
    .querySelectorAll(".auth-only")
    .forEach((x) => (x.style.display = state.user ? "" : "none"));
  document
    .querySelectorAll(".admin-only")
    .forEach((x) => (x.style.display = state.user?.is_admin ? "" : "none"));
  document.querySelector("[data-action=settings]").style.display = state.user
    ? ""
    : "none";
  document.querySelector("#userBadge").textContent = state.user
    ? state.user.username
    : "";
}
function authView(setup) {
  state.user = null;
  state.selected = null;
  state.specs = {};
  state.vehicles = [];
  state.services = [];
  state.reminders = [];
  state.fuel = [];
  state.notes = [];
  state.mods = [];
  state.receipts = [];
  chrome();
  app.innerHTML = `<section class="auth-panel card"><h1>${setup ? "Set up Garage" : "Welcome back"}</h1><p class="sub">${setup ? "Create the first administrator account." : "Sign in to your garage."}</p>${setup ? "" : '<button type="button" class="auth-mode-toggle" id="authMode">Use API token</button>'}<form id="authForm"><div class="field" data-password-login><label>Username</label><input name="username" minlength="3" autocomplete="username" required autofocus></div><div class="field" data-password-login><label>Password</label><input name="password" type="password" minlength="8" autocomplete="${setup ? "new-password" : "current-password"}" required></div>${setup ? "" : '<div class="field" data-token-login hidden><label>API token</label><input name="token" type="password" autocomplete="off" spellcheck="false" disabled required></div>'}<div class="error" id="authError"></div><button class="primary">${setup ? "Create administrator" : "Sign in"}</button></form></section>`;
  if (!setup) {
    let tokenMode = false;
    document.querySelector("#authMode").onclick = () => {
      tokenMode = !tokenMode;
      const passwordFields = document.querySelectorAll("[data-password-login]");
      const tokenField = document.querySelector("[data-token-login]");
      passwordFields.forEach((field) => {
        field.hidden = tokenMode;
        field.querySelector("input").disabled = tokenMode;
      });
      tokenField.hidden = !tokenMode;
      tokenField.querySelector("input").disabled = !tokenMode;
      document.querySelector("#authMode").textContent = tokenMode
        ? "Use username and password"
        : "Use API token";
      document.querySelector("#authError").textContent = "";
      (tokenMode ? tokenField : passwordFields[0]).querySelector("input").focus();
    };
  }
  document.querySelector("#authForm").onsubmit = async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    try {
      await api(setup ? "/setup" : "/login", {
        method: "POST",
        body: JSON.stringify(Object.fromEntries(f)),
      });
      state.user = await api("/me");
      await load();
      render();
    } catch (err) {
      document.querySelector("#authError").textContent = err.message;
    }
  };
}
function render() {
  chrome();
  if (!state.selected) return garage();
  const v = state.vehicles.find((x) => x.id === state.selected);
  if (!v) {
    state.selected = null;
    return garage();
  }
  vehicle(v);
}
function reminderStatus(v, r) {
  const cur = Math.max(v.mileage, v.est_mileage || 0),
    progress = [];
  let labels = [];
  if (r.miles_interval) {
    const used = cur - r.last_mileage,
      p = used / r.miles_interval,
      due = r.last_mileage + r.miles_interval;
    progress.push(p);
    labels.push(`${dist(Math.max(0, due - cur))} ${unit()} remaining`);
  }
  if (r.months_interval) {
    const last = new Date(`${r.last_date}T12:00:00`),
      due = new Date(last);
    due.setMonth(due.getMonth() + r.months_interval);
    const p = (Date.now() - last) / (due - last);
    progress.push(p);
    const days = Math.ceil((due - Date.now()) / 86400000);
    labels.push(days >= 0 ? `${days} days remaining` : `${-days} days late`);
  }
  if (r.due_date) {
    const due = new Date(`${r.due_date}T12:00:00`),
      days = Math.ceil((due - Date.now()) / 86400000);
    progress.push(days < 0 ? 1 : days <= 30 ? 0.8 : 0);
    labels.push(
      (days >= 0 ? `due ${date(r.due_date)}` : `${-days} days late`) +
        (r.repeats_yearly ? " · yearly" : ""),
    );
  }
  const p = Math.max(...progress, 0),
    status = p >= 1 ? "overdue" : p >= 0.8 ? "soon" : "ok";
  return {
    state: status,
    pct: Math.min(1, p),
    label: labels.join(" · ") || "No schedule",
  };
}
function vehicleStatus(v) {
  const rows = state.reminders
      .filter((r) => r.vehicle_id === v.id)
      .map((r) => ({ r, st: reminderStatus(v, r) })),
    states = rows.map((x) => x.st.state),
    statusState = states.includes("overdue")
      ? "overdue"
      : states.includes("soon")
        ? "soon"
        : "ok";
  return {
    state: statusState,
    label: rows.length
      ? rows.map((x) => `${esc(x.r.name)}: ${esc(x.st.label)}`).join(" · ")
      : "No reminders",
  };
}
function vicon(v) {
  return state.settings.use_vehicle_photos && v.photo_url
    ? `<div class="vc-icon"><img class="vc-photo" src="${v.photo_url}" alt=""></div>`
    : `<div class="vc-icon">${esc(v.icon)}</div>`;
}
function garage() {
  state.selected = null;
  const cards = state.vehicles
    .map((v) => {
      const st = vehicleStatus(v),
        fuelRows = state.fuel.filter(
          (f) => f.vehicle_id === v.id && f.mpg != null,
        ),
        avgMpg = fuelRows.length
          ? fuelRows.reduce((sum, f) => sum + f.mpg * f.gallons, 0) /
            fuelRows.reduce((sum, f) => sum + f.gallons, 0)
          : null,
        serviceCost = state.services
          .filter((s) => s.vehicle_id === v.id)
          .reduce((a, s) => a + s.cost, 0),
        fuelCost = state.fuel
          .filter((f) => f.vehicle_id === v.id)
          .reduce((a, f) => a + f.cost, 0),
        modCost = state.mods
          .filter((m) => m.vehicle_id === v.id)
          .reduce((a, m) => a + m.price, 0),
        totalCost = serviceCost + fuelCost + modCost;
      return `<button class="vehicle-card card" data-action="open" data-id="${v.id}"><div class="vc-head">${vicon(v)}<div><div class="vc-name">${esc(v.name)}</div><div class="vc-year">${esc(v.year || "Year not set")}${v.private ? " · Private" : ""}</div></div></div><div class="vc-body"><div class="vc-miles"><b>${dist(v.mileage)}</b> ${unit()}</div>${avgMpg != null ? `<div class="hint vc-mpg">${efficiency(avgMpg)} average</div>` : ``}${v.est_mileage > v.mileage ? `<div class="hint">est. current: ${dist(v.est_mileage)} ${unit()}</div>` : ""}<div class="status-line"><span class="dot ${st.state}"></span><span class="status-text ${st.state}">${st.label}</span></div><div class="vc-cost"><div class="vc-cost-row vc-cost-total"><span>Total</span><strong>${money(totalCost)}</strong></div>${[
        ["Service", serviceCost],
        ["Fuel", fuelCost],
        ["Mods", modCost],
      ]
        .map(
          ([label, amount]) =>
            `<div class="vc-cost-row${amount > 0 ? "" : " vc-cost-zero"}"><span>${label}</span><span>${money(amount)}</span></div>`,
        )
        .join("")}</div></div></button>`;
    })
    .join("");
  app.innerHTML = `<h1>${esc(state.settings.garage_name)}</h1><p class="sub">${state.vehicles.length} vehicle${state.vehicles.length === 1 ? "" : "s"}</p>${state.user.is_admin ? `<div class="vehicle-view-toggle"><label class="check-row"><input type="checkbox" data-action="show-all-vehicles" ${state.user.show_all_vehicles ? "checked" : ""}><span>Show all vehicles</span></label><span class="hint">Include private vehicles owned by other users.</span></div>` : ""}<div class="grid">${cards || '<div class="empty">No vehicles visible.</div>'}</div><div style="margin-top:18px"><button class="primary" data-action="add-vehicle">+ Add vehicle</button></div>`;
}
function visibleTabs() {
  const h = state.settings || {};
  return [
    ["specs", "Specs"],
    ["log", "Maintenance"],
    ["reminders", "Reminders"],
    ["fuel", "Fuel"],
    ["mods", "Mods"],
    ["costs", "Costs"],
    ["notes", "Notes"],
  ].filter(
    ([k]) =>
      !(
        (k === "log" && h.hide_maintenance) ||
        (k === "fuel" && h.hide_fuel) ||
        (k === "costs" && h.hide_costs) ||
        (k === "notes" && h.hide_notes)
      ),
  );
}
function vehicle(v) {
  const st = vehicleStatus(v),
    tabs = visibleTabs();
  if (state.tab === "specs" && !state.specs[v.id])
    api(`/vehicles/${v.id}/specs`).then((x) => {
      if (!state.user || state.selected !== v.id) return;
      state.specs[v.id] = x;
      if (state.tab === "specs") render();
    });
  if (!tabs.some(([k]) => k === state.tab)) state.tab = (tabs[0] || ["log"])[0];
  app.innerHTML = `<button class="back" data-action="garage">← Garage</button><div class="detail-head">${vicon(v)}<div><div class="detail-title">${esc(v.name)}</div><div class="detail-meta">${esc(v.year || "Year not set")} · ${v.private ? "Private · " : ""}${state.user.is_admin ? "Owner: " + esc(v.owner) + " · " : ""}${dist(v.mileage)} ${unit()}${v.mileage_updated_by ? ` · updated by ${esc(v.mileage_updated_by)}` : ""}${v.est_mileage > v.mileage ? ` · est. current ${dist(v.est_mileage)} ${unit()}` : ""} · added by ${esc(v.added_by)}</div></div><span class="pill"><span class="dot ${st.state}"></span>${st.label}</span><div class="spacer"></div>${canEdit(v) ? `<button class="small" data-action="mileage">Update mileage</button><button class="small" data-action="edit-vehicle">Edit</button>` : ""}</div>${
    [
      v.fuel_type && ["Fuel", v.fuel_type],
      v.tire_size && ["Tires", v.tire_size],
      v.oil_spec && ["Oil", v.oil_spec],
    ].filter(Boolean).length
      ? `<dl class="vehicle-specs">${[
          v.fuel_type && ["Fuel", v.fuel_type],
          v.tire_size && ["Tires", v.tire_size],
          v.oil_spec && ["Oil", v.oil_spec],
        ]
          .filter(Boolean)
          .map(([k, val]) => `<div><dt>${k}</dt><dd>${esc(val)}</dd></div>`)
          .join("")}</dl>`
      : ""
  }<div class="tabs">${tabs.map(([k, label]) => `<button class="tab" data-action="tab" data-tab="${k}" aria-selected="${state.tab === k}">${label}</button>`).join("")}</div><div id="tabBody">${state.tab === "specs" ? specsTab(v) : state.tab === "log" ? logTab(v) : state.tab === "reminders" ? remindersTab(v) : state.tab === "fuel" ? fuelTab(v) : state.tab === "costs" ? costTab(v) : state.tab === "mods" ? modsTab(v) : notesTab(v)}</div>`;
}
function receiptThumbs(kind, entryId) {
  const rs = state.receipts.filter(
    (r) => r.kind === kind && r.entry_id === entryId,
  );
  return rs.length
    ? `<div class="receipts">${rs.map((r) => `<a href="/api/receipts/${r.id}" target="_blank" rel="noopener"><img src="/api/receipts/${r.id}" alt="${esc(r.orig_name || "receipt photo")}" loading="lazy"></a>`).join("")}</div>`
    : "";
}
function receiptAdmin(kind, entryId) {
  const rs = state.receipts.filter(
    (r) => r.kind === kind && r.entry_id === entryId,
  );
  return rs.length
    ? `<div class="field"><label>Attached receipts</label><div class="receipts">${rs.map((r) => `<span class="receipt"><a href="/api/receipts/${r.id}" target="_blank" rel="noopener"><img src="/api/receipts/${r.id}" alt="${esc(r.orig_name || "receipt photo")}"></a><button type="button" class="small danger" data-action="delete-receipt" data-id="${r.id}" aria-label="Delete receipt">×</button></span>`).join("")}</div></div>`
    : "";
}
async function uploadReceipts(kind, entryId, input) {
  for (const file of input?.files || []) {
    const fd = new FormData();
    fd.append("kind", kind);
    fd.append("entry_id", entryId);
    fd.append("file", file);
    await api("/receipts", { method: "POST", body: fd });
  }
}
const receiptField = `<div class="field"><label>Receipt photos</label><input name="receipts" type="file" accept="image/*" capture="environment" multiple><div class="hint">Attach one or more photos of the receipt.</div></div>`;
function specsTab(v) {
  const x = state.specs[v.id];
  if (!x)
    return `<div class="card"><div class="empty">Loading specs…</div></div>`;
  const groups = [
      [
        "Powertrain",
        [
          ["engine", "Engine"],
          ["displacement", "Displacement"],
          ["horsepower", "Horsepower"],
          ["torque", "Torque"],
          ["transmission", "Transmission"],
          ["drivetrain", "Drivetrain"],
        ],
      ],
      [
        "Body and Dimensions",
        [
          ["body_style", "Body style"],
          ["exterior_color", "Exterior color"],
          ["vin", "VIN"],
          ["curb_weight", "Curb weight"],
          ["wheelbase", "Wheelbase"],
          ["dimensions", "Dimensions"],
        ],
      ],
      [
        "Wheels and Tires",
        [
          ["wheel_size", "Wheel size"],
          ["tire_size", "Tire size"],
          ["wheel_lug_torque", "Lug torque"],
        ],
      ],
      [
        "Fuel and Economy",
        [
          ["fuel_type", "Fuel type"],
          ["fuel_capacity", "Fuel capacity"],
          ["mpg_city", "MPG city"],
          ["mpg_highway", "MPG highway"],
        ],
      ],
      [
        "Capability",
        [
          ["towing_capacity", "Towing capacity"],
          ["payload", "Payload"],
        ],
      ],
      [
        "Maintenance",
        [
          ["oil_type", "Oil type"],
          ["oil_capacity", "Oil capacity"],
          ["coolant_type", "Coolant type"],
          ["brake_fluid", "Brake fluid"],
          ["battery_group", "Battery group"],
          ["spark_plugs", "Spark plugs"],
          ["wiper_sizes", "Wiper sizes"],
          ["air_filter_part_number", "Air filter part #"],
        ],
      ],
    ],
    value = (k) => x[k],
    sections = groups
      .map(([title, fields], index) => {
        const rows = fields
          .filter(([k]) => value(k))
          .map(
            ([k, label]) =>
              `<div class="spec-row"><dt>${label}</dt><dd>${esc(value(k))}</dd></div>`,
          )
          .join("");
        return rows
          ? {
              index,
              html: `<section class="spec-section" style="--spec-order:${index}"><h2>${title}</h2><dl>${rows}</dl></section>`,
            }
          : null;
      })
      .filter(Boolean),
    sectionColumns = [
      sections.filter((s) => s.index % 2 === 0),
      sections.filter((s) => s.index % 2 === 1),
    ]
      .map(
        (column) =>
          `<div class="spec-column">${column.map((s) => s.html).join("")}</div>`,
      )
      .join("");
  return `${canEdit(v) ? `<div class="toolbar"><button class="primary" data-action="edit-specs">Edit specs</button></div>` : ""}${sections.length ? `<div class="spec-sections">${sectionColumns}</div>` : '<div class="card"><div class="empty">No detailed specs yet.</div></div>'}`;
}
function specsForm(v) {
  const x = state.specs[v.id] || {},
    fields = [
      ["engine", "Engine", "e.g. DOHC inline-4"],
      ["displacement", "Displacement", "e.g. 2.0L"],
      ["transmission", "Transmission", "e.g. 5-speed manual"],
      ["drivetrain", "Drivetrain", "e.g. RWD"],
      ["body_style", "Body style", "e.g. 2-door roadster"],
      ["exterior_color", "Exterior color", ""],
      ["vin", "VIN", ""],
      ["horsepower", "Horsepower", "e.g. 116 hp"],
      ["torque", "Torque", "e.g. 100 lb-ft"],
      ["curb_weight", "Curb weight", "e.g. 2,116 lb"],
      ["wheelbase", "Wheelbase", "e.g. 89.2 in"],
      ["dimensions", "Dimensions", "L × W × H"],
      ["fuel_type", "Fuel type", "e.g. Premium gasoline"],
      ["fuel_capacity", "Fuel capacity", "e.g. 11.9 gal"],
      ["towing_capacity", "Towing capacity", ""],
      ["payload", "Payload", ""],
      ["mpg_city", "MPG city", ""],
      ["mpg_highway", "MPG highway", ""],
      ["oil_type", "Oil type", "e.g. 5W-30"],
      ["oil_capacity", "Oil capacity", "e.g. 4.5 qt"],
      ["battery_group", "Battery group", ""],
      ["spark_plugs", "Spark plugs", "part number / gap"],
      ["wiper_sizes", "Wiper sizes", "driver / passenger / rear"],
      ["coolant_type", "Coolant type", ""],
      ["brake_fluid", "Brake fluid", "e.g. DOT 3"],
      ["air_filter_part_number", "Air filter part #", ""],
      ["wheel_lug_torque", "Wheel / lug torque", "e.g. 80 lb-ft"],
      ["wheel_size", "Wheel size", "e.g. 15 × 6 in"],
      ["tire_size", "Tire size", "e.g. 215/60R16"],
    ];
  modal(
    `<h2>Edit vehicle specs</h2><form id="specsForm"><div class="specs-form-fields">${fields.map(([k, l, p]) => `<div class="field"><label>${l}</label><input name="${k}" maxlength="160" value="${esc(x[k] || "")}" placeholder="${esc(p)}"></div>`).join("")}</div>${actions()}</form>`,
  );
  document.querySelector("#specsForm").onsubmit = async (e) => {
    e.preventDefault();
    const saved = await api(`/vehicles/${v.id}/specs`, {
      method: "PUT",
      body: JSON.stringify(Object.fromEntries(new FormData(e.target))),
    });
    state.specs[v.id] = saved;
    const hv = state.vehicles.find((item) => item.id === v.id);
    if (hv)
      Object.assign(hv, {
        fuel_type: saved.fuel_type,
        tire_size: saved.tire_size,
        oil_spec: [saved.oil_type, saved.oil_capacity]
          .filter(Boolean)
          .join(", "),
      });
    close();
    render();
    notify("Specs saved");
  };
}
function logTab(v) {
  const rows = state.services
    .filter((s) => s.vehicle_id === v.id)
    .map(
      (s) =>
        `<div class="entry"><div class="e-top"><div class="e-type">${esc(s.type)}</div><div class="e-cost">${s.cost ? money(s.cost) : ""}</div></div><div class="e-sub">${s.date ? date(s.date) : "Date not set"} · ${dist(s.mileage)} ${unit()}${s.logged_by ? ` · entered by ${esc(s.logged_by)}` : ""}${s.provider ? " · " + esc(s.provider) : ""}${s.notes ? " · " + esc(s.notes) : ""}${s.reminder_id && state.reminders.find((r) => r.id === s.reminder_id) ? " · reset " + esc(state.reminders.find((r) => r.id === s.reminder_id).name) : ""}</div>${s.torque_specs || s.fluids || s.gotchas || s.youtube_url ? `<details class="mod-details"><summary>Install notes</summary><dl>${s.torque_specs ? `<div><dt>Torque</dt><dd>${esc(s.torque_specs)}</dd></div>` : ""}${s.fluids ? `<div><dt>Fluids</dt><dd>${esc(s.fluids)}</dd></div>` : ""}${s.gotchas ? `<div><dt>Gotchas</dt><dd>${esc(s.gotchas)}</dd></div>` : ""}${s.youtube_url ? `<div><dt>Video</dt><dd><a href="${esc(s.youtube_url)}" target="_blank" rel="noopener">Watch on YouTube</a></dd></div>` : ""}</dl></details>` : ""}${receiptThumbs("service", s.id)}${canEdit(v) ? `<div class="e-actions"><button class="small ghost" data-action="edit-service" data-id="${s.id}">Edit</button><button class="small ghost danger" data-action="delete-service" data-id="${s.id}">Delete</button></div>` : ""}</div>`,
    )
    .join("");
  return `${canEdit(v) ? `<div class="toolbar"><button class="primary" data-action="add-service">+ Log service</button></div>` : ""}<div class="card">${rows || '<div class="empty">No services logged yet.</div>'}</div>`;
}
function remindersTab(v) {
  const rows = state.reminders
    .filter((r) => r.vehicle_id === v.id)
    .map((r) => {
      const st = reminderStatus(v, r),
        sched = [
          r.miles_interval && `Every ${dist(r.miles_interval)} ${unit()}`,
          r.months_interval && `Every ${r.months_interval} mo`,
          r.due_date &&
            `Due ${date(r.due_date)}${r.repeats_yearly ? " · yearly" : ""}`,
        ]
          .filter(Boolean)
          .join(" · ");
      return `<div class="rem"><div class="rem-head"><span class="rem-name">${esc(r.name)}</span><span class="badge ${st.state}">${st.state === "ok" ? "OK" : st.state === "soon" ? "Due soon" : "Overdue"}</span>${canEdit(v) ? `<span class="rem-actions"><button class="small primary" data-action="log-reminder" data-id="${r.id}">Log</button><button class="small ghost" data-action="edit-reminder" data-id="${r.id}">Edit</button><button class="small ghost danger" data-action="delete-reminder" data-id="${r.id}">Delete</button></span>` : ""}</div><div class="meter"><div class="bar ${st.state}" style="width:${st.pct * 100}%"></div></div><div class="rem-detail"><span>${esc(st.label)}</span><span>${esc(sched)}</span></div></div>`;
    })
    .join("");
  return `${canEdit(v) ? `<div class="toolbar"><button class="primary" data-action="add-reminder">+ Add reminder</button></div>` : ""}<div class="card">${rows || '<div class="empty">No maintenance items yet.</div>'}</div>`;
}
function fuelTab(v) {
  const rows = state.fuel.filter((f) => f.vehicle_id === v.id);
  let tm = 0,
    tg = 0,
    tc = 0;
  rows.forEach((f) => {
    if (f.mpg != null) {
      tm += f.mpg * f.gallons;
      tg += f.gallons;
      tc += f.cost;
    }
  });
  const avg = tg ? tm / tg : null,
    cpm = tm ? tc / tm : null;
  const list = rows
    .map(
      (f) =>
        `<div class="entry"><div class="e-top"><div class="e-type">${dist(f.odometer)} ${unit()}</div><div class="e-cost">${f.cost ? money(f.cost) : ""}</div></div><div class="e-sub">${date(f.date)} · ${f.gallons} gal${f.octane ? ` · ${esc(f.octane)} octane` : ""}${f.mpg != null ? ` · ${efficiency(f.mpg)}` : ""}${f.logged_by ? ` · entered by ${esc(f.logged_by)}` : ""}</div>${receiptThumbs("fuel", f.id)}${canEdit(v) ? `<div class="e-actions"><button class="small ghost" data-action="edit-fuel" data-id="${f.id}">Edit</button><button class="small ghost danger" data-action="delete-fuel" data-id="${f.id}">Delete</button></div>` : ""}</div>`,
    )
    .join("");
  return `<div class="card stats" style="margin-bottom:14px"><div class="row2"><div><div class="cost-total">${avg != null ? avg.toFixed(1) : "—"}</div><div class="hint">running avg MPG</div></div><div><div class="cost-total">${cpm != null ? "$" + cpm.toFixed(3) : "—"}</div><div class="hint">fuel cost per mile</div></div></div></div>${canEdit(v) ? `<div class="toolbar"><button class="primary" data-action="add-fuel">+ Log fill-up</button></div>` : ""}<div class="card">${list || '<div class="empty">No fill-ups logged yet.</div>'}</div>`;
}
function notesTab(v) {
  const rows = state.notes.filter((n) => n.vehicle_id === v.id);
  const list = rows
    .map(
      (n) =>
        `<div class="entry"><div class="e-top"><div class="e-type">${date(n.date)}</div></div><div class="e-sub">${esc(n.body)}${n.logged_by ? ` · entered by ${esc(n.logged_by)}` : ""}</div>${canEdit(v) ? `<div class="e-actions"><button class="small ghost" data-action="edit-note" data-id="${n.id}">Edit</button><button class="small ghost danger" data-action="delete-note" data-id="${n.id}">Delete</button></div>` : ""}</div>`,
    )
    .join("");
  return `${canEdit(v) ? `<div class="toolbar"><button class="primary" data-action="add-note">+ Add note</button></div>` : ""}<div class="card">${list || '<div class="empty">No notes yet.</div>'}</div>`;
}
function noteForm(n = { date: today(), body: "" }) {
  modal(
    `<h2>${n.id ? "Edit" : "Add"} note</h2><form id="noteForm"><div class="field"><label>Date</label><input name="date" type="date" required value="${esc(n.date)}"></div><div class="field"><label>Note</label><textarea name="body" required maxlength="2000" placeholder="Anything worth remembering about this vehicle">${esc(n.body)}</textarea></div><div class="modal-actions"><div class="spacer"></div><button type="button" class="small" data-action="close">Cancel</button><button class="primary">${n.id ? "Save" : "Add"} note</button></div></form>`,
  );
  document.querySelector("#noteForm").onsubmit = async (e) => {
    e.preventDefault();
    const d = Object.fromEntries(new FormData(e.target));
    d.vehicle_id = state.selected;
    await api(n.id ? `/notes/${n.id}` : "/notes", {
      method: n.id ? "PUT" : "POST",
      body: JSON.stringify(d),
    });
    await load();
    close();
    render();
    notify("Note saved");
  };
}
function modsTab(v) {
  const rows = state.mods
    .filter((m) => m.vehicle_id === v.id)
    .map(
      (m, index) =>
        `<div class="entry mod-card"><div class="e-top"><span class="entry-number" aria-label="Modification ${index + 1}">${index + 1}</span><div class="e-type">${esc(m.name)}</div><div class="e-cost">${m.price ? money(m.price) : ""}</div></div><div class="e-sub">${m.date ? date(m.date) : "Date not set"}${m.logged_by ? ` · entered by ${esc(m.logged_by)}` : ""}</div>${m.torque_specs || m.gotchas || m.youtube_url ? `<details class="mod-details"><summary>Install notes</summary><dl>${m.torque_specs ? `<div><dt>Torque</dt><dd>${esc(m.torque_specs)}</dd></div>` : ""}${m.gotchas ? `<div><dt>Mod Notes</dt><dd>${esc(m.gotchas)}</dd></div>` : ""}${m.youtube_url ? `<div><dt>Video</dt><dd><a href="${esc(m.youtube_url)}" target="_blank" rel="noopener">Watch on YouTube</a></dd></div>` : ""}</dl></details>` : ""}${canEdit(v) ? `<div class="e-actions"><button class="small ghost" data-action="edit-mod" data-id="${m.id}">Edit</button><button class="small ghost danger" data-action="delete-mod" data-id="${m.id}">Delete</button></div>` : ""}</div>`,
    )
    .join("");
  return `${canEdit(v) ? `<div class="toolbar"><button class="primary" data-action="add-mod">+ Add mod</button></div>` : ""}${rows ? `<div class="mods-grid">${rows}</div>` : '<div class="card"><div class="empty">No modifications yet.</div></div>'}`;
}
function modForm(
  m = {
    name: "",
    date: "",
    price: "",
    torque_specs: "",
    gotchas: "",
    youtube_url: "",
  },
) {
  modal(
    `<h2>${m.id ? "Edit" : "Add"} modification</h2><form id="modForm"><div class="field"><label>Name</label><input name="name" required maxlength="120" value="${esc(m.name)}"></div><div class="row2"><div class="field"><label>Date (optional)</label><input name="date" type="date" value="${esc(m.date || "")}"></div><div class="field"><label>Price ($)</label><input name="price" type="number" min="0" step=".01" value="${m.price}"></div></div><div class="field"><label>Torque specs</label><input name="torque_specs" value="${esc(m.torque_specs || "")}" placeholder="e.g. 89 lb-ft"></div><div class="field"><label>Mod Notes</label><textarea name="gotchas" placeholder="Install tips or notes">${esc(m.gotchas || "")}</textarea></div><div class="field"><label>YouTube link</label><input name="youtube_url" type="url" value="${esc(m.youtube_url || "")}" placeholder="https://youtube.com/..."></div>${actions()}</form>`,
  );
  document.querySelector("#modForm").onsubmit = async (e) => {
    e.preventDefault();
    const d = Object.fromEntries(new FormData(e.target));
    d.vehicle_id = state.selected;
    d.fluids = m.fluids || "";
    d.price = +d.price || 0;
    d.date = d.date || null;
    await api(m.id ? `/mods/${m.id}` : "/mods", {
      method: m.id ? "PUT" : "POST",
      body: JSON.stringify(d),
    });
    await load();
    close();
    render();
    notify("Modification saved");
  };
}
function costTab(v) {
  const rows = state.services.filter((s) => s.vehicle_id === v.id),
    serviceTotal = rows.reduce((a, s) => a + s.cost, 0),
    fuelTotal = state.fuel
      .filter((f) => f.vehicle_id === v.id)
      .reduce((a, f) => a + f.cost, 0),
    modTotal = state.mods
      .filter((m) => m.vehicle_id === v.id)
      .reduce((a, m) => a + m.price, 0),
    total = serviceTotal + fuelTotal + modTotal,
    byYear = {},
    byType = {};
  rows.forEach((s) => {
    if (s.date) {
      const y = s.date.slice(0, 4);
      byYear[y] = (byYear[y] || 0) + s.cost;
    }
    byType[s.type] = (byType[s.type] || 0) + s.cost;
  });
  const max = Math.max(...Object.values(byYear), 1),
    bars = Object.keys(byYear)
      .sort()
      .map(
        (y) =>
          `<div class="bar-col"><div class="bar-val">${money(byYear[y])}</div><div class="bar" style="height:${Math.round((byYear[y] / max) * 110)}px"></div><div class="bar-year">${y}</div></div>`,
      )
      .join(""),
    types = Object.entries(byType)
      .sort((a, b) => b[1] - a[1])
      .map(([k, n]) => `<tr><td>${esc(k)}</td><td>${money(n)}</td></tr>`)
      .join("");
  return `<div class="card stats" style="margin-bottom:14px"><div class="row2 cost-summary"><div><div class="cost-total">${money(serviceTotal)}</div><div class="hint">service total</div></div><div><div class="cost-total">${money(fuelTotal)}</div><div class="hint">fuel total</div></div><div><div class="cost-total">${money(modTotal)}</div><div class="hint">mod total</div></div><div><div class="cost-total">${money(total)}</div><div class="hint">combined total</div></div></div></div><div class="card stats" style="margin-bottom:14px"><h2>Per year</h2><div class="bars">${bars || '<div class="empty">No costs logged yet.</div>'}</div></div>${types ? `<div class="card stats"><h2>By service type</h2><table class="type-totals">${types}</table></div>` : ""}`;
}
function modal(html) {
  modalRoot.innerHTML = `<div class="modal-back" data-action="close"><div class="modal" role="dialog" aria-modal="true">${html}</div></div>`;
  setTimeout(() => modalRoot.querySelector("input,button")?.focus());
}
function close() {
  modalRoot.innerHTML = "";
}
function closeHeaderMenu() {
  const m = document.querySelector("#headerMenu"),
    b = document.querySelector("[data-action=toggle-menu]");
  m?.classList.remove("open");
  b?.setAttribute("aria-expanded", "false");
}
function actions(title, deleteButton = "") {
  return `<div class="modal-actions">${deleteButton}<div class="spacer"></div><button type="button" class="small" data-action="close">Cancel</button><button class="primary">Save</button></div>`;
}
function vehicleForm(
  v = {
    name: "",
    year: "",
    mileage: 0,
    icon: "🚗",
    owner_id: state.user.id,
    private: false,
  },
) {
  modal(
    `<h2>${v.id ? "Edit" : "Add"} vehicle</h2><form id="vehicleForm"><div class="row2"><div class="field"><label>Name</label><input name="name" required value="${esc(v.name)}"></div><div class="field"><label>Year</label><input name="year" maxlength="4" value="${esc(v.year)}"></div></div><div class="row2"><div class="field"><label>Current distance (${unit()})</label><input name="mileage" type="number" min="0" value="${fromStored(v.mileage)}"></div><div class="field"><label>Icon</label><select name="icon">${["🚗", "🛻", "🚙", "🏎️", "🏍️", "🚐"].map((x) => `<option ${x === v.icon ? "selected" : ""}>${x}</option>`)}</select></div></div><div class="field"><div class="hint">Fuel type, tire size, and oil shown under the vehicle name come from its Specs tab.</div></div>${
      state.user.is_admin
        ? `<div class="field"><label>Owner</label><select name="owner_id">${state.users
            .filter((u) => u.active)
            .map(
              (u) =>
                `<option value="${u.id}" ${u.id == v.owner_id ? "selected" : ""}>${esc(u.username)}</option>`,
            )
            .join("")}</select></div>`
        : ""
    }<div class="field"><label class="check-row"><input name="private" type="checkbox" ${v.private ? "checked" : ""}><span>Private vehicle</span></label><div class="hint">Only the owner and administrators can see a private vehicle.</div></div><div class="field"><label>Vehicle photo</label><input name="photo" type="file" accept="image/*" capture="environment"><div class="hint">Used as the card icon when vehicle photos are enabled in Settings.</div></div>${v.id && v.photo_url ? `<div class="field"><label>Current photo</label><div class="receipts"><span class="receipt"><a href="${v.photo_url}" target="_blank" rel="noopener"><img src="${v.photo_url}" alt="vehicle photo"></a><button type="button" class="small danger" data-action="delete-receipt" data-id="${v.photo_receipt_id}" aria-label="Delete photo">×</button></span></div></div>` : ""}${actions("", v.id ? `<button type="button" class="danger" data-action="delete-vehicle">Delete vehicle</button>` : "")}</form>`,
  );
  document.querySelector("#vehicleForm").onsubmit = async (e) => {
    e.preventDefault();
    const f = Object.fromEntries(new FormData(e.target));
    f.mileage = toStored(f.mileage);
    f.private = !!f.private;
    if (f.owner_id) f.owner_id = +f.owner_id;
    const savedVehicle = await api(v.id ? `/vehicles/${v.id}` : "/vehicles", {
      method: v.id ? "PUT" : "POST",
      body: JSON.stringify(f),
    });
    if (e.target.photo?.files[0]) {
      const fd = new FormData();
      fd.append("file", e.target.photo.files[0]);
      await api(`/vehicles/${savedVehicle.id}/photo`, {
        method: "POST",
        body: fd,
      });
    }
    await load();
    close();
    render();
    notify("Vehicle saved");
  };
}
function serviceForm(
  s = {
    date: today(),
    mileage: state.vehicles.find((v) => v.id === state.selected).mileage,
    type: "",
    cost: "",
    provider: "",
    notes: "",
    torque_specs: "",
    fluids: "",
    gotchas: "",
    youtube_url: "",
  },
  rem = null,
) {
  if (rem) {
    s.type = rem.name;
    s.mileage = state.vehicles.find((v) => v.id === state.selected).mileage;
    s.reminder_id = rem.id;
  }
  s.vehicle_id = state.selected;
  modal(
    `<h2>${s.id ? "Edit" : "Log"} service</h2><form id="serviceForm"><div class="row2"><div class="field"><label>Date</label><input name="date" type="date" value="${esc(s.date || "")}"></div><div class="field"><label>Distance (${unit()})</label><input name="mileage" type="number" min="0" required value="${fromStored(s.mileage)}"></div></div><div class="field"><label>Service type</label><input name="type" required value="${esc(s.type)}" placeholder="Oil & filter change"></div><div class="field"><label>Marks maintenance done (optional)</label><select name="reminder_id" ${s.date ? "" : "disabled"}><option value="">None</option>${state.reminders
      .filter((r) => r.vehicle_id === state.selected)
      .map(
        (r) =>
          `<option value="${r.id}" ${s.reminder_id === r.id ? "selected" : ""}>${esc(r.name)}</option>`,
      )
      .join(
        "",
      )}</select><div class="hint">The chosen item's last-done date and mileage reset to this service, so it does not need a separate update.</div></div><div class="row2"><div class="field"><label>Cost ($)</label><input name="cost" type="number" min="0" step=".01" value="${s.cost}"></div><div class="field"><label>Shop / DIY</label><input name="provider" value="${esc(s.provider)}" placeholder="DIY"></div></div><div class="field"><label>Notes</label><textarea name="notes">${esc(s.notes)}</textarea></div><div class="field"><label>Torque specs</label><input name="torque_specs" value="${esc(s.torque_specs || "")}" placeholder="e.g. drain plug 29 lb-ft"></div><div class="field"><label>Fluids</label><input name="fluids" value="${esc(s.fluids || "")}" placeholder="Type and quantity"></div><div class="field"><label>Gotchas</label><textarea name="gotchas" placeholder="Tips or cautions">${esc(s.gotchas || "")}</textarea></div><div class="field"><label>YouTube link</label><input name="youtube_url" type="url" value="${esc(s.youtube_url || "")}" placeholder="https://youtube.com/..."></div>${receiptField}${s.id ? receiptAdmin("service", s.id) : ""}${actions()}</form>`,
  );
  const form = document.querySelector("#serviceForm"),
    syncReminder = () => {
      form.reminder_id.disabled = !form.date.value;
      if (!form.date.value) form.reminder_id.value = "";
    };
  form.date.onchange = syncReminder;
  syncReminder();
  form.onsubmit = async (e) => {
    e.preventDefault();
    const f = Object.fromEntries(new FormData(e.target));
    Object.assign(f, {
      vehicle_id: state.selected,
      date: f.date || null,
      mileage: toStored(f.mileage),
      cost: +f.cost || 0,
      reminder_id: f.date && f.reminder_id ? +f.reminder_id : null,
    });
    const saved = await api(s.id ? `/services/${s.id}` : "/services", {
      method: s.id ? "PUT" : "POST",
      body: JSON.stringify(f),
    });
    await uploadReceipts("service", saved.id, e.target.receipts);
    await load();
    close();
    render();
    notify("Service saved");
  };
}
function fuelForm(
  f = {
    date: today(),
    odometer: state.vehicles.find((v) => v.id === state.selected).mileage,
    gallons: "",
    cost: "",
    octane: "",
  },
) {
  modal(
    `<h2>${f.id ? "Edit" : "Log"} fill-up</h2><form id="fuelForm"><div class="row2"><div class="field"><label>Date</label><input name="date" type="date" required value="${esc(f.date)}"></div><div class="field"><label>Odometer (${unit()})</label><input name="odometer" type="number" min="0" required value="${fromStored(f.odometer)}"></div></div><div class="row2"><div class="field"><label>Gallons</label><input name="gallons" type="number" min="0.01" step="any" required value="${f.gallons}"></div><div class="field"><label>Total cost ($)</label><input name="cost" type="number" min="0" step=".01" value="${f.cost}"></div></div><div class="field"><label>Octane (optional)</label><input name="octane" list="octaneOptions" maxlength="20" value="${esc(f.octane || "")}" placeholder="87, 89, 91, 93, or other"><datalist id="octaneOptions"><option value="87"><option value="89"><option value="91"><option value="93"></datalist></div>${receiptField}${f.id ? receiptAdmin("fuel", f.id) : ""}${actions()}</form>`,
  );
  document.querySelector("#fuelForm").onsubmit = async (e) => {
    e.preventDefault();
    const x = Object.fromEntries(new FormData(e.target));
    Object.assign(x, {
      vehicle_id: state.selected,
      odometer: toStored(x.odometer),
      gallons: +x.gallons,
      cost: +x.cost || 0,
    });
    const savedFuel = await api(f.id ? `/fuel/${f.id}` : "/fuel", {
      method: f.id ? "PUT" : "POST",
      body: JSON.stringify(x),
    });
    await uploadReceipts("fuel", savedFuel.id, e.target.receipts);
    await load();
    close();
    render();
    notify("Fill-up saved");
  };
}
function reminderForm(
  r = {
    name: "",
    miles_interval: "",
    months_interval: "",
    due_date: "",
    repeats_yearly: false,
    last_date: today(),
    last_mileage: state.vehicles.find((v) => v.id === state.selected).mileage,
  },
) {
  const inferredType =
    r.due_date && !r.miles_interval && !r.months_interval
      ? "renewal"
      : "service";
  modal(
    `<h2>${r.id ? "Edit" : "Add"} reminder</h2><form id="reminderForm"><div class="field"><label>Reminder type</label><select name="reminder_type"><option value="renewal" ${inferredType === "renewal" ? "selected" : ""}>Renewal deadline</option><option value="service" ${inferredType === "service" ? "selected" : ""}>Service interval</option></select></div><div class="field"><label>Item</label><input name="name" required value="${esc(r.name)}" placeholder="${inferredType === "renewal" ? "Registration renewal" : "Oil & filter change"}"></div><div data-reminder-fields="renewal"><div class="field"><label>Due date</label><input name="due_date" type="date" value="${esc(r.due_date || "")}"><div class="hint">For registration, inspection, warranty, or another renewal deadline.</div></div><div class="field"><label class="check-row"><input name="repeats_yearly" type="checkbox" ${r.repeats_yearly ? "checked" : ""}><span>Repeats yearly</span></label></div></div><div data-reminder-fields="service"><div class="row2"><div class="field"><label>Every (${unit()})</label><input name="miles_interval" type="number" min="1" value="${r.miles_interval ? fromStored(r.miles_interval) : ""}"></div><div class="field"><label>Every (months)</label><input name="months_interval" type="number" min="1" value="${r.months_interval ?? ""}"></div></div><div class="row2"><div class="field"><label>Last done (date)</label><input name="last_date" type="date" value="${esc(r.last_date || today())}"></div><div class="field"><label>Last done (${unit()})</label><input name="last_mileage" type="number" min="0" value="${fromStored(r.last_mileage || 0)}"></div></div></div>${actions()}</form>`,
  );
  const form = document.querySelector("#reminderForm"),
    type = form.reminder_type,
    toggle = () => {
      form
        .querySelectorAll("[data-reminder-fields]")
        .forEach((x) => (x.hidden = x.dataset.reminderFields !== type.value));
      form.due_date.required = type.value === "renewal";
    };
  type.onchange = toggle;
  toggle();
  form.onsubmit = async (e) => {
    e.preventDefault();
    const f = Object.fromEntries(new FormData(e.target)),
      renewal = f.reminder_type === "renewal";
    delete f.reminder_type;
    Object.assign(f, {
      vehicle_id: state.selected,
      miles_interval: renewal
        ? null
        : f.miles_interval
          ? toStored(f.miles_interval)
          : null,
      months_interval: renewal ? null : +f.months_interval || null,
      due_date: renewal ? f.due_date || null : null,
      repeats_yearly: renewal && !!f.repeats_yearly,
      last_date: renewal ? r.last_date || today() : f.last_date || today(),
      last_mileage: renewal ? r.last_mileage || 0 : toStored(f.last_mileage),
    });
    await api(r.id ? `/reminders/${r.id}` : "/reminders", {
      method: r.id ? "PUT" : "POST",
      body: JSON.stringify(f),
    });
    await load();
    close();
    render();
    notify("Reminder saved");
  };
}

async function settingsView() {
  const s = state.settings,
    isAdmin = !!state.user?.is_admin;
  state.tokens = await api("/tokens");
  const notifySettings = isAdmin ? await api("/notifications") : null;
  const cb = (k, label) =>
    `<label class="check-row"><input name="${k}" type="checkbox" ${s[k] ? "checked" : ""}><span>${label}</span></label>`;
  const tokenRows = state.tokens
    .map(
      (t) =>
        `<div class="user-row"><div class="user-info"><b>${esc(t.name)}</b> <span class="tag">${esc(t.prefix)}…</span><div class="hint">Created ${date(t.created_at.slice(0, 10))}${t.last_used_at ? ` · last used ${date(t.last_used_at.slice(0, 10))}` : " · never used"}</div></div><button class="small danger" data-action="revoke-token" data-id="${t.id}">Revoke</button></div>`,
    )
    .join("");
  const adminSettings = isAdmin
    ? `<div class="card settings-card"><form id="settingsForm"><div class="field"><label>Garage name</label><input name="garage_name" required maxlength="80" value="${esc(s.garage_name)}"><div class="hint">Shown in the header and on the garage home screen.</div></div><h2>Vehicle page sections</h2><p class="hint">Hidden sections disappear from every vehicle page for all users.</p>${cb("hide_maintenance", "Hide Maintenance")}${cb("hide_fuel", "Hide Fuel")}${cb("hide_costs", "Hide Costs")}${cb("hide_notes", "Hide Notes")}<h2>Vehicle photos</h2>${cb("use_vehicle_photos", "Use vehicle photos as card icons")}<h2>Distance units</h2>${cb("use_kilometers", "Use kilometers and L/100km")}<p class="hint">When off, vehicles show their emoji icon instead.</p><button class="primary">Save settings</button></form></div>`
    : "";
  const notifications = isAdmin
    ? `<div class="card settings-card" style="margin-top:14px"><h2>Notifications</h2><p class="hint">Once a day Garage checks maintenance items against current or estimated mileage and dates, and sends one notification when an item newly becomes due soon or overdue. Add one or more Apprise URLs, one per line (discord://, tgram://, mailto://, ...). An http(s) URL receives a plain JSON webhook POST instead.</p><form id="notifyForm"><div class="field"><label>Apprise URLs</label><textarea name="apprise_urls" placeholder="tgram://bot_token/chat_id">${esc(notifySettings.apprise_urls)}</textarea></div><div class="modal-actions" style="margin-top:0"><button class="primary">Save</button><button type="button" class="small" data-action="test-notify">Send test notification</button></div></form></div>`
    : "";
  app.innerHTML = `<button class="back" data-action="garage">← Garage</button><div class="detail-head"><div><div class="detail-title">Settings</div><div class="detail-meta">${isAdmin ? "Customize this garage" : "Manage your account"}</div></div></div>${adminSettings}<div class="card settings-card" style="margin-top:${isAdmin ? "14" : "0"}px"><h2>API tokens</h2><p class="hint">Tokens let scripts read garage data and log entries through the REST API. Each token uses your account permissions. The full token is shown once at creation and stored hashed.</p><p class="warning"><b>Keep tokens private.</b> A token gives full API access to your garage data. Do not paste it into sites or apps you do not trust.</p><form id="tokenForm"><div class="field"><label>Token name</label><input name="name" required maxlength="60" placeholder="Home automation"></div><button class="primary">Create token</button></form><div class="token-list">${tokenRows || '<div class="empty">No tokens yet.</div>'}</div></div>${notifications}`;
  if (isAdmin) {
    document.querySelector("#settingsForm").onsubmit = async (e) => {
      e.preventDefault();
      const f = new FormData(e.target),
        d = { garage_name: f.get("garage_name") };
      for (const k of [
        "hide_maintenance",
        "hide_fuel",
        "hide_costs",
        "hide_notes",
        "use_vehicle_photos",
        "use_kilometers",
      ])
        d[k] = f.has(k);
      state.settings = await api("/settings", {
        method: "PUT",
        body: JSON.stringify(d),
      });
      chrome();
      settingsView();
      notify("Settings saved");
    };
    document.querySelector("#notifyForm").onsubmit = async (e) => {
      e.preventDefault();
      const d = Object.fromEntries(new FormData(e.target));
      await api("/notifications", { method: "PUT", body: JSON.stringify(d) });
      settingsView();
      notify("Notification settings saved");
    };
  }
  document.querySelector("#tokenForm").onsubmit = async (e) => {
    e.preventDefault();
    const name = new FormData(e.target).get("name");
    const t = await api("/tokens", {
      method: "POST",
      body: JSON.stringify({ name }),
    });
    modal(
      `<h2>Token created</h2><p class="hint">Copy this token now. It is shown only once and cannot be recovered later.</p><p class="warning"><b>Keep it private.</b> This token gives full API access to your garage data. Do not paste it into sites or apps you do not trust.</p><div class="field"><input readonly value="${esc(t.token)}" onclick="this.select()"></div><div class="modal-actions"><div class="spacer"></div><button class="primary" data-action="close">Done</button></div>`,
    );
    settingsView();
  };
}

function usersView() {
  app.innerHTML = `<button class="back" data-action="garage">← Garage</button><div class="detail-head"><div><div class="detail-title">Users</div><div class="detail-meta">Manage access to the garage</div></div><div class="spacer"></div><button class="primary" data-action="add-user">+ Add user</button></div><div class="card">${state.users.map((u) => `<div class="user-row"><div class="user-info"><b>${esc(u.username)}</b> ${u.is_admin ? '<span class="tag">Admin</span>' : ""} ${!u.active ? '<span class="tag">Inactive</span>' : ""}</div>${u.id !== state.user.id ? `<button class="small ${u.active ? "danger" : ""}" data-action="toggle-user" data-id="${u.id}">${u.active ? "Disable" : "Enable"}</button>` : ""}<button class="small" data-action="edit-user" data-id="${u.id}">Edit</button></div>`).join("")}</div>`;
}
function userForm(u = { username: "", is_admin: false, active: true }) {
  modal(
    `<h2>${u.id ? "Edit" : "Add"} user</h2><form id="userForm"><div class="field"><label>Username</label><input name="username" required minlength="3" value="${esc(u.username)}"></div><div class="field"><label>${u.id ? "New password (leave blank to keep current)" : "Password"}</label><input name="password" type="password" ${u.id ? "" : "required"} minlength="8"></div><div class="field"><label class="check-row"><input name="is_admin" type="checkbox" ${u.is_admin ? "checked" : ""}><span>Administrator</span></label></div>${u.id ? `<div class="field"><label class="check-row"><input name="active" type="checkbox" ${u.active ? "checked" : ""}><span>Active</span></label></div>` : ""}${actions()}</form>`,
  );
  document.querySelector("#userForm").onsubmit = async (e) => {
    e.preventDefault();
    const d = Object.fromEntries(new FormData(e.target));
    d.is_admin = !!d.is_admin;
    if (u.id) d.active = !!d.active;
    else if (!d.password) throw Error("Password required");
    if (!d.password) delete d.password;
    await api(u.id ? `/users/${u.id}` : "/users", {
      method: u.id ? "PUT" : "POST",
      body: JSON.stringify(d),
    });
    state.users = await api("/users");
    close();
    usersView();
    notify("User saved");
  };
}
document.addEventListener("click", async (e) => {
  const b = e.target.closest("[data-action]");
  if (!b) {
    if (!e.target.closest(".header-menu")) closeHeaderMenu();
    return;
  }
  const a = b.dataset.action,
    id = +b.dataset.id;
  if (!["toggle-menu", "refresh"].includes(a)) closeHeaderMenu();
  if (a === "close") {
    if (e.target === b || b.tagName === "BUTTON") close();
  } else if (a === "toggle-menu") {
    const m = document.querySelector("#headerMenu"),
      open = m.classList.toggle("open");
    b.setAttribute("aria-expanded", String(open));
  } else if (a === "refresh") {
    await load();
    render();
    notify("Refreshed");
  } else if (a === "show-all-vehicles") {
    state.user = await api("/me/vehicle-view", {
      method: "PUT",
      body: JSON.stringify({ show_all: b.checked }),
    });
    await load();
    render();
    notify(
      b.checked ? "Showing all vehicles" : "Showing own and shared vehicles",
    );
  } else if (a === "garage") {
    state.selected = null;
    render();
  } else if (a === "open") {
    state.selected = id;
    state.tab = "log";
    render();
  } else if (a === "tab") {
    state.tab = b.dataset.tab;
    render();
  } else if (a === "add-vehicle") vehicleForm();
  else if (a === "edit-vehicle")
    vehicleForm(state.vehicles.find((v) => v.id === state.selected));
  else if (a === "delete-vehicle") {
    if (
      confirm(
        "Delete this vehicle and all of its service history and reminders?",
      )
    ) {
      await api(`/vehicles/${state.selected}`, { method: "DELETE" });
      state.selected = null;
      await load();
      close();
      render();
    }
  } else if (a === "edit-specs")
    specsForm(state.vehicles.find((v) => v.id === state.selected));
  else if (a === "mileage") {
    const v = state.vehicles.find((v) => v.id === state.selected);
    vehicleForm(v);
  } else if (a === "add-service") serviceForm();
  else if (a === "edit-service")
    serviceForm(state.services.find((s) => s.id === id));
  else if (a === "delete-service") {
    if (confirm("Delete this service entry?")) {
      await api(`/services/${id}`, { method: "DELETE" });
      await load();
      render();
    }
  } else if (a === "add-fuel") fuelForm();
  else if (a === "edit-fuel") fuelForm(state.fuel.find((f) => f.id === id));
  else if (a === "delete-fuel") {
    if (confirm("Delete this fill-up?")) {
      await api(`/fuel/${id}`, { method: "DELETE" });
      await load();
      render();
    }
  } else if (a === "add-mod") modForm();
  else if (a === "edit-mod") modForm(state.mods.find((m) => m.id === id));
  else if (a === "delete-mod") {
    if (confirm("Delete this modification?")) {
      await api(`/mods/${id}`, { method: "DELETE" });
      await load();
      render();
    }
  } else if (a === "add-note") noteForm();
  else if (a === "edit-note") noteForm(state.notes.find((n) => n.id === id));
  else if (a === "delete-note") {
    if (confirm("Delete this note?")) {
      await api(`/notes/${id}`, { method: "DELETE" });
      await load();
      render();
    }
  } else if (a === "add-reminder") reminderForm();
  else if (a === "edit-reminder")
    reminderForm(state.reminders.find((r) => r.id === id));
  else if (a === "delete-reminder") {
    if (confirm("Delete this reminder?")) {
      await api(`/reminders/${id}`, { method: "DELETE" });
      await load();
      render();
    }
  } else if (a === "log-reminder")
    serviceForm(
      undefined,
      state.reminders.find((r) => r.id === id),
    );
  else if (a === "delete-receipt") {
    if (confirm("Delete this receipt photo?")) {
      await api(`/receipts/${id}`, { method: "DELETE" });
      await load();
      close();
      render();
    }
  } else if (a === "revoke-token") {
    if (
      confirm(
        "Revoke this API token? Requests using it will stop working immediately.",
      )
    ) {
      await api(`/tokens/${id}`, { method: "DELETE" });
      settingsView();
      notify("Token revoked");
    }
  } else if (a === "test-notify") {
    try {
      await api("/notifications/test", { method: "POST" });
      notify("Test notification sent");
    } catch (err) {
      alert(err.message);
    }
  } else if (a === "settings") settingsView();
  else if (a === "users") usersView();
  else if (a === "add-user") userForm();
  else if (a === "edit-user") userForm(state.users.find((u) => u.id === id));
  else if (a === "toggle-user") {
    const u = state.users.find((u) => u.id === id);
    await api(`/users/${id}`, {
      method: "PUT",
      body: JSON.stringify({ active: !u.active }),
    });
    state.users = await api("/users");
    usersView();
    notify(u.active ? "User disabled" : "User enabled");
  } else if (a === "logout") {
    await api("/logout", { method: "POST" });
    authView(false);
  } else if (a === "export") {
    const data = await api("/export"),
      blob = new Blob([JSON.stringify(data, null, 2)], {
        type: "application/json",
      }),
      url = URL.createObjectURL(blob),
      x = document.createElement("a");
    x.href = url;
    x.download = `garage-${today()}.json`;
    x.click();
    URL.revokeObjectURL(url);
  } else if (a === "import") document.querySelector("#importFile").click();
});
document.querySelector("#importFile").onchange = async (e) => {
  const file = e.target.files[0];
  if (!file) return;
  try {
    const data = JSON.parse(await file.text());
    if (
      confirm(
        "Replace all garage vehicles, services, and reminders with this backup?",
      )
    ) {
      await api("/import", { method: "POST", body: JSON.stringify(data) });
      state.selected = null;
      await load();
      render();
      notify("Garage imported");
    }
  } catch (err) {
    alert(err.message);
  }
  e.target.value = "";
};
boot().catch((err) => {
  app.innerHTML = `<div class="auth-panel card"><h1>Garage could not start</h1><p class="error">${esc(err.message)}</p></div>`;
});
// Registered here rather than inline so the strict CSP (script-src 'self') allows it.
if ("serviceWorker" in navigator)
  window.addEventListener("load", () =>
    navigator.serviceWorker.register("/static/sw.js"),
  );
