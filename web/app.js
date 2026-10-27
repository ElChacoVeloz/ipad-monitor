/* Desk Monitor (tema LCARS) — pinta el tablero de 1280x720 para el iPad.
   Sin librerias. Un fetch por minuto a /api/data, reloj cada segundo,
   fondos que rotan cada N minutos con crossfade y wake lock para que la
   pantalla no se duerma. */
"use strict";

const $ = (sel) => document.querySelector(sel);
const state = {
  data: null,
  version: null,
  fails: 0,
  lastFetch: 0,
  wallpapers: [],
  rotateMs: 10 * 60 * 1000,
  wpIndex: 0,
  wpTimer: null,
  layerOn: null,
};

/* ------------------------------------------------------------- utilidades */

const escapeHtml = (s) => String(s == null ? "" : s)
  .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
  .replace(/"/g, "&quot;");

const fmtCache = new Map();
function fmt(opts, date) {
  const key = JSON.stringify(opts);
  let f = fmtCache.get(key);
  if (!f) { f = new Intl.DateTimeFormat("en-US", opts); fmtCache.set(key, f); }
  return f.format(date);
}

function hexToRgb(hex) {
  const h = String(hex || "").replace("#", "");
  const full = h.length === 3 ? h.split("").map((c) => c + c).join("") : h;
  const n = parseInt(full.slice(0, 6), 16);
  if (Number.isNaN(n)) return { r: 79, g: 124, b: 247 };
  return { r: (n >> 16) & 255, g: (n >> 8) & 255, b: n & 255 };
}

/* Un color de calendario oscuro se pierde sobre la pared: se aclara hacia
   blanco antes de usarlo (y se calcula el tinte a mano, sin color-mix). */
function eventColor(hex) {
  const { r, g, b } = hexToRgb(hex);
  const lum = (0.299 * r + 0.587 * g + 0.114 * b) / 255;
  if (lum >= 0.42) return `rgb(${r},${g},${b})`;
  const mix = (c) => Math.round(c + (255 - c) * 0.38);
  return { solid: `rgb(${mix(r)},${mix(g)},${mix(b)})`, raw: { r, g, b } };
}

function tintSolid(hex) {
  const c = eventColor(hex);
  return typeof c === "string" ? c : c.solid;
}

/* La etiqueta del calendario va chica: se aclara siempre un poco mas que la
   barra para que se lea sobre la tarjeta oscura. */
function tagColor(hex) {
  const { r, g, b } = hexToRgb(hex);
  const mix = (c) => Math.round(c + (255 - c) * 0.42);
  return `rgb(${mix(r)},${mix(g)},${mix(b)})`;
}

/* ----------------------------------------------------------------- iconos */

const SUN_RAYS = `
  <path d="M12 2.4v2.3M12 19.3v2.3M2.4 12h2.3M19.3 12h2.3M5.2 5.2l1.6 1.6M17.2 17.2l1.6 1.6M18.8 5.2l-1.6 1.6M6.8 17.2l-1.6 1.6"/>`;

const CLOUD = `<path d="M7.2 18.4h9.4a3.7 3.7 0 0 0 .4-7.4 5.3 5.3 0 0 0-10.1-1.1 4 4 0 0 0 .3 8.5z"/>`;

const ICONS = {
  clear: `<circle cx="12" cy="12" r="4.15"/>${SUN_RAYS}`,
  "mostly-clear": `<circle cx="12" cy="11.4" r="4.4"/>
    <path d="M12 2.5v2M3.4 12h2M5.9 5.9l1.4 1.4M18.1 5.9l-1.4 1.4M19.1 12h1.4"/>`,
  "partly-cloudy": `<circle cx="8.6" cy="7.9" r="3"/>
    <path d="M8.6 2.6v1.6M3.3 7.9h1.6M4.9 4.2l1.1 1.1M12.3 4.2l-1.1 1.1"/>
    ${CLOUD}`,
  cloudy: `${CLOUD}`,
  fog: `${CLOUD}<path d="M6.6 20.4h10.8M8.4 22.3h7.2"/>`,
  drizzle: `${CLOUD}<path d="M8.6 20.2l-.6 1.7M12 20.2l-.6 1.7M15.4 20.2l-.6 1.7"/>`,
  rain: `${CLOUD}<path d="M8.4 19.8l-1.1 3M12 19.8l-1.1 3M15.6 19.8l-1.1 3"/>`,
  showers: `${CLOUD}<path d="M7.8 19.9l-1.5 3.1M11.4 19.9l-1.5 3.1M15 19.9l-1.5 3.1M18.2 19.9l-1 2"/>`,
  sleet: `${CLOUD}<path d="M8.6 20l-1 2.9M12 20v3M15.4 20l1 2.9"/>`,
  snow: `${CLOUD}<path d="M8.2 20.6v2.6M7 21.9h2.4M15.8 20.6v2.6M14.6 21.9h2.4"/>`,
  thunder: `${CLOUD}<path d="M13.2 19.2l-3 4.2h3l-1.6 3"/>`,
  "night-clear": `<path d="M20.4 14.6A8.6 8.6 0 1 1 9.6 3.8a6.7 6.7 0 0 0 10.8 10.8z"/>`,
  "night-partly": `<path d="M15.6 3.9a5.6 5.6 0 0 0-2 9.1 3.7 3.7 0 0 0-.4-.1 5.3 5.3 0 0 0-10 1.1 4 4 0 0 0 .3 8.5h9.4a3.7 3.7 0 0 0 .4-7.4 5.6 5.6 0 0 0 2.3-11.2z"/>`,
  alert: `<path d="M12 3.4 21 19.9H3z"/><path d="M12 9.4v4.3"/><circle cx="12" cy="16.9" r="0.7" fill="currentColor" stroke="none"/>`,
  check: `<circle cx="12" cy="12" r="8.6"/><path d="M8.2 12.3l2.6 2.6 5-5.4"/>`,
  drop: `<path d="M12 3.6c0 0 4.2 5.4 4.2 8.3a4.2 4.2 0 0 1-8.4 0C7.8 9 12 3.6 12 3.6z"/>`,
};

function icon(name, isDay, cls) {
  let key = name || "cloudy";
  if (!isDay) {
    if (key === "clear" || key === "mostly-clear") key = "night-clear";
    else if (key === "partly-cloudy") key = "night-partly";
  }
  if (!ICONS[key]) key = "cloudy";
  return `<svg class="${cls || ""}" viewBox="0 0 24 24" fill="none" stroke="currentColor"
    stroke-width="1.65" stroke-linecap="round" stroke-linejoin="round">${ICONS[key]}</svg>`;
}

function alertIcon(kind) {
  return `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.35"
    stroke-linecap="round" stroke-linejoin="round">${ICONS[kind] || ICONS.alert}</svg>`;
}

/* ------------------------------------------------------------------ reloj */

function tickClock() {
  const now = new Date();
  const h24 = now.getHours();
  const h = h24 % 12 || 12;
  const m = String(now.getMinutes()).padStart(2, "0");
  $("#clockTime").textContent = `${h}:${m}`;
  $("#clockAmPm").textContent = h24 < 12 ? "AM" : "PM";
  $("#dateLine").textContent = fmt({ weekday: "long", month: "long", day: "numeric", year: "numeric" }, now);
  const jan1 = new Date(now.getFullYear(), 0, 1);
  const doy = Math.floor((now - jan1) / 86400000) + 1;
  $("#stardate").textContent = `DAY ${String(doy).padStart(3, "0")} · ${now.getFullYear()}`;
}

/* --------------------------------------------------------------- alertas */

function renderAlerts(alerts) {
  const src = state.data && state.data.sources ? state.data.sources.alerts : null;

  if (!alerts || !alerts.items || !alerts.items.length) {
    const failed = alerts === null;
    $("#alertIcon").innerHTML = alertIcon(failed ? "alert" : "check");
    if (failed) {
      $("#alertTitle").textContent = "Weather alerts unavailable";
      $("#alertText").textContent = (src && src.error) || "no answer from api.weather.gov";
    } else {
      $("#alertTitle").textContent = "No active alerts";
      $("#alertText").textContent = "National Weather Service · emergencies show up here";
    }
    $("#alertSide").innerHTML = "";
    $("#alertHead").textContent = "NWS · ALERTS";
    return;
  }

  const worst = alerts.items[0];
  $("#alertHead").textContent = alerts.count > 1 ? `RED ALERT · ${alerts.count}` : "RED ALERT";
  $("#alertIcon").innerHTML = alertIcon("alert");
  $("#alertTitle").textContent = worst.event || "Weather alert";
  // el titular del NWS empieza repitiendo el nombre del aviso: se quita
  let body = (worst.headline || "").trim();
  const name = (worst.event || "").trim();
  if (name && body.toLowerCase().startsWith(name.toLowerCase())) {
    body = body.slice(name.length).replace(/^[\s,:–-]+/, "");
    body = body.charAt(0).toUpperCase() + body.slice(1);
  }
  $("#alertText").textContent = body || worst.area || "Emergency alert";
  let side = "";
  if (worst.ends) {
    const ends = new Date(worst.ends);
    if (!Number.isNaN(ends.getTime())) {
      side = `Expires ${fmt({ month: "short", day: "numeric" }, ends)} · ` +
             `${fmt({ hour: "numeric", minute: "2-digit" }, ends)}`;
    }
  }
  if (alerts.count > 1) side += `${side ? "  ·  " : ""}+${alerts.count - 1} more`;
  $("#alertSide").textContent = side;
}

/* ----------------------------------------------------------------- clima */

function hhmm(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return fmt({ hour: "numeric", minute: "2-digit" }, d);
}

function sideBlocks(wx) {
  const side = $("#side");
  const cur = (wx && wx.current) || {};
  const today = (wx && wx.daily && wx.daily[0]) || {};
  const r = (v, u) => (v == null ? "—" : `${Math.round(v)}${u || ""}`);
  const blocks = [
    ["s-org", "sunrise", hhmm(wx && ((wx.sun || {}).sunrise || today.sunrise))],
    ["s-pch", "sunset", hhmm(wx && ((wx.sun || {}).sunset || today.sunset))],
    ["s-lav", "high / low", `${r(today.maxC, "°")} / ${r(today.minC, "°")}`],
    ["s-gry", "humidity", r(cur.humidity, "%")],
    ["s-lav", "wind km/h", r(cur.windKph)],
    ["s-sal", "rain today", r(today.rainPct, "%")],
    ["s-gry grow", "", ""],
  ];
  side.innerHTML = blocks.map(([c, k, v]) =>
    `<div class="blk ${c}">${k ? `<span class="k">${escapeHtml(k)}</span>` : ""}${
      v ? `<span class="v">${escapeHtml(v)}</span>` : ""}</div>`).join("");
  const gry = side.querySelector(".blk.s-gry:not(.grow)");
  if (gry) gry.style.color = "#c4c8d8";
}

function renderWeather(wx) {
  const src = state.data && state.data.sources ? state.data.sources.weather : null;
  sideBlocks(wx);
  if (!wx) {
    $("#tempNow").textContent = "--";
    $("#iconNow").innerHTML = "";
    $("#feelsLike").textContent = "Weather unavailable";
    $("#descNow").textContent = (src && src.error) || "no data from Open-Meteo";
    $("#hiLo").textContent = "";
    $("#hourly").innerHTML = "";
    return;
  }
  const cur = wx.current || {};
  $("#tempNow").textContent = Math.round(cur.tempC);
  $("#iconNow").innerHTML = icon(cur.icon, cur.isDay);
  $("#feelsLike").textContent = `Feels like ${Math.round(cur.feelsC)}°`;
  $("#descNow").textContent = cur.desc || "";
  const bits = [];
  if (cur.humidity != null) bits.push(`${Math.round(cur.humidity)}% hum`);
  if (cur.windKph != null) bits.push(`${Math.round(cur.windKph)} km/h`);
  $("#hiLo").textContent = bits.join(" · ");

  const hourly = wx.hourly || [];
  $("#hourly").innerHTML = hourly.map((h) => `
    <div class="hr">
      <div class="h">${escapeHtml(h.label)}</div>
      <div class="ico">${icon(h.icon, h.isDay)}</div>
      <div class="rain">${h.rainPct != null ? `${h.rainPct}%` : ""}</div>
      <div class="t">${Math.round(h.tempC)}<i>°</i></div>
    </div>`).join("");
}

/* ------------------------------------------------------------ calendario */

/* Hora "de ahora" para el calendario. ?now=2026-10-27T13:00 la fija (QA). */
function calNow() {
  const q = new URLSearchParams(location.search).get("now");
  const t = q ? new Date(q).getTime() : NaN;
  return Number.isNaN(t) ? Date.now() : t;
}

/* Un evento con hora ya terminado sale de la pila; los de todo el dia se
   quedan hasta que cambie el dia. Sin hora de fin, cuenta el inicio + 1 h. */
function eventEnded(ev, now) {
  if (ev.allDay) return false;
  const end = new Date(ev.end || "").getTime();
  if (!Number.isNaN(end)) return end <= now;
  const start = new Date(ev.start || "").getTime();
  return !Number.isNaN(start) && start + 3600000 <= now;
}

function eventLive(ev, now) {
  if (ev.allDay) return false;
  const a = new Date(ev.start || "").getTime();
  const b = new Date(ev.end || "").getTime();
  return !Number.isNaN(a) && !Number.isNaN(b) && a <= now && now < b;
}

function eventRow(ev) {
  const row = document.createElement("div");
  row.className = "event" + (eventLive(ev, calNow()) ? " live" : "");
  let when = "All day";
  if (!ev.allDay && ev.start) {
    const d = new Date(ev.start);
    if (!Number.isNaN(d.getTime())) when = fmt({ hour: "numeric", minute: "2-digit" }, d);
  }
  row.innerHTML = `<div class="when" style="background:${tagColor(ev.color)}">${escapeHtml(when)}</div>
    <div class="what">${escapeHtml(ev.title)}</div>
    <div class="cal-tag" style="color:${tagColor(ev.color)}">${escapeHtml(ev.calendar || "")}</div>`;
  return row;
}

function renderCalendar(cal) {
  const body = $("#calBody");
  const range = $("#calRange");
  body.innerHTML = "";
  const src = state.data && state.data.sources ? state.data.sources.calendar : null;

  if (!cal) {
    range.textContent = "";
    body.innerHTML = `<div class="cal-empty">Calendar is not available right now${
      src && src.error ? ` — ${escapeHtml(src.error)}` : ""}</div>`;
    return;
  }
  if (cal.unconfigured) {
    range.textContent = "";
    body.innerHTML = `<div class="cal-empty">No calendars yet</div>`;
    return;
  }

  const now = calNow();
  const days = (cal.days || []).map((d) => {
    const events = (d.events || []).filter((ev) => !eventEnded(ev, now));
    return Object.assign({}, d, { events, done: (d.events || []).length - events.length });
  });

  /* Nada de scroll ni recorte silencioso. Con muchos calendarios (los 5 de la
     oficina) el dia se llena: primero se aprieta la densidad, luego se cortan
     eventos por dia con un "+N more", y solo al final se van dias completos.
     Todo lo que se quito se anuncia en el encabezado o en la propia linea. */
  const todayIso = (state.data && state.data.today) || "";
  const fits = () => body.scrollHeight <= body.clientHeight + 1;

  /* Regla del Capitan: primero completo, despues cantidad. Se pinta todo a
     tamano normal; si no cabe se van DIAS COMPLETOS por el final (3, 2...), y
     nunca se recorta un dia que si se muestra. Solo en el extremo de que un
     unico dia no quepa se cortan sus ultimos eventos y se cuentan ("+N"). */
  body.classList.remove("dense");
  const buildAll = () => {
    // el relleno extra del render anterior no debe contaminar la medicion
    body.style.setProperty("--row-pad", "0px");
    body.innerHTML = "";
    for (const day of days) {
      const dEl = document.createElement("div");
      dEl.className = "day" + (day.date === todayIso ? " today" : "");
      dEl.dataset.date = day.date;
      dEl.innerHTML = `<div class="day-head">
          <div class="day-num">${Number(String(day.date).slice(8, 10))}</div>
          <div class="day-name">${escapeHtml(day.date === todayIso ? "Today" : day.weekday || "")}</div>
          <div class="day-rule"></div>
        </div>`;
      if (day.done > 0) {
        const chip = document.createElement("span");
        chip.className = "day-done";
        chip.textContent = `${day.done} done`;
        dEl.querySelector(".day-head").insertBefore(chip, dEl.querySelector(".day-rule"));
      }
      for (const ev of day.events || []) dEl.appendChild(eventRow(ev));
      if (!(day.events || []).length && day.done > 0) {
        const none = document.createElement("div");
        none.className = "day-clear";
        none.textContent = "Nothing else today";
        dEl.appendChild(none);
      }
      body.appendChild(dEl);
    }
  };

  buildAll();

  /* Antes de sacrificar un dia entero se aprieta el hueco entre dias (18 -> 10).
     Con dias cargados, un pixel de mas costaba un dia completo. */
  const MAX_GAP = 18;
  const MIN_GAP = 10;
  const fitGap = () => {
    for (let g = MAX_GAP; g >= MIN_GAP; g -= 2) {
      body.style.gap = `${g}px`;
      if (fits()) return g;
    }
    return null;
  };
  let gapUsed = fitGap();
  const dropped = [];
  while (gapUsed === null && body.children.length > 1) {
    dropped.unshift(body.removeChild(body.lastElementChild));
    gapUsed = fitGap();
  }
  if (gapUsed === null) body.style.gap = `${MIN_GAP}px`;

  /* Avance del dia siguiente: en vez de dejar el hueco, el primer dia que no
     cupo entra con los eventos que quepan y un "+N" en su encabezado. Si no
     cabe ni el encabezado con un evento, no se pinta. */
  const markHidden = (dEl) => {
    dEl.classList.add("cut");
    const day = days.find((d) => d.date === dEl.dataset.date) || { events: [] };
    const painted = dEl.querySelectorAll(".event").length;
    let count = dEl.querySelector(".day-count");
    if (!count) {
      count = document.createElement("span");
      count.className = "day-count";
      dEl.querySelector(".day-head").insertBefore(count, dEl.querySelector(".day-rule"));
    }
    count.textContent = `+${Math.max(0, day.events.length - painted)} more`;
  };
  if (dropped.length) {
    const nextDay = dropped[0];
    nextDay.classList.add("partial");
    body.appendChild(nextDay);
    let rowsLeft = nextDay.querySelectorAll(".event");
    while (!fits() && rowsLeft.length > 1) {
      nextDay.removeChild(rowsLeft[rowsLeft.length - 1]);
      rowsLeft = nextDay.querySelectorAll(".event");
      markHidden(nextDay);
    }
    if (!fits() || !rowsLeft.length) body.removeChild(nextDay);
  }

  while (!fits() && body.children.length === 1) {
    const only = body.lastElementChild;
    const rows = only.querySelectorAll(".event");
    if (!rows.length) break;
    only.removeChild(rows[rows.length - 1]);
    const day = days.find((d) => d.date === only.dataset.date) || { events: [] };
    const painted = only.querySelectorAll(".event").length;
    let count = only.querySelector(".day-count");
    if (!count) {
      count = document.createElement("span");
      count.className = "day-count";
      only.querySelector(".day-head").insertBefore(count, only.querySelector(".day-rule"));
    }
    count.textContent = `+${Math.max(0, day.events.length - painted)} more`;
  }
  if (!body.children.length) {
    body.innerHTML = `<div class="cal-empty">No room for the calendar</div>`;
    return;
  }

  /* Si sobran dias (pocos dias muy cargados), el aire se reparte entre los
     renglones en vez de dejar un hueco enorme entre un dia y el siguiente. */
  body.style.setProperty("--row-pad", "0px");
  /* scrollHeight no sirve aqui: con space-between el contenido corto devuelve el
     alto del contenedor y el hueco real se pierde. Se mide hijo por hijo. */
  const kids = [...body.children];
  const gapPx = parseFloat(getComputedStyle(body).gap) || 0;
  const contentH = kids.reduce((acc, el) => acc + el.offsetHeight, 0) +
                   Math.max(0, kids.length - 1) * gapPx;
  const slack = body.clientHeight - contentH;
  const rows = body.querySelectorAll(".event").length;
  // el relleno va arriba y abajo de cada renglon: gana 2 x rowPad por fila,
  // con tope bajo para no volver la lista un globo
  let rowPad = 0;
  if (slack > 40 && rows) {
    rowPad = Math.min(9, Math.floor((slack - 16) / (rows * 2)));
    if (rowPad > 0) body.style.setProperty("--row-pad", `${rowPad}px`);
  }

  const dbgTrace = { ch: body.clientHeight, contentH: Math.round(contentH),
                     slack: Math.round(slack), rows, rowPad,
                     daysShown: body.children.length };

  body.dataset.trace = JSON.stringify(dbgTrace);
  const shownEls = [...body.querySelectorAll(".day")];
  const shownDates = shownEls.map((el) => el.dataset.date).sort();
  const hiddenDays = days.length - shownEls.length;
  let hiddenEvents = 0;
  for (const day of days) {
    if (!shownDates.includes(day.date)) continue;
    const el = body.querySelector(`.day[data-date="${day.date}"]`);
    const painted = el ? el.querySelectorAll(".event").length : 0;
    hiddenEvents += Math.max(0, (day.events || []).length - painted);
  }

  const head = [];
  if (shownDates.length) {
    const a = new Date(`${shownDates[0]}T12:00:00`);
    const b = new Date(`${shownDates[shownDates.length - 1]}T12:00:00`);
    head.push(`${fmt({ month: "short", day: "numeric" }, a)} – ${fmt({ month: "short", day: "numeric" }, b)}`);
  }
  if (cal.errors && cal.errors.length) {
    head.push(`${cal.errors.length} feed${cal.errors.length > 1 ? "s" : ""} down`);
    range.classList.add("warn");
  } else {
    range.classList.remove("warn");
  }
  if (hiddenDays > 0) {
    const hidden = days.filter((d) => !shownDates.includes(d.date)).map((d) => Number(String(d.date).slice(8, 10)));
    head.push(`+${hiddenDays} day${hiddenDays > 1 ? "s" : ""} (${hidden.join(", ")})`);
  }
  if (hiddenEvents > 0) head.push(`+${hiddenEvents} hidden`);
  range.textContent = head.join(" · ");
  // ?debug=1 pinta las metricas del reparto (para revisar en la pared)
  if (new URLSearchParams(location.search).get("debug") === "1") {
    range.textContent += ` · [ch${dbgTrace.ch} c${dbgTrace.contentH} s${dbgTrace.slack}` +
      ` g${parseFloat(getComputedStyle(body).gap)} p${dbgTrace.rowPad} f${getComputedStyle(document.documentElement).getPropertyValue("--fs")}]`;
  }
}

/* ------------------------------------------------------------------ Nest */

function renderNest(nest) {
  const src = state.data && state.data.sources ? state.data.sources.nest : null;
  if (!nest) {
    $("#nestAmbient").textContent = "--";
    $("#nestTarget").textContent = "—";
    $("#nestHumidity").textContent = "—";
    $("#nestMode").textContent = "—";
    const st = $("#nestStatus");
    st.textContent = "offline";
    st.className = "ph-badge";
    $("#nestCard").classList.add("err-state");
    return;
  }
  $("#nestAmbient").textContent = nest.ambientC != null ? Math.round(nest.ambientC) : "--";
  $("#nestTarget").textContent = nest.targetC != null ? `${Math.round(nest.targetC)}°` : "—";
  $("#nestHumidity").textContent = nest.humidity != null ? `${Math.round(nest.humidity)}%` : "—";
  const modeMap = { COOL: "Cool", HEAT: "Heat", HEATCOOL: "Auto", OFF: "Off", ECO: "Eco" };
  $("#nestMode").textContent = modeMap[nest.mode] || nest.mode || "—";
  const statusMap = { COOLING: "cooling", HEATING: "heating", OFF: "idle", FAN: "fan" };
  const st = $("#nestStatus");
  const label = statusMap[nest.status] || (nest.status || "").toLowerCase() || "—";
  const name = nest.name && nest.name.toLowerCase() !== "nest" ? ` · ${nest.name}` : "";
  st.textContent = label + name;
  st.className = "ph-badge" + (label === "idle" ? "" : " on");
}

/* --------------------------------------------------------- slot rotativo */

/* La alerta y el Nest comparten una sola tarjeta y se turnan cada 5 minutos,
   sincronizados al reloj (asi un reload no reinicia el turno). Sin alerta
   activa el slot se queda en el Nest; sin Nest, en la alerta. */
const SLOT_MS = 5 * 60 * 1000;

function slotAvailability() {
  const d = state.data;
  const hasAlert = !!(d && d.alerts && d.alerts.items && d.alerts.items.length);
  const hasNest = !!(d && d.nest);
  return { hasAlert, hasNest };
}

function applySlot() {
  if (!state.data) return;
  const { hasAlert, hasNest } = slotAvailability();
  const forced = new URLSearchParams(location.search).get("slot"); // QA: ?slot=alert|nest
  let show;
  if (forced === "alert" || forced === "nest") {
    show = forced;
  } else if (hasAlert && hasNest) {
    show = Math.floor(Date.now() / SLOT_MS) % 2 === 0 ? "alert" : "nest";
  } else if (hasAlert) {
    show = "alert";
  } else if (hasNest) {
    show = "nest";
  } else {
    show = "alert"; // sin datos de ninguno: la tarjeta de avisos da el motivo
  }
  $("#alertCard").classList.toggle("on", show === "alert");
  $("#nestCard").classList.toggle("on", show === "nest");
  const slot = $("#slotCard");
  slot.classList.toggle("alert-on", show === "alert" && hasAlert);
  slot.classList.toggle("clear-state", show === "alert" && !hasAlert);
  const dots = $("#slotDots");
  dots.classList.toggle("hide", !(hasAlert && hasNest));
  [...dots.children].forEach((el, i) => el.classList.toggle("on", i === (show === "alert" ? 0 : 1)));
}

/* ------------------------------------------------------------- render all */

function renderFooter(d) {
  if (d.locationLabel) $("#locLabel").textContent = d.locationLabel;
  $("#botCode").textContent = `REF ${String(d.version || "").slice(0, 8)}`;
  const names = { weather: "WX", calendar: "CAL", nest: "NEST", alerts: "NWS" };
  const s = d.sources || {};
  $("#sources").innerHTML = Object.keys(names).filter((k) => s[k]).map((k) => {
    const x = s[k];
    const cls = !x.ok ? "bad" : x.stale ? "stale" : "";
    const txt = !x.ok ? "OFFLINE" : x.stale ? "STALE" : "OK";
    return `<div class="src ${cls}"><b>${names[k]}</b><span>${txt}</span></div>`;
  }).join("");
}

function render() {
  const d = state.data;
  if (!d) return;
  renderFooter(d);
  renderAlerts(d.alerts);
  renderWeather(d.weather);
  renderNest(d.nest);
  applySlot();
  ensureFit();       // la columna izquierda primero: puede cambiar el alto
  renderCalendar(d.calendar);
}

/* --------------------------------------------------------------- fondos */

function setLayer(url, immediate) {
  const layers = [$("#bgA"), $("#bgB")];
  const current = state.layerOn;
  const nextEl = current === layers[0] ? layers[1] : layers[0];
  const curEl = current;
  nextEl.style.backgroundImage = `url("${url}")`;
  if (immediate) {
    nextEl.classList.add("on");
    if (curEl) curEl.classList.remove("on");
  } else {
    // fuerza el pintado del nuevo fondo antes del crossfade
    void nextEl.offsetWidth;
    nextEl.classList.add("on");
    if (curEl) curEl.classList.remove("on");
  }
  state.layerOn = nextEl;
}

function scheduleWallpaper() {
  clearTimeout(state.wpTimer);
  const n = state.wallpapers.length;
  if (!n) return;
  const now = Date.now();
  const delay = state.rotateMs - (now % state.rotateMs) + 400;
  state.wpTimer = setTimeout(() => {
    state.wpIndex = (state.wpIndex + 1) % state.wallpapers.length;
    setLayer(state.wallpapers[state.wpIndex]);
    const nextUrl = state.wallpapers[(state.wpIndex + 1) % state.wallpapers.length];
    if (nextUrl) { const img = new Image(); img.src = nextUrl; }
    scheduleWallpaper();
  }, Math.min(delay, 3600000));
}

function applyWallpapers(wp) {
  if (!wp || !wp.items || !wp.items.length) return;
  const changed = wp.items.join("|") !== state.wallpapers.join("|");
  if (!changed) return;
  state.wallpapers = wp.items;
  state.rotateMs = Math.max(1, Number(wp.rotateMinutes) || 10) * 60000;
  state.wpIndex = Math.floor(Date.now() / state.rotateMs) % state.wallpapers.length;
  // ?wp=3 fija un fondo concreto (para revisar legibilidad sin esperar 10 min)
  const forced = new URLSearchParams(location.search).get("wp");
  if (forced !== null && !Number.isNaN(parseInt(forced, 10))) {
    state.wpIndex = Math.max(0, Math.min(state.wallpapers.length - 1, parseInt(forced, 10)));
  }
  setLayer(state.wallpapers[state.wpIndex], true);
  const nextUrl = state.wallpapers[(state.wpIndex + 1) % state.wallpapers.length];
  if (nextUrl) { const img = new Image(); img.src = nextUrl; }
  scheduleWallpaper();
}

/* ----------------------------------------------------------------- datos */

async function fetchData() {
  try {
    const res = await fetch("/api/data", { cache: "no-store" });
    const data = await res.json();
    if (data.error) throw new Error(data.error);
    state.fails = 0;
    state.lastFetch = Date.now();
    if (state.version && data.version !== state.version) {
      location.reload();
      return;
    }
    state.version = data.version;
    state.data = data;
    render();
  } catch (err) {
    state.fails += 1;
    if (!state.data) {
      $("#alertCard").classList.add("on");
      $("#nestCard").classList.remove("on");
      $("#slotCard").classList.add("clear-state");
      $("#slotDots").classList.add("hide");
      $("#alertIcon").innerHTML = alertIcon("alert");
      $("#alertTitle").textContent = "Waiting for the server…";
      $("#alertText").textContent = String(err && err.message ? err.message : err);
      $("#alertSide").innerHTML = "";
      $("#descNow").textContent = "no data yet";
    }
    // kiosco que se auto-cura: si lleva mucho sin poder hablar con el servidor
    if (state.fails >= 5) location.reload();
  }
}

/* ---------------------------------------------------------------- escalado */

/* El iPad no es 16:9: es 4:3. En vez de dejar franjas negras arriba y abajo, el
   escenario crece en alto (1280 de ancho fijo, alto variable) y la tipografia
   sube con el alto para que quepa mas y se lea mejor. En 16:9 queda 1280x720
   como estaba. */
function fitStage() {
  const sim = new URLSearchParams(location.search).get("inset"); // QA: ?inset=24,20
  if (sim) {
    const [t, b] = sim.split(",").map((x) => parseFloat(x) || 0);
    document.documentElement.style.setProperty("--sat", `${t}px`);
    document.documentElement.style.setProperty("--sab", `${b}px`);
  }
  const iw = window.innerWidth;
  const ih = window.innerHeight;
  const stageH = Math.round(Math.min(1020, Math.max(800, 1280 * (ih / iw))));
  document.documentElement.style.setProperty("--stage-h", `${stageH}px`);
  const s = Math.min(iw / 1280, ih / stageH);
  $("#stage").style.transform =
    `translate(${((iw - 1280 * s) / 2).toFixed(2)}px, ${((ih - stageH * s) / 2).toFixed(2)}px) scale(${s.toFixed(4)})`;
  if (state.data) render();
}
/* Pantallas mas bajas que el iPad (16:9): primero se compacta la columna
   izquierda y, si aun no cabe, se quitan horas del pronostico por el final. */
function ensureFit() {
  const col = document.querySelector(".col-left");
  const stage = $("#stage");
  if (!col) return;
  const over = () => {
    const last = col.lastElementChild;
    return last && last.getBoundingClientRect().bottom > col.getBoundingClientRect().bottom + 1;
  };
  stage.classList.remove("compact");
  if (!over()) return;
  stage.classList.add("compact");
  const rows = () => col.querySelectorAll(".hr");
  while (over() && rows().length > 2) rows()[rows().length - 1].remove();
}

/* -------------------------------------------------------------- wake lock */

let wakeLock = null;
async function requestWakeLock() {
  try {
    if ("wakeLock" in navigator) {
      wakeLock = await navigator.wakeLock.request("screen");
      wakeLock.addEventListener("release", () => { wakeLock = null; });
    }
  } catch (err) { /* sin permiso o iOS viejo: se queda el Auto-Lock del iPad */ }
}

document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") {
    requestWakeLock();
    if (Date.now() - state.lastFetch > 90000) fetchData();
  }
});

