#!/usr/bin/env python3
"""
Refresh item prices in bosses.json from the OSRS Wiki real-time prices API.

Runs weekly from GitHub Actions (.github/workflows/update-prices.yml) and can also be run by hand.

Designed to be gentle on the Wiki:
  * At most TWO requests per run: /mapping (item names -> IDs) and /latest (every item's price in
    one response). Items are never requested one by one, and failed requests are never retried.
  * /mapping is cached in item_mapping.json for 7 days when run locally (in GitHub Actions there is
    no cache, so a weekly run makes both requests).
  * Refuses to run again within MIN_HOURS of the last update unless --force is given.
  * Sends a descriptive User-Agent with a contact, as the Wiki asks:
    https://oldschool.runescape.wiki/w/RuneScape:Real-time_Prices

bosses.json is only rewritten when BOTH responses arrive, are valid JSON in the expected shape, and
pass sanity checks on the prices. Otherwise it is left exactly as it was and the run exits with an
error. Every run that contacts the Wiki overwrites price_update_log.json with what happened: each
request's URL, status, headers and full response body (including error pages, rate-limit responses
and anything that could not be read as JSON), plus any validation problems or crash traceback.

Usage:  python update_prices.py            (normal)
        python update_prices.py --force    (ignore the minimum interval)
Needs only the Python standard library.
"""
import json
import os
import sys
import tempfile
import time
import traceback
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
TIMEOUT_SECONDS = 60
MAX_LOGGED_TEXT = 500_000        # cap on non-JSON bodies kept in the log (e.g. an HTML error page)

# Sanity checks: if the Wiki returns something odd, keep the old prices rather than publish bad ones.
MIN_MAPPING_ITEMS = 1000         # /mapping normally lists several thousand items
MIN_PRICED_ITEMS = 1000          # /latest normally prices several thousand items
MIN_MATCHED_SHARE = 0.80         # share of our tradeable items that must still be found in /mapping
MAX_WILD_CHANGE_SHARE = 0.10     # abort if more than 10% of our prices move by more than...
WILD_CHANGE_FACTOR = 10          # ...10x up or down in a single week

log = {"runStartedAt": None, "userAgent": USER_AGENT, "outcome": None,
       "bossesJsonChanged": False, "previousPricesUpdatedAt": None,
       "requests": [], "problems": [], "summary": {}}


class UpdateFailed(Exception):
    """A problem that means bosses.json must not be changed."""


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def write_log():
    log["runFinishedAt"] = now_iso()
    try:
        LOG.write_text(json.dumps(log, indent=1, ensure_ascii=False, default=str), encoding="utf-8")
    except Exception as e:  # never let logging hide the real outcome
        print(f"Could not write {LOG.name}: {e!r}", file=sys.stderr)


def problem(msg):
    log["problems"].append(msg)
    return UpdateFailed(msg)


def get(path):
    """GET one endpoint. Records the full response (or error) in the log. Returns parsed JSON."""
    url = f"{API}/{path}"
    entry = {"url": url, "requestedAt": now_iso()}
    log["requests"].append(entry)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    started = time.monotonic()
    raw = b""
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as r:
            entry.update(status=r.status, reason=getattr(r, "reason", None), finalUrl=r.geturl(),
                         headers=dict(r.headers.items()))
            raw = r.read()
    except urllib.error.HTTPError as e:
        # 4xx / 5xx, including 429 Too Many Requests (see Retry-After in the headers)
        try:
            raw = e.read()
        except Exception:
            raw = b""
        entry.update(status=e.code, reason=e.reason, headers=dict(e.headers.items()) if e.headers else {},
                     seconds=round(time.monotonic() - started, 2), bytes=len(raw), body=_body_for_log(raw))
        if e.code == 429:
            entry["note"] = "Rate limited by the Wiki. Not retried; see Retry-After in the headers."
        raise problem(f"{path}: HTTP {e.code} {e.reason}")
    except Exception as e:
        # DNS failure, refused connection, TLS error, timeout, connection reset mid-download...
        entry.update(status=None, error=repr(e), seconds=round(time.monotonic() - started, 2),
                     bytes=len(raw), body=_body_for_log(raw) if raw else None)
        raise problem(f"{path}: request failed ({type(e).__name__}: {e})")
    entry["seconds"] = round(time.monotonic() - started, 2)
    entry["bytes"] = len(raw)
    try:
        body = json.loads(raw)
    except Exception as e:
        entry["body"] = _body_for_log(raw)
        entry["parseError"] = repr(e)
        raise problem(f"{path}: HTTP {entry['status']} but the response is not valid JSON "
                      f"(Content-Type: {entry['headers'].get('Content-Type')})")
    entry["body"] = body
    return body


