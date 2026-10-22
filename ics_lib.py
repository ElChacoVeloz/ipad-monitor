"""ICS reading, recurrence expansion and normalization for the Omarchy calendar plugin.

Python 3 standard library only: urllib for the fetch, zoneinfo for the timezones,
no third-party package anywhere. Everything here is importable and unit-testable
without a network (see test/test_gcal.py).

Two ideas carry the whole file:

  * An ICS feed is a text stream of folded `NAME;PARAM=VALUE:value` lines. Once
    unfolded and split, a VEVENT is just a dict of those.
  * A recurring event is one VEVENT plus an RRULE, and Google writes the
    recurrence as a rule over *dates* with a time of day attached. So the
    expansion works on dates and re-attaches the time afterwards, which keeps
    COUNT/UNTIL/BYDAY arithmetic honest across DST.
"""

from __future__ import annotations

import datetime as dt
import re
import urllib.error
import urllib.request
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

MAX_OCCURRENCES = 20000
WEEKDAY_INDEX = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}

MEETING_HOSTS = (
    "zoom.us",
    "meet.google.com",
    "teams.microsoft.com",
    "teams.live.com",
    "webex.com",
    "whereby.com",
    "discord.gg",
    "discord.com",
    "jitsi",
    "meet.jit.si",
    "chime.aws",
    "bluejeans.com",
)
MEETING_RE = re.compile(r'https?://[^\s<>"\']+')


class IcsError(Exception):
    """Raised when a feed is not usable at all (transport or empty body)."""


# --------------------------------------------------------------------------
# transport
# --------------------------------------------------------------------------

def normalize_url(url: str) -> str:
    """webcal:// es el mismo feed servido por https; urllib no lo conoce."""
    url = (url or "").strip()
    if url.lower().startswith("webcal://"):
        return "https://" + url[len("webcal://"):]
    return url


def fetch_ics(url: str, timeout: int = 20) -> str:
    """GET an .ics feed. Raises IcsError with a readable message on failure."""
    if not url:
        raise IcsError("no URL configured")
    url = normalize_url(url)
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "omarchy-calendar/1.0 (+https://github.com/)",
            "Accept": "text/calendar, text/plain, */*",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        raise IcsError("HTTP %s %s" % (exc.code, exc.reason)) from exc
    except urllib.error.URLError as exc:
        raise IcsError("network: %s" % (exc.reason,)) from exc
    except OSError as exc:
        raise IcsError("network: %s" % (exc,)) from exc

    text = raw.decode("utf-8", errors="replace")
    if "BEGIN:VCALENDAR" not in text.upper():
        raise IcsError("not an iCalendar feed (login page or error body?)")
    return text


# --------------------------------------------------------------------------
# parsing primitives
# --------------------------------------------------------------------------

def unfold(raw: str) -> list[str]:
    """Undo RFC 5545 line folding: a CRLF followed by space/tab is a continuation."""
    text = raw.replace("\r\n", "\n").replace("\r", "\n")
    out: list[str] = []
    for line in text.split("\n"):
        if line[:1] in (" ", "\t") and out:
            out[-1] += line[1:]
        elif line.strip():
            out.append(line)
    return out


def _split_head(head: str) -> list[str]:
    """Split the NAME;PARAM=VALUE section on semicolons that are outside quotes."""
    parts: list[str] = []
    current: list[str] = []
    in_quotes = False
    for char in head:
        if char == '"':
            in_quotes = not in_quotes
            current.append(char)
        elif char == ";" and not in_quotes:
            parts.append("".join(current))
            current = []
        else:
            current.append(char)
    parts.append("".join(current))
    return parts


def split_prop(line: str) -> tuple[str, dict[str, str], str]:
    """Split one content line into (NAME, params, value). Quotes protect colons."""
    index = 0
    in_quotes = False
    while index < len(line):
        char = line[index]
        if char == '"':
            in_quotes = not in_quotes
        elif char == ":" and not in_quotes:
            break
        index += 1

    head, value = line[:index], line[index + 1:]
    parts = _split_head(head)
    name = parts[0].strip().upper()
    params: dict[str, str] = {}
    for part in parts[1:]:
        if "=" in part:
            key, val = part.split("=", 1)
            params[key.strip().upper()] = val.strip().strip('"')
    return name, params, unescape_text(value)


