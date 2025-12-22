import json
import os
import requests
from datetime import datetime, timedelta, timezone
from dateutil import parser
from collections import defaultdict
import re

DISCORD_API_BASE = 'https://discord.com/api/v10'
CONNECTIONS_LINK = 'https://www.nytimes.com/games/connections'
BANDLE_LINK = 'https://bandle.app/daily'
PIPS_LINK = 'https://www.nytimes.com/games/pips'
SPORTS_CONNECTIONS_LINK = 'https://www.nytimes.com/athletic/connections-sports-edition'
MAPTAP_LINK = 'https://maptap.gg'
GLOBLE_LINK = 'https://globle.org'
FLAGLE_LINK = 'https://flagle.org'
WORLDLE_LINK = 'https://worldlegame.io'
WHEREDLE_LINK = 'https://wheredle.xyz'
QUIZL_LINK = 'https://quizl.io'
CHRONOPHOTO_LINK = 'https://www.chronophoto.app/daily.html'

DISCORD_BOT_ID = os.getenv('DISCORD_BOT_ID') or 0
DISCORD_BOT_TOKEN = os.getenv('DISCORD_BOT_TOKEN')
INPUT_CHANNEL_ID = os.getenv('INPUT_CHANNEL_ID')
OUTPUT_CHANNEL_ID = os.getenv('OUTPUT_CHANNEL_ID')
TEST_CHANNEL_ID = os.getenv('TEST_CHANNEL_ID')
HUNDREDS_OF_MESSAGES = int(os.getenv('HUNDREDS_OF_MESSAGES') or 1)
MINIMUM_PLAYERS = int(os.getenv('MINIMUM_PLAYERS') or 1)

CONNECTIONS_START_DATE = datetime(2023, 6, 12)
BANDLE_START_DATE = datetime(2022, 8, 18)
PIPS_START_DATE = datetime(2025, 8, 18)
SPORTS_CONNECTIONS_START_DATE = datetime(2024, 9, 24)
MAPTAP_START_DATE = datetime(2024, 6, 22)
QUIZL_START_DATE = datetime(2022, 3, 16)

bandle_total = 6
wheredle_total = 7
quizl_total = 5

# variables for games that don't include an identifier for each day
UTC_OFFSET = int(os.getenv('UTC_OFFSET') or 0)
TIME_WINDOW_HOURS = int(os.getenv('TIME_WINDOW_HOURS') or 24)
HOURS_AFTER_MIDNIGHT = int(os.getenv('HOURS_AFTER_MIDNIGHT') or 0)
MSG_TIMEZONE = timezone(timedelta(hours=UTC_OFFSET))
NOW_OFFSET = datetime.now(MSG_TIMEZONE)
YESTERDAY_START = (NOW_OFFSET - timedelta(days=1)).replace(
        hour=HOURS_AFTER_MIDNIGHT, minute=0, second=0, microsecond=0
    )
YESTERDAY_END = YESTERDAY_START + timedelta(hours=TIME_WINDOW_HOURS)

yesterday = datetime.now() - timedelta(days=1)
connections_puzzle_number = int((yesterday - CONNECTIONS_START_DATE).days + 1)
bandle_puzzle_number = int((yesterday - BANDLE_START_DATE).days + 1)
sports_puzzle_number = int((yesterday - SPORTS_CONNECTIONS_START_DATE).days + 1)
pips_puzzle_number = int((yesterday - PIPS_START_DATE).days + 1)
maptap_number = int((yesterday - MAPTAP_START_DATE).days + 1)
quizl_puzzle_number = int((yesterday - QUIZL_START_DATE).days + 1)
maptap_date = f'{yesterday.strftime('%B')} {yesterday.day}'
globle_number = f'{yesterday.strftime('%B')} {yesterday.day}'
worldle_number = f'{yesterday.strftime('%B')} {yesterday.day}'
flagle_number = f'{yesterday.strftime('%B')} {yesterday.day}'
wheredle_number = f'{yesterday.strftime('%B')} {yesterday.day}'
chronophoto_number = f'{yesterday.month}/{yesterday.day}/{yesterday.year}'