def _body_for_log(raw):
    """Keep error bodies readable: JSON if possible, otherwise (capped) text."""
    try:
        return json.loads(raw)
    except Exception:
        text = raw.decode("utf-8", "replace")
        if len(text) > MAX_LOGGED_TEXT:
            return text[:MAX_LOGGED_TEXT] + f"\n...[truncated, {len(text)} characters in total]"
        return text


def validate_mapping(mapping):
    if not isinstance(mapping, list):
        raise problem(f"/mapping: expected a JSON list of items, got {type(mapping).__name__}")
    good = [m for m in mapping if isinstance(m, dict) and isinstance(m.get("name"), str)
            and isinstance(m.get("id"), int)]
    if len(good) < MIN_MAPPING_ITEMS:
        raise problem(f"/mapping: only {len(good)} usable entries (with 'id' and 'name') out of "
                      f"{len(mapping)}; expected at least {MIN_MAPPING_ITEMS}. The API format may have changed.")
    return good


def validate_latest(latest):
    if not isinstance(latest, dict) or not isinstance(latest.get("data"), dict):
        keys = list(latest)[:10] if isinstance(latest, dict) else type(latest).__name__
        raise problem(f"/latest: expected an object with a 'data' object, got {keys}. The API format may have changed.")
    data = latest["data"]
    usable = 0
    for v in data.values():
        if isinstance(v, dict) and any(isinstance(v.get(k), (int, float)) and v.get(k) > 0 for k in ("high", "low")):
            usable += 1
    if usable < MIN_PRICED_ITEMS:
        raise problem(f"/latest: only {usable} items have a numeric 'high' or 'low' price out of {len(data)}; "
                      f"expected at least {MIN_PRICED_ITEMS}. The API format may have changed.")
    return data


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


