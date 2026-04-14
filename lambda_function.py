import json
import os
import requests
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from collections import defaultdict

from game_parser import (
    compute_puzzle_numbers, build_game_regexes, match_message,
    format_scoreboard, format_scoreboard_components, make_timestamp_checker,
    _avatar_ahash,
)

DISCORD_API_BASE = 'https://discord.com/api/v10'

DISCORD_BOT_ID = os.getenv('DISCORD_BOT_ID') or 0
DISCORD_BOT_TOKEN = os.getenv('DISCORD_BOT_TOKEN')
INPUT_CHANNEL_ID = os.getenv('INPUT_CHANNEL_ID')
OUTPUT_CHANNEL_ID = os.getenv('OUTPUT_CHANNEL_ID')
TEST_CHANNEL_ID = os.getenv('TEST_CHANNEL_ID')
WORDLE_BOT_ID = os.getenv('WORDLE_BOT_ID')
HUNDREDS_OF_MESSAGES = int(os.getenv('HUNDREDS_OF_MESSAGES') or 1)
MINIMUM_PLAYERS = int(os.getenv('MINIMUM_PLAYERS') or 1)

TIMEZONE = ZoneInfo(os.getenv('TIMEZONE') or 'UTC')
TIME_WINDOW_HOURS = int(os.getenv('TIME_WINDOW_HOURS') or 24)
HOURS_AFTER_MIDNIGHT = int(os.getenv('HOURS_AFTER_MIDNIGHT') or 0)

def get_messages(channel_id):
    headers = {
        'Authorization': f'Bot {DISCORD_BOT_TOKEN}',
        'Content-Type': 'application/json'
    }

    url = f'{DISCORD_API_BASE}/channels/{channel_id}/messages?limit=100'
    response = requests.get(url, headers=headers)
    response.raise_for_status()
    messages = response.json()

    for x in range(HUNDREDS_OF_MESSAGES - 1):
        last_msg_id = messages[-1]['id']
        url_id = url + f'&before={last_msg_id}'
        response = requests.get(url_id, headers=headers)
        response.raise_for_status()
        messages += response.json()
    return messages

def send_message(channel_id, message=None, components=None):
    headers = {
        'Authorization': f'Bot {DISCORD_BOT_TOKEN}',
        'Content-Type': 'application/json'
    }

    url = f'{DISCORD_API_BASE}/channels/{channel_id}/messages'

    if components is not None:
        payload = {
            'components': components,
            'flags': 32768,
            'allowed_mentions': {'parse': ['users']},
        }
    else:
        payload = {'content': message, 'allowed_mentions': {'parse': ['users']}, 'flags': 4}

    response = requests.post(url, headers=headers, json=payload)
    response.raise_for_status()

    return response.json()

def pin_message(channel_id, message_id):
    headers = {
        'Authorization': f'Bot {DISCORD_BOT_TOKEN}',
        'Content-Type': 'application/json'
    }

    url = f'{DISCORD_API_BASE}/channels/{channel_id}/messages/pins/{message_id}'
    requests.put(url, headers=headers)


def _fetch_user_avatar_hash(user_id):
    """Fetch a Discord user's global avatar and return its aHash.

    Returns None for users with no custom avatar (default gradient), or on any
    fetch/download failure. Uses a module-level cache so repeated calls within
    one invocation don't hit the CDN twice.
    """
    from PIL import Image
    import io
    if user_id in _avatar_hash_cache:
        return _avatar_hash_cache[user_id]
    try:
        headers = {'Authorization': f'Bot {DISCORD_BOT_TOKEN}'}
        r = requests.get(f'{DISCORD_API_BASE}/users/{user_id}', headers=headers, timeout=5)
        r.raise_for_status()
        avatar = r.json().get('avatar')
        if not avatar:
            _avatar_hash_cache[user_id] = None
            return None
        cdn_url = f'https://cdn.discordapp.com/avatars/{user_id}/{avatar}.png?size=128'
        img_resp = requests.get(cdn_url, timeout=5)
        img_resp.raise_for_status()
        img = Image.open(io.BytesIO(img_resp.content)).convert('RGB')
        h = _avatar_ahash(img)
        _avatar_hash_cache[user_id] = h
        return h
    except Exception:
        _avatar_hash_cache[user_id] = None
        return None


_avatar_hash_cache = {}


def build_avatar_pool(messages):
    """Collect unique user IDs from channel messages and build {uid: ahash}."""
    user_ids = set()
    for m in messages:
        author = m.get('author', {})
        if author.get('id'):
            user_ids.add(author['id'])
        iu_id = m.get('interaction_metadata', {}).get('user', {}).get('id')
        if iu_id:
            user_ids.add(iu_id)
    pool = {}
    for uid in user_ids:
        h = _fetch_user_avatar_hash(uid)
        if h is not None:
            pool[uid] = h
    return pool


def lambda_handler(event, context):
    yesterday = datetime.now() - timedelta(days=1)
    puzzle_numbers = compute_puzzle_numbers(yesterday)
    game_regexes = build_game_regexes(puzzle_numbers)
    checker = make_timestamp_checker(yesterday, TIMEZONE, HOURS_AFTER_MIDNIGHT, TIME_WINDOW_HOURS)

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

    avatar_pool = build_avatar_pool(messages)

    results = defaultdict(dict)
    for msg in messages:
        entries = match_message(msg, game_regexes, checker, wordle_bot_id=WORDLE_BOT_ID, avatar_hashes=avatar_pool)
        for game_key, score, metadata, uid_override in entries:
            user_id = uid_override or msg.get('interaction_metadata', {}).get('user', {}).get('id') or msg['author']['id']
            results[game_key][user_id] = score
            puzzle_numbers.update(metadata)

    components = format_scoreboard_components(results, yesterday, puzzle_numbers, minimum_players=MINIMUM_PLAYERS)

    channel = TEST_CHANNEL_ID if 'test' in event else OUTPUT_CHANNEL_ID
    response = send_message(channel, components=components)

    if 'test' in event:
        msg = 'TEST: Scoreboard posted'
    else:
        msg = 'Scoreboard posted'
        pin_message(channel, response['id'])

    return {
        'statusCode': 200,
        'body': json.dumps(msg)
    }

if __name__ == '__main__':
    print(lambda_handler('', ''))
