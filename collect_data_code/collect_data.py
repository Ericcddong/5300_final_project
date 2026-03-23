import requests
import json
import os
import time


# API Configuration
# Riot API key (personal development key, expires periodically)
API_KEY = "RGAPI-cf9b42d4-c107-40b2-ae9e-0517d8a12429".strip()
# Regional route used for match/account endpoints (covers NA, BR, LAN, LAS)
REGION_ROUTE = "americas"
# Platform route used for summoner/league endpoints
PLATFORM_ROUTE = "na1"



# Fields to retain from each participant object in match data
# Covers identity, champion, team, KDA, economy, time, and objective stats
KEEP_FIELDS = {
    'participantId', 'puuid', 'riotIdGameName', 'riotIdTagline',
    'summonerId', 'summonerLevel', 'profileIcon',
    'championName', 'championId', 'teamId', 'win',
    'kills', 'deaths', 'assists',
    'teamPosition', 'individualPosition', 'role', 'lane',
    'goldEarned', 'goldSpent', 'champLevel', 'champExperience',
    'timePlayed', 'totalTimeSpentDead', 'longestTimeSpentLiving',
    'killingSprees', 'largestKillingSpree', 'largestMultiKill',
    'firstBloodKill', 'firstBloodAssist', 'firstTowerKill', 'firstTowerAssist',
    'turretKills', 'inhibitorKills', 'nexusKills',
    'gameEndedInSurrender', 'gameEndedInEarlySurrender', 'teamEarlySurrendered',
    'placement', 'playerSubteamId', 'subteamPlacement',
}

# Minimal fields used for end-of-game summary stats
FINAL_STAT_FIELDS = {
    'kills', 'deaths', 'assists',
    'turretKills', 'win',
    'championName', 'teamId', 'riotIdGameName',
}

# Only these timeline event types are kept to reduce payload size
KEEP_EVENT_TYPES = {
    'CHAMPION_KILL',
    'CHAMPION_SPECIAL_KILL',
    'ELITE_MONSTER_KILL',
    'BUILDING_KILL',
    'TURRET_PLATE_DESTROYED',
    'DRAGON_SOUL_GIVEN',
    'GAME_END',
}

# Cutoff timestamp for early-game analysis: 10 minutes in milliseconds
TEN_MIN_MS = 10 * 60 * 1000


def filter_participant(p: dict) -> dict:
    """Return a participant dict containing only the fields in KEEP_FIELDS."""
    return {k: v for k, v in p.items() if k in KEEP_FIELDS}


def extract_final_stats(participants: list) -> list:
    """Return a slimmed list of participant dicts with only FINAL_STAT_FIELDS."""
    return [{k: v for k, v in p.items() if k in FINAL_STAT_FIELDS}
            for p in participants]


def filter_timeline(timeline_data):
    """
    Trim timeline frames to the first 10 minutes and keep only relevant events.
    Modifies timeline_data in-place and returns it.
    """
    if not timeline_data or 'info' not in timeline_data:
        return timeline_data
    frames = timeline_data['info'].get('frames', [])
    filtered = []
    for frame in frames:
        # Stop processing frames beyond the 10-minute mark
        if frame['timestamp'] > TEN_MIN_MS:
            break
        # Drop any events not in the allowed set
        frame['events'] = [
            e for e in frame['events']
            if e['type'] in KEEP_EVENT_TYPES
        ]
        filtered.append(frame)
    timeline_data['info']['frames'] = filtered
    return timeline_data


def compute_10min_stats(timeline_data) -> dict:
    """
    Compute per-participant statistics at the 10-minute mark from timeline data.

    Returns a dict keyed by participantId (int) with the following fields:
      gold, kills, deaths, assists, cs, vision_score, xp, damage_dealt
    """
    if not timeline_data or 'info' not in timeline_data:
        return {}
    frames = timeline_data['info'].get('frames', [])

    # Find the last frame that falls within the first 10 minutes
    last_frame = None
    for frame in frames:
        if frame['timestamp'] <= TEN_MIN_MS:
            last_frame = frame
        else:
            break
    if not last_frame:
        return {}

    # Snapshot of each participant's state at ~10 minutes
    p_frames = last_frame.get('participantFrames', {})

    # Accumulate KDA counts from CHAMPION_KILL events up to 10 minutes
    kill_count = {}
    death_count = {}
    assist_count = {}
    for frame in frames:
        if frame['timestamp'] > TEN_MIN_MS:
            break
        for event in frame.get('events', []):
            if event['type'] == 'CHAMPION_KILL':
                killer = event.get('killerId', 0)
                victim = event.get('victimId', 0)
                assists = event.get('assistingParticipantIds', [])
                if killer:
                    kill_count[killer] = kill_count.get(killer, 0) + 1
                if victim:
                    death_count[victim] = death_count.get(victim, 0) + 1
                for a in assists:
                    assist_count[a] = assist_count.get(a, 0) + 1

    # Build the final stats dict for each participant
    stats_10min = {}
    for pid_str, pf in p_frames.items():
        pid = int(pid_str)
        # CS = lane minions + jungle monsters killed
        cs = pf.get('minionsKilled', 0) + pf.get('jungleMinionsKilled', 0)
        dmg_stats = pf.get('damageStats', {})
        stats_10min[pid] = {
            'gold': pf.get('totalGold', 0),
            'kills': kill_count.get(pid, 0),
            'deaths': death_count.get(pid, 0),
            'assists': assist_count.get(pid, 0),
            'cs': cs,
            'vision_score': pf.get('visionScore', 0),
            'xp': pf.get('xp', 0),
            'damage_dealt': dmg_stats.get('totalDamageDoneToChampions', 0),
        }
    return stats_10min


