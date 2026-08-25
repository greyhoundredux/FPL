import requests
import pandas as pd
from openpyxl import load_workbook
from openpyxl.styles import PatternFill
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

print("Initialising...")

# === Set your mini-league ID ===
league_id = '542663'

# === Shared session with retry/backoff ===
session = requests.Session()
retries = Retry(
    total=5,
    backoff_factor=0.5,
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=["GET"],
)
session.mount("https://", HTTPAdapter(max_retries=retries, pool_maxsize=20))

BASE = "https://fantasy.premierleague.com/api"


def get_json(url):
    resp = session.get(url, timeout=15)
    if resp.status_code != 200:
        return None
    return resp.json()


# === Get league standings ===
league_url = f"{BASE}/leagues-classic/{league_id}/standings/"
league_data = get_json(league_url)

entry_map = {}
for entry in league_data['standings']['results']:
    entry_map[entry['entry']] = {
        'manager_name': entry['player_name'],
        'team_name': entry['entry_name']
    }

entries = list(entry_map.keys())

# === Get player static data ===
bootstrap = get_json(f"{BASE}/bootstrap-static/")
elements = bootstrap['elements']
teams = bootstrap['teams']
positions = bootstrap['element_types']

id_to_name = {e['id']: e['web_name'] for e in elements}
id_to_team = {e['id']: teams[e['team'] - 1]['name'] for e in elements}
id_to_position = {e['id']: positions[e['element_type'] - 1]['singular_name'] for e in elements}

# === Caches shared across steps to avoid re-fetching the same data ===
picks_cache = {}   # (entry_id, gw) -> picks list or None
live_cache = {}     # gw -> {player_id: total_points}


def get_picks(entry_id, gw):
    key = (entry_id, gw)
    if key not in picks_cache:
        data = get_json(f"{BASE}/entry/{entry_id}/event/{gw}/picks/")
        picks_cache[key] = data['picks'] if data else None
    return picks_cache[key]


def get_live_points(gw):
    if gw not in live_cache:
        data = get_json(f"{BASE}/event/{gw}/live/")
        if data:
            live_cache[gw] = {p['id']: p['stats']['total_points'] for p in data['elements']}
        else:
            live_cache[gw] = None
    return live_cache[gw]


# === Step 1: Collect chip usage ===
# Every chip can now be used twice a season (once per half). Rather than
# hardcoding a gameweek cutoff, we sort each manager's usages of a given
# chip chronologically and label them "1st"/"2nd" in the order they
# actually happened - this works regardless of where the season splits fall.
CHIP_LABELS = {
    "wildcard": "Wildcard",
    "freehit": "Free Hit",
    "bboost": "Bench Boost",
    "3xc": "Triple Captain",
}

chip_data = []
free_hit_weeks_by_entry = {}      # entry_id -> [gw, ...]
triple_captain_weeks_by_entry = {}  # entry_id -> [gw, ...]

print("Fetching chip data...")
for entry_id in entries:
    history = get_json(f"{BASE}/entry/{entry_id}/history/")

    chip_dict = {
        'Manager Name': entry_map[entry_id]['manager_name'],
        'Team Name': entry_map[entry_id]['team_name'],
    }
    for label in CHIP_LABELS.values():
        chip_dict[f"{label} 1"] = '-'
        chip_dict[f"{label} 2"] = '-'
    chip_dict["Triple Captain 1 Player"] = '-'
    chip_dict["Triple Captain 1 Points"] = '-'
    chip_dict["Triple Captain 2 Player"] = '-'
    chip_dict["Triple Captain 2 Points"] = '-'

    # Group this entry's chip usages by chip name, in chronological order
    usages_by_name = {}
    if history:
        for chip in sorted(history.get("chips", []), key=lambda c: c['event']):
            usages_by_name.setdefault(chip['name'], []).append(chip['event'])

    free_hit_weeks = usages_by_name.get("freehit", [])
    triple_captain_weeks = usages_by_name.get("3xc", [])

    free_hit_weeks_by_entry[entry_id] = free_hit_weeks
    triple_captain_weeks_by_entry[entry_id] = triple_captain_weeks

    for name, label in CHIP_LABELS.items():
        weeks = usages_by_name.get(name, [])
        for i, gw in enumerate(weeks[:2]):  # only ever 2 uses per chip
            chip_dict[f"{label} {i + 1}"] = gw

    for i, gw in enumerate(triple_captain_weeks[:2]):
        picks = get_picks(entry_id, gw)
        player_stats = get_live_points(gw)

        if picks and player_stats:
            captain_id = next((p['element'] for p in picks if p['is_captain']), None)
            if captain_id:
                chip_dict[f"Triple Captain {i + 1} Player"] = id_to_name.get(captain_id, '-')
                chip_dict[f"Triple Captain {i + 1} Points"] = player_stats.get(captain_id, '-')

    chip_data.append(chip_dict)
