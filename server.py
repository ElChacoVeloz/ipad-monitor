#!/usr/bin/env python3
"""iPad Desk Monitor — servidor del tablero 1280x720 para el iPad del escritorio.

Sirve `web/` y publica UN solo endpoint `GET /api/data` que junta, con cache por
fuente y en paralelo:

  * clima actual + siguientes horas + 5 dias + salida/puesta del sol (Open-Meteo)
  * avisos oficiales del NWS (weather emergency)
  * calendario vertical (los mismos feeds iCal de Monitor + los de la oficina)
  * termostato Nest (Google SDM)
  * lista de fondos de pantalla para la rotacion de 10 minutos

Cada fuente falla sola: si una se cae, /api/data devuelve su error y la tarjeta
lo dice en una linea; nunca se inventa un dato. Sin dependencias (solo stdlib).

Uso:
  python3 server.py                      # http://0.0.0.0:8772
  python3 server.py --port 8772 --mock   # datos de ejemplo, sin credenciales
  python3 server.py --once               # imprime el JSON una vez y sale
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo

BASE = Path(__file__).resolve().parent
WEB = BASE / "web"
sys.path.insert(0, str(BASE))
import ics_lib  # noqa: E402  libreria de ICS/repeticiones (solo stdlib)

CONFIG_PATH = BASE / "config.json"

# ----------------------------------------------------------------- utilidades


def load_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as fh:
        return json.load(fh)


def read_text(path: str) -> str | None:
    try:
        return Path(os.path.expanduser(path)).read_text(encoding="utf-8").strip()
    except OSError:
        return None


def http_json(url: str, *, method: str | None = None, headers: dict | None = None,
              body: dict | None = None, form: dict | None = None,
              timeout: int = 20) -> dict:
    """JSON por HTTP. Sin `method` explicito: GET si no hay cuerpo, POST si lo hay
    (mandar cuerpo con method='GET' hace que Google conteste una pagina 400 en
    HTML en vez del JSON con el error real)."""
    data = None
    hdrs = dict(headers or {})
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        hdrs.setdefault("Content-Type", "application/x-www-form-urlencoded")
    elif body is not None:
        data = json.dumps(body).encode()
        hdrs.setdefault("Content-Type", "application/json")
    if method is None:
        method = "POST" if data is not None else "GET"
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    for var in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
                "all_proxy", "ALL_PROXY"):
        os.environ.pop(var, None)  # un proxy mal puesto rompe iCloud/Google
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"{type(exc).__name__}: {exc}") from exc
    return json.loads(raw) if raw.strip() else {}


def short_error(text: str, limit: int = 120) -> str:
    """Errores que van a la pantalla: sin HTML, sin stack, en una linea."""
    t = (text or "").replace("\n", " ")
    low = t.lower()
    if "invalid_grant" in low:
        return "credential expired — re-authorize"
    if "insufficient" in low:
        return "credential has no permission for this source"
    if "<!doctype" in low or "<html" in low:
        return "service returned an error page instead of data"
    return t[:limit] + ("…" if len(t) > limit else "")


class Cache:
    """Cache por fuente con TTL y 'ultimo bueno' para servir algo si truena."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data: dict[str, tuple[float, object]] = {}
        self._last: dict[str, tuple[float, object]] = {}

    def run(self, key: str, ttl: int, fn):
        now = time.time()
        with self._lock:
            hit = self._data.get(key)
            if hit and hit[0] > now:
                return hit[1], {"stale": False}
        try:
            value = fn()
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                prev = self._last.get(key)
            if prev:
                return prev[1], {"stale": True, "error": f"{exc}"}
            return None, {"stale": False, "error": f"{exc}"}
        with self._lock:
            self._data[key] = (now + ttl, value)
            self._last[key] = (now, value)
        return value, {"stale": False, "error": None}


CACHE = Cache()

# --------------------------------------------------------------------- clima

