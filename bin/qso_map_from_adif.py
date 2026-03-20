#!/usr/bin/env python3
"""Generate an interactive HTML map from QSOs stored in an ADIF file.

The script reads callsigns from ADIF/ADIF-like files, looks each station up via
QRZ.com's XML API, extracts latitude/longitude (or derives them from the
Maidenhead grid if needed), and writes a self-contained Leaflet-based HTML map.

Examples:
    python3 bin/qso_map_from_adif.py \
        --adif log.adi \
        --output qso-map.html \
        --qrz-username YOUR_QRZ_USERNAME \
        --qrz-password YOUR_QRZ_PASSWORD

Credentials can also be provided via environment variables:
    QRZ_USERNAME / QRZ_PASSWORD
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

QRZ_ENDPOINT = "https://xmldata.qrz.com/xml/current/"
USER_AGENT = "qso-map-from-adif/1.0"
TAG_PATTERN = re.compile(r"<([^>]+)>", re.IGNORECASE)


@dataclass
class QSORecord:
    call: str
    band: str = ""
    mode: str = ""
    qso_date: str = ""
    time_on: str = ""


class ADIFParseError(RuntimeError):
    pass


class QRZAPIError(RuntimeError):
    pass


class QRZClient:
    def __init__(self, username: str, password: str, *, timeout: float = 15.0) -> None:
        self.username = username
        self.password = password
        self.timeout = timeout
        self.session_key: str | None = None

    def login(self) -> None:
        params = {
            "username": self.username,
            "password": self.password,
            "agent": USER_AGENT,
        }
        root = self._request(params)
        session = root.find("Session")
        if session is None:
            raise QRZAPIError("QRZ login failed: missing <Session> node in response.")

        error = session.findtext("Error")
        if error:
            raise QRZAPIError(f"QRZ login failed: {error}")

        key = session.findtext("Key")
        if not key:
            raise QRZAPIError("QRZ login failed: response did not include a session key.")

        self.session_key = key.strip()

    def lookup_callsign(self, callsign: str) -> dict[str, str]:
        if not self.session_key:
            self.login()

        assert self.session_key is not None
        root = self._request({"s": self.session_key, "callsign": callsign})

        session = root.find("Session")
        if session is not None:
            error = (session.findtext("Error") or "").strip()
            if error:
                if "session" in error.lower():
                    self.login()
                    assert self.session_key is not None
                    root = self._request({"s": self.session_key, "callsign": callsign})
                    session = root.find("Session")
                    error = (session.findtext("Error") or "").strip() if session is not None else ""
                if error:
                    raise QRZAPIError(f"QRZ lookup failed for {callsign}: {error}")

            next_key = (session.findtext("Key") or "").strip()
            if next_key:
                self.session_key = next_key

        callsign_node = root.find("Callsign")
        if callsign_node is None:
            raise QRZAPIError(f"QRZ lookup failed for {callsign}: missing <Callsign> node.")

        return {
            child.tag.lower(): (child.text or "").strip()
            for child in callsign_node
            if child.tag and child.text is not None
        }

    def _request(self, params: dict[str, str]) -> ET.Element:
        url = f"{QRZ_ENDPOINT}?{urllib.parse.urlencode(params)}"
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = response.read()
        except Exception as exc:  # noqa: BLE001
            raise QRZAPIError(f"Failed to contact QRZ.com: {exc}") from exc

        try:
            return ET.fromstring(payload)
        except ET.ParseError as exc:
            raise QRZAPIError("QRZ returned malformed XML.") from exc


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Load QSOs from ADIF, enrich them via QRZ.com and create an interactive HTML map.",
    )
    parser.add_argument("--adif", required=True, help="Path to the input ADIF/ADI file.")
    parser.add_argument("--output", default="qso_map.html", help="Path to the output HTML map.")
    parser.add_argument("--qrz-username", default=os.environ.get("QRZ_USERNAME"), help="QRZ.com username.")
    parser.add_argument("--qrz-password", default=os.environ.get("QRZ_PASSWORD"), help="QRZ.com password.")
    parser.add_argument(
        "--cache-file",
        default=".qrz_cache.json",
        help="JSON cache file for QRZ lookups (default: .qrz_cache.json).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Optional limit of unique callsigns to process (0 = all).",
    )
    parser.add_argument(
        "--pause-seconds",
        type=float,
        default=0.0,
        help="Optional delay between QRZ requests to reduce API load.",
    )
    parser.add_argument(
        "--overwrite-cache",
        action="store_true",
        help="Ignore cached QRZ data and fetch fresh data for every callsign.",
    )
    return parser.parse_args()


def parse_adif_records(content: str) -> list[dict[str, str]]:
    upper_content = content.upper()
    eoh_index = upper_content.find("<EOH>")
    if eoh_index != -1:
        content = content[eoh_index + len("<EOH>") :]

    records: list[dict[str, str]] = []
    current: dict[str, str] = {}
    position = 0

    while position < len(content):
        match = TAG_PATTERN.search(content, position)
        if not match:
            break

        tag_body = match.group(1).strip()
        position = match.end()
        if not tag_body:
            continue

        field_name = tag_body.split(":", 1)[0].strip().upper()
        if field_name == "EOR":
            if current:
                records.append(current)
                current = {}
            continue
        if field_name == "EOH":
            continue

        parts = tag_body.split(":")
        if len(parts) < 2 or not parts[1].isdigit():
            continue

        field_length = int(parts[1])
        value = content[position : position + field_length]
        position += field_length
        current[field_name] = value.strip()

    if current:
        records.append(current)

    return records


def load_qsos(adif_path: Path) -> list[QSORecord]:
    try:
        content = adif_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise ADIFParseError(f"Unable to read ADIF file {adif_path}: {exc}") from exc

    raw_records = parse_adif_records(content)
    qsos: list[QSORecord] = []
    for record in raw_records:
        call = normalize_callsign(record.get("CALL", ""))
        if not call:
            continue
        qsos.append(
            QSORecord(
                call=call,
                band=record.get("BAND", ""),
                mode=record.get("MODE", ""),
                qso_date=record.get("QSO_DATE", ""),
                time_on=record.get("TIME_ON", ""),
            )
        )

    if not qsos:
        raise ADIFParseError("The ADIF file did not contain any QSO records with a CALL field.")

    return qsos


def normalize_callsign(value: str) -> str:
    value = value.strip().upper()
    value = value.replace(" ", "")
    return value


def load_cache(cache_path: Path) -> dict[str, dict[str, Any]]:
    if not cache_path.exists():
        return {}
    try:
        raw = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {str(key).upper(): value for key, value in raw.items() if isinstance(value, dict)}


def save_cache(cache_path: Path, cache: dict[str, dict[str, Any]]) -> None:
    cache_path.write_text(json.dumps(cache, indent=2, sort_keys=True), encoding="utf-8")


def maidenhead_to_latlon(grid: str) -> tuple[float, float] | None:
    grid = grid.strip().upper()
    if len(grid) < 4 or len(grid) % 2 != 0:
        return None

    lon = -180.0
    lat = -90.0
    lon_step = 20.0
    lat_step = 10.0

    try:
        lon += (ord(grid[0]) - ord("A")) * lon_step
        lat += (ord(grid[1]) - ord("A")) * lat_step

        lon_step /= 10.0
        lat_step /= 10.0
        lon += int(grid[2]) * lon_step
        lat += int(grid[3]) * lat_step

        index = 4
        pair_index = 0
        while index + 1 < len(grid):
            lon_char = grid[index]
            lat_char = grid[index + 1]
            if pair_index % 2 == 0:
                lon_step /= 24.0
                lat_step /= 24.0
                lon += (ord(lon_char) - ord("A")) * lon_step
                lat += (ord(lat_char) - ord("A")) * lat_step
            else:
                lon_step /= 10.0
                lat_step /= 10.0
                lon += int(lon_char) * lon_step
                lat += int(lat_char) * lat_step
            pair_index += 1
            index += 2
    except (ValueError, IndexError):
        return None

    return (lat + lat_step / 2.0, lon + lon_step / 2.0)


def extract_location(data: dict[str, str]) -> tuple[float, float] | None:
    lat_raw = data.get("lat", "")
    lon_raw = data.get("lon", "")
    if lat_raw and lon_raw:
        try:
            return float(lat_raw), float(lon_raw)
        except ValueError:
            pass

    grid = data.get("grid", "")
    if grid:
        return maidenhead_to_latlon(grid)
    return None


def build_popup(call: str, qso_count: int, data: dict[str, str], location: tuple[float, float]) -> str:
    lines = [
        f"<strong>{html.escape(call)}</strong>",
        f"QSOs in log: {qso_count}",
    ]

    name = data.get("name_fmt") or data.get("fname") or data.get("name")
    if name:
        lines.append(f"Name: {html.escape(name)}")

    address_parts = [
        data.get("addr2", ""),
        data.get("state", ""),
        data.get("country", "") or data.get("land", ""),
    ]
    address = ", ".join(part for part in address_parts if part)
    if address:
        lines.append(f"Location: {html.escape(address)}")

    grid = data.get("grid", "")
    if grid:
        lines.append(f"Grid: {html.escape(grid)}")

    geoloc = data.get("geoloc", "")
    if geoloc:
        lines.append(f"Source: {html.escape(geoloc)}")

    lines.append(f"Coordinates: {location[0]:.5f}, {location[1]:.5f}")
    return "<br/>".join(lines)


def write_map(output_path: Path, points: list[dict[str, Any]]) -> None:
    markers_json = json.dumps(points, ensure_ascii=False)

    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>QSO map</title>
  <link
    rel="stylesheet"
    href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"
    integrity="sha256-p4NxAoJBhIIN+hmNHrzRCf9tD/miZyoHS5obTRR9BMY="
    crossorigin=""
  />
  <style>
    html, body, #map {{ height: 100%; margin: 0; }}
    .legend {{
      position: absolute;
      top: 12px;
      right: 12px;
      z-index: 999;
      background: rgba(255, 255, 255, 0.95);
      padding: 10px 12px;
      border-radius: 8px;
      box-shadow: 0 2px 10px rgba(0, 0, 0, 0.15);
      font: 14px/1.4 sans-serif;
      max-width: 280px;
    }}
  </style>
</head>
<body>
  <div id="map"></div>
  <div class="legend">
    <strong>QSO map</strong><br/>
    Unique callsigns shown: {len(points)}<br/>
    Generated by {html.escape(USER_AGENT)}
  </div>
  <script
    src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"
    integrity="sha256-20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo="
    crossorigin=""
  ></script>
  <script>
    const map = L.map('map');
    const markers = {markers_json};
    L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
      maxZoom: 18,
      attribution: '&copy; OpenStreetMap contributors'
    }}).addTo(map);

    const bounds = [];
    for (const marker of markers) {{
      const latLng = [marker.lat, marker.lon];
      bounds.push(latLng);
      L.marker(latLng)
        .addTo(map)
        .bindPopup(marker.popup);
    }}

    if (bounds.length === 1) {{
      map.setView(bounds[0], 6);
    }} else {{
      map.fitBounds(bounds, {{ padding: [25, 25] }});
    }}
  </script>
</body>
</html>
"""
    output_path.write_text(html_content, encoding="utf-8")