print("✅")

df_chips = pd.DataFrame(chip_data)

# === Step 2: Captaincy data (reuses cached picks/live from Step 1 where available) ===
captaincy_data = []

for gw in range(1, 39):
    print(f"Fetching GW{gw} captain data...")

    player_points_map = get_live_points(gw)
    if player_points_map is None:
        print(f"⚠️ Skipping GW{gw} - live data unavailable.")
        continue

    for entry_id in entries:
        picks = get_picks(entry_id, gw)
        if not picks:
            continue

        captain_id = next((p['element'] for p in picks if p['is_captain']), None)

        if captain_id:
            manager_name = entry_map[entry_id]['manager_name']
            team_name = entry_map[entry_id]['team_name']
            player_name = id_to_name.get(captain_id, '-')
            player_points = player_points_map.get(captain_id, '-')
            tc_weeks = triple_captain_weeks_by_entry.get(entry_id, [])
            if gw in tc_weeks:
                triple_captain_used = "Yes"
                # which of the (up to 2) uses this was, e.g. "1st" or "2nd"
                triple_captain_instance = f"{tc_weeks.index(gw) + 1}"
            else:
                triple_captain_used = "No"
                triple_captain_instance = "-"

            captaincy_data.append({
                'Manager Name': manager_name,
                'Team Name': team_name,
                'Gameweek': gw,
                'Captain': player_name,
                'Captain Points': player_points,
                'Triple Captain Used': triple_captain_used,
                'Triple Captain Instance': triple_captain_instance
            })

print("✅")
df_captaincy = pd.DataFrame(captaincy_data)

# === Step 3: Collect transfer data ===
all_transfers = []

print("Fetching transfer data...")
for entry_id in entries:
    transfers = get_json(f"{BASE}/entry/{entry_id}/transfers/")

    if transfers:
        for t in transfers:
            if t['element_in'] not in id_to_name or t['element_out'] not in id_to_name:
                continue

            free_hit_used = "Yes" if t['event'] in free_hit_weeks_by_entry.get(entry_id, []) else "No"

            all_transfers.append({
                'Manager Name': entry_map[entry_id]['manager_name'],
                'Team Name': entry_map[entry_id]['team_name'],
                'Gameweek': t['event'],
                'Player Out': id_to_name[t['element_out']],
                'Out - Team': id_to_team[t['element_out']],
                'Out - Position': id_to_position[t['element_out']],
                'Player In': id_to_name[t['element_in']],
                'In - Team': id_to_team[t['element_in']],
                'In - Position': id_to_position[t['element_in']],
                'Free Hit Active': free_hit_used
            })
print("✅")

df_transfers = pd.DataFrame(all_transfers)

if not df_transfers.empty:
    df_transfers.sort_values(by=['Gameweek', 'Manager Name'], inplace=True)

# === Step 4: Write to Excel ===
print("Writing...")
file_name = "WWHALigaData.xlsx"

with pd.ExcelWriter(file_name, engine="openpyxl") as writer:
    df_transfers.to_excel(writer, sheet_name="Transfers", index=False)
    df_chips.to_excel(writer, sheet_name="Chip Usage", index=False)
    df_captaincy.to_excel(writer, sheet_name="Captaincy", index=False)

# === Step 5: Highlight TC usage in green ===
wb = load_workbook(file_name)
ws = wb["Captaincy"]
green_fill = PatternFill(start_color="C6EFCE", end_color="C6EFCE", fill_type="solid")

for row in ws.iter_rows(min_row=2, min_col=6, max_col=6):  # Triple Captain Used column
    for cell in row:
        if cell.value == "Yes":
            cell.fill = green_fill

wb.save(file_name)

print("✅ All done")