WMO = {
    0: ("Clear", "clear"),
    1: ("Mainly clear", "mostly-clear"),
    2: ("Partly cloudy", "partly-cloudy"),
    3: ("Overcast", "cloudy"),
    45: ("Fog", "fog"),
    48: ("Rime fog", "fog"),
    51: ("Light drizzle", "drizzle"),
    53: ("Drizzle", "drizzle"),
    55: ("Heavy drizzle", "drizzle"),
    56: ("Freezing drizzle", "sleet"),
    57: ("Freezing drizzle", "sleet"),
    61: ("Light rain", "rain"),
    63: ("Rain", "rain"),
    65: ("Heavy rain", "rain"),
    66: ("Freezing rain", "sleet"),
    67: ("Freezing rain", "sleet"),
    71: ("Light snow", "snow"),
    73: ("Snow", "snow"),
    75: ("Heavy snow", "snow"),
    77: ("Snow grains", "snow"),
    80: ("Light showers", "showers"),
    81: ("Showers", "showers"),
    82: ("Violent showers", "showers"),
    85: ("Snow showers", "snow"),
    86: ("Snow showers", "snow"),
    95: ("Thunderstorm", "thunder"),
    96: ("Thunderstorm, hail", "thunder"),
    99: ("Thunderstorm, hail", "thunder"),
}


def wmo(code) -> tuple[str, str]:
    return WMO.get(int(code or 0), ("Unknown", "cloudy"))


def hour_label(dt: datetime) -> str:
    """'1pm' / '12am', como en el diseno (sin :00)."""
    hour = dt.hour % 12 or 12
    return f"{hour}{'am' if dt.hour < 12 else 'pm'}"


def weather(cfg: dict, mock: bool) -> dict:
    loc = cfg["location"]
    disp = cfg.get("display") or {}
    n_hours = int(disp.get("hourlyCount", 4))
    n_days = int(disp.get("dailyCount", 5))
    tz = ZoneInfo(loc["timezone"])
    now = datetime.now(tz)

    if mock:
        base = [0, 1, 2, 3, 61, 80, 95]
        hourly = []
        for i in range(1, n_hours + 1):
            t = (now + timedelta(hours=i)).replace(minute=0, second=0, microsecond=0)
            code = base[i % len(base)]
            desc, icon = wmo(code)
            hourly.append({"time": t.isoformat(), "label": hour_label(t),
                           "tempC": 27 + i, "code": code, "icon": icon,
                           "desc": desc, "rainPct": (i * 10) % 40,
                           "isDay": 6 <= t.hour < 19})
        daily = []
        for i in range(n_days):
            d = now.date() + timedelta(days=i)
            code = base[i % len(base)]
            desc, icon = wmo(code)
            daily.append({"date": d.isoformat(), "weekday": d.strftime("%A"),
                          "code": code, "icon": icon, "desc": desc,
                          "maxC": 29 - i, "minC": 17 + (i % 3),
                          "rainPct": (i * 15) % 50})
        return {"unit": "C",
                "current": {"tempC": 27.0, "feelsC": 28.6, "humidity": 51,
                            "windKph": 7.4, "code": 0, "desc": "Clear",
                            "icon": "clear", "isDay": True},
                "hourly": hourly, "daily": daily,
                "sun": {"sunrise": f"{daily[0]['date']}T07:28",
                        "sunset": f"{daily[0]['date']}T18:45"},
                "placeholder": True}

    url = "https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode({
        "latitude": loc["lat"], "longitude": loc["lon"],
        "current": "temperature_2m,relative_humidity_2m,apparent_temperature,"
                   "weather_code,wind_speed_10m,is_day",
        "hourly": "temperature_2m,weather_code,precipitation_probability,is_day",
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,"
                 "precipitation_probability_max,sunrise,sunset",
        "timezone": loc["timezone"], "forecast_days": max(7, n_days),
        "temperature_unit": "celsius", "wind_speed_unit": "kmh",
    })
    raw = http_json(url, timeout=20)
    cur = raw.get("current", {})
    desc, icon = wmo(cur.get("weather_code"))

    hourly_raw = raw.get("hourly", {})
    times = hourly_raw.get("time") or []
    hourly = []
    for i, iso in enumerate(times):
        try:
            t = datetime.fromisoformat(iso).replace(tzinfo=tz)
        except ValueError:
            continue
        if t <= now:
            continue
        code = (hourly_raw.get("weather_code") or [None] * len(times))[i]
        hdesc, hicon = wmo(code)
        hourly.append({"time": t.isoformat(), "label": hour_label(t),
                       "tempC": round((hourly_raw.get("temperature_2m") or [0] * len(times))[i], 1),
                       "code": code, "icon": hicon, "desc": hdesc,
                       "rainPct": (hourly_raw.get("precipitation_probability") or [None] * len(times))[i],
                       "isDay": bool((hourly_raw.get("is_day") or [1] * len(times))[i])})
        if len(hourly) >= n_hours:
            break

    daily = []
    d = raw.get("daily", {})
    for i, day in enumerate(d.get("time", [])):
        ddesc, dicon = wmo(d["weather_code"][i])
        daily.append({"date": day, "weekday": datetime.fromisoformat(day).strftime("%A"),
                      "code": d["weather_code"][i], "desc": ddesc, "icon": dicon,
                      "maxC": round(d["temperature_2m_max"][i], 1),
                      "minC": round(d["temperature_2m_min"][i], 1),
                      "rainPct": (d.get("precipitation_probability_max") or [None] * len(d["time"]))[i],
                      "sunrise": d["sunrise"][i], "sunset": d["sunset"][i]})
    return {
        "unit": "C",
        "current": {
            "tempC": round(cur.get("temperature_2m", 0), 1),
            "feelsC": round(cur.get("apparent_temperature", 0), 1),
            "humidity": cur.get("relative_humidity_2m"),
            "windKph": cur.get("wind_speed_10m"),
            "code": cur.get("weather_code"), "desc": desc, "icon": icon,
            "isDay": bool(cur.get("is_day", 1)),
        },
        "hourly": hourly,
        "daily": daily[:n_days],
        "sun": {"sunrise": daily[0].get("sunrise") if daily else None,
                "sunset": daily[0].get("sunset") if daily else None},
    }