def unescape_text(value: str) -> str:
    return (
        value.replace("\\n", "\n")
        .replace("\\N", "\n")
        .replace("\\,", ",")
        .replace("\\;", ";")
        .replace("\\\\", "\\")
    )


def parse_components(lines: list[str]):
    """Yield (component_name, {PROP: [(params, value), ...]}) for every component."""
    stack: list[tuple[str, dict]] = []
    for line in lines:
        if not line.strip():
            continue
        name, params, value = split_prop(line)
        if name == "BEGIN":
            stack.append((value.strip().upper(), {}))
            continue
        if name == "END":
            if stack:
                component, props = stack.pop()
                yield component, props
            continue
        if stack:
            stack[-1][1].setdefault(name, []).append((params, value))


def prop_value(props: dict, name: str, default=None):
    entries = props.get(name)
    return entries[0][1] if entries else default


def prop_params(props: dict, name: str) -> dict:
    entries = props.get(name)
    return entries[0][0] if entries else {}


def prop_list(props: dict, name: str) -> list[tuple[dict, str]]:
    return list(props.get(name, []))


def resolve_tz(tzid: str | None, fallback: dt.tzinfo) -> dt.tzinfo:
    """Google ships real IANA names; other feeds ship things like /free/x/York."""
    if not tzid:
        return fallback
    candidates = [tzid]
    stripped = tzid.strip("/")
    if stripped != tzid:
        candidates.append(stripped)
    bits = stripped.split("/")
    if len(bits) > 2:
        candidates.append("/".join(bits[-2:]))
    for candidate in candidates:
        try:
            return ZoneInfo(candidate)
        except (ZoneInfoNotFoundError, ValueError, KeyError):
            continue
    return fallback


class Stamp:
    """A DTSTART/DTEND/RECURRENCE-ID: either a date (all-day) or a datetime."""

    __slots__ = ("value", "all_day", "tz", "floating")

    def __init__(self, value, all_day: bool, tz: dt.tzinfo | None, floating: bool = False):
        self.value = value
        self.all_day = all_day
        self.tz = tz
        self.floating = floating

    @property
    def date(self) -> dt.date:
        return self.value if self.all_day else self.value.date()

    @property
    def time(self) -> dt.time:
        return dt.time(0, 0) if self.all_day else self.value.timetz().replace(tzinfo=None)

    @property
    def naive(self) -> dt.datetime:
        return dt.datetime.combine(self.date, self.time)


def parse_stamp(value: str, params: dict, default_tz: dt.tzinfo) -> Stamp:
    text = (value or "").strip()
    if params.get("VALUE", "").upper() == "DATE" or re.fullmatch(r"\d{8}", text):
        return Stamp(_parse_date(text), True, None)
    if text.endswith("Z"):
        body = text[:-1]
        tz: dt.tzinfo = dt.timezone.utc
        return Stamp(_parse_datetime(body).replace(tzinfo=tz), False, tz)

    tzid = params.get("TZID")
    tz = resolve_tz(tzid, default_tz)
    floating = not tzid
    return Stamp(_parse_datetime(text).replace(tzinfo=tz), False, tz, floating)


def _parse_date(text: str) -> dt.date:
    text = text.strip()
    if not re.fullmatch(r"\d{8}", text):
        raise IcsError("bad DATE value: %r" % text)
    return dt.date(int(text[0:4]), int(text[4:6]), int(text[6:8]))


def _parse_datetime(text: str) -> dt.datetime:
    text = text.strip().rstrip("Z")
    if not re.fullmatch(r"\d{8}T\d{4,6}", text):
        raise IcsError("bad DATE-TIME value: %r" % text)
    second = int(text[13:15]) if len(text) >= 15 else 0
    return dt.datetime(
        int(text[0:4]), int(text[4:6]), int(text[6:8]),
        int(text[9:11]), int(text[11:13]), second,
    )


