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
PIPS_START_DATE = datetime(2025, 8, 15)
SPORTS_CONNECTIONS_START_DATE = datetime(2024, 9, 24)

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

for msg in messages:
    content = msg['content']
    author = msg['author']['id']
    if re.search(connections_search, content, re.IGNORECASE | re.DOTALL):
        results[author]['connections'] = get_connections_results(content)
    elif re.search(bandle_search, content, re.IGNORECASE):
        bandle_match = re.search(bandle_search, content, re.IGNORECASE)
        score = bandle_match.group(1)
        results[author]['bandle'] = 7 if score == 'X' else int(score)
    elif re.search(sports_search, content, re.IGNORECASE):    
        results[author]['sports'] = get_connections_results(content)

# Calculate total scores (lower is better for all these games)
standings = []
for user, games in results.items():
    total = sum(games.values())
    standings.append({
        'user': user,
        'total': total,
        'games': games
    })

# Sort by total score (ascending - lower is better)
standings.sort(key=lambda x: x['total'])

# Build scoreboard message
yesterday = (datetime.utcnow() - timedelta(days=1)).strftime('%B %d, %Y')
message = f"📊 **Daily Game Scoreboard** - {yesterday}\n\n"

medals = ['🥇', '🥈', '🥉']
for i, entry in enumerate(standings):
    medal = medals[i] if i < 3 else f"#{i+1}"
    user = entry['user']
    total = entry['total']
    games = entry['games']
    
    game_details = []
    if 'sports' in games:
        game_details.append(f"Sports: {games['sports']}")
    if 'connections' in games:
        game_details.append(f"Connections: {games['connections']}")
    if 'bandle' in games:
        game_details.append(f"Bandle: {games['bandle']}")
    if 'pips' in games:
        game_details.append(f"Pips: {games['pips']}")
    
    details = " | ".join(game_details)
    message += f"{medal} **<@{user}>** - {total} points\n   {details}\n\n"

"""Post message to Discord channel"""
headers = {
    'Authorization': f'Bot {DISCORD_BOT_TOKEN}',
    'Content-Type': 'application/json'
}

url = f'{DISCORD_API_BASE}/channels/{CHANNEL_ID}/messages'
payload = {'content': message, 'allowed_mentions': {'parse': ['users']}}

response = requests.post(url, headers=headers, json=payload)

if response.status_code != 200:
    print(f"Error posting message: {response.status_code}")
    print(response.text)