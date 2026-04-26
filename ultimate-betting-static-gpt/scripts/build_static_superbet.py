#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib.util
import json
import math
import shutil
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace


LOCAL_FETCHER = Path(__file__).resolve().with_name("superbet_odds_chatgpt_web.py")
WORKSPACE_FETCHER = Path(__file__).resolve().parents[2] / "superbet_odds_chatgpt_web.py"
DEFAULT_FETCHER = LOCAL_FETCHER if LOCAL_FETCHER.exists() else WORKSPACE_FETCHER
MAIN_MARKET_NAMES = {
    "mecz",
    "liczba goli",
    "obie druzyny strzela",
    "obie drużyny strzelą",
    "podwojna szansa",
    "podwójna szansa",
    "remis bez zakladu",
    "zaklad bez remisu",
    "zakład bez remisu",
}


def load_fetcher(path: Path):
    spec = importlib.util.spec_from_file_location("superbet_odds_chatgpt_web", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load fetcher from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def build_args(args: argparse.Namespace) -> SimpleNamespace:
    return SimpleNamespace(
        contains=[],
        market=[],
        competition=[],
        event_code=[],
        start_date=args.start_date,
        end_date=args.end_date,
        timezone=args.timezone,
        timeout=args.timeout,
        retries=args.retries,
        batch_size=args.batch_size,
    )


def active_selections(event: dict) -> list[dict]:
    rows: list[dict] = []
    for market in event.get("markets") or []:
        for odd in market.get("odds") or []:
            price = odd.get("price")
            if odd.get("status") != 1 or odd.get("display") is not True or not isinstance(price, (int, float)) or price <= 1:
                continue
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
                    "selection_name": odd.get("outcome_name"),
                    "selection_code": odd.get("code"),
                    "selection_info": odd.get("info"),
                    "price": price,
                    "market_line_code": odd.get("market_line_code"),
                    "market_line_uuid": odd.get("market_line_uuid"),
                    "market_tags": market.get("tags") or [],
                    "selection_tags": odd.get("tags") or [],
                }
            )
    return rows


def compact_event(event: dict) -> dict:
    rows = active_selections(event)
    return {
        "event_id": event.get("event_id"),
        "event_code": event.get("event_code"),
        "teams": event.get("teams"),
        "home_team": event.get("home_team"),
        "away_team": event.get("away_team"),
        "starts_at_local": event.get("starts_at_local"),
        "starts_at_utc": event.get("starts_at_utc"),
        "category_id": event.get("category_id"),
        "tournament_id": event.get("tournament_id"),
        "betradar_id": event.get("betradar_id"),
        "market_count_total": event.get("market_count_total"),
        "returned_market_count": event.get("returned_market_count"),
        "active_selection_count": len(rows),
    }


def normalize_market_name(name: str | None) -> str:
    value = (name or "").casefold()
    replacements = str.maketrans({"ł": "l", "ó": "o", "ą": "a", "ę": "e", "ż": "z", "ź": "z", "ć": "c", "ń": "n", "ś": "s"})
    return value.translate(replacements)


def main_market_rows(selections: list[dict]) -> list[dict]:
    rows = []
    for row in selections:
        market_name = normalize_market_name(row.get("market_name"))
        if market_name in MAIN_MARKET_NAMES:
            rows.append(row)
    return rows


def candidate_rows(selections: list[dict]) -> list[dict]:
    by_event_market: dict[tuple[object, object, object], list[dict]] = defaultdict(list)
    for row in main_market_rows(selections):
        key = (row.get("event_id"), row.get("market_id"), row.get("market_line_uuid"))
        by_event_market[key].append(row)

    candidates: list[dict] = []
    for rows in by_event_market.values():
        prices = [row.get("price") for row in rows if isinstance(row.get("price"), (int, float)) and row.get("price") > 1]
        if len(prices) < 2:
            continue
        implied_sum = sum(1 / price for price in prices)
        overround = implied_sum - 1
        for row in rows:
            price = row.get("price")
            implied = 1 / price
            no_vig = implied / implied_sum if implied_sum > 0 else None
            candidates.append(
                row
                | {
                    "implied_probability": round(implied, 6),
                    "market_overround": round(overround, 6),
                    "market_no_vig_probability": round(no_vig, 6) if no_vig is not None else None,
                    "candidate_reason": "main-market candidate; requires web research and fair-probability estimate before any bet",
                }
            )
    candidates.sort(key=lambda item: (item.get("starts_at_utc") or "", item.get("teams") or "", item.get("market_name") or ""))
    return candidates


