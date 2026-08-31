#!/usr/bin/env python3
"""
RalloHR automatic daily updater.

What it does:
1. Gets today's MLB schedule from the public MLB Stats API.
2. Reads probable pitchers and, when available, official batting orders.
3. Gets current-season hitter/pitcher stats.
4. Scores hitters who are actually scheduled to play.
5. Publishes exactly 20 ranked HR candidates when enough data exists.
6. Writes data/rallohr.json and updates the website's timestamp.

This is a research model, not a guarantee of a home run.
"""

from __future__ import annotations

import json
import math
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)
OUT = DATA_DIR / "rallohr.json"
TIMESTAMP = ROOT / "last-updated.txt"
INDEX = ROOT / "index.html"

MLB = "https://statsapi.mlb.com/api/v1"
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "RalloHR/1.0 (+GitHub Actions)"})


def get(path: str, params: dict | None = None) -> dict:
    r = SESSION.get(f"{MLB}{path}", params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def safe_num(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def today_ny() -> str:
    return datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")


def fetch_schedule(date: str):
    payload = get(
        "/schedule",
        {
            "sportId": 1,
            "date": date,
            "hydrate": "probablePitcher,team,venue",
        },
    )
    return payload.get("dates", [{}])[0].get("games", []) if payload.get("dates") else []


def fetch_live(game_pk: int):
    try:
        return get(f"/game/{game_pk}/feed/live")
    except Exception as exc:
        print(f"Live feed unavailable for {game_pk}: {exc}")
        return {}


def extract_lineup(feed: dict, side: str) -> list[dict]:
    team_box = feed.get("liveData", {}).get("boxscore", {}).get("teams", {}).get(side, {})
    batting_order = team_box.get("battingOrder", [])
    players = team_box.get("players", {})
    result = []

    for idx, pid in enumerate(batting_order, start=1):
        p = players.get(f"ID{pid}", {})
        person = p.get("person", {})
        stats = p.get("stats", {}).get("batting", {})
        result.append(
            {
                "id": pid,
                "name": person.get("fullName", "Unknown"),
                "bat_side": person.get("batSide", {}).get("code", ""),
                "position": idx,
                "home_runs": safe_num(stats.get("homeRuns")),
                "at_bats": safe_num(stats.get("atBats")),
            }
        )
    return result


def fetch_season_hitting(season: str) -> dict[int, dict]:
    # The MLB endpoint can return the season leaderboard for all MLB hitters.
    payload = get(
        "/stats",
        {
            "stats": "season",
            "group": "hitting",
            "season": season,
            "sportIds": 1,
            "limit": 1000,
            "sortStat": "homeRuns",
        },
    )
    out = {}
    for row in payload.get("stats", []):
        pid = row.get("player", {}).get("id")
        if pid:
            out[pid] = row
    return out


def fetch_pitcher_stats(pid: int, season: str) -> dict:
    try:
        payload = get(
            f"/people/{pid}/stats",
            {"stats": "season", "group": "pitching", "season": season},
        )
        return (payload.get("stats") or [{}])[0]
    except Exception:
        return {}


def player_details(pid: int) -> dict:
    try:
        return (get(f"/people/{pid}") .get("people") or [{}])[0]
    except Exception:
        return {}


def clamp(x, lo=0.0, hi=10.0):
    return max(lo, min(hi, x))


def score_hitter(hitter: dict, season: dict, pitcher: dict, lineup_spot: int) -> tuple[float, list[str], float]:
    hr = safe_num(season.get("homeRuns"))
    ab = safe_num(season.get("atBats"))
    avg = safe_num(season.get("avg"))
    slg = safe_num(season.get("slg"))
    ops = safe_num(season.get("ops"))

    hr_rate = (hr / ab) if ab else 0.0
    base = 4.0
    reasons = []

    # Power profile
    power = clamp((hr / 50.0) * 3.0 + (slg / 0.600) * 1.5, 0, 4.5)
    base += power
    reasons.append(f"{int(hr)} HR this season")

    # Platoon advantage
    batter_hand = hitter.get("bat_side", "")
    pitcher_hand = pitcher.get("pitch_hand", "")
    if batter_hand and pitcher_hand and batter_hand != "S":
        if batter_hand == "L" and pitcher_hand == "R":
            base += 0.7
            reasons.append("lefty-vs-righty platoon edge")
        elif batter_hand == "R" and pitcher_hand == "L":
            base += 0.7
            reasons.append("righty-vs-lefty platoon edge")
        else:
            base -= 0.25

    # Lineup position / expected plate appearances
    if lineup_spot <= 2:
        base += 0.8
        reasons.append(f"batting {lineup_spot}th")
    elif lineup_spot <= 4:
        base += 0.6
        reasons.append(f"batting {lineup_spot}th")
    elif lineup_spot <= 6:
        base += 0.3
    else:
        base -= 0.2

    # Opposing pitcher home-run vulnerability
    p_hr9 = safe_num(pitcher.get("homeRunsPer9"))
    p_era = safe_num(pitcher.get("era"))
    if p_hr9 >= 1.5:
        base += 0.9
        reasons.append(f"opposing pitcher HR/9 {p_hr9:.2f}")
    elif p_hr9 >= 1.2:
        base += 0.5
        reasons.append(f"opposing pitcher HR/9 {p_hr9:.2f}")
    if p_era >= 4.75:
        base += 0.4
    elif p_era >= 4.25:
        base += 0.2

    # Small bonus for rate production; avoid letting AVG dominate.
    if hr_rate >= 0.08:
        base += 0.5
    if ops >= 0.900:
        base += 0.4
    elif ops >= 0.800:
        base += 0.2

    # "Heat" proxy from season production/rates. The MLB Stats API response
    # does not always expose a uniform rolling-7-day field, so use HR rate,
    # SLG and OPS as a stable fallback rather than inventing recent stats.
    heat = clamp((hr_rate / 0.08) * 3.0 + (slg / 0.500) * 2.0 + (ops / 0.800) * 1.0, 0, 6)
    if heat >= 4:
        reasons.append("strong power/production profile")

    return round(clamp(base, 0, 10), 1), reasons, heat


def build_board():
    date = today_ny()
    season = date[:4]
    games = fetch_schedule(date)
    hitting = fetch_season_hitting(season)

    candidates = []
    for game in games:
        game_pk = game.get("gamePk")
        if not game_pk:
            continue

        away = game.get("teams", {}).get("away", {})
        home = game.get("teams", {}).get("home", {})
        away_team = away.get("team", {})
        home_team = home.get("team", {})

        away_pitcher = away.get("probablePitcher") or {}
        home_pitcher = home.get("probablePitcher") or {}

        feed = fetch_live(game_pk)
        away_lineup = extract_lineup(feed, "away")
        home_lineup = extract_lineup(feed, "home")

        # If official lineups are not posted yet, do not invent a batting order.
        # We fall back to the MLB season HR leaderboard only for players on teams
        # playing today, with a neutral lineup score.
        for side, lineup, opp_pitcher, team in [
            ("away", away_lineup, home_pitcher, away_team),
            ("home", home_lineup, away_pitcher, home_team),
        ]:
            opponent_pitcher_id = opp_pitcher.get("id")
            if not opponent_pitcher_id:
                continue

            p_details = player_details(opponent_pitcher_id)
            pitch_hand = p_details.get("pitchHand", {}).get("code", "")
            pstats = fetch_pitcher_stats(opponent_pitcher_id, season)
            pitcher = {
                "id": opponent_pitcher_id,
                "name": opp_pitcher.get("fullName", p_details.get("fullName", "TBD")),
                "pitch_hand": pitch_hand,
                **pstats,
            }

            team_id = team.get("id")
            game_label = f"{away_team.get('name', '')} at {home_team.get('name', '')}"

            if lineup:
                selected = lineup
            else:
                # No official lineup yet. Use top HR hitters associated with the
                # scheduled team; mark lineup status as provisional.
                selected = []
                for pid, row in hitting.items():
                    if row.get("team", {}).get("id") == team_id:
                        selected.append(
                            {
                                "id": pid,
                                "name": row.get("player", {}).get("fullName", "Unknown"),
                                "bat_side": "",
                                "position": 9,
                            }
                        )
                selected = sorted(
                    selected,
                    key=lambda x: safe_num(hitting.get(x["id"], {}).get("homeRuns")),
                    reverse=True,
                )[:12]

            for hitter in selected:
                pid = hitter["id"]
                season_row = hitting.get(pid, {})
                if not season_row:
                    continue

                score, reasons, heat = score_hitter(hitter, season_row, pitcher, hitter.get("position", 9))
                candidates.append(
                    {
                        "name": hitter["name"],
                        "team": team.get("abbreviation", team.get("nameShort", "")),
                        "opponent": opp_pitcher.get("fullName", "TBD"),
                        "opponentHand": pitch_hand or "TBD",
                        "batHand": hitter.get("bat_side", ""),
                        "lineupSpot": hitter.get("position", 0),
                        "score": score,
                        "heat": round(heat, 1),
                        "pick": "HOME RUN",
                        "reason": ". ".join(reasons) + ".",
                        "game": game_label,
                        "lineupConfirmed": bool(lineup),
                    }
                )

    # De-duplicate players, keeping the best matchup.
    best = {}
    for c in candidates:
        old = best.get(c["name"])
        if old is None or c["score"] > old["score"]:
            best[c["name"]] = c

    ranked = sorted(best.values(), key=lambda x: x["score"], reverse=True)[:20]
    for i, p in enumerate(ranked, start=1):
        p["rank"] = i

    # Underrated = not simply the highest raw score. Prefer players with a
    # strong matchup/heat signal whose overall score is below the obvious stars.
    # This keeps the section focused on overlooked upside.
    pool = [p for p in best.values() if p["score"] >= 6.0]
    if len(pool) < 8:
        pool = list(best.values())

    median_score = sorted([p["score"] for p in pool])[len(pool)//2] if pool else 0
    underrated = sorted(
        pool,
        key=lambda p: (
            (p["heat"] * 0.45)
            + (0.9 if ("platoon edge" in p["reason"]) else 0)
            + (0.8 if p["lineupSpot"] and p["lineupSpot"] <= 6 else 0)
            + max(0, median_score - p["score"]) * 0.35
        ),
        reverse=True,
    )

    # Avoid duplicating the very top obvious picks when possible.
    underrated = [p for p in underrated if p["rank"] > 5][:5]
    if len(underrated) < 5:
        existing = {p["name"] for p in underrated}
        for p in underrated + ranked:
            if p["name"] not in existing and len(underrated) < 5:
                underrated.append(p)
                existing.add(p["name"])

    for i, p in enumerate(underrated, start=1):
        p = p  # keep object identity for JSON output

    lineup_confirmed = sum(1 for p in ranked if p["lineupConfirmed"])
    status = "Official lineups available" if lineup_confirmed >= 10 else "Provisional until official lineups post"

    payload = {
        "updatedAt": datetime.now(ZoneInfo("America/New_York")).isoformat(),
        "date": date,
        "status": status,
        "count": len(ranked),
        "bestPick": ranked[0] if ranked else None,
        "picks": ranked,
        "underrated": underrated,
        "method": [
            "Today's MLB schedule",
            "Probable pitcher and handedness",
            "Season home-run/power production",
            "Expected or official lineup position",
            "Opposing pitcher HR/9 and ERA when available",
            "Platoon matchup",
        ],
        "note": "Research tool only. Starting lineups and probable pitchers can change.",
    }
    return payload


def write_site(data: dict):
    OUT.write_text(json.dumps(data, indent=2), encoding="utf-8")
    TIMESTAMP.write_text(
        f"RalloHR updated: {data['updatedAt']}\n",
        encoding="utf-8",
    )

    # The page is data-driven; do not embed today's players into HTML.
    if not INDEX.exists():
        raise SystemExit("index.html is missing. Keep the RalloHR website index.html in the repo.")


if __name__ == "__main__":
    try:
        data = build_board()
        if not data["picks"]:
            raise RuntimeError("MLB data returned no usable candidates today.")
        write_site(data)
        print(f"Published {len(data['picks'])} RalloHR picks for {data['date']}.")
        print(f"#1: {data['bestPick']['name']} — {data['bestPick']['score']}/10")
    except Exception as exc:
        print(f"RalloHR updater failed: {exc}")
        raise
