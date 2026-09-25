#!/usr/bin/env python3
"""
Refresh item prices in bosses.json from the OSRS Wiki real-time prices API.

Designed to be gentle on the Wiki:
  * Makes at most TWO requests per run: /mapping (item names -> IDs) and /latest
    (every item's price in one response). It never requests items one by one.
  * The /mapping result is cached in item_mapping.json and only re-downloaded
    when it is older than 7 days (it rarely changes).
  * Refuses to run again within MIN_HOURS of the last update unless --force.
  * Sends a descriptive User-Agent, as the Wiki asks
    (https://oldschool.runescape.wiki/w/RuneScape:Real-time_Prices).

Usage:  python update_prices.py            (normal)
        python update_prices.py --force    (ignore the minimum interval)
Needs only the Python standard library.
"""
import json, sys, time, urllib.request
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).parent
DATA = HERE / "bosses.json"
MAPPING_CACHE = HERE / "item_mapping.json"
API = "https://prices.runescape.wiki/api/v1/osrs"
# Please put your own contact (Discord name or email) here so the Wiki can reach you.
USER_AGENT = "OSRS Pet Hunt Profit Calculator - price snapshot updater (contact: CHANGE_ME)"
MIN_HOURS = 6
MAPPING_MAX_AGE_DAYS = 7


def get(path):
    req = urllib.request.Request(f"{API}/{path}", headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def main():
    force = "--force" in sys.argv
    data = json.loads(DATA.read_text(encoding="utf-8"))
    meta = data["meta"]

    last = meta.get("pricesUpdatedAt")
    if last and not force:
        age_h = (time.time() - datetime.fromisoformat(last).timestamp()) / 3600
        if age_h < MIN_HOURS:
            print(f"Prices were updated {age_h:.1f}h ago (minimum {MIN_HOURS}h). Use --force to override.")
            return

    # 1) Name -> ID mapping, cached locally
    if MAPPING_CACHE.exists() and (time.time() - MAPPING_CACHE.stat().st_mtime) < MAPPING_MAX_AGE_DAYS * 86400:
        mapping = json.loads(MAPPING_CACHE.read_text(encoding="utf-8"))
    else:
        print("Downloading item mapping (1 request)...")
        mapping = get("mapping")
        MAPPING_CACHE.write_text(json.dumps(mapping), encoding="utf-8")
    by_name = {m["name"].lower(): m for m in mapping}

    # 2) All latest prices in one request
    print("Downloading latest prices (1 request)...")
    latest = get("latest")["data"]

    updated, missing = 0, []
    for name, item in data["items"].items():
        m = by_name.get(name.lower())
        if not m:
            if item.get("tradeable"):
                missing.append(name)
            continue
        item["id"] = m["id"]
        p = latest.get(str(m["id"]))
        if not p:
            continue
        highs_lows = [v for v in (p.get("high"), p.get("low")) if v]
        if highs_lows:
            item["price"] = round(sum(highs_lows) / len(highs_lows))
            item["tradeable"] = True
            updated += 1

    # Items valued from another item (e.g. Tokkul = Uncut onyx / 260,000)
    for item in data["items"].values():
        src = item.get("derivedFrom")
        if src and src["item"] in data["items"]:
            item["price"] = round(data["items"][src["item"]]["price"] / src["divisor"], 4)

    now = datetime.now(timezone.utc)
    meta["pricesUpdated"] = now.date().isoformat()
    meta["pricesUpdatedAt"] = now.isoformat(timespec="seconds")
    meta["priceSource"] = "prices.runescape.wiki /latest (average of instant buy and sell)"
    DATA.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"Updated {updated} item prices.")
    if missing:
        print("Not found in the Wiki mapping (kept old price):", ", ".join(missing))


if __name__ == "__main__":
    main()
