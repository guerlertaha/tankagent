#!/usr/bin/env python3
"""Liest die Kraftstoffpreise der JET-Tankstelle Mainz, Wormser Str. 135,
von jet.de und haengt bei einer Aenderung eine Zeile an data/prices.csv an.

Quelle 1: der JSON-Endpunkt, den die Stationsseite selbst zur Aktualisierung
          der Preistabelle abruft (liefert ein HTML-Fragment).
Quelle 2: die Stationsseite als Fallback. Beide enthalten dieselben Element-IDs.

Optionale Umgebungsvariablen:
  JET_STATION_CODE  Stationscode aus dem Attribut data-station der Seite
  JET_PAGE_URL      URL der Stationsseite
  CSV_PATH          Zieldatei, Standard: data/prices.csv
  JITTER_MAX_S      zufaellige Wartezeit vor der Abfrage in Sekunden, Standard: 0

Nur Standardbibliothek, keine Abhaengigkeiten.
"""

import csv
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from zoneinfo import ZoneInfo

STATION_CODE = "31e1ff9c20"
PAGE_URL = "https://www.jet.de/tankstellen/mainz/wormser-str-135"
API_URL = "https://www.jet.de/api/v2/station/price-table/update/fuel?station={code}"
USER_AGENT = "tankagent-collector/1.0 (privater Preistracker, eine Station, alle 15 min)"

# CSV-Spalte -> Element-ID auf jet.de
FUEL_IDS = {
    "diesel": "product_price_fuel_diesel",
    "super": "product_price_fuel_super",
    "super_e10": "product_price_fuel_super_e10",
    "super_plus": "product_price_fuel_super_plus",
}
TIMESTAMP_ID = "product_price_timestamp_fuel"
FUELS = list(FUEL_IDS)
FIELDS = ["fetched_utc", "price_stand"] + FUELS + ["source"]
COMPARE = ["price_stand"] + FUELS

PRICE_RE = re.compile(r"(\d),(\d{2,3})")
STAND_RE = re.compile(r"(\d{2})\.(\d{2})\.(\d{4}),?\s+(\d{1,2}):(\d{2})")
VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input",
             "link", "meta", "source", "track", "wbr"}
STATION_TZ = ZoneInfo("Europe/Berlin")


class LayoutError(RuntimeError):
    """Die erwarteten Elemente fehlen, vermutlich wurde die Seite umgebaut."""


class IdTextExtractor(HTMLParser):
    """Sammelt den Textinhalt der Elemente mit den gesuchten IDs."""

    def __init__(self, wanted_ids):
        super().__init__(convert_charrefs=True)
        self.wanted = set(wanted_ids)
        self.found = {}
        self._current = None
        self._depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in VOID_TAGS:
            return
        if self._current is not None:
            self._depth += 1
            return
        element_id = dict(attrs).get("id")
        if element_id in self.wanted and element_id not in self.found:
            self._current = element_id
            self._depth = 1
            self.found[element_id] = ""

    def handle_endtag(self, tag):
        if self._current is None or tag in VOID_TAGS:
            return
        self._depth -= 1
        if self._depth == 0:
            self._current = None

    def handle_data(self, data):
        if self._current is not None:
            self.found[self._current] += data


def parse_price(text: str) -> str:
    """'EUR 2,119' -> '2.119'. Leer, wenn kein Preis im Text steht."""
    match = PRICE_RE.search("".join(text.split()))
    if not match:
        return ""
    value = float(f"{match.group(1)}.{match.group(2)}")
    if not 0.5 <= value <= 5.0:
        raise ValueError(f"Preis ausserhalb des plausiblen Bereichs: {text.strip()!r}")
    return f"{value:.3f}"


