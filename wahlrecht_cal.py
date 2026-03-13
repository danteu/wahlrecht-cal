#!/usr/bin/env python3
"""
wahlrecht-cal: Parse state and federal German election dates from wahlrecht.de
and generate an iCalendar (.ics) feed compatible with Apple Calendar.

Usage:
    python wahlrecht_cal.py             # print ICS to stdout
    python wahlrecht_cal.py -o out.ics  # write ICS to file
    python wahlrecht_cal.py --list      # print extracted events as text
"""

import argparse
import hashlib
import re
import sys
import urllib.request
from datetime import date

from bs4 import BeautifulSoup

SOURCE_URL = "https://www.wahlrecht.de/termine.htm"

# State capital coordinates used for Apple Calendar map previews.
# "alle Bundesländer" / Bundesversammlung use Berlin / centre-of-Germany.
STATE_COORDS: dict[str, tuple[float, float]] = {
    "Baden-Württemberg":      (48.7758,  9.1829),  # Stuttgart
    "Bayern":                 (48.1351, 11.5820),  # München
    "Berlin":                 (52.5200, 13.4050),
    "Brandenburg":            (52.3906, 13.0645),  # Potsdam
    "Bremen":                 (53.0793,  8.8017),
    "Hamburg":                (53.5753, 10.0153),
    "Hessen":                 (50.0782,  8.2397),  # Wiesbaden
    "Mecklenburg-Vorpommern": (53.6355, 11.4012),  # Schwerin
    "Niedersachsen":          (52.3744,  9.7386),  # Hannover
    "Nordrhein-Westfalen":    (51.2217,  6.7762),  # Düsseldorf
    "Rheinland-Pfalz":        (49.9929,  8.2473),  # Mainz
    "Saarland":               (49.2354,  7.0021),  # Saarbrücken
    "Sachsen":                (51.0504, 13.7373),  # Dresden
    "Sachsen-Anhalt":         (52.1205, 11.6276),  # Magdeburg
    "Schleswig-Holstein":     (54.3233, 10.1228),  # Kiel
    "Thüringen":              (50.9847, 11.0298),  # Erfurt
    # National / supra-state events
    "alle Bundesländer":      (51.1657, 10.4515),  # geographic centre of Germany
    "Bundesversammlung":      (52.5200, 13.4050),  # Bundestag, Berlin
}

MONTH_MAP = {
    "Januar": 1,
    "Februar": 2,
    "März": 3,
    "April": 4,
    "Mai": 5,
    "Juni": 6,
    "Juli": 7,
    "August": 8,
    "September": 9,
    "Oktober": 10,
    "November": 11,
    "Dezember": 12,
}


# ---------------------------------------------------------------------------
# ICS serialisation helpers (RFC 5545)
# ---------------------------------------------------------------------------

def _ics_escape(text: str) -> str:
    """Escape special characters in iCalendar TEXT values."""
    return (text
            .replace("\\", "\\\\")
            .replace(";", "\\;")
            .replace(",", "\\,")
            .replace("\n", "\\n"))


def _ics_fold(line: str) -> str:
    """Fold a property line to max 75 octets per RFC 5545."""
    encoded = line.encode("utf-8")
    if len(encoded) <= 75:
        return line + "\r\n"
    parts = []
    while encoded:
        limit = 75 if not parts else 74  # continuation lines lose 1 octet to the leading space
        chunk = encoded[:limit]
        # Don't split in the middle of a multi-byte UTF-8 sequence
        while chunk and (chunk[-1] & 0xC0) == 0x80:
            chunk = chunk[:-1]
        parts.append(chunk.decode("utf-8"))
        encoded = encoded[len(chunk):]
    return "\r\n ".join(parts) + "\r\n"


def _prop(name: str, value: str, params: dict[str, str] | None = None) -> str:
    """Serialise one iCalendar property line (with optional parameters)."""
    param_str = ""
    if params:
        for k, v in params.items():
            # Parameter values containing commas, colons, or semicolons must be quoted
            if any(c in v for c in (",", ":", ";")):
                v = f'"{v}"'
            param_str += f";{k}={v}"
    return _ics_fold(f"{name}{param_str}:{value}")


# ---------------------------------------------------------------------------
# Date parsing
# ---------------------------------------------------------------------------

def parse_date(text: str) -> date | None:
    """Parse a German date string like '8. März 2026'. Returns None if no match."""
    m = re.match(r"(\d{1,2})\.\s*(\w+)\s*(\d{4})", text.strip())
    if m:
        day, month_name, yr = m.groups()
        month = MONTH_MAP.get(month_name)
        if month:
            try:
                return date(int(yr), month, int(day))
            except ValueError:
                pass
    return None


# ---------------------------------------------------------------------------
# HTML scraping
# ---------------------------------------------------------------------------

def is_bold_cell(cell) -> bool:
    """Return True if the cell contains at least one <strong> or <b> element."""
    return bool(cell.find("strong") or cell.find("b"))


def cell_text(cell) -> str:
    """Extract text, normalise non-breaking spaces and comma spacing."""
    raw = cell.get_text(strip=True).replace("\xa0", " ")
    return re.sub(r",\s*", ", ", raw)


def bold_text(cell) -> str:
    """Extract only bold (<strong>/<b>) text from a cell, joined by ', '."""
    parts = []
    for tag in cell.find_all(["strong", "b"]):
        t = tag.get_text(strip=True).replace("\xa0", " ").strip(", ")
        if t:
            parts.append(t)
    raw = ", ".join(parts)
    return re.sub(r",\s*", ", ", raw)


