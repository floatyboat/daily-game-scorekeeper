import json
import os
import requests
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from collections import defaultdict

from game_parser import (
    compute_puzzle_numbers, build_game_regexes,
    match_message, make_timestamp_checker,
)

DISCORD_API_BASE = 'https://discord.com/api/v10'

DISCORD_BOT_ID = os.getenv('DISCORD_BOT_ID') or 0
DISCORD_BOT_TOKEN = os.getenv('DISCORD_BOT_TOKEN')
INPUT_CHANNEL_ID = os.getenv('INPUT_CHANNEL_ID')
TEST_CHANNEL_ID = os.getenv('TEST_CHANNEL_ID')

TIMEZONE = ZoneInfo(os.getenv('TIMEZONE') or 'UTC')
TIME_WINDOW_HOURS = int(os.getenv('TIME_WINDOW_HOURS') or 24)
HOURS_AFTER_MIDNIGHT = int(os.getenv('HOURS_AFTER_MIDNIGHT') or 0)

# Discord's @silent flag — message posts without pinging users who have
# channel notifications enabled. Required so reposts don't notify everyone.
FLAG_SUPPRESS_NOTIFICATIONS = 1 << 12

_session = requests.Session()
_session.headers.update({
    'Authorization': f'Bot {DISCORD_BOT_TOKEN}',
    'Content-Type': 'application/json',
})


def get_messages(channel_id, limit=100):
    per_page = min(limit, 100)
    url = f'{DISCORD_API_BASE}/channels/{channel_id}/messages?limit={per_page}'
    r = _session.get(url)
    r.raise_for_status()
    messages = r.json()
    if not isinstance(messages, list):
        return []
    while len(messages) < limit:
        last_id = messages[-1]['id']
        remaining = min(limit - len(messages), 100)
        page_url = f'{DISCORD_API_BASE}/channels/{channel_id}/messages?limit={remaining}&before={last_id}'
        r = _session.get(page_url)
        r.raise_for_status()
        page = r.json()
        if not isinstance(page, list) or not page:
            break
        messages += page
    return messages


PLAY_BUTTON_CUSTOM_ID = 'sticky_play'
SCORES_BUTTON_CUSTOM_ID = 'sticky_scores'

STICKY_COMPONENTS = [{
    'type': 1,
    'components': [
        {'type': 2, 'style': 1, 'label': 'Play', 'custom_id': PLAY_BUTTON_CUSTOM_ID},
        {'type': 2, 'style': 2, 'label': 'Scores', 'custom_id': SCORES_BUTTON_CUSTOM_ID},
    ],
}]


def send_sticky(channel_id, content):
    payload = {
        'content': content,
        'components': STICKY_COMPONENTS,
        'flags': FLAG_SUPPRESS_NOTIFICATIONS,
        'allowed_mentions': {'parse': []},
    }
    url = f'{DISCORD_API_BASE}/channels/{channel_id}/messages'
    r = _session.post(url, json=payload)
    r.raise_for_status()
    return r.json()


def delete_message(channel_id, message_id):
    url = f'{DISCORD_API_BASE}/channels/{channel_id}/messages/{message_id}'
    _session.delete(url)


def find_sticky(messages):
    """Locate the bot's most recent message — that's our sticky.

    The bot only posts stickies in this channel (the daily scoreboard
    posts to OUTPUT_CHANNEL, /play replies are ephemeral), so author
    identity alone uniquely identifies our sticky.
    """
    for msg in messages:
        author = msg.get('author', {})
        if DISCORD_BOT_ID and author.get('id') == str(DISCORD_BOT_ID):
            return msg
        if not DISCORD_BOT_ID and author.get('bot'):
            return msg
    return None


def get_reference_date():
    now = datetime.now(TIMEZONE)
    if now.hour < HOURS_AFTER_MIDNIGHT:
        now = now - timedelta(days=1)
    return now.replace(hour=0, minute=0, second=0, microsecond=0).replace(tzinfo=None)


def count_unique_players(results):
    players = set()
    for game_scores in results.values():
        players.update(game_scores.keys())
    return len(players)


def _has_play_button(sticky):
    return any(
        c.get('custom_id') == PLAY_BUTTON_CUSTOM_ID
        for row in (sticky.get('components') or [])
        for c in row.get('components', [])
    )


STICKY_HEADING = "\U0001F47E **Now Playing**"


def build_sticky_content(results):
    player_count = count_unique_players(results)
    game_count = sum(1 for scores in results.values() if scores)
    if player_count == 0:
        return f"{STICKY_HEADING}\nNo scores yet today"
    p = 'player' if player_count == 1 else 'players'
    g = 'game' if game_count == 1 else 'games'
    return f"{STICKY_HEADING}\n{player_count} {p} · {game_count} {g} today"


def update_sticky(channel_id, channel_messages, results):
    """Maintain a single sticky at the bottom of channel_id.

    No-op only when our sticky is already the most recent message AND its
    content matches what we'd render now — content comparison catches the
    day-transition case where the sticky is still at the bottom but shows
    yesterday's stats.
    """
    sticky = find_sticky(channel_messages)
    content = build_sticky_content(results)

    if (sticky and channel_messages
            and channel_messages[0]['id'] == sticky['id']
            and sticky.get('content', '') == content
            and _has_play_button(sticky)):
        return 'unchanged'

    if sticky:
        delete_message(channel_id, sticky['id'])

    send_sticky(channel_id, content)
    return 'reposted' if sticky else 'created'


def lambda_handler(event, context):
    is_test = isinstance(event, dict) and 'test' in event

    # Active window: [HOURS_AFTER_MIDNIGHT, midnight) in TIMEZONE. The daily
    # scoreboard fires at HOURS_AFTER_MIDNIGHT and posts yesterday's summary;
    # before that point we'd be tracking the previous day's already-finalized
    # leaders, so stay dormant until it runs. Test events bypass this guard.
    if not is_test and datetime.now(TIMEZONE).hour < HOURS_AFTER_MIDNIGHT:
        return {'statusCode': 200, 'body': json.dumps('Outside active window')}

    today = get_reference_date()
    puzzle_numbers = compute_puzzle_numbers(today)
    game_regexes = build_game_regexes(puzzle_numbers)
    checker = make_timestamp_checker(today, TIMEZONE, HOURS_AFTER_MIDNIGHT, TIME_WINDOW_HOURS)

    input_messages = get_messages(INPUT_CHANNEL_ID, limit=200)

    results = defaultdict(dict)
    for msg in input_messages:
        for game_key, score, metadata, uid_override in match_message(msg, game_regexes, checker):
            user_id = uid_override or msg.get('interaction_metadata', {}).get('user', {}).get('id') or msg['author']['id']
            results[game_key][user_id] = score
            puzzle_numbers.update(metadata)

    # Sticky lives in the test channel under a test event so we don't post
    # to the real input channel during local runs.
    sticky_channel = TEST_CHANNEL_ID if is_test else INPUT_CHANNEL_ID
    sticky_messages = get_messages(TEST_CHANNEL_ID, limit=50) if is_test else input_messages
    action = update_sticky(sticky_channel, sticky_messages, results)

    return {'statusCode': 200, 'body': json.dumps(f'Sticky: {action}')}


if __name__ == '__main__':
    print(lambda_handler({'test': True}, None))
