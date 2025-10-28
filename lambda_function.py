import json
import os
import requests
from datetime import datetime, timedelta
from collections import defaultdict
import re

DISCORD_API_BASE = 'https://discord.com/api/v10'
CONNECTIONS_LINK = 'https://www.nytimes.com/games/connections'
BANDLE_LINK = 'https://bandle.app/daily'
PIPS_LINK = 'https://www.nytimes.com/games/pips'
SPORTS_CONNECTIONS_LINK = 'https://www.nytimes.com/athletic/connections-sports-edition'
MAPTAP_LINK = 'https://maptap.gg'
GLOBLE_LINK = 'https://globle.org/'

DISCORD_BOT_TOKEN = os.getenv('DISCORD_BOT_TOKEN')
INPUT_CHANNEL_ID = os.getenv('INPUT_CHANNEL_ID')
OUTPUT_CHANNEL_ID = os.getenv('OUTPUT_CHANNEL_ID')
TEST_CHANNEL_ID = os.getenv('TEST_CHANNEL_ID')

CONNECTIONS_START_DATE = datetime(2023, 6, 12)
BANDLE_START_DATE = datetime(2022, 8, 18)
PIPS_START_DATE = datetime(2025, 8, 18)
SPORTS_CONNECTIONS_START_DATE = datetime(2024, 9, 24)
MAPTAP_START_DATE = datetime(2024, 6, 21)

bandle_total = 6
yesterday = datetime.now() - timedelta(days=1)
connections_puzzle_number = (yesterday - CONNECTIONS_START_DATE).days + 1
bandle_puzzle_number = (yesterday - BANDLE_START_DATE).days + 1
sports_puzzle_number = (yesterday - SPORTS_CONNECTIONS_START_DATE).days + 1
pips_puzzle_number = (yesterday - PIPS_START_DATE).days + 1
maptap_number = (yesterday - MAPTAP_START_DATE).days + 1
maptap_date = yesterday.strftime('%B %d')


def get_messages(channel_id):
    headers = {
        'Authorization': f'Bot {DISCORD_BOT_TOKEN}',
        'Content-Type': 'application/json'
    }

    url = f'{DISCORD_API_BASE}/channels/{channel_id}/messages?limit=100'
    response = requests.get(url, headers=headers)
    return response.json()


def get_connections_results(content):
    squares = re.findall(r'[🟨🟩🟦🟪🟡🟢🔵🟣]', content)
    if len(squares) % 4 == 0:
        rows = [squares[i:i+4] for i in range(0, len(squares), 4)]
        solved_groups = sum(1 for row in rows if len(set(row)) == 1)
        mistakes = len(rows) - solved_groups

        # Check for vertical connections (4 rows, all failed, but vertical match)
        # commented out, broken
        is_vertical = False
        if len(rows) == 4 and solved_groups == 0:
            # Check each column for all same emoji
            vert = set()
            for col in range(4):
                column = [rows[row][col] for row in range(4)]
                if len(set(column)) == 1:
                    vert.add(column[0])
                else:
                    break
            is_vertical = len(vert) == 4
        
        if is_vertical:
            return (-1, 0) 
        else:
            return (mistakes, solved_groups)
    return (69, 0)

def parse_game_results(messages):
    global bandle_total
    results = defaultdict(lambda: defaultdict(dict))

    connections_search = rf'Connections.*?Puzzle #{connections_puzzle_number}'
    bandle_search = rf'Bandle #{bandle_puzzle_number} (\d+|x)/(\d+)'
    sports_search = rf'Connections: Sports Edition\n Puzzle #{sports_puzzle_number}'
    pips_search = rf'Pips #{pips_puzzle_number} Hard'
    maptap_search = rf'www.MapTap.gg {maptap_date}'

    for msg in messages:
        content = msg['content']
        author = msg['author']['id']
        if re.search(connections_search, content, re.IGNORECASE | re.DOTALL):
            results['connections'][author] = get_connections_results(content)
        elif re.search(bandle_search, content, re.IGNORECASE):
            bandle_match = re.search(bandle_search, content, re.IGNORECASE)
            score = bandle_match.group(1)
            bandle_total = int(bandle_match.group(2))
            results['bandle'][author] = 7 if score == 'x' else int(score)
        elif re.search(sports_search, content, re.IGNORECASE):    
            results['sports'][author] = get_connections_results(content)
        elif re.search(pips_search, content, re.IGNORECASE):
            pips_match = re.search(r'(\d+):(\d+)', content, re.IGNORECASE)
            minutes = int(pips_match.group(1))
            seconds = int(pips_match.group(2))
            total_seconds = minutes * 60 + seconds
            results['pips'][author] = total_seconds
        elif re.search(maptap_search, content, re.IGNORECASE):
            score = (re.search(r'Final Score: (\d+)', content, re.IGNORECASE)).group(1)
            results['maptap'][author] = int(score)


    return results