class RandomPlayerCrawler:
    def __init__(self, api_key: str, region: str, platform: str):
        self.api_key = api_key
        self.region = region
        self.platform = platform
        # Auth header required by every Riot API request
        self.headers = {"X-Riot-Token": api_key}
        # Running total of match files successfully saved
        self.match_count = 0

    def _get(self, url, params=None, retries=6):
        """
        Send a GET request with automatic retry logic.

        Handles:
          - Network errors: retry after 5 s
          - 429 Rate Limit: honour Retry-After header
          - 5xx Server errors: exponential back-off
        Returns the Response object on HTTP 200, otherwise None after exhausting retries.
        """
        for attempt in range(retries):
            try:
                res = requests.get(url, headers=self.headers, params=params, timeout=10)
            except requests.exceptions.RequestException as e:
                print(f"\n Network error: {e}, retrying in 5s...")
                time.sleep(5)
                continue
            if res.status_code == 200:
                return res
            elif res.status_code == 429:
                # Respect the server-specified back-off window
                retry_after = int(res.headers.get("Retry-After", 10))
                print(f"\n Rate limited, waiting {retry_after}s...")
                time.sleep(retry_after + 1)
            elif res.status_code in (500, 502, 503, 504):
                # Exponential back-off for transient server errors
                time.sleep(2 ** attempt)
            else:
                return res
        return None

    def get_ranked_players(self, page=1):
        """
        Fetch a page of Diamond I ranked solo/duo ladder entries.
        Additional rank tiers are commented out for easy switching.
        Returns a list of league entry dicts, or [] on failure.
        """
        # We collected data from five different levels.
        url = f"https://{self.platform}.api.riotgames.com/lol/league/v4/entries/RANKED_SOLO_5x5/DIAMOND/I"
        # url = f"https://{self.platform}.api.riotgames.com/lol/league/v4/entries/RANKED_SOLO_5x5/DIAMOND/II"
        # url = f"https://{self.platform}.api.riotgames.com/lol/league/v4/entries/RANKED_SOLO_5x5/DIAMOND/III"
        # url = f"https://{self.platform}.api.riotgames.com/lol/league/v4/entries/RANKED_SOLO_5x5/DIAMOND/IV"
        # url = f"https://{self.platform}.api.riotgames.com/lol/league/v4/entries/RANKED_SOLO_5x5/EMERALD/I"
        res = self._get(url, params={"page": page})
        return res.json() if res and res.status_code == 200 else []

    def get_summoner_name(self, puuid):
        """
        Resolve a PUUID to a Riot ID game name via the Account v1 API.
        Returns the gameName string, or None on failure.
        """
        url = f"https://{self.region}.api.riotgames.com/riot/account/v1/accounts/by-puuid/{puuid}"
        res = self._get(url)
        return res.json().get('gameName', 'Unknown') if res and res.status_code == 200 else None

    def get_match_ids(self, puuid, count=10):
        """
        Fetch the most recent match IDs for a given PUUID.
        Excludes matches that ended within the last 19 minutes (likely still in progress).
        Returns a list of match ID strings, or [] on failure.
        """
        url = f"https://{self.region}.api.riotgames.com/lol/match/v5/matches/by-puuid/{puuid}/ids"
        res = self._get(url, params={"start": 0, "count": count, "endTime": int(time.time()) - 19 * 60})
        return res.json() if res and res.status_code == 200 else []

    def get_match_data(self, match_id):
        """
        Fetch full match details (participants, teams, metadata) for a given match ID.
        Returns the match dict, or None on failure.
        """
        url = f"https://{self.region}.api.riotgames.com/lol/match/v5/matches/{match_id}"
        res = self._get(url)
        return res.json() if res and res.status_code == 200 else None

    def get_timeline(self, match_id):
        """
        Fetch the frame-by-frame timeline for a given match ID.
        Returns the timeline dict, or None on failure.
        """
        url = f"https://{self.region}.api.riotgames.com/lol/match/v5/matches/{match_id}/timeline"
        res = self._get(url)
        return res.json() if res and res.status_code == 200 else None

    def save_match(self, player_name, match_id, match_data, timeline_data):
        """
        Process and persist a single match to disk as a JSON file.

        The saved file contains:
          - matchId: unique match identifier
          - win: 1 if blue side (teamId 100) won, 0 otherwise
          - players: slim participant list (identity + position fields only)
          - stats_10min: per-participant early-game statistics
          - timeline: trimmed timeline (first 10 min, filtered events)

        Skips saving if the file already exists (deduplication).
        Saves to ~/Desktop/match_data/<match_id>.json
        """
        info = match_data.get('info', {}) if match_data else {}
        participants = info.get('participants', [])
        teams = info.get('teams', [])

        # Determine the winning side from teamId 100 (blue side)
        win = next((t['win'] for t in teams if t['teamId'] == 100), None)
        if win is None:
            return

        # Keep only essential identity/role fields per participant
        keep = {'participantId', 'championName', 'teamId', 'teamPosition', 'win'}
        slim_participants = [{k: v for k, v in p.items() if k in keep} for p in participants]

        # Compute 10-minute stats and enrich with champion/position/team labels
        stats_10min = compute_10min_stats(timeline_data)
        pid_to_champ = {p['participantId']: p.get('championName', '') for p in participants}
        pid_to_pos = {p['participantId']: p.get('teamPosition', '') for p in participants}
        pid_to_team = {p['participantId']: p.get('teamId', 0) for p in participants}

        stats_10min_labeled = []
        for pid, s in stats_10min.items():
            stats_10min_labeled.append({
                'participantId': pid,
                'championName': pid_to_champ.get(pid, ''),
                'teamPosition': pid_to_pos.get(pid, ''),
                'teamId': pid_to_team.get(pid, 0),
                **s
            })
        # Sort by participantId for consistent ordering
        stats_10min_labeled.sort(key=lambda x: x['participantId'])

        # Trim the timeline to 10 minutes and drop verbose damage sub-fields
        slim_timeline = filter_timeline(timeline_data)
        for frame in slim_timeline['info']['frames']:
            for event in frame.get('events', []):
                event.pop('victimDamageDealt', None)
                event.pop('victimDamageReceived', None)

        # Write to ~/Desktop/match_data/
        desktop = os.path.join(os.path.expanduser("~"), "Desktop", "match_data")
        os.makedirs(desktop, exist_ok=True)
        filepath = os.path.join(desktop, f"{match_id}.json")

        # Skip if already saved (avoids duplicate processing)
        if os.path.exists(filepath):
            return

        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump({
                "matchId": match_id,
                "win": int(win),
                "players": slim_participants,
                "stats_10min": stats_10min_labeled,
                "timeline": slim_timeline,
            }, f, separators=(',', ':'), ensure_ascii=False)
        self.match_count += 1

    def crawl_player(self, puuid, player_index, total):
        """
        Crawl up to 10 recent matches for a single player identified by PUUID.
        Logs progress to stdout and applies a short sleep between match requests
        to avoid hitting rate limits.
        """
        print(f"[{player_index}/{total}] Fetching player info...", end=" ", flush=True)
        player_name = self.get_summoner_name(puuid)
        if not player_name:
            print("Failed to get player name")
            return
        print(f"{player_name}...", end=" ", flush=True)

        match_ids = self.get_match_ids(puuid, count=10)
        if not match_ids:
            print("No match data")
            return

        saved = 0
        for match_id in match_ids:
            match_data = self.get_match_data(match_id)
            timeline_data = self.get_timeline(match_id)
            if match_data and timeline_data:
                self.save_match(player_name, match_id, match_data, timeline_data)
                saved += 1
            # Brief pause between requests to stay within rate limits
            time.sleep(0.3)

        print(f"✓ Saved {saved} matches (total: {self.match_count})")
        # Slightly longer pause between players to avoid sustained burst traffic
        time.sleep(1)

    def run(self):
        """
        Main entry point for the crawler.

        1. Paginate through the Diamond I ladder until all entries are fetched.
        2. Process the first 500 players, crawling up to 10 matches each.
        3. Print a final summary of total matches saved.
        """
        all_players = []
        page = 1

        # Collect all ladder pages until the API returns an empty response
        while True:
            players = self.get_ranked_players(page=page)
            if not players:
                break
            all_players.extend(players)
            page += 1
            time.sleep(0.5)

        print(f"Leaderboard: {len(all_players)} players, fetching top 500, 10 matches each\n")

        # Crawl the top 500 players from the collected ladder entries
        for idx, player in enumerate(all_players[:500], 1):
            puuid = player.get('puuid')
            if puuid:
                self.crawl_player(puuid, idx, 500)

        print(f"Done! Total saved: {self.match_count} matches")

if __name__ == "__main__":
    crawler = RandomPlayerCrawler(API_KEY, REGION_ROUTE, PLATFORM_ROUTE)
    crawler.run()