# --------------------------------------------------------------- calendario


def load_calendars(cfg: dict) -> list[dict]:
    """Nombre, cuenta, color y direccion secreta iCal de cada calendario."""
    path = Path(os.path.expanduser(cfg["calendar"]["configFile"]))
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    return [c for c in data.get("calendars", []) if c.get("enabled", True)]


def ui_event(ev: dict) -> dict:
    return {
        "id": ev.get("occurrenceKey") or ev.get("uid"),
        "calendar": ev.get("calendar") or "",
        "title": ev.get("summary") or "(no title)",
        "location": ev.get("location") or None,
        "start": ev.get("start") or f"{ev.get('startDay', '')}T00:00",
        "end": ev.get("end") or None,
        "allDay": bool(ev.get("allDay")),
        "color": ev.get("color") or "#4f7cf7",
    }


def calendar_week(cfg: dict, mock: bool) -> dict:
    """Los proximos dias, leidos de las direcciones iCal secretas.

    Solo lectura. Un feed roto no tumba la lista: se reporta en `errors`.
    """
    tz = ZoneInfo(cfg["location"]["timezone"])
    today = datetime.now(tz).date()
    days = int((cfg.get("display") or {}).get("calendarDays", 5))
    start = today
    end = start + timedelta(days=days - 1)

    if mock:
        palette = {"Home": "#e5a50a", "Work": "#e5484d", "Family": "#1a7a45",
                   "Personal": "#7c5cd6", "Holidays": "#0d9aa8",
                   "Team": "#f08a24"}
        samples = [
            (0, [("09:00", "Team standup", "Work", None),
                 ("12:30", "Lunch with Sam", "Personal", None),
                 ("20:00", "Take out the trash", "Home", None)]),
            (1, [("09:00", "Team standup", "Work", None),
                 ("17:30", "Dentist", "Personal", None)]),
            (2, [("all", "Alex's birthday", "Family", None),
                 ("11:00", "Farmers market", "Home", None)]),
            (3, [("all", "Full moon", "Holidays", None)]),
            (4, [("09:30", "1:1 with Dana", "Team", None),
                 ("17:30", "Dinner reservation", "Family", None)]),
        ]
        out_days = []
        for off in range(days):
            d = start + timedelta(days=off)
            evs = []
            for t, title, cal, loc in (samples[off][1] if off < len(samples) else []):
                allday = t == "all"
                st = (datetime.combine(d, datetime.min.time(), tz).isoformat()
                      if allday else
                      datetime.strptime(f"{d.isoformat()} {t}", "%Y-%m-%d %H:%M")
                      .replace(tzinfo=tz).isoformat())
                en = (datetime.combine(d + timedelta(days=1), datetime.min.time(), tz).isoformat()
                      if allday else None)
                evs.append({"id": f"mock-{off}-{title}", "calendar": cal,
                            "title": title, "location": loc, "start": st, "end": en,
                            "allDay": allday, "color": palette.get(cal, "#5b8def")})
            evs.sort(key=lambda e: (not e["allDay"], e["start"] or ""))
            out_days.append({"date": d.isoformat(),
                             "weekday": d.strftime("%A"), "events": evs})
        return {"ok": True, "placeholder": True,
                "calendars": [{"name": k, "color": v} for k, v in palette.items()],
                "days": out_days, "range": {"start": start.isoformat(), "end": end.isoformat()},
                "errors": []}

    feeds = load_calendars(cfg)
    if not feeds:
        return {"calendars": [], "unconfigured": True,
                "days": [{"date": (start + timedelta(days=off)).isoformat(),
                          "weekday": (start + timedelta(days=off)).strftime("%A"),
                          "events": []} for off in range(days)],
                "range": {"start": start.isoformat(), "end": end.isoformat()},
                "errors": [], "foundCount": 0}

    def pull(feed: dict) -> list[dict]:
        # Google contesta 404/503 de vez en cuando a peticiones paralelas: un
        # reintento evita pintar "feed down" por un hipo de dos segundos.
        last = None
        for attempt in range(2):
            try:
                text = ics_lib.fetch_ics(feed["url"], timeout=30)
                return ics_lib.parse_feed(
                    text, feed.get("account", ""), "", feed["name"],
                    feed.get("color") or "#4f7cf7", tz, start, end)
            except Exception as exc:  # noqa: BLE001
                last = exc
                time.sleep(1.2)
        raise last

    errors: list[dict] = []
    events: list[dict] = []
    with ThreadPoolExecutor(max_workers=min(8, len(feeds))) as pool:
        futures = {feed["name"]: pool.submit(pull, feed) for feed in feeds}
        for name, fut in futures.items():
            try:
                events.extend(fut.result())
            except Exception as exc:  # noqa: BLE001
                errors.append({"calendar": name, "error": short_error(str(exc))})

    if not events and errors and len(errors) == len(feeds):
        raise RuntimeError("; ".join(f"{e['calendar']}: {e['error']}" for e in errors))

    seen: set[str] = set()
    unique = []
    for ev in events:
        key = ev.get("occurrenceKey") or f"{ev.get('uid')}|{ev.get('startDay')}"
        if key in seen:
            continue
        seen.add(key)
        unique.append(ev)

    out_days = []
    for off in range(days):
        iso = (start + timedelta(days=off)).isoformat()
        picked = []
        for ev in unique:
            if ev.get("declined"):
                continue
            if ev["allDay"]:
                if ev["startDay"] <= iso <= ev["endDay"]:
                    picked.append(ev)
            elif ev["startDay"] == iso:
                picked.append(ev)
        picked.sort(key=lambda e: (not e["allDay"], e.get("start") or "", e["summary"]))
        out_days.append({"date": iso,
                         "weekday": (start + timedelta(days=off)).strftime("%A"),
                         "events": [ui_event(e) for e in picked]})

    return {
        "calendars": [{"name": f["name"], "color": f.get("color") or "#4f7cf7",
                       "account": f.get("account", "")} for f in feeds],
        "days": out_days,
        "range": {"start": start.isoformat(), "end": end.isoformat()},
        "errors": errors,
        "foundCount": len(feeds),
        "eventsInWindow": len(unique),
    }


