#!/usr/bin/env python3
"""
Refresh item prices in bosses.json from the OSRS Wiki real-time prices API.

Runs weekly from GitHub Actions (.github/workflows/update-prices.yml) and can also be run by hand.

Designed to be gentle on the Wiki:
  * At most TWO requests per run: /mapping (item names -> IDs) and /latest (every item's price in
    one response). Items are never requested one by one.
  * /mapping is cached in item_mapping.json for 7 days when run locally (in GitHub Actions there is
    no cache, so a weekly run makes both requests).
  * Refuses to run again within MIN_HOURS of the last update unless --force is given.
  * Sends a descriptive User-Agent with a contact, as the Wiki asks:
    https://oldschool.runescape.wiki/w/RuneScape:Real-time_Prices

Every run overwrites price_update_log.json with what happened, including the Wiki's responses
(status, headers and body) for each request made.

Usage:  python update_prices.py            (normal)
        python update_prices.py --force    (ignore the minimum interval)
Needs only the Python standard library.
"""
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).parent
DATA = HERE / "bosses.json"
MAPPING_CACHE = HERE / "item_mapping.json"
LOG = HERE / "price_update_log.json"
API = "https://prices.runescape.wiki/api/v1/osrs"
USER_AGENT = "OSRS Pet Hunt Profit Calculator - weekly price snapshot (github.com/HarveyMahon/Pet-Hunt-Profit-Calculator; Discord: Adamolf#7451)"
MIN_HOURS = 6
MAPPING_MAX_AGE_DAYS = 7
MIN_PRICED_ITEMS = 1000  # sanity check: /latest normally returns several thousand items

log = {"runStartedAt": None, "userAgent": USER_AGENT, "outcome": None, "requests": [], "summary": {}}


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def write_log():
    LOG.write_text(json.dumps(log, indent=1, ensure_ascii=False), encoding="utf-8")


def get(path):
    """GET one endpoint and record the full response in the log."""
    url = f"{API}/{path}"
    entry = {"url": url, "requestedAt": now_iso()}
    log["requests"].append(entry)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    started = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read()
            entry.update(status=r.status, headers=dict(r.headers.items()))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        entry.update(status=e.code, headers=dict(e.headers.items()), body=body,
                     seconds=round(time.monotonic() - started, 2))
        raise
    except Exception as e:  # network errors, timeouts
        entry.update(status=None, error=repr(e), seconds=round(time.monotonic() - started, 2))
        raise
    entry["seconds"] = round(time.monotonic() - started, 2)
    entry["bytes"] = len(raw)
    body = json.loads(raw)
    entry["body"] = body
    return body


def apply_derived(items):
    """Price items that are valued from other items (e.g. Tokkul, vestiges, bludgeon pieces)."""
    for item in items.values():
        f = item.get("derivedFrom")
        if not f:
            continue
        if "terms" in f:
            if all(t["item"] in items for t in f["terms"]):
                item["price"] = round(max(0, sum(items[t["item"]]["price"] * t["factor"] for t in f["terms"])), 4)
        elif f.get("item") in items:
            item["price"] = round(items[f["item"]]["price"] / f["divisor"], 4)


def main():
    force = "--force" in sys.argv
    log["runStartedAt"] = now_iso()
    data = json.loads(DATA.read_text(encoding="utf-8"))
    meta = data["meta"]

    last = meta.get("pricesUpdatedAt")
    if last and not force:
        age_h = (time.time() - datetime.fromisoformat(last).timestamp()) / 3600
        if age_h < MIN_HOURS:
            log["outcome"] = f"skipped: prices were updated {age_h:.1f}h ago (minimum {MIN_HOURS}h); no requests made"
            write_log()
            print(log["outcome"])
            return 0

    try:
        # 1) Name -> ID mapping (cached locally for a week)
        if MAPPING_CACHE.exists() and (time.time() - MAPPING_CACHE.stat().st_mtime) < MAPPING_MAX_AGE_DAYS * 86400:
            mapping = json.loads(MAPPING_CACHE.read_text(encoding="utf-8"))
            log["summary"]["mapping"] = "from local cache (no request)"
        else:
            print("Downloading item mapping (1 request)...")
            mapping = get("mapping")
            MAPPING_CACHE.write_text(json.dumps(mapping), encoding="utf-8")
            log["summary"]["mapping"] = "downloaded"

        # 2) All latest prices in one request
        print("Downloading latest prices (1 request)...")
        latest = get("latest")["data"]
    except Exception as e:
        log["outcome"] = f"failed: {e!r}; bosses.json was not changed"
        write_log()
        print(log["outcome"])
        return 1

    if len(latest) < MIN_PRICED_ITEMS:
        log["outcome"] = f"failed: /latest returned only {len(latest)} items; bosses.json was not changed"
        write_log()
        print(log["outcome"])
        return 1

    by_name = {m["name"].lower(): m for m in mapping}
    updated, unchanged, missing = [], [], []
    for name, item in data["items"].items():
        if item.get("derivedFrom"):
            continue  # priced from other items below, never from the market
        m = by_name.get(name.lower())
        if not m:
            if item.get("tradeable") and not item.get("derivedFrom"):
                missing.append(name)
            continue
        item["id"] = m["id"]
        p = latest.get(str(m["id"]))
        prices = [v for v in ((p or {}).get("high"), (p or {}).get("low")) if v]
        if not prices:
            unchanged.append(name)
            continue
        new = round(sum(prices) / len(prices))
        if new != item.get("price"):
            updated.append({"item": name, "old": item.get("price"), "new": new})
        item["price"] = new
        item["tradeable"] = True

    apply_derived(data["items"])

    meta["pricesUpdated"] = datetime.now(timezone.utc).date().isoformat()
    meta["pricesUpdatedAt"] = now_iso()
    meta["priceSource"] = "prices.runescape.wiki /latest (average of instant buy and sell)"
    DATA.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    log["outcome"] = "updated"
    log["summary"].update({
        "itemsInFile": len(data["items"]),
        "pricesChanged": len(updated),
        "noRecentTrade": unchanged,
        "notOnGrandExchange": missing,
        "changes": updated,
    })
    write_log()
    print(f"Updated prices: {len(updated)} changed. Log written to {LOG.name}.")
    if missing:
        print("Not found in the Wiki mapping (kept old price):", ", ".join(missing))
    return 0


if __name__ == "__main__":
    sys.exit(main())