def parse_duration(value: str) -> dt.timedelta:
    match = re.fullmatch(
        r"(?P<sign>[+-])?P(?:(?P<weeks>\d+)W)?(?:(?P<days>\d+)D)?"
        r"(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?)?",
        (value or "").strip().upper(),
    )
    if not match:
        return dt.timedelta(0)
    parts = {key: int(val) for key, val in match.groupdict().items() if key != "sign" and val}
    delta = dt.timedelta(
        weeks=parts.get("weeks", 0), days=parts.get("days", 0),
        hours=parts.get("hours", 0), minutes=parts.get("minutes", 0),
        seconds=parts.get("seconds", 0),
    )
    return -delta if match.group("sign") == "-" else delta


# --------------------------------------------------------------------------
# recurrence
# --------------------------------------------------------------------------

def parse_rrule(value: str) -> dict:
    rule: dict = {}
    for chunk in (value or "").split(";"):
        if "=" not in chunk:
            continue
        key, val = chunk.split("=", 1)
        rule[key.strip().upper()] = val.strip()
    return rule


def _byday_tokens(rule: dict) -> list[tuple[int | None, int]]:
    tokens: list[tuple[int | None, int]] = []
    for entry in (rule.get("BYDAY") or "").split(","):
        entry = entry.strip().upper()
        if not entry:
            continue
        match = re.fullmatch(r"([+-]?\d{1,2})?(MO|TU|WE|TH|FR|SA|SU)", entry)
        if not match:
            continue
        ordinal = int(match.group(1)) if match.group(1) else None
        tokens.append((ordinal, WEEKDAY_INDEX[match.group(2)]))
    return tokens


def _nth_weekday(year: int, month: int, weekday: int, ordinal: int) -> dt.date | None:
    """The ordinal-th `weekday` of a month; negative counts from the end."""
    if ordinal > 0:
        first = dt.date(year, month, 1)
        offset = (weekday - first.weekday()) % 7
        day = 1 + offset + (ordinal - 1) * 7
    else:
        last = _last_day_of_month(year, month)
        offset = (last.weekday() - weekday) % 7
        day = last.day - offset + (ordinal + 1) * 7
    if day < 1 or day > _last_day_of_month(year, month).day:
        return None
    return dt.date(year, month, day)


def _last_day_of_month(year: int, month: int) -> dt.date:
    if month == 12:
        return dt.date(year, 12, 31)
    return dt.date(year, month + 1, 1) - dt.timedelta(days=1)


def _candidate_dates(rule: dict, first: dt.date, window_end: dt.date):
    """Yield every date the rule names, in order, starting at `first`."""
    freq = (rule.get("FREQ") or "DAILY").upper()
    interval = max(1, int(rule.get("INTERVAL") or 1))
    byday = _byday_tokens(rule)
    bymonthday = [int(x) for x in (rule.get("BYMONTHDAY") or "").split(",") if x.strip().lstrip("+-").isdigit()]
    bymonth = [int(x) for x in (rule.get("BYMONTH") or "").split(",") if x.strip().isdigit()]
    week_start = WEEKDAY_INDEX.get((rule.get("WKST") or "MO").upper(), 0)

    def keep(day: dt.date) -> bool:
        if bymonth and day.month not in bymonth:
            return False
        return day >= first

    if freq == "DAILY":
        cursor = first
        while cursor <= window_end:
            yield cursor
            cursor += dt.timedelta(days=interval)
        return

    if freq == "WEEKLY":
        weekdays = [token[1] for token in byday] or [first.weekday()]
        anchor = first - dt.timedelta(days=(first.weekday() - week_start) % 7)
        while anchor <= window_end:
            for weekday in sorted(weekdays):
                day = anchor + dt.timedelta(days=(weekday - week_start) % 7)
                if keep(day):
                    yield day
            anchor += dt.timedelta(weeks=interval)
        return

    if freq == "MONTHLY":
        year, month = first.year, first.month
        while dt.date(year, month, 1) <= window_end:
            days: list[dt.date] = []
            if byday:
                for ordinal, weekday in byday:
                    if ordinal is None:
                        day = dt.date(year, month, 1)
                        while day.month == month:
                            if day.weekday() == weekday:
                                days.append(day)
                            day += dt.timedelta(days=1)
                    else:
                        day = _nth_weekday(year, month, weekday, ordinal)
                        if day:
                            days.append(day)
            elif bymonthday:
                for raw in bymonthday:
                    day = _nth_weekday_named_monthday(year, month, raw)
                    if day:
                        days.append(day)
            else:
                day = _nth_weekday_named_monthday(year, month, first.day)
                if day:
                    days.append(day)
            for day in sorted(set(days)):
                if keep(day):
                    yield day
            month += interval
            while month > 12:
                month -= 12
                year += 1
        return

    if freq == "YEARLY":
        year = first.year
        while dt.date(year, 1, 1) <= window_end:
            days: list[dt.date] = []
            months = bymonth or [first.month]
            if byday:
                for month in months:
                    for ordinal, weekday in byday:
                        if ordinal is None:
                            day = dt.date(year, month, 1)
                            while day.month == month:
                                if day.weekday() == weekday:
                                    days.append(day)
                                day += dt.timedelta(days=1)
                        else:
                            day = _nth_weekday(year, month, weekday, ordinal)
                            if day:
                                days.append(day)
            else:
                monthdays = bymonthday or [first.day]
                for month in months:
                    for raw in monthdays:
                        day = _nth_weekday_named_monthday(year, month, raw)
                        if day:
                            days.append(day)
            for day in sorted(set(days)):
                if keep(day):
                    yield day
            year += interval
        return

    # Unknown FREQ: the series is at least its own first occurrence.
    yield first