def parse_stand(text: str) -> str:
    """'16.11.2026, 20:13' -> '2026-11-16T20:13:00+01:00'.

    Annahme: jet.de gibt die Zeit in deutscher Ortszeit an.
    Laesst sich der Text nicht lesen, wird er unveraendert gespeichert.
    """
    match = STAND_RE.search(text)
    if not match:
        return " ".join(text.split())
    day, month, year, hour, minute = (int(part) for part in match.groups())
    try:
        local = datetime(year, month, day, hour, minute, tzinfo=STATION_TZ)
    except ValueError:
        return " ".join(text.split())
    return local.isoformat()


def parse_html(html_text: str) -> dict:
    """Extrahiert Preise und Preisstand aus Seite oder Fragment."""
    extractor = IdTextExtractor(list(FUEL_IDS.values()) + [TIMESTAMP_ID])
    extractor.feed(html_text)
    extractor.close()
    if not any(element_id in extractor.found for element_id in FUEL_IDS.values()):
        raise LayoutError("Keine Preiselemente gefunden, Seitenstruktur pruefen")
    row = {"price_stand": parse_stand(extractor.found.get(TIMESTAMP_ID, ""))}
    for fuel, element_id in FUEL_IDS.items():
        row[fuel] = parse_price(extractor.found.get(element_id, ""))
    return row


def http_get(url: str, attempts: int = 3) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    last_error = None
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(request, timeout=25) as response:
                return response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as err:
            last_error = f"HTTP {err.code}"
            if 400 <= err.code < 500 and err.code != 429:
                break
        except (urllib.error.URLError, TimeoutError) as err:
            last_error = type(err).__name__
        if attempt < attempts - 1:
            time.sleep(10 * (attempt + 1))
    raise RuntimeError(f"Abruf fehlgeschlagen ({last_error})")


def fetch_from_api(code: str) -> dict:
    payload = json.loads(http_get(API_URL.format(code=code)))
    fragment = payload.get("fuelPricesHtml")
    if payload.get("status") != "success" or not isinstance(fragment, str):
        raise LayoutError(f"Unerwartete Antwort, status={payload.get('status')!r}")
    return parse_html(fragment)


def fetch_from_page(url: str) -> dict:
    return parse_html(http_get(url))


def fetch_prices(code: str, page_url: str) -> dict:
    """Versucht erst den Endpunkt, dann die Seite."""
    try:
        row = fetch_from_api(code)
        row["source"] = "api"
        return row
    except (RuntimeError, ValueError, json.JSONDecodeError) as err:
        print(f"Endpunkt nicht nutzbar ({err}), nehme die Seite", file=sys.stderr)
    row = fetch_from_page(page_url)
    row["source"] = "page"
    return row


def read_last_row(path: Path):
    if not path.exists():
        return None
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    return rows[-1] if rows else None


def has_changed(last_row, new_row: dict) -> bool:
    if last_row is None:
        return True
    return any(last_row.get(key, "") != new_row[key] for key in COMPARE)


def append_row(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    is_new = not path.exists() or path.stat().st_size == 0
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        if is_new:
            writer.writeheader()
        writer.writerow(row)


def main() -> int:
    code = os.environ.get("JET_STATION_CODE", "").strip() or STATION_CODE
    page_url = os.environ.get("JET_PAGE_URL", "").strip() or PAGE_URL
    csv_path = Path(os.environ.get("CSV_PATH", "data/prices.csv"))
    jitter = float(os.environ.get("JITTER_MAX_S", "0") or 0)

    if jitter > 0:
        time.sleep(random.uniform(0, jitter))

    try:
        new_row = fetch_prices(code, page_url)
    except (RuntimeError, ValueError) as err:
        print(f"FEHLER: {err}", file=sys.stderr)
        return 1

    new_row["fetched_utc"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    summary = ", ".join(f"{key}={new_row[key] or '-'}" for key in COMPARE)

    if has_changed(read_last_row(csv_path), new_row):
        append_row(csv_path, new_row)
        print(f"Aenderung gespeichert ({new_row['source']}): {summary}")
    else:
        print(f"Keine Aenderung ({new_row['source']}): {summary}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