def get_messages(channel_id):
    headers = {
        'Authorization': f'Bot {DISCORD_BOT_TOKEN}',
        'Content-Type': 'application/json'
    }

    url = f'{DISCORD_API_BASE}/channels/{channel_id}/messages?limit=100'
    response = requests.get(url, headers=headers)
    messages = response.json()

    for x in range(HUNDREDS_OF_MESSAGES - 1):
        last_msg_id = messages[-1]['id']
        url_id = url + f'&before={last_msg_id}'
        response = requests.get(url_id, headers=headers)
        messages += response.json()
    return messages

def was_yesterday(iso_timestamp):
    try:
        timestamp = parser.isoparse(iso_timestamp)
    except (ValueError, TypeError) as e:
        raise ValueError(f"Invalid ISO8601 timestamp: {iso_timestamp}") from e

    timestamp_in_ref_tz = timestamp.astimezone(MSG_TIMEZONE)

    return YESTERDAY_START <= timestamp_in_ref_tz < YESTERDAY_END


def get_connections_results(content):
    squares = re.findall(r'[🟨🟩🟦🟪🟡🟢🔵🟣]', content)
    if len(squares) % 4 == 0:
        rows = [squares[i:i+4] for i in range(0, len(squares), 4)]
        solved_groups = sum(1 for row in rows if len(set(row)) == 1)
        mistakes = len(rows) - solved_groups

        # Check for vertical connections (4 rows, all failed, but vertical match)
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
    sports_search = rf'Connections: Sports Edition\nPuzzle #{sports_puzzle_number}'
    pips_search = rf'Pips #{pips_puzzle_number} Hard'
    maptap_search = rf'(.*)MapTap(.*){maptap_date}'
    globle_search = r'I guessed today’s Globle in (\d+) tr'
    worldle_search = r'I guessed today’s Worldle in (\d+) tr'
    flagle_search = r'I guessed today’s Flag in (\d+) tr'
    wheredle_search = r'#Wheredle'
    quizl_search = rf'Quizl#{quizl_puzzle_number}'
    chronophoto_search = rf"I got a score of (\d+) on today's Chronophoto: {chronophoto_number}"

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
        elif re.search(chronophoto_number, content, re.IGNORECASE):
            score = (re.search(chronophoto_search, content, re.IGNORECASE)).group(1)
            results['chronophoto'][author] = int(score)
        elif re.search(globle_search, content, re.IGNORECASE) and was_yesterday(msg['timestamp']): 
            score = re.search(globle_search, content, re.IGNORECASE).group(1)
            results['globle'][author] = int(score)
        elif re.search(worldle_search, content, re.IGNORECASE) and was_yesterday(msg['timestamp']):
            score = re.search(worldle_search, content, re.IGNORECASE).group(1)
            results['worldle'][author] = int(score)
        elif re.search(flagle_search, content, re.IGNORECASE) and was_yesterday(msg['timestamp']):
            score = re.search(flagle_search, content, re.IGNORECASE).group(1)
            results['flagle'][author] = int(score)
        elif re.search(wheredle_search, content, re.IGNORECASE) and was_yesterday(msg['timestamp']):
            yellow_squares = re.findall(r'🟨', content)
            green_squares = re.findall(r'🟩', content)
            if green_squares == 0:
                score = wheredle_total + 1
            else:
                score = len(yellow_squares) + len(green_squares)
            results['wheredle'][author] = score
        elif re.search(quizl_search, content, re.IGNORECASE):
            score = len(re.findall(r'🟩', content))
            results['quizl'][author] = score
    return results

