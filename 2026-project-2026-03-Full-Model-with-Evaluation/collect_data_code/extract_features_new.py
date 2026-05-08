import json
import os
import pandas as pd
from pathlib import Path

# 10 minutes expressed in milliseconds — used as the early-game cutoff throughout
TEN_MIN_MS = 10 * 60 * 1000


def get_jungle_side(x: int, y: int) -> int:
    # The map diagonal runs roughly along x+y=15000.
    # Camps below that line belong to blue side (100), above to red side (200).
    return 100 if (x + y) < 15000 else 200

def extract_features(filepath: str) -> dict:
    # Load the pre-processed match JSON produced by the crawler
    with open(filepath, 'r', encoding='utf-8') as f:
        data = json.load(f)

    frames = data['timeline']['info']['frames']
    players = data['players']
    # Build a participantId → stats dict for O(1) lookup later
    stats_10min_raw = {s['participantId']: s for s in data['stats_10min']}

    # Guard: a valid 5v5 match must have exactly 10 participants
    participants = [p for p in players if p['participantId'] <= 10]
    if len(participants) != 10:
        raise ValueError(f"Unexpected participant count: {len(participants)}, skipping")

    # Convenience lookup tables used throughout this function
    pid_to_team     = {p['participantId']: p['teamId']              for p in participants}
    pid_to_champ    = {p['participantId']: p['championName']         for p in participants}
    pid_to_position = {p['participantId']: p.get('teamPosition', '') for p in participants}

    # Start the feature dict with the match label and identifier
    features = {
        'win':     int(data['win']),
        'matchId': data['matchId'],
    }

    # Per-minute delta features (gold / xp / cs / damage taken) — first 10 minutes
    # We compute how much each resource changed between consecutive timeline frames,
    # then normalise by the time elapsed to get a per-minute rate.

    prev_frame = None
    # One list per resource per participant; we'll average them at the end
    deltas_by_pid = {
        pid: {'gold': [], 'xp': [], 'cs': [], 'damage_taken': []}
        for pid in pid_to_team
    }

    for frame in frames:
        if frame['timestamp'] > TEN_MIN_MS:
            break
        # The very first frame has no predecessor, so skip it and store as baseline
        if frame['timestamp'] < 60000:
            prev_frame = frame
            continue
        pf = frame['participantFrames']
        prev_pf = prev_frame['participantFrames'] if prev_frame else None

        for pid_str, pdata in pf.items():
            pid = int(pid_str)
            if pid not in pid_to_team:
                continue
            if prev_pf and pid_str in prev_pf:
                # dt is the gap between this frame and the previous one, in minutes
                dt = (frame['timestamp'] - prev_frame['timestamp']) / 60000
                if dt > 0:
                    deltas_by_pid[pid]['gold'].append(
                        (pdata['totalGold'] - prev_pf[pid_str]['totalGold']) / dt)
                    deltas_by_pid[pid]['xp'].append(
                        (pdata['xp'] - prev_pf[pid_str]['xp']) / dt)
                    deltas_by_pid[pid]['cs'].append(
                        (pdata['minionsKilled'] + pdata['jungleMinionsKilled']
                         - prev_pf[pid_str]['minionsKilled']
                         - prev_pf[pid_str]['jungleMinionsKilled']) / dt)
                    # Damage taken may be stored inside a nested 'damageStats' dict
                    # or as a top-level field depending on the API version; handle both
                    dmg_now  = (pdata.get('damageStats', {}).get('totalDamageTaken')
                                or pdata.get('totalDamageTaken', 0))
                    dmg_prev = (prev_pf[pid_str].get('damageStats', {}).get('totalDamageTaken')
                                or prev_pf[pid_str].get('totalDamageTaken', 0))
                    deltas_by_pid[pid]['damage_taken'].append((dmg_now - dmg_prev) / dt)
        prev_frame = frame

    # Average the per-minute deltas and combine with the raw 10-min snapshot values
    for pid, team in pid_to_team.items():
        d = deltas_by_pid.get(pid, {})
        s = stats_10min_raw.get(pid, {})
        features[f'p{pid}_team']              = team
        features[f'p{pid}_champ']             = pid_to_champ.get(pid, '')
        features[f'p{pid}_position']          = pid_to_position.get(pid, '')
        features[f'p{pid}_gold_per_min']      = sum(d['gold'])         / len(d['gold'])         if d['gold']         else 0
        features[f'p{pid}_xp_per_min']        = sum(d['xp'])           / len(d['xp'])           if d['xp']           else 0
        features[f'p{pid}_cs_per_min']        = sum(d['cs'])           / len(d['cs'])           if d['cs']           else 0
        features[f'p{pid}_dmg_taken_per_min'] = sum(d['damage_taken']) / len(d['damage_taken']) if d['damage_taken'] else 0
        features[f'p{pid}_gold_10min']        = s.get('gold', 0)
        features[f'p{pid}_cs_10min']          = s.get('cs', 0)
        features[f'p{pid}_xp_10min']          = s.get('xp', 0)
        features[f'p{pid}_kills_10min']       = s.get('kills', 0)
        features[f'p{pid}_deaths_10min']      = s.get('deaths', 0)
        features[f'p{pid}_assists_10min']     = s.get('assists', 0)
        features[f'p{pid}_damage_10min']      = s.get('damage_dealt', 0)

    # team-level events + jungle monster distribution
    # Walk the timeline events to count kills, deaths, first blood, and
    # elite monster kills split by own-jungle vs invaded-jungle.

    kills_by_pid  = {pid: 0 for pid in pid_to_team}
    deaths_by_pid = {pid: 0 for pid in pid_to_team}

    kills_100, kills_200 = 0, 0
    first_blood_team = None  # will be set to 100 or 200 when first blood fires

    jungle_own_100   = 0  # blue side kills a camp on their own side
    jungle_enemy_100 = 0  # blue side invades and kills a red-side camp
    jungle_own_200   = 0
    jungle_enemy_200 = 0

    for frame in frames:
        if frame['timestamp'] > TEN_MIN_MS:
            break
        for event in frame['events']:
            etype = event['type']

            if etype == 'CHAMPION_KILL':
                killer_id   = event.get('killerId', 0)
                victim_id   = event.get('victimId', 0)
                killer_team = pid_to_team.get(killer_id, 0)
                if killer_id in kills_by_pid:
                    kills_by_pid[killer_id] += 1
                if victim_id in deaths_by_pid:
                    deaths_by_pid[victim_id] += 1
                if killer_team == 100:
                    kills_100 += 1
                elif killer_team == 200:
                    kills_200 += 1

            elif etype == 'CHAMPION_SPECIAL_KILL':
                # KILL_FIRST_BLOOD fires once per game; record which team got it
                if event.get('killType') == 'KILL_FIRST_BLOOD':
                    killer_id = event.get('killerId', 0)
                    first_blood_team = pid_to_team.get(killer_id, 0)

            elif etype == 'ELITE_MONSTER_KILL':
                killer_id   = event.get('killerId', 0)
                # killerTeamId is used as fallback when the killer is not a champion (e.g. turret kill)
                killer_team = pid_to_team.get(killer_id, event.get('killerTeamId', 0))
                pos = event.get('position', {})
                x, y = pos.get('x', 7500), pos.get('y', 7500)
                # Compare the camp's map side to the killer's team side to detect invasions
                monster_side = get_jungle_side(x, y)
                if killer_team == 100:
                    if monster_side == 100:
                        jungle_own_100 += 1
                    else:
                        jungle_enemy_100 += 1
                elif killer_team == 200:
                    if monster_side == 200:
                        jungle_own_200 += 1
                    else:
                        jungle_enemy_200 += 1

    # Aggregate individual counts into team-level kill/death features
    features['team100_kills_10min']  = sum(kills_by_pid[p] for p, t in pid_to_team.items() if t == 100)
    features['team200_kills_10min']  = sum(kills_by_pid[p] for p, t in pid_to_team.items() if t == 200)
    features['team100_deaths_10min'] = sum(deaths_by_pid[p] for p, t in pid_to_team.items() if t == 100)
    features['team200_deaths_10min'] = sum(deaths_by_pid[p] for p, t in pid_to_team.items() if t == 200)
    features['kill_diff_10min']      = features['team100_kills_10min'] - features['team200_kills_10min']
    features['kills_100']            = kills_100
    features['kills_200']            = kills_200
    features['kill_diff']            = kills_100 - kills_200
    # Binary flag: 1 if blue side got first blood, 0 otherwise
    features['first_blood_team100']  = int(first_blood_team == 100) if first_blood_team else 0

    # Jungle control features: own camps secured vs. enemy camps invaded
    features['jungle_own_100']     = jungle_own_100
    features['jungle_enemy_100']   = jungle_enemy_100
    features['jungle_own_200']     = jungle_own_200
    features['jungle_enemy_200']   = jungle_enemy_200
    # Positive value means blue side invaded more; negative means red side invaded more
    features['jungle_invade_diff'] = jungle_enemy_100 - jungle_enemy_200

    # Team gold / XP totals and differential at the 10-minute mark 
    # Use the last frame that falls within the cutoff to get a clean end-of-window snapshot.

    last_frame_pf = None
    for frame in frames:
        if frame['timestamp'] <= TEN_MIN_MS:
            last_frame_pf = frame['participantFrames']
        else:
            break

    if last_frame_pf:
        gold_100, gold_200 = 0, 0
        xp_100, xp_200     = 0, 0
        for pid_str, pdata in last_frame_pf.items():
            pid  = int(pid_str)
            if pid not in pid_to_team:
                continue
            team = pid_to_team.get(pid, 0)
            if team == 100:
                gold_100 += pdata['totalGold']
                xp_100   += pdata['xp']
            elif team == 200:
                gold_200 += pdata['totalGold']
                xp_200   += pdata['xp']
        features['gold_diff_10min'] = gold_100 - gold_200
        features['xp_diff_10min']   = xp_100 - xp_200
    else:
        features['gold_diff_10min'] = 0
        features['xp_diff_10min']   = 0

    return features

