#!/usr/bin/env python3
"""Scrape BADUC's black sheet steel table and update godxf.json sheet_price_lei_m2 values.

For each thickness in the configured material (normally S), all S235JR table
variants are considered. Prices are normalized to lei/m² (LEI/buc divided by
sheet area; LEI/mp used as-is), and the HIGHEST price for each thickness is
rounded according to config and written to the matching materials.steel.thicknesses entry's `sheet_price_lei_m2`.
"""
import json
import logging
import math
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
LOG = logging.getLogger("baduc-price-scraper")


def number(text):
    """Parse Romanian/European numbers such as 1.234,56 or 381.15."""
    value = str(text).strip().replace("\u00a0", " ").replace(" ", "")
    if "," in value:
        value = value.replace(".", "").replace(",", ".")
    match = re.search(r"\d+(?:\.\d+)?", value)
    if not match:
        raise ValueError(f"Cannot parse number from {text!r}")
    return float(match.group(0))


def header_index(headers, patterns):
    for i, header in enumerate(headers):
        normalized = header.lower().replace("ș", "s").replace("ț", "t")
        if any(pattern in normalized for pattern in patterns):
            return i
    return None


def find_s235jr_table(soup):
    """Find the first table after the S235JR quality heading, with fallback."""
    for heading in soup.find_all(string=re.compile(r"CALITATE\s+OTEL\s*:\s*S235JR", re.I)):
        parent = heading.parent
        for sibling in parent.find_all_next("table"):
            # Stop if another steel quality heading appears before this table.
            prior = sibling.find_previous(string=re.compile(r"CALITATE\s+OTEL\s*:", re.I))
            if prior and re.search(r"S355", str(prior), re.I):
                break
            return sibling
    # BADUC currently presents S235JR as the first table; use header detection.
    for table in soup.find_all("table"):
        headers = [c.get_text(" ", strip=True).lower() for c in table.find_all("th")]
        joined = " ".join(headers)
        if "grosime" in joined and "latime" in joined and "pret" in joined:
            return table
    raise ValueError("Could not find BADUC's S235JR steel price table; page structure may have changed")


def parse_price_rows(html, product):
    soup = BeautifulSoup(html, "html.parser")
    table = find_s235jr_table(soup)
    rows = table.find_all("tr")
    if not rows:
        raise ValueError("The S235JR table is empty")

    header_row = None
    headers = []
    for row in rows:
        cells = row.find_all(["th", "td"])
        candidate = [c.get_text(" ", strip=True) for c in cells]
        joined = " ".join(candidate).lower()
        if "grosime" in joined and "latime" in joined and "lungime" in joined and "pret" in joined:
            header_row, headers = row, candidate
            break
    if header_row is None:
        raise ValueError("Could not identify table columns for thickness, width, length and price")

    idx_t = header_index(headers, ["grosime"])
    idx_w = header_index(headers, ["latime"])
    idx_l = header_index(headers, ["lungime"])
    idx_p = header_index(headers, ["pret"])
    if None in (idx_t, idx_w, idx_l, idx_p):
        raise ValueError(f"Unrecognized BADUC table headers: {headers}")

    prices_by_thickness = {}
    for row in rows[rows.index(header_row) + 1:]:
        cells = row.find_all(["th", "td"])
        values = [c.get_text(" ", strip=True) for c in cells]
        if len(values) <= max(idx_t, idx_w, idx_l, idx_p):
            continue
        try:
            thickness = number(values[idx_t])
            width_mm = number(values[idx_w])
            length_mm = number(values[idx_l])
            price_text = values[idx_p]
            price = number(price_text)
        except ValueError:
            continue
        if thickness <= 0 or width_mm <= 0 or length_mm <= 0 or price <= 0:
            continue

        # Table prices may be per whole sheet (/buc) or per square metre (/mp).
        unit_text = price_text.lower().replace(" ", "")
        area_m2 = (width_mm / 1000.0) * (length_mm / 1000.0)
        if "/mp" in unit_text or "lei/mp" in unit_text:
            price_m2 = price
        elif "/buc" in unit_text or "lei/buc" in unit_text:
            price_m2 = price / area_m2
        else:
            raise ValueError(f"Unrecognized price unit in table row: {price_text!r}")

        entry = {
            "thickness_mm": thickness,
            "width_mm": width_mm,
            "length_mm": length_mm,
            "area_m2": area_m2,
            "price_text": price_text,
            "price_lei_m2": price_m2,
        }
        prices_by_thickness.setdefault(thickness, []).append(entry)

    if not prices_by_thickness:
        raise ValueError("No valid price rows were found in the S235JR table")
    return prices_by_thickness