# ------------------------------------------------------------------- alertas

SEVERITY_ORDER = {"Extreme": 0, "Severe": 1, "Moderate": 2, "Minor": 3, "Unknown": 4}


def weather_alerts(cfg: dict, mock: bool) -> dict:
    """Avisos oficiales del NWS (api.weather.gov) para el punto de la casa."""
    a = cfg.get("alerts") or {}
    if mock:
        tz = ZoneInfo(cfg["location"]["timezone"])
        ends = datetime.now(tz) + timedelta(hours=6)
        return {"count": 1, "source": "api.weather.gov",
                "items": [{"event": "Demo alert", "severity": "Moderate",
                           "headline": "Demo alert issued by the National Weather Service "
                                       "for Demo County",
                           "area": "Demo County", "ends": ends.isoformat(),
                           "onset": (ends - timedelta(hours=8)).isoformat()}],
                "placeholder": True}
    if not a.get("enabled", True):
        raise RuntimeError("alerts disabled in config.json")

    lat = a.get("lat") or cfg["location"]["lat"]
    lon = a.get("lon") or cfg["location"]["lon"]
    url = f"https://api.weather.gov/alerts/active?point={lat},{lon}"
    raw = http_json(url, headers={
        "User-Agent": a.get("userAgent", "ipad-monitor/1.0 (desk display)"),
        "Accept": "application/geo+json",
    }, timeout=25)

    items = []
    for feature in raw.get("features", []):
        p = feature.get("properties") or {}
        if (p.get("status") or "").lower() == "test":
            continue
        items.append({
            "event": p.get("event") or "Weather alert",
            "severity": p.get("severity") or "Unknown",
            "headline": (p.get("headline") or "").strip(),
            "area": (p.get("areaDesc") or "").split(";")[0].strip(),
            "ends": p.get("ends") or p.get("expires"),
            "onset": p.get("onset") or p.get("effective"),
        })
    items.sort(key=lambda i: SEVERITY_ORDER.get(i["severity"], 9))
    return {"count": len(items), "items": items[:3], "source": "api.weather.gov"}