def _nth_weekday_named_monthday(year: int, month: int, monthday: int) -> dt.date | None:
    last = _last_day_of_month(year, month).day
    if monthday > 0:
        return dt.date(year, month, monthday) if monthday <= last else None
    day = last + monthday + 1
    return dt.date(year, month, day) if day >= 1 else None


def _until_bound(rule: dict, tz: dt.tzinfo) -> tuple[dt.date | None, dt.datetime | None]:
    raw = rule.get("UNTIL")
    if not raw:
        return None, None
    stamp = parse_stamp(raw, {}, tz)
    if stamp.all_day:
        return stamp.date, None
    value = stamp.value
    if value.tzinfo is None:
        value = value.replace(tzinfo=tz)
    return None, value.astimezone(tz)


def occurrence_starts(
    rule: dict,
    start: Stamp,
    window_from: dt.date,
    window_end: dt.date,
    exdates: set[dt.datetime],
    exdate_days: set[dt.date],
) -> list[dt.date]:
    """The dates (within the window) on which a recurring event actually happens."""
    tz = start.tz or dt.timezone.utc
    until_day, until_moment = _until_bound(rule, tz)
    count_limit = int(rule["COUNT"]) if rule.get("COUNT", "").isdigit() else None
    produced = 0
    out: list[dt.date] = []
    seen: set[dt.date] = set()

    for day in _candidate_dates(rule, start.date, window_end):
        if day in seen:
            continue
        seen.add(day)
        if until_day and day > until_day:
            break
        if until_moment and dt.datetime.combine(day, start.time).replace(tzinfo=tz) > until_moment:
            break
        produced += 1
        if count_limit is not None and produced > count_limit:
            break
        if produced > MAX_OCCURRENCES:
            break
        if day in exdate_days:
            continue
        if dt.datetime.combine(day, start.time) in exdates:
            continue
        if window_from <= day <= window_end:
            out.append(day)
    return out


# --------------------------------------------------------------------------
# VEVENT -> event dicts
# --------------------------------------------------------------------------

def detect_meeting_url(props: dict) -> str:
    for _, value in prop_list(props, "CONFERENCE"):
        match = MEETING_RE.search(value or "")
        if match:
            return match.group(0)
    haystack = " ".join(
        filter(None, [prop_value(props, "DESCRIPTION", ""), prop_value(props, "LOCATION", "")])
    )
    for candidate in MEETING_RE.findall(haystack):
        lowered = candidate.lower()
        if any(host in lowered for host in MEETING_HOSTS):
            return candidate
    return ""


def is_declined(props: dict, account_email: str) -> bool:
    if not account_email:
        return False
    for params, value in prop_list(props, "ATTENDEE"):
        address = (value or "").replace("mailto:", "").replace("MAILTO:", "").strip().lower()
        if address == account_email.strip().lower() and params.get("PARTSTAT", "").upper() == "DECLINED":
            return True
    return False