def write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_shards(base: Path, rows: list[dict], shard_size: int) -> list[dict]:
    shard_dir = base / "latest" / "shards"
    shard_dir.mkdir(parents=True, exist_ok=True)
    manifest = []
    for index in range(math.ceil(len(rows) / shard_size)):
        chunk = rows[index * shard_size : (index + 1) * shard_size]
        name = f"selections-{index:03d}.json"
        write_json(shard_dir / name, {"shard": index, "selection_count": len(chunk), "selections": chunk})
        manifest.append({"shard": index, "path": f"latest/shards/{name}", "selection_count": len(chunk)})
    return manifest


def build_static(args: argparse.Namespace) -> None:
    fetcher = load_fetcher(args.fetcher)
    output = args.output.resolve()
    latest = output / "latest"
    if latest.exists():
        shutil.rmtree(latest)
    latest.mkdir(parents=True, exist_ok=True)

    payload = fetcher.build_payload(build_args(args))
    events = payload.get("events") or []
    event_index = [compact_event(event) for event in events]
    all_selections: list[dict] = []

    for event in events:
        selections = active_selections(event)
        all_selections.extend(selections)
        write_json(
            latest / "events" / f"{event.get('event_id')}.json",
            {
                "source_kind": payload.get("source_kind"),
                "generated_at_utc": payload.get("generated_at_utc"),
                "event": compact_event(event),
                "market_count": len(event.get("markets") or []),
                "active_selection_count": len(selections),
                "markets": event.get("markets") or [],
                "selections": selections,
            },
        )

    event_index.sort(key=lambda item: (item.get("starts_at_utc") or "", item.get("teams") or ""))
    all_selections.sort(key=lambda item: (item.get("starts_at_utc") or "", item.get("teams") or "", item.get("market_name") or ""))
    main_rows = main_market_rows(all_selections)
    candidates = candidate_rows(all_selections)
    shards = write_shards(output, all_selections, args.shard_size)

    manifest = {
        "ok": True,
        "architecture": "static-superbet-football-warehouse",
        "source": payload.get("source"),
        "source_kind": payload.get("source_kind"),
        "generated_at_utc": payload.get("generated_at_utc"),
        "built_at_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "requested_date_range_local": payload.get("requested_date_range_local"),
        "event_count": len(event_index),
        "active_selection_count": len(all_selections),
        "main_market_selection_count": len(main_rows),
        "candidate_count": len(candidates),
        "files": {
            "events_index": "latest/events-index.json",
            "main_markets": "latest/main-markets.json",
            "candidates": "latest/candidates.json",
            "shards_manifest": "latest/shards-manifest.json",
            "event_file_pattern": "latest/events/{event_id}.json",
        },
        "warnings": payload.get("warnings") or [],
    }

    write_json(latest / "manifest.json", manifest)
    write_json(latest / "events-index.json", {"events": event_index})
    write_json(latest / "main-markets.json", {"selection_count": len(main_rows), "selections": main_rows})
    write_json(latest / "candidates.json", {"candidate_count": len(candidates), "candidates": candidates[: args.max_candidates]})
    write_json(latest / "shards-manifest.json", {"selection_count": len(all_selections), "shard_size": args.shard_size, "shards": shards})
    write_json(output / "index.json", manifest)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fetcher", type=Path, default=DEFAULT_FETCHER)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--start-date")
    parser.add_argument("--end-date")
    parser.add_argument("--timezone", default="Europe/Warsaw")
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--batch-size", type=int, default=4)
    parser.add_argument("--shard-size", type=int, default=500)
    parser.add_argument("--max-candidates", type=int, default=2000)
    return parser.parse_args()


if __name__ == "__main__":
    build_static(parse_args())
