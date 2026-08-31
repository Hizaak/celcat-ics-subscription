#!/usr/bin/env python3
"""
Genere un fichier .ics a partir du calendrier public CELCAT de l'Universite de Bordeaux.

Usage:
    python celcat_ics.py --group "4TVL904S M2 Algorithms, Models and Verification" -o edt.ics
"""

import argparse
import hashlib
import html
import json
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

BASE = "https://celcat.u-bordeaux.fr/calendar"
ENDPOINT = f"{BASE}/Home/GetCalendarData"
PARIS = ZoneInfo("Europe/Paris")
RES_TYPE_GROUP = 103  # confirme par EntityTypeAsIntegerString dans l'URL de la vue groupe

VTIMEZONE = """BEGIN:VTIMEZONE
TZID:Europe/Paris
BEGIN:DAYLIGHT
TZOFFSETFROM:+0100
TZOFFSETTO:+0200
TZNAME:CEST
DTSTART:19700329T020000
RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=-1SU
END:DAYLIGHT
BEGIN:STANDARD
TZOFFSETFROM:+0200
TZOFFSETTO:+0100
TZNAME:CET
DTSTART:19701025T030000
RRULE:FREQ=YEARLY;BYMONTH=10;BYDAY=-1SU
END:STANDARD
END:VTIMEZONE"""


