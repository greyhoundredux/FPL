import time
import sys
import math
import requests
import pandas as pd
from typing import Dict, List, Tuple
from tqdm import tqdm

# ========= Configuration =========
LEAGUE_ID = 542663  # your mini-league
USER_AGENT = "FPL-Data-Collector/1.0 (+https://example.com)"
REQUEST_DELAY = 0.35  # seconds between requests to be polite
INCLUDE_ONLY_FINALISED_GWS = True  # True = only GWs with data_checked==True
OUTPUT_CSV = f"fpl_league_{LEAGUE_ID}_picks_wide.csv"
# =================================


session = requests.Session()
session.headers.update({"User-Agent": USER_AGENT})


def get_json(url: str, params: dict | None = None, retries: int = 3, backoff: float = 0.8):
    """GET JSON with simple retry/backoff."""
    for attempt in range(1, retries + 1):
        try:
            resp = session.get(url, params=params, timeout=20)
            if resp.status_code == 200:
                return resp.json()
            else:
                # Handle rate limits or transient errors
                time.sleep(backoff * attempt)
        except requests.RequestException:
            time.sleep(backoff * attempt)
    resp.raise_for_status()


def get_bootstrap() -> dict:
    return get_json("https://fantasy.premierleague.com/api/bootstrap-static/")


def get_league_entries(league_id: int) -> List[dict]:
    """
    Paginate through classic league standings to get all entries.
    Returns list of dicts with keys like 'entry', 'entry_name', 'player_name'.
    """
    page = 1
    all_results = []
    while True:
        url = f"https://fantasy.premierleague.com/api/leagues-classic/{league_id}/standings/"
        data = get_json(url, params={"page_standings": page})
        if "standings" not in data or "results" not in data["standings"]:
            break
        results = data["standings"]["results"]
        if not results:
            break
        all_results.extend(results)
        has_next = data["standings"].get("has_next", False)
        if not has_next:
            break
        page += 1
        time.sleep(REQUEST_DELAY)
    return all_results


def get_entry_picks(entry_id: int, event_id: int) -> dict | None:
    """
    Returns picks JSON for a given entry and event, or None if not available.
    """
    url = f"https://fantasy.premierleague.com/api/entry/{entry_id}/event/{event_id}/picks/"
    try:
        data = get_json(url)
        return data
    except requests.HTTPError as e:
        # If GW not available or entry missing for this event
        return None


def build_element_maps(bootstrap: dict) -> Tuple[Dict[int, str], Dict[int, str], Dict[int, str]]:
    """
    From bootstrap-static:
    - element_to_name: {element_id -> "First Last"}
    - element_to_team_short: {element_id -> team short name}
    - element_to_position: {element_id -> 'GK'|'DEF'|'MID'|'FWD'}
    """
    elements = bootstrap["elements"]
    teams = {t["id"]: t for t in bootstrap["teams"]}

    type_map = {1: "GK", 2: "DEF", 3: "MID", 4: "FWD"}

    element_to_name = {}
    element_to_team_short = {}
    element_to_position = {}

    for e in elements:
        element_id = e["id"]
        name = f'{e["first_name"]} {e["second_name"]}'.strip()
        team_short = teams.get(e["team"], {}).get("short_name", "")
        pos = type_map.get(e["element_type"], "")
        element_to_name[element_id] = name
        element_to_team_short[element_id] = team_short
        element_to_position[element_id] = pos

    return element_to_name, element_to_team_short, element_to_position


def get_events_to_include(bootstrap: dict, include_only_finalised: bool = True) -> List[int]:
    """
    Return a list of event ids to include.
    If include_only_finalised=True, include events with data_checked==True.
    Otherwise include all events that exist (typically 1..38).
    """
    events = bootstrap["events"]
    if include_only_finalised:
        ids = [e["id"] for e in events if e.get("data_checked")]
    else:
        ids = [e["id"] for e in events]  # up to 38
    return sorted(ids)


def main():
    print(f"Fetching bootstrap-static …", flush=True)
    bootstrap = get_bootstrap()
    element_to_name, element_to_team_short, element_to_position = build_element_maps(bootstrap)

    print(f"Fetching league entries for league {LEAGUE_ID} …", flush=True)
    entries = get_league_entries(LEAGUE_ID)
    if not entries:
        print("No entries found. Is the league ID correct or league private?", file=sys.stderr)
        sys.exit(1)

    # Build a base index: one row per entry x roster_slot (1..15)
    base_rows = []
    for r in entries:
        entry_id = r["entry"]
        entry_name = r["entry_name"]
        manager_name = r["player_name"]
        for slot in range(1, 16):
            base_rows.append({
                "league_id": LEAGUE_ID,
                "entry_id": entry_id,
                "entry_name": entry_name,
                "manager_name": manager_name,
                "roster_slot": slot,   # FPL pick.position (1..15)
            })
    df = pd.DataFrame(base_rows)

    gw_ids = get_events_to_include(bootstrap, include_only_finalised=INCLUDE_ONLY_FINALISED_GWS)
    if not gw_ids:
        print("No gameweeks available (yet) based on current settings.", file=sys.stderr)
        sys.exit(0)

    print(f"Including gameweeks: {gw_ids}", flush=True)

    # For progress visibility
    total_calls = len(entries) * len(gw_ids)
    print(f"Fetching picks: {len(entries)} entries × {len(gw_ids)} GWs = ~{total_calls} calls", flush=True)

    # For each GW, build a temporary mapping: (entry_id, roster_slot) -> (Player, Team, Position)
    for gw in tqdm(gw_ids, desc="Gameweeks"):
        triplet_cols = (f"GW{gw}_Player", f"GW{gw}_Team", f"GW{gw}_Position")
        # Initialize empty columns
        for col in triplet_cols:
            df[col] = None

        for r in entries:
            entry_id = r["entry"]
            picks_json = get_entry_picks(entry_id, gw)
            time.sleep(REQUEST_DELAY)

            if not picks_json or "picks" not in picks_json:
                # Leave as None if no data for this entry & GW
                continue

            for pick in picks_json["picks"]:
                element_id = pick["element"]
                slot = pick["position"]  # 1..15 per FPL
                player = element_to_name.get(element_id, f"Element {element_id}")
                team = element_to_team_short.get(element_id, "")
                pos = element_to_position.get(element_id, "")

                mask = (df["entry_id"] == entry_id) & (df["roster_slot"] == slot)
                df.loc[mask, triplet_cols[0]] = player
                df.loc[mask, triplet_cols[1]] = team
                df.loc[mask, triplet_cols[2]] = pos

    # Sort and save
    df.sort_values(by=["entry_name", "manager_name", "roster_slot"], inplace=True, ignore_index=True)
    df.to_csv(OUTPUT_CSV, index=False)
    print(f"\n✅ Done. Saved to {OUTPUT_CSV}\n")
    print("Columns include identifiers plus, for each GW, the triplet: GWn_Player, GWn_Team, GWn_Position.")


if __name__ == "__main__":
    main()
