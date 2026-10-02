"""Shared interval validation, display helpers, CSV import and calendar export."""

from __future__ import annotations

import csv
import io
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

TYPE_LABELS = {
    "working_time": ("Working time", "Arbeitszeit"),
    "doctors_appointment": ("Doctor's appointment", "Arztgang"),
    "business_errand": ("Business errand", "Dienstgang"),
    "education_and_training": ("Education and Training", "Fortbildung"),
}
TYPES = tuple(TYPE_LABELS)


def duration(minutes: int | float | None) -> str:
    if minutes is None:
        return "—"
    value = int(minutes)
    return f"{'−' if value < 0 else ''}{abs(value) // 60}:{abs(value) % 60:02d}"


def interval(
    day: str,
    start: str,
    end: str,
    *,
    kind: str = "working_time",
    comment: str = "",
    timezone: str = "Europe/Berlin",
) -> dict:
    """Accept local HH:MM or timezone-aware ISO timestamps, without rounding."""
    try:
        zone = ZoneInfo(timezone)
        parsed_day = date.fromisoformat(day)

        def timestamp(value):
            if len(value) == 5:
                local = time.fromisoformat(value)
                naive = datetime.combine(parsed_day, local)
                result = naive.replace(tzinfo=zone)
                # Reject DST gaps and ambiguous wall times; ISO offsets resolve ambiguity.
                if (
                    result.astimezone(ZoneInfo("UTC")).astimezone(zone).replace(tzinfo=None)
                    != naive
                    or result.utcoffset() != result.replace(fold=1).utcoffset()
                ):
                    raise ValueError
                return result
            result = datetime.fromisoformat(value)
            if result.tzinfo is None:
                raise ValueError
            return result.astimezone(zone)

        lower, upper = timestamp(start), timestamp(end)
        if (
            lower.date() != parsed_day
            or upper.date() != parsed_day
            or lower >= upper
            or upper > datetime.now(zone)
            or lower.second
            or upper.second
            or lower.microsecond
            or upper.microsecond
        ):
            raise ValueError
        if kind not in TYPES or len(comment) > 500:
            raise ValueError
        return {
            "start": lower.isoformat(),
            "end": upper.isoformat(),
            "type": kind,
            "comment": comment,
        }
    except (ValueError, TypeError, KeyError):
        raise ValueError("invalid_interval") from None


def import_csv(content: str, *, timezone: str = "Europe/Berlin") -> list[dict]:
    reader = csv.DictReader(io.StringIO(content))
    if not reader.fieldnames or not {"date", "start", "end"} <= set(reader.fieldnames):
        raise ValueError("invalid_import")
    entries = []
    for row in reader:
        if row.get("end", "").strip() in {"", "N/A"}:
            raise ValueError("unfinished_interval")
        entries.append(
            interval(
                row["date"],
                row["start"],
                row["end"],
                kind=row.get("type") or "working_time",
                comment=row.get("comment") or "",
                timezone=timezone,
            )
        )
    for index, entry in enumerate(entries):
        lower, upper = datetime.fromisoformat(entry["start"]), datetime.fromisoformat(entry["end"])
        for other in entries[:index]:
            if lower < datetime.fromisoformat(other["end"]) and upper > datetime.fromisoformat(
                other["start"]
            ):
                raise ValueError("overlap")
    if not entries:
        raise ValueError("invalid_import")
    return entries


def calendar_ics(events: list[dict]) -> str:
    def escape(value):
        return (
            str(value)
            .replace("\\", "\\\\")
            .replace("\r", "")
            .replace("\n", "\\n")
            .replace(";", "\\;")
            .replace(",", "\\,")
        )

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//HR WORKS Employee//EN",
        "CALSCALE:GREGORIAN",
    ]
    for event in events:
        lines.extend(
            [
                "BEGIN:VEVENT",
                f"UID:{escape(event['uid'])}@hrworks-employee",
                "DTSTAMP:" + datetime.now(ZoneInfo("UTC")).strftime("%Y%m%dT%H%M%SZ"),
                "DTSTART;VALUE=DATE:" + date.fromisoformat(event["start"]).strftime("%Y%m%d"),
                "DTEND;VALUE=DATE:" + date.fromisoformat(event["end"]).strftime("%Y%m%d"),
                "SUMMARY:" + escape(event.get("summary", event.get("title", event["kind"]))),
                "DESCRIPTION:" + escape(event.get("status", "")),
                "END:VEVENT",
            ]
        )
    lines.append("END:VCALENDAR")
    # RFC 5545 folding counts octets and never splits a UTF-8 character.
    folded = []
    for line in lines:
        current = ""
        for character in line:
            if len((current + character).encode()) > 75:
                folded.append(current)
                current = " "
            current += character
        folded.append(current)
    return "\r\n".join(folded) + "\r\n"