# ---------------------------------------------------------------------- Nest


def nest_state(cfg: dict, mock: bool) -> dict:
    n = cfg.get("nest", {})
    if mock:
        return {"name": "Hallway", "ambientC": 21.3, "targetC": 22.7, "humidity": 56,
                "mode": "COOL", "status": "OFF", "placeholder": True}
    if not n.get("enabled"):
        raise RuntimeError("Nest not configured")
    mode = n.get("mode", "sdm")
    if mode == "sdm":
        token_path = os.path.expanduser(n["refreshTokenFile"])
        cred = json.loads(Path(token_path).read_text(encoding="utf-8"))
        payload = http_json("https://oauth2.googleapis.com/token", form={
            "client_id": cred["client_id"], "client_secret": cred["client_secret"],
            "refresh_token": cred["refresh_token"], "grant_type": "refresh_token"})
        tok = payload["access_token"]
        res = http_json(
            f"https://smartdevicemanagement.googleapis.com/v1/enterprises/"
            f"{n['projectId']}/devices/{n['deviceId']}",
            headers={"Authorization": f"Bearer {tok}"})
        tr = res.get("traits", {})
        amb = tr.get("sdm.devices.traits.Temperature", {}).get("ambientTemperatureCelsius")
        hum = tr.get("sdm.devices.traits.Humidity", {}).get("ambientHumidityPercent")
        sp = tr.get("sdm.devices.traits.ThermostatTemperatureSetpoint", {})
        target = sp.get("heatCelsius", sp.get("coolCelsius"))
        return {"name": tr.get("sdm.devices.traits.Info", {}).get("customName") or "Nest",
                "ambientC": round(amb, 1) if amb is not None else None,
                "targetC": round(target, 1) if target is not None else None,
                "humidity": hum,
                "mode": tr.get("sdm.devices.traits.ThermostatMode", {}).get("mode"),
                "status": tr.get("sdm.devices.traits.ThermostatHvac", {}).get("status")}
    if mode == "http":
        return http_json(n["url"], headers={"Authorization": n.get("auth", "")})
    raise RuntimeError(f"unknown nest mode: {mode}")


# --------------------------------------------------------------- wallpapers


