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

# Shared HTTPS session so TCP/TLS handshakes are reused across all Discord calls.
# Cold-start cost is significant on Lambda; a single handshake per host beats
# paying it on every bare `requests.get()`.
_session = requests.Session()
_session.headers.update({
    'Authorization': f'Bot {DISCORD_BOT_TOKEN}',
    'Content-Type': 'application/json',
})
_adapter = requests.adapters.HTTPAdapter(pool_connections=4, pool_maxsize=32)
_session.mount('https://', _adapter)


def get_messages(channel_id):
    url = f'{DISCORD_API_BASE}/channels/{channel_id}/messages?limit=100'
    response = _session.get(url)
    response.raise_for_status()
    messages = response.json()

    for x in range(HUNDREDS_OF_MESSAGES - 1):
        last_msg_id = messages[-1]['id']
        url_id = url + f'&before={last_msg_id}'
        response = _session.get(url_id)
        response.raise_for_status()
        messages += response.json()
    return messages

def send_message(channel_id, message=None, components=None):
    url = f'{DISCORD_API_BASE}/channels/{channel_id}/messages'

    if components is not None:
        payload = {
            'components': components,
            'flags': 32768,
            'allowed_mentions': {'parse': ['users']},
        }
    else:
        payload = {'content': message, 'allowed_mentions': {'parse': ['users']}, 'flags': 4}

    response = _session.post(url, json=payload)
    response.raise_for_status()

    return response.json()

def pin_message(channel_id, message_id):
    url = f'{DISCORD_API_BASE}/channels/{channel_id}/messages/pins/{message_id}'
    _session.put(url)


_avatar_hash_cache = {}


def _extract_user_avatars(messages):
    """Collect unique (user_id, discord_avatar_hash) pairs from message payloads.

    Reads from `author` and `interaction_metadata.user` — no HTTP calls.
    Skips users with no custom avatar (default gradient avatars can't be
    distinguished by aHash, so there's no point fetching them).
    """
    out = {}
    for m in messages:
        candidates = [m.get('author')]
        iu = m.get('interaction_metadata', {})
        if iu:
            candidates.append(iu.get('user'))
        for src in candidates:
            if not src:
                continue
            uid = src.get('id')
            avatar = src.get('avatar')
            if uid and avatar and uid not in out:
                out[uid] = avatar
    return out


def _has_multiplayer_wordle(messages, checker):
    """True if any in-window Wordle bot attachment description signals multiple grids.

    Avatar matching is only needed for multi-player images. Single-player
    images attribute the score to `interaction_metadata.user` without lookups.
    Scoped to the scoreboard's time window so a multi-player game from an
    earlier day doesn't force avatar downloads on a single-player day.
    """
    for m in messages:
        if m.get('author', {}).get('id') != WORDLE_BOT_ID:
            continue
        if not checker(m['timestamp']):
            continue
        for att in (m.get('attachments') or []):
            desc = att.get('description', '')
            # "2 finished games", "1 unfinished and 2 finished games", etc.
            if 'finished games' in desc:
                return True
    return False


def _download_avatar_hash(uid, discord_avatar):
    """Download one avatar PNG and return its aHash. None on failure."""
    if uid in _avatar_hash_cache:
        return _avatar_hash_cache[uid]
    from PIL import Image
    import io
    try:
        url = f'https://cdn.discordapp.com/avatars/{uid}/{discord_avatar}.png?size=64'
        r = _session.get(url, timeout=3)
        r.raise_for_status()
        img = Image.open(io.BytesIO(r.content)).convert('RGB')
        h = _avatar_ahash(img)
        _avatar_hash_cache[uid] = h
        return h
    except Exception:
        _avatar_hash_cache[uid] = None
        return None


def build_avatar_pool(messages, checker):
    """Build {user_id: ahash} for candidate users, only if needed.

    Short-circuits to `{}` when no in-window multi-player Wordle image is
    present. Otherwise fetches avatars in parallel via a thread pool.
    """
    if not _has_multiplayer_wordle(messages, checker):
        return {}
    uid_to_discord_avatar = _extract_user_avatars(messages)
    if not uid_to_discord_avatar:
        return {}

    from concurrent.futures import ThreadPoolExecutor, as_completed
    pool = {}
    with ThreadPoolExecutor(max_workers=16) as ex:
        futures = {
            ex.submit(_download_avatar_hash, uid, avatar): uid
            for uid, avatar in uid_to_discord_avatar.items()
        }
        for fut in as_completed(futures):
            uid = futures[fut]
            h = fut.result()
            if h is not None:
                pool[uid] = h
    return pool


def lambda_handler(event, context):
    import time
    t0 = time.time()

    yesterday = datetime.now() - timedelta(days=1)
    puzzle_numbers = compute_puzzle_numbers(yesterday)
    game_regexes = build_game_regexes(puzzle_numbers)
    checker = make_timestamp_checker(yesterday, TIMEZONE, HOURS_AFTER_MIDNIGHT, TIME_WINDOW_HOURS)

    messages = get_messages(INPUT_CHANNEL_ID)
    print(f'[t+{time.time()-t0:.2f}s] fetched {len(messages)} messages')

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

    avatar_pool = build_avatar_pool(messages, checker)
    print(f'[t+{time.time()-t0:.2f}s] avatar pool has {len(avatar_pool)} users')

    results = defaultdict(dict)
    for msg in messages:
        entries = match_message(msg, game_regexes, checker, wordle_bot_id=WORDLE_BOT_ID, avatar_hashes=avatar_pool)
        for game_key, score, metadata, uid_override in entries:
            user_id = uid_override or msg.get('interaction_metadata', {}).get('user', {}).get('id') or msg['author']['id']
            results[game_key][user_id] = score
            puzzle_numbers.update(metadata)
    print(f'[t+{time.time()-t0:.2f}s] parsed {sum(len(v) for v in results.values())} game results')

    components = format_scoreboard_components(results, yesterday, puzzle_numbers, minimum_players=MINIMUM_PLAYERS)

    channel = TEST_CHANNEL_ID if 'test' in event else OUTPUT_CHANNEL_ID
    response = send_message(channel, components=components)
    print(f'[t+{time.time()-t0:.2f}s] posted scoreboard')

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