def main() -> int:
    args = parse_args()
    adif_path = Path(args.adif)
    output_path = Path(args.output)
    cache_path = Path(args.cache_file)

    try:
        qsos = load_qsos(adif_path)
    except ADIFParseError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    by_call: dict[str, list[QSORecord]] = defaultdict(list)
    for qso in qsos:
        by_call[qso.call].append(qso)

    callsigns = sorted(by_call)
    if args.limit > 0:
        callsigns = callsigns[: args.limit]

    cache = load_cache(cache_path)
    missing_callsigns = [call for call in callsigns if args.overwrite_cache or call not in cache]
    if missing_callsigns and (not args.qrz_username or not args.qrz_password):
        print(
            "Error: provide QRZ credentials via --qrz-username/--qrz-password or QRZ_USERNAME/QRZ_PASSWORD.",
            file=sys.stderr,
        )
        return 2

    client = QRZClient(args.qrz_username or "", args.qrz_password or "")
    points: list[dict[str, Any]] = []
    failures: list[str] = []

    for callsign in callsigns:
        try:
            if args.overwrite_cache or callsign not in cache:
                cache[callsign] = client.lookup_callsign(callsign)
                if args.pause_seconds > 0:
                    time.sleep(args.pause_seconds)

            data = cache[callsign]
            location = extract_location(data)
            if not location:
                failures.append(f"{callsign}: missing coordinates in QRZ response")
                continue

            points.append(
                {
                    "call": callsign,
                    "lat": location[0],
                    "lon": location[1],
                    "popup": build_popup(callsign, len(by_call[callsign]), data, location),
                }
            )
        except QRZAPIError as exc:
            failures.append(str(exc))

    save_cache(cache_path, cache)

    if not points:
        print("Error: no station locations could be resolved.", file=sys.stderr)
        if failures:
            print("Details:", file=sys.stderr)
            for failure in failures:
                print(f"  - {failure}", file=sys.stderr)
        return 1

    write_map(output_path, points)

    print(f"Loaded QSOs: {len(qsos)}")
    print(f"Unique callsigns in map: {len(points)}")
    print(f"Output map: {output_path}")
    if failures:
        print("Warnings:")
        for failure in failures:
            print(f"  - {failure}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