def format_message(results):
    # Define games and their display info
    games = [
        ('bandle', '🎵', 'Bandle', 'guesses', bandle_total, bandle_puzzle_number, BANDLE_LINK),
        ('chronophoto', '📷', 'Chronophoto', 'score', 0, chronophoto_number, CHRONOPHOTO_LINK),
        ('connections', '🔗', 'Connections', 'connections', 4, connections_puzzle_number, CONNECTIONS_LINK),
        ('flagle', '🏁', 'Flagle', 'guesses', 0, f'{flagle_number}', FLAGLE_LINK),
        ('globle', '🌍', 'Globle', 'guesses', 0, f'{globle_number}', GLOBLE_LINK),
        ('maptap', '🎯', 'MapTap', 'score', 0, maptap_number, MAPTAP_LINK),
        ('pips', '🎲', 'Pips', 'time', 0, pips_puzzle_number, PIPS_LINK),
        ('quizl','⁉️', 'Quizl', 'score', quizl_total, quizl_puzzle_number, QUIZL_LINK),
        ('sports', '🏈', 'Sports Connections', 'connections', 4, sports_puzzle_number, SPORTS_CONNECTIONS_LINK),
        ('wheredle', '🛣️', 'Wheredle', 'guesses', wheredle_total, f'{wheredle_number}', WHEREDLE_LINK),
        ('worldle', '🗺️', 'Worldle', 'guesses', 0, f'{worldle_number}', WORLDLE_LINK)
    ]
    medals = ['👑', '🥈', '🥉']
    message = "🧮 **Daily Game Scoreboard**"
    no_players_reached = False
    one_player_reached = False
    if not results:
        message += "\n\nNo results found for yesterday!"
    else:
        message += f" - {yesterday.strftime('%B %d, %Y')}\n\n"
        # sort game order by number of players
        games.sort(key=lambda x: len(results.get(x[0], {})), reverse=True)
        for game_key, game_emoji, game_title, metric, total, puzzle, link in games:
            game_title = f"[{game_title}]({link})"

            if game_key not in results or not results[game_key] or len(results[game_key]) < MINIMUM_PLAYERS:
                if not no_players_reached:
                    if one_player_reached:
                        message += '\n'
                    message += f'-# Other games:\t'
                no_players_reached = True
                message += f'{game_emoji} {game_title}\t'
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
            
            message += f'**{game_title} {game_emoji} {f'#{puzzle}' if type(puzzle) == int else f'#67'}**\n'
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
                    score_str = f"{str(current_score)}"
                    if total > 0:
                        score_str = f"{score_str}/{total}"
                else: #guesses
                    if total == 0:
                        score_str = f"{str(current_score)} {metric}"
                    else:
                        if current_score > total:
                            medal = '💩'
                            current_score = 'X'
                        score_str = f"{str(current_score)}/{total} {metric}"
                
                # Join tied players
                players_str = " ".join(reversed(tied_players))
                message += f'{medal}'
                message += f"{players_str}: {score_str}\n"
                
                prev_score = current_score
                i = j
            
            if len(players) >= MINIMUM_PLAYERS:
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

    return response.json()

def pin_message(channel_id, message_id):
    headers = {
        'Authorization': f'Bot {DISCORD_BOT_TOKEN}',
        'Content-Type': 'application/json'
    }

    url = f'{DISCORD_API_BASE}/channels/{channel_id}/messages/pins/{message_id}'
    requests.put(url, headers=headers)

def lambda_handler(event, context):
    # Get messages from the channel
    messages = get_messages(INPUT_CHANNEL_ID)
    if not messages:
        return {
            'statusCode': 400,
            'body': json.dumps('No messages found')
        }
    elif messages[0]['author']['id'] == DISCORD_BOT_ID:
        return {
            'statusCode': 200,
            'body': json.dumps('Function triggered twice. No message sent.')
        }

    results = parse_game_results(messages)

    output = format_message(results)

    if 'test' in event:
        response = send_message(TEST_CHANNEL_ID, output)
        msg = f'TEST: Scoreboard posted'
    else:
        response = send_message(OUTPUT_CHANNEL_ID, output)
        msg = f'Scoreboard posted'
        pin_message(OUTPUT_CHANNEL_ID, response['id'])
    
    return {
        'statusCode': 200,
        'body': json.dumps(msg)
    }

if __name__ == '__main__':
    print(lambda_handler('', ''))