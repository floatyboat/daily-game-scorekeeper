import json
import os
import time
import requests
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from collections import defaultdict

from game_parser import (
    compute_puzzle_numbers, build_game_regexes, match_message,
    format_scoreboard, make_timestamp_checker, BANDLE_LINK, PIPS_LINK,
    CONNECTIONS_LINK, SPORTS_CONNECTIONS_LINK, MAPTAP_LINK, GLOBLE_LINK,
    FLAGLE_LINK, WORLDLE_LINK, WHEREDLE_LINK, QUIZL_LINK, CHRONOPHOTO_LINK,
    DEFAULT_BANDLE_TOTAL, DEFAULT_WHEREDLE_TOTAL, DEFAULT_QUIZL_TOTAL,
)

DISCORD_API_BASE = 'https://discord.com/api/v10'

DISCORD_BOT_ID = os.getenv('DISCORD_BOT_ID') or 0
DISCORD_BOT_TOKEN = os.getenv('DISCORD_BOT_TOKEN')
INPUT_CHANNEL_ID = os.getenv('INPUT_CHANNEL_ID')
OUTPUT_CHANNEL_ID = os.getenv('OUTPUT_CHANNEL_ID')
TEST_CHANNEL_ID = os.getenv('TEST_CHANNEL_ID')
MINIMUM_PLAYERS = int(os.getenv('MINIMUM_PLAYERS') or 1)

TIMEZONE = ZoneInfo(os.getenv('TIMEZONE') or 'UTC')
TIME_WINDOW_HOURS = int(os.getenv('TIME_WINDOW_HOURS') or 24)
HOURS_AFTER_MIDNIGHT = int(os.getenv('HOURS_AFTER_MIDNIGHT') or 0)

CHECKMARK = '\u2705'

GAME_EMOJIS = {
    'connections': '🔗',
    'bandle': '🎵',
    'sports': '🏈',
    'pips': '🎲',
    'maptap': '🎯',
    'chronophoto': '📷',
    'globle': '🌍',
    'worldle': '🗺️',
    'flagle': '🏁',
    'wheredle': '🛣️',
    'quizl': '⁉️',
}

GAME_TITLES = {
    'connections': 'Connections',
    'bandle': 'Bandle',
    'sports': 'Sports Connections',
    'pips': 'Pips',
    'maptap': 'MapTap',
    'chronophoto': 'Chronophoto',
    'globle': 'Globle',
    'worldle': 'Worldle',
    'flagle': 'Flagle',
    'wheredle': 'Wheredle',
    'quizl': 'Quizl',
}

GAME_LINKS = {
    'connections': CONNECTIONS_LINK,
    'bandle': BANDLE_LINK,
    'sports': SPORTS_CONNECTIONS_LINK,
    'pips': PIPS_LINK,
    'maptap': MAPTAP_LINK,
    'chronophoto': CHRONOPHOTO_LINK,
    'globle': GLOBLE_LINK,
    'worldle': WORLDLE_LINK,
    'flagle': FLAGLE_LINK,
    'wheredle': WHEREDLE_LINK,
    'quizl': QUIZL_LINK,
}

GAME_METRICS = {
    'connections': 'connections',
    'bandle': 'guesses',
    'sports': 'connections',
    'pips': 'time',
    'maptap': 'score',
    'chronophoto': 'score',
    'globle': 'guesses',
    'worldle': 'guesses',
    'flagle': 'guesses',
    'wheredle': 'guesses',
    'quizl': 'score',
}


def get_headers():
    return {
        'Authorization': f'Bot {DISCORD_BOT_TOKEN}',
        'Content-Type': 'application/json'
    }


def get_messages(channel_id, limit=100):
    """Fetch messages with pagination. Discord API max is 100 per request."""
    per_page = min(limit, 100)
    url = f'{DISCORD_API_BASE}/channels/{channel_id}/messages?limit={per_page}'
    response = requests.get(url, headers=get_headers())
    messages = response.json()
    if not isinstance(messages, list):
        return []

    while len(messages) < limit:
        last_msg_id = messages[-1]['id']
        remaining = min(limit - len(messages), 100)
        page_url = f'{DISCORD_API_BASE}/channels/{channel_id}/messages?limit={remaining}&before={last_msg_id}'
        response = requests.get(page_url, headers=get_headers())
        page = response.json()
        if not isinstance(page, list) or not page:
            break
        messages += page

    return messages


def add_reaction(channel_id, message_id, emoji):
    url = f'{DISCORD_API_BASE}/channels/{channel_id}/messages/{message_id}/reactions/{emoji}/@me'
    requests.put(url, headers=get_headers())


def send_message(channel_id, message, reply_to_id=None, suppress_mentions=False):
    payload = {
        'content': message,
        'allowed_mentions': {'parse': []} if suppress_mentions else {'parse': ['users']},
        'flags': 4,
    }
    if reply_to_id:
        payload['message_reference'] = {'message_id': reply_to_id}

    url = f'{DISCORD_API_BASE}/channels/{channel_id}/messages'
    response = requests.post(url, headers=get_headers(), json=payload)
    return response.json()


def edit_message(channel_id, message_id, message):
    url = f'{DISCORD_API_BASE}/channels/{channel_id}/messages/{message_id}'
    payload = {
        'content': message,
        'allowed_mentions': {'parse': ['users']},
        'flags': 4,
    }
    response = requests.patch(url, headers=get_headers(), json=payload)
    return response.json()


def is_processed(msg):
    """Check if bot already reacted to this message with a checkmark."""
    for reaction in msg.get('reactions', []):
        if reaction.get('me') and reaction['emoji'].get('name') == CHECKMARK:
            return True
    return False


