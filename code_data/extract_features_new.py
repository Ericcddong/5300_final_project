import json
import os
import pandas as pd
from pathlib import Path

TEN_MIN_MS = 10 * 60 * 1000


def get_jungle_side(x: int, y: int) -> int:
    return 100 if (x + y) < 15000 else 200


def extract_features(filepath: str) -> dict:
    with open(filepath, 'r', encoding='utf-8') as f:
        data = json.load(f)

    frames = data['timeline']['info']['frames']
    players = data['players']
    stats_10min_raw = {s['participantId']: s for s in data['stats_10min']}

    participants = [p for p in players if p['participantId'] <= 10]
    if len(participants) != 10:
        raise ValueError(f"参与者数量异常: {len(participants)}人，跳过")

    pid_to_team     = {p['participantId']: p['teamId']              for p in participants}
    pid_to_champ    = {p['participantId']: p['championName']         for p in participants}
    pid_to_position = {p['participantId']: p.get('teamPosition', '') for p in participants}

    features = {
        'win':     int(data['win']),
        'matchId': data['matchId'],
    }

    # ══════════════════════════════════════════════
    # 1. 每分钟增量特征（金币/经验/补刀/承伤）— 前10分钟
    # ══════════════════════════════════════════════
    prev_frame = None
    deltas_by_pid = {
        pid: {'gold': [], 'xp': [], 'cs': [], 'damage_taken': []}
        for pid in pid_to_team
    }

    for frame in frames:
        if frame['timestamp'] > TEN_MIN_MS:
            break
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
                    dmg_now  = (pdata.get('damageStats', {}).get('totalDamageTaken')
                                or pdata.get('totalDamageTaken', 0))
                    dmg_prev = (prev_pf[pid_str].get('damageStats', {}).get('totalDamageTaken')
                                or prev_pf[pid_str].get('totalDamageTaken', 0))
                    deltas_by_pid[pid]['damage_taken'].append((dmg_now - dmg_prev) / dt)
        prev_frame = frame

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

    # ══════════════════════════════════════════════
    # 2. 队伍事件 + 野怪分布
    # ══════════════════════════════════════════════
    kills_by_pid  = {pid: 0 for pid in pid_to_team}
    deaths_by_pid = {pid: 0 for pid in pid_to_team}

    kills_100, kills_200 = 0, 0
    first_blood_team = None

    jungle_own_100   = 0
    jungle_enemy_100 = 0
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
                if event.get('killType') == 'KILL_FIRST_BLOOD':
                    killer_id = event.get('killerId', 0)
                    first_blood_team = pid_to_team.get(killer_id, 0)

            elif etype == 'ELITE_MONSTER_KILL':
                killer_id   = event.get('killerId', 0)
                killer_team = pid_to_team.get(killer_id, event.get('killerTeamId', 0))
                pos = event.get('position', {})
                x, y = pos.get('x', 7500), pos.get('y', 7500)
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

    # 队伍击杀汇总
    features['team100_kills_10min']  = sum(kills_by_pid[p] for p, t in pid_to_team.items() if t == 100)
    features['team200_kills_10min']  = sum(kills_by_pid[p] for p, t in pid_to_team.items() if t == 200)
    features['team100_deaths_10min'] = sum(deaths_by_pid[p] for p, t in pid_to_team.items() if t == 100)
    features['team200_deaths_10min'] = sum(deaths_by_pid[p] for p, t in pid_to_team.items() if t == 200)
    features['kill_diff_10min']      = features['team100_kills_10min'] - features['team200_kills_10min']
    features['kills_100']            = kills_100
    features['kills_200']            = kills_200
    features['kill_diff']            = kills_100 - kills_200
    features['first_blood_team100']  = int(first_blood_team == 100) if first_blood_team else 0

    # 野怪分布
    features['jungle_own_100']     = jungle_own_100
    features['jungle_enemy_100']   = jungle_enemy_100
    features['jungle_own_200']     = jungle_own_200
    features['jungle_enemy_200']   = jungle_enemy_200
    features['jungle_invade_diff'] = jungle_enemy_100 - jungle_enemy_200

    # ══════════════════════════════════════════════
    # 3. 第10分钟末金币/经验差
    # ══════════════════════════════════════════════
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
    rows = []
    errors = []
    path = Path(match_data_dir)
    files = list(path.glob('*.json'))
    print(f"找到 {len(files)} 个比赛文件")

    for i, fp in enumerate(files):
        try:
            row = extract_features(str(fp))
            rows.append(row)
            if (i + 1) % 100 == 0:
                print(f"已处理 {i+1}/{len(files)}")
        except Exception as e:
            errors.append(fp.name)
            print(f"✗ 跳过 {fp.name}: {e}")

    df = pd.DataFrame(rows)
    print(f"\n✓ 数据集构建完成: {len(df)} 场比赛, {len(df.columns)} 个特征")
    if errors:
        print(f"⚠ 跳过了 {len(errors)} 个文件: {', '.join(errors)}")
    return df


if __name__ == "__main__":
    base = os.path.dirname(os.path.abspath(__file__))
    data_dir = os.path.join(base, "match_data_emerald1")
    df = build_dataset(data_dir)

    output_path = os.path.join(base, "match_data_emerald1.csv")
    df.to_csv(output_path, index=False, encoding='utf-8-sig')
    print(f"✓ 特征文件已保存到: {output_path}")
    print(df.head())