def build_dataset(match_data_dir: str) -> pd.DataFrame:
    """
    Iterate over all JSON files in match_data_dir, extract features from each,
    and return the results as a single pandas DataFrame.
    Files that raise an exception are skipped and reported at the end.
    """
    rows = []
    errors = []
    path = Path(match_data_dir)
    files = list(path.glob('*.json'))
    print(f"Found {len(files)} match files")

    for i, fp in enumerate(files):
        try:
            row = extract_features(str(fp))
            rows.append(row)
            if (i + 1) % 100 == 0:
                print(f"Processed {i+1}/{len(files)}")
        except Exception as e:
            errors.append(fp.name)
            print(f"Skipping {fp.name}: {e}")

    df = pd.DataFrame(rows)
    print(f"\nDataset built: {len(df)} matches, {len(df.columns)} features")
    if errors:
        print(f"Skipped {len(errors)} file(s): {', '.join(errors)}")
    return df

if __name__ == "__main__":
    base = os.path.dirname(os.path.abspath(__file__))
    data_dir = os.path.join(base, "match_data_emerald1")
    df = build_dataset(data_dir)

    output_path = os.path.join(base, "match_data_emerald1.csv")
    df.to_csv(output_path, index=False, encoding='utf-8-sig')
    print(f"Feature file saved to: {output_path}")