def fetch_events(group, start, end):
    payload = {
        "start": start,
        "end": end,
        "resType": str(RES_TYPE_GROUP),
        "calView": "month",
        "federationIds[]": group,
        "colourScheme": "3",
    }
    headers = {
        "X-Requested-With": "XMLHttpRequest",
        "Referer": f"{BASE}/cal",
        "User-Agent": "Mozilla/5.0 (celcat-ics)",
    }
    body = urllib.parse.urlencode(payload).encode("utf-8")
    headers["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(ENDPOINT, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if not isinstance(data, list):
        raise SystemExit(f"Reponse inattendue de CELCAT: {type(data)}")
    return data


def clean(raw):
    """Le champ description est du HTML avec des <br /> comme separateurs."""
    if not raw:
        return []
    parts = re.split(r"<br\s*/?>", raw, flags=re.I)
    out = []
    for p in parts:
        p = html.unescape(re.sub(r"<[^>]+>", "", p)).strip()
        if p and p not in out:
            out.append(p)
    return out


def to_local(naive_str):
    """Heure locale + TZID, comme attendu par les clients les plus tatillons."""
    return datetime.fromisoformat(naive_str[:19]).strftime("%Y%m%dT%H%M%S")


def esc(text):
    return (
        text.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\n", "\\n")
    )


def fold(line):
    """RFC 5545: 75 octets max par ligne."""
    raw = line.encode("utf-8")
    if len(raw) <= 73:
        return line
    chunks, cur = [], b""
    for ch in line:
        b = ch.encode("utf-8")
        if len(cur) + len(b) > 73:
            chunks.append(cur.decode("utf-8"))
            cur = b""
        cur += b
    chunks.append(cur.decode("utf-8"))
    return "\r\n ".join(chunks)


CODE_RE = re.compile(r"^\d[A-Z]{2,4}\d{3}[A-Z]\b")
ROOM_RE = re.compile(r"^[A-Z]+\d*\s*/\s*\S")
WEEKS_RE = re.compile(r"^[\d\s,\-]+$")
NOISE_RE = re.compile(r"\bID\s*\d+", re.I)
STAFF_RE = re.compile(r"^[A-ZÀ-Þ][A-ZÀ-Þ'\- ]+\s+[A-ZÀ-Þ][a-zà-ÿ]")


def parse_fields(fields, group, category=""):
    """Repartit les lignes de la description CELCAT par nature."""
    info = {"module": None, "room": None, "weeks": None, "staff": [], "other": []}
    gnorm = group.strip().lower()
    cnorm = (category or "").strip().lower()

    for f in fields:
        if f.strip().lower() in (gnorm, cnorm):
            continue                          # groupe / categorie, redondants
        if NOISE_RE.search(f):
            continue                          # "Salle TD avec ID 1604791"
        if WEEKS_RE.match(f):
            info["weeks"] = f
        elif ROOM_RE.match(f):
            info["room"] = re.sub(r"\s*/\s*", " / ", f)
        elif CODE_RE.match(f) and info["module"] is None:
            info["module"] = f
        elif STAFF_RE.match(f):
            info["staff"].append(f)
        else:
            info["other"].append(f)
    return info


def build_ics(events, calname, exclude=()):
    exclude = [e.strip().lower() for e in exclude if e.strip()]
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//celcat-ics//u-bordeaux//FR",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{esc(calname)}",
        "X-WR-TIMEZONE:Europe/Paris",
        VTIMEZONE.replace("\n", "\r\n"),
    ]

    for ev in events:
        if not ev.get("start"):
            continue
        fields = clean(ev.get("description"))
        category = (ev.get("eventCategory") or "").strip()
        info = parse_fields(fields, calname, category)

        # Titre : nom du module sans son code, + type de seance.
        title = ""
        if info["module"]:
            title = CODE_RE.sub("", info["module"]).strip(" -")
        if not title and info["other"]:
            title = info["other"][0]
        if not title:
            title = category or "Cours"
        summary = title if not category or category == title else f"{title} - {category}"

        # Modules explicitement ecartes (--exclude), par code ou par nom.
        haystack = f"{info['module'] or ''} {title}".lower()
        if any(x in haystack for x in exclude):
            continue

        location = info["room"] or ", ".join(s for s in (ev.get("sites") or []) if s)

        desc = []
        if info["module"]:
            desc.append(info["module"])
        if info["staff"]:
            desc.append(", ".join(info["staff"]))
        if location:
            desc.append(location)
        if info["weeks"]:
            desc.append(f"Semaines {info['weeks']}")
        desc.extend(info["other"])

        uid_seed = f"{ev.get('id','')}|{ev['start']}|{title}"
        uid = hashlib.sha1(uid_seed.encode("utf-8")).hexdigest()

        # DTSTAMP fixe : deterministe (pas de commit inutile a chaque run)
        # et sans date future, que certains clients digerent mal.
        stamp = "20200101T000000Z"

        lines.append("BEGIN:VEVENT")
        lines.append(f"UID:{uid}@celcat.u-bordeaux.fr")
        lines.append(f"DTSTAMP:{stamp}")
        lines.append(f"DTSTART;TZID=Europe/Paris:{to_local(ev['start'])}")
        end = ev.get("end") or ev["start"]
        lines.append(f"DTEND;TZID=Europe/Paris:{to_local(end)}")
        lines.append(fold(f"SUMMARY:{esc(summary)}"))
        if location:
            lines.append(fold(f"LOCATION:{esc(location)}"))
        if desc:
            lines.append(fold(f"DESCRIPTION:{esc(chr(10).join(desc))}"))
        lines.append("END:VEVENT")

    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--group", required=True, help="federationId exact du groupe")
    ap.add_argument("--start", default="2026-09-01")
    ap.add_argument("--end", default="2027-07-31")
    ap.add_argument("-o", "--output", default="edt.ics")
    ap.add_argument("--exclude", action="append", default=[],
                    help="module a ignorer (code ou nom, repetable)")
    args = ap.parse_args()

    events = fetch_events(args.group, args.start, args.end)
    print(f"{len(events)} evenements recuperes", file=sys.stderr)
    if not events:
        print("Aucun evenement: verifie le nom exact du groupe.", file=sys.stderr)

    ics = build_ics(events, args.group, args.exclude)
    with open(args.output, "w", encoding="utf-8", newline="") as f:
        f.write(ics)
    print(f"Ecrit dans {args.output}", file=sys.stderr)


if __name__ == "__main__":
    main()