def get_reference_date():
    """Get today's reference date, accounting for timezone and hours-after-midnight."""
    now = datetime.now(TIMEZONE)
    # Before HOURS_AFTER_MIDNIGHT, treat "today" as the previous calendar day
    if now.hour < HOURS_AFTER_MIDNIGHT:
        now = now - timedelta(days=1)
    return now.replace(hour=0, minute=0, second=0, microsecond=0).replace(tzinfo=None)


def find_live_scoreboard(messages, bot_id):
    """Find an existing Live Scoreboard message from today in the output channel."""
    for msg in messages:
        if (msg['author']['id'] == str(bot_id) and
                '**Live Scoreboard**' in msg.get('content', '')):
            return msg
    return None


def format_mini_scoreboard(game_key, game_scores, puzzle_numbers):
    """Format a per-game mini-scoreboard for reply messages."""
    emoji = GAME_EMOJIS.get(game_key, '')
    title = GAME_TITLES.get(game_key, game_key)
    metric = GAME_METRICS.get(game_key, 'score')
    medals = ['👑', '🥈', '🥉']

    bandle_total = puzzle_numbers.get('bandle_total', DEFAULT_BANDLE_TOTAL)
    wheredle_total = puzzle_numbers.get('wheredle_total', DEFAULT_WHEREDLE_TOTAL)
    quizl_total = puzzle_numbers.get('quizl_total', DEFAULT_QUIZL_TOTAL)

    # Get total for this game
    total_map = {
        'bandle': bandle_total,
        'connections': 4,
        'sports': 4,
        'quizl': quizl_total,
        'wheredle': wheredle_total,
    }
    total = total_map.get(game_key, 0)

    # Sort players
    if metric == 'connections':
        players = sorted(game_scores.items(), key=lambda x: (x[1][0], -x[1][1]))
    elif metric == 'score':
        players = sorted(game_scores.items(), key=lambda x: (-x[1]))
    else:
        players = sorted(game_scores.items(), key=lambda x: x[1])

    link = GAME_LINKS.get(game_key, '')
    linked_title = f"[{title}]({link})" if link else title
    lines = [f"{emoji} **{linked_title} Leaderboard:**"]
    for idx, (player_id, score) in enumerate(players):
        medal = medals[idx] if idx < len(medals) else ''

        if metric == 'time':
            minutes = score // 60
            seconds = score % 60
            score_str = f"{minutes}:{seconds:02d}"
        elif metric == 'connections':
            mistakes, solved = score
            if mistakes == -1:
                score_str = "VERT 🧗"
            elif mistakes == total:
                score_str = f"{mistakes}/{total} mistakes ({solved} solved)"
            else:
                score_str = f"{mistakes}/{total} mistakes"
        elif metric == 'score':
            score_str = str(score)
            if total > 0:
                score_str = f"{score_str}/{total}"
        else:  # guesses
            if total == 0:
                score_str = f"{score} {metric}"
            else:
                display_score = 'X' if score > total else str(score)
                score_str = f"{display_score}/{total} {metric}"

        lines.append(f"{medal} <@{player_id}>: {score_str}")

    return "\n".join(lines)


POLL_INTERVAL = 10
POLL_ITERATIONS = 6


def poll_once(is_test=False, seen_ids=None):
    today = get_reference_date()
    puzzle_numbers = compute_puzzle_numbers(today)
    game_regexes = build_game_regexes(puzzle_numbers)
    checker = make_timestamp_checker(today, TIMEZONE, HOURS_AFTER_MIDNIGHT, TIME_WINDOW_HOURS)

    # Fetch messages from input channel
    input_messages = get_messages(INPUT_CHANNEL_ID, limit=200)
    if not input_messages:
        return 'No messages found'

    # Parse ALL messages to build full results, track which are new
    results = defaultdict(dict)
    new_messages = []  # (msg, game_key) tuples for unprocessed matched messages

    for msg in input_messages:
        result = match_message(msg['content'], msg['timestamp'], game_regexes, checker)
        if result:
            game_key, score, metadata = result
            results[game_key][msg['author']['id']] = score
            puzzle_numbers.update(metadata)

            if not is_processed(msg) and msg['id'] not in (seen_ids or set()):
                new_messages.append((msg, game_key))

    if not new_messages:
        return 'No new messages to process'

    reply_channel = TEST_CHANNEL_ID if is_test else INPUT_CHANNEL_ID

    # Process each new message: react + reply with mini-scoreboard
    for msg, game_key in new_messages:
        # React with checkmark to mark as processed (always on input channel)
        add_reaction(INPUT_CHANNEL_ID, msg['id'], CHECKMARK)

        # Reply with mini-scoreboard for this game
        if game_key in results and results[game_key]:
            mini = format_mini_scoreboard(game_key, results[game_key], puzzle_numbers)
            reply_to = msg['id'] if not is_test else None
            send_message(reply_channel, mini, reply_to_id=reply_to, suppress_mentions=True)

    # Track processed IDs so subsequent poll iterations skip them
    if seen_ids is not None:
        for msg, _ in new_messages:
            seen_ids.add(msg['id'])

    return f'Processed {len(new_messages)} new messages.'


def lambda_handler(event, context):
    is_test = 'test' in event if isinstance(event, dict) else False
    iterations = 1 if is_test else POLL_ITERATIONS
    results = []
    seen_ids = set()

    for i in range(iterations):
        result = poll_once(is_test, seen_ids)
        results.append(result)

        if i < iterations - 1:
            time.sleep(POLL_INTERVAL)

    return {
        'statusCode': 200,
        'body': json.dumps(results)
    }


if __name__ == '__main__':
    print(lambda_handler({}, None))