/* -------------------------------------------------------------- arranque */

fitStage();
tickClock();
// primer pintado sin transiciones, y luego se habilitan
requestAnimationFrame(() => {
  setTimeout(() => document.documentElement.classList.remove("booting"), 1200);
});
setInterval(tickClock, 1000);
window.addEventListener("resize", fitStage);
window.addEventListener("orientationchange", () => setTimeout(fitStage, 300));

fetchData();
setInterval(fetchData, 60000);

/* Con la tipografia de respaldo los renglones miden distinto y el calendario
   recortaba un dia en el primer pintado; al terminar de cargar Inter se vuelve
   a repartir. */
if (document.fonts && document.fonts.ready) {
  document.fonts.ready.then(() => { if (state.data) render(); });
}
setInterval(applySlot, 1000);
requestWakeLock();
// iOS a veces exige un gesto para conceder el wake lock la primera vez.
document.addEventListener("touchend", requestWakeLock, { once: true });
document.addEventListener("click", requestWakeLock, { once: true });

// Paleta oscura fija: no hay tema dia/noche. Para probar el tablero desde la
// consola del kiosco (o por CDP) se expone el estado y el render:
//   monitorDebug.renderCalendar(payloadFalso)
window.monitorDebug = { state, renderCalendar, render, fetchData, applyWallpapers };