def event_from_vevent(
    props: dict,
    account_email: str,
    account_label: str,
    calendar_name: str,
    color: str,
    local_tz: dt.tzinfo,
    window_from: dt.date,
    window_end: dt.date,
    time_format: str = "24h",
) -> list[dict]:
    """Normalize one VEVENT (plus its recurrence) into zero or more event dicts."""
    start_entries = prop_list(props, "DTSTART")
    if not start_entries:
        return []
    start_params, start_value = start_entries[0]
    default_tz = local_tz
    try:
        start = parse_stamp(start_value, start_params, default_tz)
    except IcsError:
        return []

    status = (prop_value(props, "STATUS", "") or "").upper()
    if status == "CANCELLED":
        return []

    end_entries = prop_list(props, "DTEND")
    end_params, end_value = end_entries[0] if end_entries else ({}, None)
    duration: dt.timedelta | None = None
    try:
        if end_value:
            end = parse_stamp(end_value, end_params, default_tz)
            duration = end.naive - start.naive
        elif prop_value(props, "DURATION"):
            duration = parse_duration(prop_value(props, "DURATION"))
    except IcsError:
        duration = None
    if duration is None or duration <= dt.timedelta(0):
        duration = dt.timedelta(days=1) if start.all_day else dt.timedelta(0)

    body = {
        "uid": prop_value(props, "UID", "") or "",
        "summary": (prop_value(props, "SUMMARY", "") or "").strip() or "(sin título)",
        "location": (prop_value(props, "LOCATION", "") or "").strip(),
        "description": (prop_value(props, "DESCRIPTION", "") or "").strip(),
        "organizer": (prop_value(props, "ORGANIZER", "") or "").replace("mailto:", "").strip(),
        "meetingUrl": detect_meeting_url(props),
        "account": account_email,
        "accountLabel": account_label,
        "calendar": calendar_name,
        "color": color,
        "allDay": bool(start.all_day),
        "declined": is_declined(props, account_email),
        "status": status.lower() or "confirmed",
    }

    def exdates() -> tuple[set[dt.datetime], set[dt.date]]:
        moments: set[dt.datetime] = set()
        days: set[dt.date] = set()
        for params, value in prop_list(props, "EXDATE"):
            for chunk in (value or "").split(","):
                chunk = chunk.strip()
                if not chunk:
                    continue
                try:
                    stamp = parse_stamp(chunk, params, default_tz)
                except IcsError:
                    continue
                if stamp.all_day:
                    days.add(stamp.date)
                else:
                    moments.add(stamp.naive)
        return moments, days

    rrule_value = prop_value(props, "RRULE")
    extra_days: list[dt.date] = []
    for params, value in prop_list(props, "RDATE"):
        for chunk in (value or "").split(","):
            chunk = chunk.strip()
            if not chunk:
                continue
            try:
                stamp = parse_stamp(chunk, params, default_tz)
            except IcsError:
                continue
            extra_days.append(stamp.date)

    starts: list[Stamp] = []
    if rrule_value:
        rule = parse_rrule(rrule_value)
        moments, days = exdates()
        for day in occurrence_starts(rule, start, window_from, window_end, moments, days):
            starts.append(Stamp(dt.datetime.combine(day, start.time), start.all_day, start.tz))
    else:
        # A one-off event still has to fall in the window: Google's feeds carry the
        # whole history, and the grid only ever asks about the fetched span. A
        # multi-day event that started before the window but is still running is
        # kept, so a trip does not lose its middle days.
        span_end = (start.naive + duration - dt.timedelta(seconds=1)).date()
        if start.date <= window_end and span_end >= window_from:
            starts.append(start)

    for day in extra_days:
        if window_from <= day <= window_end:
            starts.append(Stamp(dt.datetime.combine(day, start.time), start.all_day, start.tz))

    events: list[dict] = []
    for stamp in starts:
        events.append(serialize(body, stamp, duration, local_tz, recurring=bool(rrule_value)))
    return events