def wallpapers(cfg: dict, mock: bool) -> dict:
    w = cfg.get("wallpapers") or {}
    folder = BASE / (w.get("dir") or "wallpapers/opt")
    base = w.get("urlBase") or "/wallpapers/"
    files = sorted(p.name for p in folder.glob("*")
                   if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp"))
    return {"items": [base + f for f in files],
            "rotateMinutes": float(w.get("rotateMinutes") or 10)}


# ------------------------------------------------------------------- armado


def web_version() -> str:
    """Huella de web/ para que la pagina se recargue sola cuando hay cambios."""
    h = hashlib.sha1()
    for name in ("index.html", "style.css", "app.js"):
        path = WEB / name
        try:
            st = path.stat()
            h.update(f"{name}:{st.st_mtime_ns}:{st.st_size}".encode())
        except OSError:
            h.update(f"{name}:missing".encode())
    return h.hexdigest()[:12]


def build_payload(cfg: dict, mock: bool) -> dict:
    tz = ZoneInfo(cfg["location"]["timezone"])
    now = datetime.now(tz)
    jobs = {
        "weather": (480, lambda: weather(cfg, mock)),
        "calendar": (300, lambda: calendar_week(cfg, mock)),
        "nest": (120, lambda: nest_state(cfg, mock)),
        "alerts": (480, lambda: weather_alerts(cfg, mock)),
        "wallpapers": (1800, lambda: wallpapers(cfg, mock)),
    }
    out: dict = {"generatedAt": now.isoformat(), "version": web_version(),
                 "timezone": cfg["location"]["timezone"],
                 "today": now.date().isoformat(), "now": now.isoformat(),
                 "locationLabel": cfg["location"].get("label", ""),
                 "config": {"calendarDays": (cfg.get("display") or {}).get("calendarDays", 5),
                            "hourlyCount": (cfg.get("display") or {}).get("hourlyCount", 4),
                            "dailyCount": (cfg.get("display") or {}).get("dailyCount", 5)},
                 "sources": {}}
    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = {name: pool.submit(CACHE.run, name, ttl, fn)
                   for name, (ttl, fn) in jobs.items()}
        for name, fut in futures.items():
            value, meta = fut.result()
            if value is None:
                out[name] = None
                out["sources"][name] = {"ok": False,
                                        "error": short_error(str(meta.get("error")))}
            else:
                out[name] = value
                out["sources"][name] = {"ok": True, "stale": bool(meta.get("stale")),
                                        "error": short_error(str(meta.get("error")))
                                        if meta.get("error") else None}
    return out


# -------------------------------------------------------------------- server


class Handler(SimpleHTTPRequestHandler):
    mock = False
    cfg: dict = {}

    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(WEB), **kw)

    def log_message(self, fmt, *args):  # silencio util
        if os.environ.get("IPAD_MONITOR_DEBUG"):
            super().log_message(fmt, *args)

    def translate_path(self, path: str) -> str:
        clean = path.split("?", 1)[0].split("#", 1)[0]
        if clean.startswith("/wallpapers/"):
            name = os.path.basename(urllib.parse.unquote(clean))
            return str(BASE / (self.cfg.get("wallpapers", {}).get("dir") or "wallpapers/opt") / name)
        return super().translate_path(clean)

    def do_GET(self):  # noqa: N802
        if self.path.split("?")[0] in ("/api/data", "/api/data/"):
            try:
                payload = build_payload(self.cfg, self.mock)
                body = json.dumps(payload).encode()
                self.send_response(200)
            except Exception as exc:  # noqa: BLE001
                body = json.dumps({"error": f"{type(exc).__name__}: {exc}"}).encode()
                self.send_response(500)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path in ("/", ""):
            self.path = "/index.html"
        return super().do_GET()

    def end_headers(self):
        # OJO: la ruta puede traer query (?v=1) — comparar la extension sobre
        # self.path tal cual deja el archivo cacheado y el iPad sigue pintando
        # el JS viejo aunque el servidor ya tenga el nuevo.
        ext = self.path.split("?", 1)[0].split("#", 1)[0]
        if ext.endswith((".html", ".js", ".css", ".webmanifest")):
            self.send_header("Cache-Control", "no-store, must-revalidate")
        super().end_headers()


def main() -> int:
    ap = argparse.ArgumentParser(description="iPad desk monitor server")
    ap.add_argument("--port", type=int, default=8772)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--mock", action="store_true", help="datos de ejemplo")
    ap.add_argument("--once", action="store_true", help="imprime /api/data y sale")
    args = ap.parse_args()

    cfg = load_config()
    mock = args.mock or os.environ.get("IPAD_MONITOR_MOCK") == "1"
    if args.once:
        print(json.dumps(build_payload(cfg, mock), indent=2, ensure_ascii=False))
        return 0

    Handler.mock = mock
    Handler.cfg = cfg
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"iPad monitor on http://{args.host}:{args.port}  (mock={mock})", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
