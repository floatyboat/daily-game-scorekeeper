import json
import os
import requests
from datetime import datetime, timedelta
from collections import defaultdict
import re

DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
DISCORD_API_BASE = 'https://discord.com/api/v10'
CHANNEL_ID = os.getenv('CHANNEL_ID')
CONNECTIONS_START_DATE = datetime(2023, 6, 12)
BANDLE_START_DATE = datetime(2022, 8, 18)
PIPS_START_DATE = datetime(2025, 8, 18)
SPORTS_CONNECTIONS_START_DATE = datetime(2024, 9, 24)

# Define games and their display info
games = [
    ('connections', '🔗 Connections', 'mistakes'),
    ('bandle', '🎵 Bandle', 'guesses'),
    ('pips', '🎯 Pips', 'time'),
    ('sports_connections', '⚽ Sports Connections', 'mistakes'),
]
medals = ['🥇', '🥈', '🥉']

def get_connections_results(content):
    squares = re.findall(r'[🟨🟩🟦🟪🟡🟢🔵🟣]', content)
    if len(squares) % 4 == 0:
        rows = [squares[i:i+4] for i in range(0, len(squares), 4)]
        
        # Count rows where all 4 emojis are the same (solved groups)
        solved_groups = sum(1 for row in rows if len(set(row)) == 1)
        
        # Mistakes = total rows - solved groups
        mistakes = len(rows) - solved_groups
        return mistakes
    return 69

yesterday = datetime.now() - timedelta(days=1)
connections_puzzle_number = (yesterday - CONNECTIONS_START_DATE).days + 1
bandle_puzzle_number = (yesterday - BANDLE_START_DATE).days + 1
sports_puzzle_number = (yesterday - SPORTS_CONNECTIONS_START_DATE).days + 1
pips_puzzle_number = (yesterday - PIPS_START_DATE).days + 1

headers = {
        'Authorization': f'Bot {DISCORD_BOT_TOKEN}',
        'Content-Type': 'application/json'
    }

url = f'{DISCORD_API_BASE}/channels/{CHANNEL_ID}/messages?limit=100'
response = requests.get(url, headers=headers)
messages = response.json()
recent_messages = []
for msg in messages:
    msg_time = datetime.fromisoformat(msg['timestamp'].replace('Z', '+00:00'))
    if msg_time.replace(tzinfo=None) >= yesterday:
        recent_messages.append(msg)

results = defaultdict(lambda: defaultdict(dict))

connections_search = rf'Connections.*?Puzzle #{connections_puzzle_number}'
bandle_search = rf'Bandle #{bandle_puzzle_number} (\d+|X)/6'
sports_search = rf'Connections: Sports Edition\n Puzzle #{sports_puzzle_number}'
pips_search = rf'Pips #{pips_puzzle_number} Hard'

for msg in messages:
    content = msg['content']
    author = msg['author']['id']
    if re.search(connections_search, content, re.IGNORECASE | re.DOTALL):
        results['connections'][author] = get_connections_results(content)
    elif re.search(bandle_search, content, re.IGNORECASE):
        bandle_match = re.search(bandle_search, content, re.IGNORECASE)
        score = bandle_match.group(1)
        results['bandle'][author] = 7 if score == 'X' else int(score)
    elif re.search(sports_search, content, re.IGNORECASE):    
        results['sports'][author] = get_connections_results(content)
    elif re.search(pips_search, content, re.IGNORECASE):
        pips_match = re.search(r'(\d+):(\d+)', content, re.IGNORECASE)
        minutes = int(pips_match.group(1))
        seconds = int(pips_match.group(2))
        total_seconds = minutes * 60 + seconds
        results['pips'][author] = total_seconds

if not results:
    message = "📊 **Daily Game Scoreboard**\n\nNo results found for yesterday!"
else:
    yesterday = (datetime.utcnow() - timedelta(days=1)).strftime('%B %d, %Y')
    message = f"📊 **Daily Game Scoreboard** - {yesterday}\n\n"

    for game_key, game_title, metric in games:
        # Get players who played this game
        if game_key not in results or not results[game_key]:
            continue
        
        # Sort players by score (ascending - lower is better)
        players = sorted(results[game_key].items(), key=lambda x: x[1])
        
        message += f"**{game_title}**\n"
        
        # Group players by score for ties
        rank = 0
        prev_score = None
        i = 0
        
        while i < len(players):
            current_score = players[i][1]
            
            # If score changed, update rank
            if current_score != prev_score:
                rank = i + 1
            
            # Find all players with this score
            tied_players = [f'<@{players[i][0]}>']
            j = i + 1
            while j < len(players) and players[j][1] == current_score:
                tied_players.append(f'<@{players[j][0]}>')
                j += 1
            
            # Format medal/rank
            medal = medals[rank - 1] if rank <= 3 else f"  {rank}."
            
            # Format score
            if metric == 'time':
                minutes = current_score // 60
                seconds = current_score % 60
                score_str = f"{minutes}:{seconds:02d}"
            else:
                score_str = str(current_score)
            
            # Join tied players
            players_str = ", ".join(tied_players)
            message += f"{medal} {players_str}: {score_str}\n"
            
            prev_score = current_score
            i = j
        
        message += "\n"

url = f'{DISCORD_API_BASE}/channels/{CHANNEL_ID}/messages'
payload = {'content': message, 'allowed_mentions': {'parse': ['users']}}

response = requests.post(url, headers=headers, json=payload)

if response.status_code != 200:
    print(f"Error posting message: {response.status_code}")
    print(response.text)