def serialize(body: dict, start: Stamp, duration: dt.timedelta, local_tz: dt.tzinfo, recurring: bool) -> dict:
    if start.all_day:
        start_local = dt.datetime.combine(start.date, dt.time(0, 0), tzinfo=local_tz)
        end_local = start_local + duration
        end_day = (end_local - dt.timedelta(seconds=1)).date()
        if end_day < start_local.date():
            end_day = start_local.date()
    else:
        assert isinstance(start.value, dt.datetime)
        start_local = start.value.astimezone(local_tz)
        end_moment = start.value + duration
        end_local = end_moment.astimezone(local_tz)
        end_day = (end_local - dt.timedelta(seconds=1)).date()
        if end_day < start_local.date():
            end_day = start_local.date()

    event = dict(body)
    event.update(
        {
            "recurring": recurring,
            "start": "" if start.all_day else start_local.isoformat(timespec="minutes"),
            "end": "" if start.all_day else end_local.isoformat(timespec="minutes"),
            "startTime": "" if start.all_day else start_local.strftime("%H:%M"),
            "endTime": "" if start.all_day else end_local.strftime("%H:%M"),
            "startDay": start_local.date().isoformat(),
            "endDay": end_day.isoformat(),
            "occurrenceKey": "%s|%s" % (body["uid"], start_local.strftime("%Y-%m-%dT%H:%M")),
            # The occurrence's own wall clock in the calendar's own timezone: this
            # is what a RECURRENCE-ID names, so an override finds its occurrence
            # without either side having to agree on the machine's timezone.
            "recurrenceMatch": start.naive.strftime("%Y-%m-%dT%H:%M"),
        }
    )
    return event


def parse_feed(
    text: str,
    account_email: str,
    account_label: str,
    calendar_name: str,
    color: str,
    local_tz: dt.tzinfo,
    window_from: dt.date,
    window_end: dt.date,
) -> list[dict]:
    """Every event in a feed, expanded and normalized, with overrides applied."""
    lines = unfold(text)
    masters: list[tuple[str, dict]] = []
    overrides: dict[str, list[dict]] = {}
    for component, props in parse_components(lines):
        if component != "VEVENT":
            continue
        uid = prop_value(props, "UID", "") or ""
        if prop_value(props, "RECURRENCE-ID") is not None:
            continue
        masters.append((uid, props))
    for component, props in parse_components(lines):
        if component == "VEVENT" and prop_value(props, "RECURRENCE-ID") is not None:
            overrides.setdefault(prop_value(props, "UID", "") or "", []).append(props)

    events: list[dict] = []
    for uid, props in masters:
        produced = event_from_vevent(
            props, account_email, account_label, calendar_name, color,
            local_tz, window_from, window_end,
        )
        replaced: set[str] = set()
        for override in overrides.get(uid, []):
            replaced.add(_override_key(override, local_tz))
            events.extend(
                event_from_vevent(
                    _strip_recurrence(override), account_email, account_label,
                    calendar_name, color, local_tz, window_from, window_end,
                )
            )
        for event in produced:
            if event["recurrenceMatch"] in replaced:
                continue
            events.append(event)

    # Overrides for series we never saw (feeding a partial window) still count.
    seen_uids = {uid for uid, _ in masters}
    for uid, props_list in overrides.items():
        if uid in seen_uids:
            continue
        for props in props_list:
            events.extend(
                event_from_vevent(
                    _strip_recurrence(props), account_email, account_label,
                    calendar_name, color, local_tz, window_from, window_end,
                )
            )
    return events


def _strip_recurrence(props: dict) -> dict:
    return {key: value for key, value in props.items() if key != "RRULE"}


def _override_key(props: dict, local_tz: dt.tzinfo) -> str:
    entries = prop_list(props, "RECURRENCE-ID")
    if not entries:
        return ""
    params, value = entries[0]
    try:
        stamp = parse_stamp(value, params, local_tz)
    except IcsError:
        return ""
    return stamp.naive.strftime("%Y-%m-%dT%H:%M")


def sort_events(events: list[dict]) -> list[dict]:
    def key(event: dict):
        return (event["startDay"], not event["allDay"], event.get("startTime") or "", event["summary"])

    return sorted(events, key=key)