def write_json_atomically(path, obj):
    """Write to a temporary file then rename, so bosses.json is never left half-written."""
    text = json.dumps(obj, indent=2, ensure_ascii=False)
    json.loads(text)  # round-trip check
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def run(force):
    data = json.loads(DATA.read_text(encoding="utf-8"))
    meta = data["meta"]
    last = meta.get("pricesUpdatedAt")
    log["previousPricesUpdatedAt"] = last

    if last and not force:
        age_h = (time.time() - datetime.fromisoformat(last).timestamp()) / 3600
        if age_h < MIN_HOURS:
            # Not a refresh: leave the previous log (and any error it records) in place.
            print(f"Skipped: prices were updated {age_h:.1f}h ago (minimum {MIN_HOURS}h). No requests made; log not changed.")
            return 0, False

    # 1) Name -> ID mapping (cached locally for a week; only cached once it has been validated)
    mapping = None
    if MAPPING_CACHE.exists() and (time.time() - MAPPING_CACHE.stat().st_mtime) < MAPPING_MAX_AGE_DAYS * 86400:
        try:
            mapping = validate_mapping(json.loads(MAPPING_CACHE.read_text(encoding="utf-8")))
            log["summary"]["mapping"] = "from local cache (no request)"
        except Exception:
            log["problems"].pop() if log["problems"] else None
            mapping = None
    if mapping is None:
        print("Downloading item mapping (1 request)...")
        mapping = validate_mapping(get("mapping"))
        log["summary"]["mapping"] = "downloaded"
        fresh_mapping = True
    else:
        fresh_mapping = False

    # 2) All latest prices in one request
    print("Downloading latest prices (1 request)...")
    latest = validate_latest(get("latest"))

    # 3) Work out new prices on a copy; nothing is saved until every check has passed
    items = json.loads(json.dumps(data["items"]))
    by_name = {m["name"].lower(): m for m in mapping}
    tradeable = [n for n, it in items.items() if it.get("tradeable") and not it.get("derivedFrom")]
    matched = [n for n in tradeable if n.lower() in by_name]
    if tradeable and len(matched) / len(tradeable) < MIN_MATCHED_SHARE:
        raise problem(f"Only {len(matched)} of our {len(tradeable)} tradeable items were found in /mapping "
                      f"(expected at least {MIN_MATCHED_SHARE:.0%}). Item names may have changed.")

    updated, unchanged, missing, wild = [], [], [], []
    for name, item in items.items():
        if item.get("derivedFrom"):
            continue  # priced from other items below, never from the market
        m = by_name.get(name.lower())
        if not m:
            if item.get("tradeable"):
                missing.append(name)
            continue
        item["id"] = m["id"]
        p = latest.get(str(m["id"]))
        prices = [v for v in ((p or {}).get("high"), (p or {}).get("low")) if isinstance(v, (int, float)) and v > 0]
        if not prices:
            unchanged.append(name)
            continue
        new = round(sum(prices) / len(prices))
        old = item.get("price") or 0
        if new != old:
            updated.append({"item": name, "old": old, "new": new})
            if old and (new / old > WILD_CHANGE_FACTOR or old / new > WILD_CHANGE_FACTOR):
                wild.append({"item": name, "old": old, "new": new})
        item["price"] = new
        item["tradeable"] = True

    priced = len(matched) - len(unchanged)
    if priced and len(wild) / priced > MAX_WILD_CHANGE_SHARE:
        log["summary"]["wildChanges"] = wild
        raise problem(f"{len(wild)} of {priced} prices moved more than {WILD_CHANGE_FACTOR}x since the last update "
                      f"(limit {MAX_WILD_CHANGE_SHARE:.0%}). The data looks wrong, so it was not saved.")

    apply_derived(items)

    # 4) All checks passed: save
    data["items"] = items
    meta["pricesUpdated"] = datetime.now(timezone.utc).date().isoformat()
    meta["pricesUpdatedAt"] = now_iso()
    meta["priceSource"] = "prices.runescape.wiki /latest (average of instant buy and sell)"
    write_json_atomically(DATA, data)
    if fresh_mapping:  # only cache a mapping that produced a successful update
        MAPPING_CACHE.write_text(json.dumps(mapping), encoding="utf-8")

    log["bossesJsonChanged"] = True
    log["outcome"] = "updated"
    log["summary"].update({
        "itemsInFile": len(items),
        "pricesChanged": len(updated),
        "largeMoves": wild,
        "noRecentTrade": unchanged,
        "notOnGrandExchange": missing,
        "changes": updated,
    })
    print(f"Updated prices: {len(updated)} changed. Log written to {LOG.name}.")
    if missing:
        print("Not found in the Wiki mapping (kept old price):", ", ".join(missing))
    return 0, True


def main():
    force = "--force" in sys.argv
    log["runStartedAt"] = now_iso()
    try:
        code, contacted = run(force)
    except UpdateFailed as e:
        log["outcome"] = f"failed: {str(e).rstrip('.')}. bosses.json was not changed."
        write_log()
        print(log["outcome"], file=sys.stderr)
        return 1
    except Exception as e:
        # Anything unexpected (including a crash in this script): record it and leave bosses.json alone
        log["outcome"] = f"failed: unexpected error {type(e).__name__}: {str(e).rstrip('.')}. bosses.json was not changed."
        log["traceback"] = traceback.format_exc()
        write_log()
        print(log["outcome"], file=sys.stderr)
        return 1
    if contacted:
        write_log()
    return code


if __name__ == "__main__":
    sys.exit(main())
