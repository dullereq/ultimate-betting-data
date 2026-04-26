#!/usr/bin/env python3
"""Fetch public Superbet football prematch odds as JSON.

This file is intentionally standalone so it can be uploaded to ChatGPT on the
web and used in a code/agent environment without the local SZPONT plugin.

Examples:
  python3 superbet_odds_chatgpt_web.py --output superbet-live-board.json
  python3 superbet_odds_chatgpt_web.py --contains "Arsenal" --market "Mecz"
  python3 superbet_odds_chatgpt_web.py --start-date 2026-04-24 --end-date 2026-04-26
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, date, datetime, time as dtime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo


OFFER_HOST = "https://production-superbet-offer-pl.freetls.fastly.net"
LOCALE = "pl-PL"
FOOTBALL_SPORT_ID = 5
DEFAULT_TIMEZONE = "Europe/Warsaw"
USER_AGENT = "Mozilla/5.0 (compatible; ChatGPT Superbet public odds fetcher)"

TRANSLITERATION_TABLE = str.maketrans({"ł": "l", "ø": "o", "đ": "d", "ß": "ss", "æ": "ae"})


def normalize_text(value: str | None) -> str:
    normalized = unicodedata.normalize("NFKD", (value or "").casefold()).translate(TRANSLITERATION_TABLE)
    return "".join(char for char in normalized if not unicodedata.combining(char))


def parse_local_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def utc_param(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def resolve_window(start_date: str | None, end_date: str | None, timezone: str) -> tuple[date, date, str, str]:
    tz = ZoneInfo(timezone)
    start_day = parse_local_date(start_date) if start_date else datetime.now(tz).date()
    end_day = parse_local_date(end_date) if end_date else start_day
    if end_day < start_day:
        raise ValueError("--end-date must be the same as or later than --start-date")
    start_dt = datetime.combine(start_day, dtime.min, tzinfo=tz)
    end_dt = datetime.combine(end_day + timedelta(days=1), dtime.min, tzinfo=tz)
    return start_day, end_day, utc_param(start_dt), utc_param(end_dt)


def build_prematch_url(start_utc: str, end_utc: str) -> str:
    query = urllib.parse.urlencode(
        {"sports": FOOTBALL_SPORT_ID, "startDate": start_utc, "endDate": end_utc}
    )
    return f"{OFFER_HOST}/v3/subscription/{LOCALE}/prematch?{query}"


def build_event_url(event_ids: list[int]) -> str:
    query = urllib.parse.urlencode({"events": ",".join(str(event_id) for event_id in sorted(event_ids))})
    return f"{OFFER_HOST}/v3/subscription/{LOCALE}/events?{query}"


def request_sse_frame(url: str, *, timeout: float, retries: int) -> str:
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/event-stream"})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                frame: list[str] = []
                while True:
                    raw_line = response.readline()
                    if raw_line == b"":
                        break
                    line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
                    if line.startswith("data:"):
                        frame.append(line[5:].strip())
                    elif not line.strip() and frame:
                        return "\n".join(frame)
                if frame:
                    return "\n".join(frame)
        except (urllib.error.URLError, TimeoutError) as error:
            last_error = error
            if attempt < retries:
                time.sleep(0.8 * (attempt + 1))
    raise RuntimeError(f"request failed for {url}: {last_error}")


def parse_sse_json(frame: str) -> list[dict]:
    """Decode one Superbet Server-Sent Events JSON frame."""
    decoded = json.loads(frame)
    if not isinstance(decoded, list):
        raise RuntimeError("Expected Superbet SSE data frame to decode into a JSON list")
    return decoded


def fetch_sse_json(url: str, *, timeout: float, retries: int) -> list[dict]:
    return parse_sse_json(request_sse_frame(url, timeout=timeout, retries=retries))


def split_teams(name: str | None) -> tuple[str, str]:
    text = name or ""
    if "·" in text:
        home, away = text.split("·", 1)
        return home.strip(), away.strip()
    return text.strip(), ""


def event_code(raw_code: object) -> str:
    return str(raw_code or "").split(".", 1)[0]


def split_tags(tags: str | list[str] | None) -> list[str]:
    if not tags:
        return []
    if isinstance(tags, list):
        return [str(item).strip() for item in tags if str(item).strip()]
    return [part for part in (item.strip() for item in str(tags).split(",")) if part]


def matches_market_filter(market: dict, requested: list[str]) -> bool:
    if not requested:
        return True
    market_id = str(market.get("id", ""))
    market_name = normalize_text(str(market.get("name", "")))
    aliases = {
        "mecz": ("mecz",),
        "liczba-goli": ("liczba goli",),
        "obie-strzela": ("obie druzyny strzela",),
    }
    for item in requested:
        token = item.strip()
        if not token:
            continue
        if token.isdigit() and token == market_id:
            return True
        normalized = normalize_text(token)
        if any(normalize_text(alias) in market_name for alias in aliases.get(normalized, (token,))):
            return True
    return False


def normalize_odd(odd: dict) -> dict:
    metadata = odd.get("metadata") or {}
    return {
        "uuid": odd.get("uuid"),
        "price": odd.get("price"),
        "status": odd.get("status"),
        "display": odd.get("display"),
        "outcome_name": metadata.get("name"),
        "code": metadata.get("code"),
        "info": metadata.get("info"),
        "market_line_code": metadata.get("market_line_code"),
        "market_line_uuid": metadata.get("market_line_uuid"),
        "offer_state_id": metadata.get("offer_state_id"),
        "super_advantage_eligible": metadata.get("super_advantage_eligible"),
        "tags": split_tags(metadata.get("tags")),
        "extra": metadata.get("extra"),
    }


def normalize_market(market: dict) -> dict:
    metadata = market.get("metadata") or {}
    odds = [normalize_odd(odd) for odd in market.get("odds") or []]
    return {
        "id": market.get("id"),
        "name": market.get("name"),
        "market_group_order": metadata.get("market_group_order"),
        "tags": split_tags(metadata.get("tags")),
        "odd_count": len(odds),
        "odds": odds,
    }


def filter_snapshot_events(
    rows: list[dict],
    *,
    contains: list[str],
    competition: list[str],
    event_codes: set[str],
) -> tuple[list[dict], list[str]]:
    warnings: list[str] = []
    team_filters = [normalize_text(item) for item in contains if item.strip()]
    numeric_competitions = {item.strip() for item in competition if item.strip().isdigit()}
    if any(item.strip() and not item.strip().isdigit() for item in competition):
        warnings.append("Competition filters must be numeric category/tournament ids; non-numeric filters were ignored.")

    filtered: list[dict] = []
    for row in rows:
        fixture = row.get("fixture") or {}
        teams = str(fixture.get("event_name") or "")
        code = event_code(fixture.get("event_code"))
        if team_filters and not any(token in normalize_text(teams) for token in team_filters):
            continue
        if event_codes and code not in event_codes:
            continue
        if numeric_competitions:
            tournament_id = str(fixture.get("tournament_id"))
            category_id = str(fixture.get("category_id"))
            if tournament_id not in numeric_competitions and category_id not in numeric_competitions:
                continue
        filtered.append(row)
    return filtered, warnings


def parse_board(rows: list[dict], *, markets: list[str]) -> list[dict]:
    events: list[dict] = []
    for row in rows:
        fixture = row.get("fixture") or {}
        teams = str(fixture.get("event_name") or "")
        normalized_markets = [
            normalize_market(market)
            for market in row.get("markets") or []
            if matches_market_filter(market, markets)
        ]
        if markets and not normalized_markets:
            continue
        home_team, away_team = split_teams(teams)
        stats = row.get("inplay_stats_metadata") or {}
        counts = stats.get("counts") or {}
        events.append(
            {
                "event_id": row.get("event_id"),
                "increment_id": row.get("increment_id"),
                "event_code": event_code(fixture.get("event_code")),
                "teams": teams,
                "home_team": home_team,
                "away_team": away_team,
                "starts_at_local": fixture.get("event_date"),
                "starts_at_utc": fixture.get("utc_date"),
                "betradar_id": fixture.get("betradar_id"),
                "sport_id": fixture.get("sport_id"),
                "category_id": fixture.get("category_id"),
                "tournament_id": fixture.get("tournament_id"),
                "event_tags": split_tags(fixture.get("event_tags")),
                "streams": fixture.get("streams") or {},
                "super_advantage": fixture.get("super_advantage"),
                "market_count_total": stats.get("market_count"),
                "prematch_market_count_total": (counts.get("markets") or {}).get("1"),
                "prematch_odd_count_total": (counts.get("odds") or {}).get("1"),
                "returned_market_count": len(normalized_markets),
                "is_full_market_update": row.get("is_full_market_update"),
                "markets": normalized_markets,
            }
        )
    events.sort(key=lambda item: (item.get("starts_at_utc") or "", item.get("teams") or ""))
    return events


def hydrate_events(event_ids: list[int], *, batch_size: int, timeout: float, retries: int) -> tuple[list[dict], list[str]]:
    warnings: list[str] = []
    batches = [event_ids[index : index + batch_size] for index in range(0, len(event_ids), batch_size)]

    def fetch_batch(batch: list[int]) -> tuple[list[dict], str | None]:
        try:
            return fetch_sse_json(build_event_url(batch), timeout=timeout, retries=retries), None
        except Exception as error:
            return [], f"{len(batch)} event(s) ({','.join(str(item) for item in batch)}): {error}"

    hydrated: list[dict] = []
    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = {executor.submit(fetch_batch, batch): batch for batch in batches}
        for future in as_completed(futures):
            rows, warning = future.result()
            hydrated.extend(rows)
            if warning:
                warnings.append(warning)
    return hydrated, warnings


def build_payload(args: argparse.Namespace) -> dict:
    start_day, end_day, start_utc, end_utc = resolve_window(args.start_date, args.end_date, args.timezone)
    source_url = build_prematch_url(start_utc, end_utc)
    snapshot = fetch_sse_json(source_url, timeout=args.timeout, retries=args.retries)
    filtered_snapshot, filter_warnings = filter_snapshot_events(
        snapshot,
        contains=args.contains,
        competition=args.competition,
        event_codes={event_code(item) for item in args.event_code if item.strip()},
    )

    event_ids = [int(row["event_id"]) for row in filtered_snapshot if str(row.get("event_id", "")).isdigit()]
    hydrated, hydration_warnings = hydrate_events(
        event_ids,
        batch_size=args.batch_size,
        timeout=args.timeout,
        retries=args.retries,
    )
    if hydration_warnings:
        hydrated_ids = {row.get("event_id") for row in hydrated}
        hydrated.extend(row for row in filtered_snapshot if row.get("event_id") not in hydrated_ids)

    events = parse_board(hydrated, markets=args.market)
    return {
        "source": source_url,
        "source_kind": "superbet-live-sse",
        "requested_on": date.today().isoformat(),
        "generated_at_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "locale": LOCALE,
        "sport_id": FOOTBALL_SPORT_ID,
        "requested_date_range_local": {"start_date": start_day.isoformat(), "end_date": end_day.isoformat()},
        "requested_date_range_utc": {"start": start_utc, "end": end_utc},
        "filters": {
            "contains": args.contains,
            "market": args.market,
            "competition": args.competition,
            "event_code": args.event_code,
        },
        "total_events_in_snapshot": len(snapshot),
        "event_count": len(events),
        "events": events,
        "warnings": [
            *filter_warnings,
            *[f"Full-market hydration skipped: {item}" for item in hydration_warnings[:8]],
            "Only public prematch Superbet football markets are ingested.",
            "Competition names are not exposed by the anonymous live feed.",
        ],
    }


def flatten_payload(payload: dict) -> dict:
    rows: list[dict] = []
    for event in payload.get("events") or []:
        for market in event.get("markets") or []:
            for odd in market.get("odds") or []:
                rows.append(
                    {
                        "event_id": event.get("event_id"),
                        "event_code": event.get("event_code"),
                        "teams": event.get("teams"),
                        "home_team": event.get("home_team"),
                        "away_team": event.get("away_team"),
                        "starts_at_local": event.get("starts_at_local"),
                        "starts_at_utc": event.get("starts_at_utc"),
                        "category_id": event.get("category_id"),
                        "tournament_id": event.get("tournament_id"),
                        "market_id": market.get("id"),
                        "market_name": market.get("name"),
                        "market_tags": market.get("tags") or [],
                        "selection_name": odd.get("outcome_name"),
                        "selection_code": odd.get("code"),
                        "price": odd.get("price"),
                        "status": odd.get("status"),
                        "display": odd.get("display"),
                        "market_line_code": odd.get("market_line_code"),
                        "market_line_uuid": odd.get("market_line_uuid"),
                        "super_advantage_eligible": odd.get("super_advantage_eligible"),
                        "selection_tags": odd.get("tags") or [],
                    }
                )
    return {
        "source": payload.get("source"),
        "source_kind": payload.get("source_kind"),
        "generated_at_utc": payload.get("generated_at_utc"),
        "requested_date_range_local": payload.get("requested_date_range_local"),
        "filters": payload.get("filters"),
        "event_count": payload.get("event_count"),
        "selection_count": len(rows),
        "rows": rows,
        "warnings": payload.get("warnings") or [],
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch public Superbet football prematch odds as JSON.")
    parser.add_argument("--contains", action="append", default=[], help="Case-insensitive team-name filter.")
    parser.add_argument("--market", action="append", default=[], help="Market name/id filter, e.g. 'Mecz'.")
    parser.add_argument("--competition", action="append", default=[], help="Numeric category/tournament id filter.")
    parser.add_argument("--event-code", action="append", default=[], help="Specific Superbet event code.")
    parser.add_argument("--start-date", help="Local start date in YYYY-MM-DD. Defaults to today.")
    parser.add_argument("--end-date", help="Local end date in YYYY-MM-DD. Defaults to start date.")
    parser.add_argument("--timezone", default=DEFAULT_TIMEZONE, help="Timezone for date windows.")
    parser.add_argument("--output", help="Optional output JSON path. Defaults to stdout.")
    parser.add_argument("--flatten", action="store_true", help="Output one row per market selection.")
    parser.add_argument("--indent", type=int, default=2, help="JSON indentation. Use 0 for compact output.")
    parser.add_argument("--timeout", type=float, default=20.0, help="HTTP timeout per request.")
    parser.add_argument("--retries", type=int, default=2, help="HTTP retry count.")
    parser.add_argument("--batch-size", type=int, default=4, help="Event hydration batch size.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    payload = build_payload(args)
    if args.flatten:
        payload = flatten_payload(payload)
    indent = None if args.indent == 0 else args.indent
    text = json.dumps(payload, ensure_ascii=False, indent=indent)
    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(text + "\n", encoding="utf-8")
    else:
        print(text)
    return 0 if payload["event_count"] else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