def format_message(results):
    # Define games and their display info
    games = [
        ('bandle', '🎵', 'Bandle', 'guesses', bandle_total, bandle_puzzle_number, BANDLE_LINK),
        ('connections', '🔗', 'Connections', 'connections', 4, connections_puzzle_number, CONNECTIONS_LINK),
        ('maptap', '📍', 'MapTap', 'score', 0, maptap_number, MAPTAP_LINK),
        ('pips', '🎲', 'Pips', 'time', 0, pips_puzzle_number, PIPS_LINK),
        ('sports', '🏈', 'Sports Connections', 'connections', 4, sports_puzzle_number, SPORTS_CONNECTIONS_LINK)
    ]
    medals = ['👑', '🥈', '🥉']
    message = "🧮 **Daily Game Scoreboard**"
    if not results:
        message += "\n\nNo results found for yesterday!"
    else:
        message += f" - {yesterday.strftime('%B %d, %Y')}\n\n"
        # sort game order by number of players
        games.sort(key=lambda x: len(results.get(x[0], {})), reverse=True)
        for game_key, game_emoji, game_title, metric, total, puzzle, link in games:
            # Get players who played this game
            if game_key not in results or not results[game_key]:
                continue
            
            # Sort players by score
            # For connections: sort by (mistakes, -solved_groups) so fewer mistakes first, then more solved
            # For others: sort by score ascending (lower is better)
            if metric == 'connections':
                players = sorted(results[game_key].items(), key=lambda x: (x[1][0], -x[1][1]))
            elif metric == 'score':
                players = sorted(results[game_key].items(), key=lambda x: (-x[1]))
            else:
                players = sorted(results[game_key].items(), key=lambda x: x[1])
            
            message += f"**[{game_title}]({link}) {game_emoji} {f'#{puzzle}' if puzzle else ''}**\n"
            
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
                medal = f"{medals[rank - 1]} " if rank <= len(medals) else f""
                
                # Format score
                if metric == 'time':
                    minutes = current_score // 60
                    seconds = current_score % 60
                    score_str = f"{minutes}:{seconds:02d}"
                elif metric == 'connections':
                    mistakes, solved = current_score
                    if mistakes == -1:
                        score_str = "VERT 🧗"
                    elif mistakes == total:
                        score_str = f"{mistakes}/{total} mistakes ({solved} solved)"
                        if solved == 0:
                            medal = '💩'
                    else:
                        score_str = f"{mistakes}/{total} mistakes"
                elif metric == 'score':
                    score_str = f"{current_score}"
                else: #guesses
                    if current_score > total:
                        medal = '💩'
                        current_score = 'X'
                    score_str = f"{str(current_score)}/{total} {metric}"
                
                # Join tied players
                players_str = " ".join(tied_players)
                message += f"{medal}{players_str}: {score_str}\n"
                
                prev_score = current_score
                i = j
            
            message += "\n"
    return message

def send_message(channel_id, message):
    headers = {
        'Authorization': f'Bot {DISCORD_BOT_TOKEN}',
        'Content-Type': 'application/json'
    }

    url = f'{DISCORD_API_BASE}/channels/{channel_id}/messages'
    # send messages, allow user mentions, disallow embeds
    payload = {'content': message, 'allowed_mentions': {'parse': ['users']}, 'flags': 4}

    response = requests.post(url, headers=headers, json=payload)

    return response

def lambda_handler(event, context):
    # Get messages from the channel
    messages = get_messages(INPUT_CHANNEL_ID)

    results = parse_game_results(messages)

    output = format_message(results)

    if 'test' in event:
        send_message(TEST_CHANNEL_ID, output)
        response = f'TEST Scoreboard posted'
    else:
        send_message(OUTPUT_CHANNEL_ID, output)
        response = f'Scorboard posted'

    return {
        'statusCode': 200,
        'body': json.dumps(response)
    }

if __name__ == '__main__':
    print(lambda_handler('', ''))