def rounded_price(value, mode):
    if mode == "ceil":
        return int(math.ceil(value))
    if mode == "round":
        return int(round(value))
    if mode == "none":
        return round(value, 2)
    raise ValueError(f"Unsupported rounding mode: {mode}")


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
    config = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    target = config["target"]
    rules = config["scraper"]
    json_path = ROOT / target.get("json_path", "godxf.json")
    if not json_path.exists():
        raise FileNotFoundError(f"Missing {json_path.name}; put this script beside godxf.json")

    url = config["source"]["url"]
    response = requests.get(url, headers={"User-Agent": "Mozilla/5.0 (compatible; LaserGuyPriceUpdater/1.0)"}, timeout=rules.get("request_timeout_seconds", 30))
    response.raise_for_status()
    all_prices = parse_price_rows(response.text, config["source"])

    data = json.loads(json_path.read_text(encoding="utf-8"))
    material_key = target.get("material_key", "steel")
    price_field = target.get("price_field", "sheet_price_lei_m2")
    try:
        material = data["materials"][material_key]
        thickness_entries = material["thicknesses"]
    except (KeyError, TypeError):
        raise ValueError(
            "JSON schema mismatch: expected data['materials']["
            f"{material_key!r}]['thicknesses'] in godxf.json"
        )
    if not isinstance(thickness_entries, list):
        raise ValueError("JSON schema mismatch: materials.steel.thicknesses must be a list")

    changes, checked = [], []
    rounding = rules.get("rounding", "ceil")
    minimum = float(rules.get("minimum_valid_price", 0.01))
    maximum = float(rules.get("maximum_valid_price", 10000))
    max_change = rules.get("maximum_change_percent")

    for entry in thickness_entries:
        if not isinstance(entry, dict) or "thickness_mm" not in entry or price_field not in entry:
            continue
        thickness = float(entry["thickness_mm"])
        thickness_key = str(entry["thickness_mm"])
        candidates = all_prices.get(thickness)
        if not candidates:
            LOG.warning("No BADUC S235JR table price for %s mm; leaving JSON unchanged", thickness_key)
            continue
        highest = max(candidates, key=lambda item: item["price_lei_m2"])
        new_price = rounded_price(highest["price_lei_m2"], rounding)
        if not minimum <= new_price <= maximum:
            raise ValueError(f"{thickness_key} mm: computed price {new_price} outside {minimum}..{maximum}")
        old_price = entry[price_field]
        if old_price is not None and float(old_price) > 0 and max_change is not None:
            pct = abs(new_price - float(old_price)) / float(old_price) * 100
            if pct > float(max_change):
                raise ValueError(f"{thickness_key} mm: price change {pct:.1f}% exceeds {max_change}%; review manually")
        checked.append({
            "material_key": material_key,
            "thickness_mm": thickness,
            "selected_price_lei_m2": new_price,
            "highest_source_price_lei_m2": round(highest["price_lei_m2"], 4),
            "selected_variant": {k: highest[k] for k in ("width_mm", "length_mm", "area_m2", "price_text")},
            "all_variants": candidates,
            "old_price_lei_m2": old_price,
        })
        if float(old_price) != float(new_price):
            entry[price_field] = new_price
            changes.append({"thickness_mm": thickness, "old_price": old_price, "new_price": new_price})

    if not checked:
        raise ValueError("No JSON thickness entries matched prices from the BADUC table")

    report = {
        "updated_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_url": url,
        "selection_rule": "highest normalized price per m² among all S235JR variants of each thickness",
        "json_path": "materials." + material_key + ".thicknesses[]." + price_field,
        "price_field": price_field,
        "changes": changes,
        "checked_thicknesses": checked,
    }
    if changes:
        rendered = json.dumps(data, ensure_ascii=False, indent=4) + "\n"
        json.loads(rendered)
        json_path.write_text(rendered, encoding="utf-8")
        (ROOT / "price_scraper_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        for item in changes:
            LOG.info("%s %s mm: %s -> %s lei/m²", material_key, item["thickness_mm"], item["old_price"], item["new_price"])
        LOG.info("Updated %d thickness price(s).", len(changes))
    else:
        LOG.info("No price changes detected; godxf.json remains unchanged.")
        (ROOT / "price_scraper_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        LOG.exception("Stopped safely; workflow must not commit failed runs: %s", exc)
        sys.exit(1)
