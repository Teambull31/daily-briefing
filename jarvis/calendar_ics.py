"""Agenda (Google Agenda, Proton, Nextcloud… via l'adresse iCal secrète, en lecture seule) :
rendez-vous du jour, rappels avant chaque événement, créneaux libres pour se concentrer."""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

log = logging.getLogger("jarvis.calendar")


@dataclass(frozen=True)
class Event:
    title: str
    start: datetime
    end: datetime
    all_day: bool = False
    location: str = ""
    uid: str = ""

    @property
    def key(self) -> str:
        return f"{self.uid}|{self.start.isoformat()}"


def parse_events(ics_texts: list[str], start: datetime, end: datetime, tz: ZoneInfo) -> list[Event]:
    """Événements (récurrences comprises) qui chevauchent [start, end[, triés, en heure locale."""
    import icalendar
    import recurring_ical_events

    events: list[Event] = []
    for text in ics_texts:
        try:
            cal = icalendar.Calendar.from_ical(text)
            components = recurring_ical_events.of(cal).between(start, end)
        except Exception:
            log.exception("Agenda illisible")
            continue
        for comp in components:
            if comp.name != "VEVENT":
                continue
            s, e = comp.get("DTSTART"), comp.get("DTEND")
            s = s.dt if s else None
            e = e.dt if e else None
            if s is None:
                continue
            all_day = not isinstance(s, datetime)
            s, e = _localize(s, tz), _localize(e, tz) if e else None
            if e is None:
                e = s + (timedelta(days=1) if all_day else timedelta(hours=1))
            events.append(Event(
                title=str(comp.get("SUMMARY", "(sans titre)")),
                start=s, end=e, all_day=all_day,
                location=str(comp.get("LOCATION", "") or ""),
                uid=str(comp.get("UID", "")),
            ))
    return sorted(events, key=lambda ev: (ev.start, ev.title))


def _localize(value, tz: ZoneInfo) -> datetime:
    if not isinstance(value, datetime):  # journée entière
        return datetime(value.year, value.month, value.day, tzinfo=tz)
    if value.tzinfo is None:  # heure « flottante » : on la considère locale
        return value.replace(tzinfo=tz)
    return value.astimezone(tz)


def free_slots(busy: list[Event], window_start: datetime, window_end: datetime, minutes: int) -> list[tuple]:
    """Créneaux libres d'au moins `minutes` dans la fenêtre (les journées entières ne bloquent pas)."""
    slots, cursor = [], window_start
    for ev in sorted((e for e in busy if not e.all_day), key=lambda e: e.start):
        if ev.end <= cursor or ev.start >= window_end:
            continue
        if ev.start - cursor >= timedelta(minutes=minutes):
            slots.append((cursor, ev.start))
        cursor = max(cursor, ev.end)
    if window_end - cursor >= timedelta(minutes=minutes):
        slots.append((cursor, window_end))
    return slots


def make_ics(title: str, start: datetime, end: datetime, description: str = "") -> bytes:
    """Petit fichier .ics : le toucher sur le téléphone propose de l'ajouter à l'agenda."""
    fmt = "%Y%m%dT%H%M%SZ"
    utc = ZoneInfo("UTC")

    def esc(text: str) -> str:
        return text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")

    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Jarvis//FR", "METHOD:PUBLISH",
        "BEGIN:VEVENT",
        f"UID:{uuid.uuid4()}@jarvis",
        f"DTSTAMP:{datetime.now(utc):{fmt}}",
        f"DTSTART:{start.astimezone(utc):{fmt}}",
        f"DTEND:{end.astimezone(utc):{fmt}}",
        f"SUMMARY:{esc(title)}",
        f"DESCRIPTION:{esc(description)}",
        "BEGIN:VALARM", "ACTION:DISPLAY", f"DESCRIPTION:{esc(title)}", "TRIGGER:-PT5M", "END:VALARM",
        "END:VEVENT", "END:VCALENDAR",
    ]
    return ("\r\n".join(lines) + "\r\n").encode()


def fmt_event(ev: Event) -> str:
    when = "toute la journée" if ev.all_day else f"{ev.start:%H:%M}–{ev.end:%H:%M}"
    where = f" ({ev.location})" if ev.location else ""
    return f"{when} · {ev.title}{where}"


class CalendarSource:
    """Télécharge les agendas (avec un petit cache) et répond aux questions de dates."""

    def __init__(self, urls: tuple[str, ...], tz: ZoneInfo, cache_seconds: int = 600):
        self.urls, self.tz, self.cache_seconds = urls, tz, cache_seconds
        self._cache: tuple[float, list[str]] | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.urls)

    async def _texts(self) -> list[str]:
        if self._cache and time.monotonic() - self._cache[0] < self.cache_seconds:
            return self._cache[1]
        texts = []
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            for url in self.urls:
                try:
                    r = await client.get(url)
                    r.raise_for_status()
                    texts.append(r.text)
                except httpx.HTTPError as exc:
                    log.warning("Agenda injoignable (%s)", exc.__class__.__name__)
        if texts:
            self._cache = (time.monotonic(), texts)
        return texts

    async def events(self, start: datetime, end: datetime) -> list[Event]:
        if not self.enabled:
            return []
        return parse_events(await self._texts(), start, end, self.tz)

    async def day(self, day: date) -> list[Event]:
        start = datetime(day.year, day.month, day.day, tzinfo=self.tz)
        return await self.events(start, start + timedelta(days=1))