def fetch_elections() -> list[dict]:
    """Fetch the page and extract state and federal (bold) elections."""
    req = urllib.request.Request(SOURCE_URL, headers={"User-Agent": "wahlrecht-cal/1.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        soup = BeautifulSoup(resp.read(), "html.parser")

    # Locate the section header, then the next <table>
    heading = soup.find(lambda tag: tag.name in ("h2", "h3") and "nächsten Wahlen" in tag.get_text())
    if not heading:
        raise RuntimeError("Could not find 'Die nächsten Wahlen in Deutschland' heading.")
    table = heading.find_next("table")
    if not table:
        raise RuntimeError("Could not find the elections table.")

    elections: list[dict] = []
    seen: set[tuple] = set()

    for tbody in table.find_all("tbody"):
        for tr in tbody.find_all("tr"):
            cells = tr.find_all("td")
            if len(cells) < 4:
                continue

            date_cell = cells[1]
            land_cell = cells[2]
            organ_cell = cells[3]

            # Only process bold (state & federal) elections
            if not is_bold_cell(date_cell):
                continue

            date_text = date_cell.get_text(strip=True)
            bundesland = cell_text(land_cell)
            organ = bold_text(organ_cell) or cell_text(organ_cell)

            if not bundesland:
                continue

            election_date = parse_date(date_text)
            if election_date is None:
                continue

            # Deduplicate: same date + same Bundesland = same event
            key = (election_date, bundesland)
            if key in seen:
                continue
            seen.add(key)

            elections.append(
                {
                    "date": election_date,
                    "bundesland": bundesland,
                    "organ": organ,
                }
            )

    if not elections:
        raise RuntimeError(
            "No elections found — the page structure may have changed."
        )

    elections.sort(key=lambda e: e["date"])
    return elections


# ---------------------------------------------------------------------------
# ICS generation
# ---------------------------------------------------------------------------

def build_ics(elections: list[dict]) -> bytes:
    """Serialise the election list as an RFC 5545 iCalendar feed."""
    out: list[str] = []

    out.append("BEGIN:VCALENDAR\r\n")
    out.append(_prop("VERSION", "2.0"))
    out.append(_prop("PRODID", "-//wahlrecht-cal//wahlrecht.de//DE"))
    out.append(_prop("CALSCALE", "GREGORIAN"))
    out.append(_prop("METHOD", "PUBLISH"))
    out.append(_prop("X-WR-CALNAME", _ics_escape("Wahlen in Deutschland")))
    out.append(_prop("X-WR-CALDESC", _ics_escape(f"Wichtige Wahlen in Deutschland (Quelle: {SOURCE_URL})")))
    out.append(_prop("X-WR-TIMEZONE", "Europe/Berlin"))
    out.append(_prop("X-WR-RELCALID", "wahlrecht-cal-feed"))
    out.append(_prop("X-PUBLISHED-TTL", "P1D"))
    out.append(_prop("REFRESH-INTERVAL", "P1D", {"VALUE": "DURATION"}))

    for e in elections:
        out.append("BEGIN:VEVENT\r\n")
        out.append(_prop("SUMMARY", _ics_escape(f"{e['bundesland']}: {e['organ']}")))
        out.append(_prop("DTSTART", e["date"].strftime("%Y%m%d"), {"VALUE": "DATE"}))
        out.append(_prop("TRANSP", "TRANSPARENT"))

        # Location
        bl = e["bundesland"]
        # Longest match avoids "Sachsen" shadowing "Sachsen-Anhalt"
        bl_key = max(
            (k for k in STATE_COORDS if k.lower() in bl.lower()),
            key=len,
            default=None,
        )
        lat, lon = STATE_COORDS.get(bl_key, STATE_COORDS["alle Bundesländer"])

        if bl_key == "alle Bundesländer" or "bundesversammlung" in bl.lower():
            location = "Deutschland"
        else:
            location = f"{bl}, Deutschland"

        out.append(_prop("LOCATION", _ics_escape(location)))
        # X-APPLE-STRUCTURED-LOCATION triggers the map preview in Apple Calendar;
        # the value is a geo: URI so it must not be TEXT-escaped.
        out.append(_prop("X-APPLE-STRUCTURED-LOCATION", f"geo:{lat},{lon}",
                         {"VALUE": "URI", "X-TITLE": location}))

        out.append(_prop("URL", SOURCE_URL))

        uid_source = f"{e['date'].isoformat()}-{e['bundesland']}@wahlrecht-cal"
        uid = hashlib.sha1(uid_source.encode()).hexdigest() + "@wahlrecht-cal"
        out.append(_prop("UID", uid))

        out.append("END:VEVENT\r\n")

    out.append("END:VCALENDAR\r\n")
    return "".join(out).encode("utf-8")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate an Apple Calendar-compatible iCal feed of state and federal German elections."
    )
    parser.add_argument(
        "-o", "--output",
        metavar="FILE",
        help="Write the .ics feed to FILE instead of stdout.",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="Print extracted elections as plain text instead of generating ICS.",
    )
    args = parser.parse_args()

    print("Fetching elections from wahlrecht.de …", file=sys.stderr)
    elections = fetch_elections()
    print(f"Found {len(elections)} state/federal election(s).", file=sys.stderr)

    if args.list:
        for e in elections:
            print(f"{e['date'].isoformat()}  {e['bundesland']:35s}  {e['organ']}")
        return

    ics_bytes = build_ics(elections)

    if args.output:
        with open(args.output, "wb") as f:
            f.write(ics_bytes)
        print(f"Written to {args.output}", file=sys.stderr)
    else:
        sys.stdout.buffer.write(ics_bytes)


if __name__ == "__main__":
    main()
