#!/usr/bin/env python3
"""Alta de calendarios del iPad Desk Monitor (direcciones secretas iCal).

El tablero es de solo lectura: no hay OAuth, cada calendario es una direccion
`…/private-XXXXXXXX/basic.ics` de Google Calendar (o un feed de iCloud).

Uso:
  python3 office-calendars.py list
  python3 office-calendars.py add --name "Dana" --color "#38bdf8"
  python3 office-calendars.py add            # pide nombre y direccion
  python3 office-calendars.py check
  python3 office-calendars.py color --name "Dana" --color "#a3e635"
  python3 office-calendars.py remove --name "Dana"

La direccion se pide con getpass: no queda en el historial del shell.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))
import ics_lib  # noqa: E402

CFG = json.loads((BASE / "config.json").read_text(encoding="utf-8"))
CAL_PATH = Path(os.path.expanduser(CFG["calendar"]["configFile"]))
TZ = ZoneInfo(CFG["location"]["timezone"])
ACCOUNT = "ipad-monitor"

# Familias de color libres: no se repiten vecinos (amarillo/naranja, verde/turquesa)
# Cinco familias distintas para los cinco de la oficina: nada de vecinos
# (naranja junto a ambar, salmon junto a rojo, purpura junto a violeta se
# confunden a un metro). El gris queda de comodin.
PALETTE = ["#f08a24", "#38bdf8", "#a3e635", "#c084fc", "#94a3b8", "#f472b6"]


def load() -> dict:
    if not CAL_PATH.exists():
        return {"calendars": []}
    return json.loads(CAL_PATH.read_text(encoding="utf-8"))


def save(data: dict) -> None:
    CAL_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                        encoding="utf-8")
    os.chmod(CAL_PATH, 0o600)


def mask(url: str) -> str:
    """Nunca imprimir el path intermedio: ahi vive el secreto del feed.

    Solo los feeds de Google terminan en `basic.ics` (ese nombre no revela
    nada); en iCloud el ultimo segmento ES el token, asi que se corta.
    """
    m = re.match(r"^(webcal|https?)://([^/]+)/", url)
    if not m:
        return "…"
    tail = url.rstrip("/").rsplit("/", 1)[-1]
    if tail not in ("basic.ics", "calendar.ics", "holidays.ics"):
        tail = (tail[:5] + "…") if len(tail) > 8 else "…"
    return f"{m.group(1)}://{m.group(2)}/…/{tail}"


def fetch(url: str) -> str:
    return ics_lib.fetch_ics(url, timeout=30)


def count_week(text: str) -> tuple[int, int]:
    today = datetime.now(TZ).date()
    events = ics_lib.parse_feed(text, "", "", "probe", "#fff", TZ,
                                today, today + timedelta(days=7))
    return len(events), text.count("BEGIN:VEVENT")


def cmd_list(args) -> int:
    data = load()
    cals = data.get("calendars", [])
    if not cals:
        print("sin calendarios: el tablero pinta 'No calendars yet'")
        return 0
    width = max(len(c["name"]) for c in cals)
    for c in cals:
        print(f"{c['name']:{width}s}  {c.get('color', '?')}  {mask(c['url'])}")
    print(f"\n{cals.__len__()} calendarios en {CAL_PATH}")
    return 0


def cmd_add(args) -> int:
    data = load()
    name = args.name or input("Nombre del calendario: ").strip()
    if not name:
        print("sin nombre no hay alta")
        return 2
    if any(c["name"].lower() == name.lower() for c in data["calendars"]):
        print(f"ya existe un calendario llamado '{name}'")
        return 2

    used = {c.get("color", "").lower() for c in data["calendars"]}
    color = args.color or next((c for c in PALETTE if c.lower() not in used), "#38bdf8")

    if args.url_file:
        url = Path(os.path.expanduser(args.url_file)).read_text(encoding="utf-8").strip()
    else:
        print("Pega la direccion secreta (Direccion secreta en formato iCal).")
        print("Debe llevar /private-XXXXXXXX/basic.ics — la publica no sirve.")
        url = getpass.getpass("iCal URL (no se muestra): ").strip()
    if not url:
        print("sin direccion no hay alta")
        return 2
    if not re.match(r"^(https?|webcal)://", url):
        print("esa direccion no empieza con https:// ni webcal://")
        return 2

    for c in data["calendars"]:
        if c["url"] == url:
            print(f"esa MISMA direccion ya esta en '{c['name']}' (dos feeds iguales dejan una semana incompleta)")
            return 2

    try:
        text = fetch(url)
    except Exception as exc:  # noqa: BLE001
        print(f"no pude bajar el feed: {exc}")
        print("revisa que sea la direccion secreta del calendario correcto")
        return 1

    week, total = count_week(text)
    data["calendars"].append({"name": name, "account": ACCOUNT, "color": color,
                              "url": url, "enabled": True})
    save(data)
    print(f"alta: {name}  color {color}  {mask(url)}")
    print(f"feed OK: {total} eventos en total, {week} esta semana")
    print("reinicia el servicio para verlo: systemctl --user restart ipad-monitor")
    libres = [c for c in PALETTE if c.lower() not in {x.get('color', '').lower() for x in data['calendars']}]
    if libres:
        print(f"colores libres para el siguiente: {', '.join(libres)}")
    return 0


def cmd_color(args) -> int:
    data = load()
    for c in data["calendars"]:
        if c["name"].lower() == args.name.lower():
            c["color"] = args.color
            save(data)
            print(f"{c['name']} ahora es {args.color}")
            return 0
    print(f"no encontre '{args.name}'")
    return 1


def cmd_remove(args) -> int:
    data = load()
    before = len(data["calendars"])
    data["calendars"] = [c for c in data["calendars"] if c["name"].lower() != args.name.lower()]
    if len(data["calendars"]) == before:
        print(f"no encontre '{args.name}'")
        return 1
    save(data)
    print(f"fuera '{args.name}'")
    return 0


def cmd_check(args) -> int:
    data = load()
    cals = data.get("calendars", [])
    if not cals:
        print("sin calendarios")
        return 0
    urls: dict[str, list[str]] = {}
    bad = 0
    for c in cals:
        urls.setdefault(c["url"], []).append(c["name"])
        try:
            text = fetch(c["url"])
            week, total = count_week(text)
            note = "" if week else "  (0 esta semana: puede ser un mes flojo, no un feed roto)"
            print(f"{c['name']:32s} OK   {total:5d} eventos, {week} esta semana{note}")
        except Exception as exc:  # noqa: BLE001
            bad += 1
            print(f"{c['name']:32s} FAIL {exc}")
    dup = {u: n for u, n in urls.items() if len(n) > 1}
    for u, names in dup.items():
        print(f"OJO: misma direccion en {', '.join(names)} — solo una se vera")
        bad += 1
    return 0 if not bad else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="Calendarios del iPad Desk Monitor")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="lista (direccion enmascarada)")

    add = sub.add_parser("add", help="alta de un calendario")
    add.add_argument("--name")
    add.add_argument("--color")
    add.add_argument("--url-file", help="archivo con la direccion (alternativa a getpass)")

    col = sub.add_parser("color", help="cambia el color de uno")
    col.add_argument("--name", required=True)
    col.add_argument("--color", required=True)

    rem = sub.add_parser("remove", help="quita uno")
    rem.add_argument("--name", required=True)

    sub.add_parser("check", help="baja cada feed y cuenta eventos")
    args = ap.parse_args()
    return {"list": cmd_list, "add": cmd_add, "color": cmd_color,
            "remove": cmd_remove, "check": cmd_check}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
