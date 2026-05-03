import base64
import json
import os
import requests
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from collections import defaultdict
from nacl.signing import VerifyKey
from nacl.exceptions import BadSignatureError

from game_parser import (
    compute_puzzle_numbers, build_games_list, build_game_regexes,
    match_message, make_timestamp_checker, format_scoreboard_components,
)

DISCORD_API_BASE = 'https://discord.com/api/v10'

DISCORD_PUBLIC_KEY = os.getenv('DISCORD_PUBLIC_KEY', '')
DISCORD_BOT_TOKEN = os.getenv('DISCORD_BOT_TOKEN')
INPUT_CHANNEL_ID = os.getenv('INPUT_CHANNEL_ID')
TIMEZONE = ZoneInfo(os.getenv('TIMEZONE') or 'UTC')
TIME_WINDOW_HOURS = int(os.getenv('TIME_WINDOW_HOURS') or 24)
HOURS_AFTER_MIDNIGHT = int(os.getenv('HOURS_AFTER_MIDNIGHT') or 0)
MINIMUM_PLAYERS = int(os.getenv('MINIMUM_PLAYERS') or 1)

_session = requests.Session()
_session.headers.update({
    'Authorization': f'Bot {DISCORD_BOT_TOKEN}',
    'Content-Type': 'application/json',
})


def get_body(event):
    """Extract the raw body string, decoding base64 if needed."""
    body = event.get('body', '')
    if event.get('isBase64Encoded'):
        body = base64.b64decode(body).decode('utf-8')
    return body


def verify_signature(body, event):
    """Verify Discord Ed25519 request signature. Raises on failure."""
    headers = {k.lower(): v for k, v in event.get('headers', {}).items()}
    signature = headers.get('x-signature-ed25519', '')
    timestamp = headers.get('x-signature-timestamp', '')

    verify_key = VerifyKey(bytes.fromhex(DISCORD_PUBLIC_KEY))
    verify_key.verify(f'{timestamp}{body}'.encode(), bytes.fromhex(signature))


def get_reference_date():
    now = datetime.now(TIMEZONE)
    if now.hour < HOURS_AFTER_MIDNIGHT:
        now = now - timedelta(days=1)
    return now.replace(hour=0, minute=0, second=0, microsecond=0).replace(tzinfo=None)


def build_scoreboard_response():
    """Build today's scoreboard as an ephemeral text reply.

    Single page (limit=100) keeps the call under Discord's 3-second
    interaction-response budget; the daily summary lambda is the source
    of truth for the full archive, this is a live preview.
    """
    today = get_reference_date()
    puzzle_numbers = compute_puzzle_numbers(today)
    game_regexes = build_game_regexes(puzzle_numbers)
    checker = make_timestamp_checker(today, TIMEZONE, HOURS_AFTER_MIDNIGHT, TIME_WINDOW_HOURS)

    url = f'{DISCORD_API_BASE}/channels/{INPUT_CHANNEL_ID}/messages?limit=100'
    r = _session.get(url)
    r.raise_for_status()
    messages = r.json() if isinstance(r.json(), list) else []

    results = defaultdict(dict)
    for msg in messages:
        for game_key, score, metadata, uid_override in match_message(msg, game_regexes, checker):
            user_id = uid_override or msg.get('interaction_metadata', {}).get('user', {}).get('id') or msg['author']['id']
            results[game_key][user_id] = score
            puzzle_numbers.update(metadata)

    components = format_scoreboard_components(
        results, today, puzzle_numbers,
        title="Today's Scores", minimum_players=MINIMUM_PLAYERS,
    )

    # 64 (EPHEMERAL) | 1<<15 (IS_COMPONENTS_V2). V2 messages can't have a
    # content field, so the builder's output goes directly into components.
    return {
        "type": 4,
        "data": {
            "flags": 32832,
            "components": components,
        },
    }


def build_play_response():
    """Build an ephemeral message with link buttons for all tracked games."""
    today = datetime.utcnow()
    puzzle_numbers = compute_puzzle_numbers(today)
    games = build_games_list(puzzle_numbers)

    buttons = [
        {"type": 2, "style": 5, "label": f"{emoji} {name}", "url": url}
        for _, emoji, name, _, _, _, url in games
    ]

    action_rows = []
    for i in range(0, len(buttons), 5):
        action_rows.append({"type": 1, "components": buttons[i:i + 5]})

    return {
        "type": 4,
        "data": {
            "flags": 64,
            "content": "Pick a game to play!",
            "components": action_rows,
        },
    }


def lambda_handler(event, context):
    # Direct invocations (AWS console/CLI) don't come through the Function URL
    # and already require IAM auth, so skip signature verification
    is_direct = 'requestContext' not in event

    if is_direct:
        body = event
    else:
        raw_body = get_body(event)
        try:
            verify_signature(raw_body, event)
        except (BadSignatureError, ValueError, Exception):
            return {'statusCode': 401, 'body': 'Invalid request signature'}
        body = json.loads(raw_body)

    # PING (type 1) — Discord endpoint validation
    if body.get('type') == 1:
        return {
            'statusCode': 200,
            'headers': {'Content-Type': 'application/json'},
            'body': json.dumps({'type': 1}),
        }

    # APPLICATION_COMMAND (type 2)
    if body.get('type') == 2:
        command_name = body.get('data', {}).get('name', '')
        if command_name == 'play':
            return {
                'statusCode': 200,
                'headers': {'Content-Type': 'application/json'},
                'body': json.dumps(build_play_response()),
            }

    # MESSAGE_COMPONENT (type 3) — sticky buttons
    if body.get('type') == 3:
        custom_id = body.get('data', {}).get('custom_id')
        if custom_id == 'sticky_play':
            return {
                'statusCode': 200,
                'headers': {'Content-Type': 'application/json'},
                'body': json.dumps(build_play_response()),
            }
        if custom_id == 'sticky_scores':
            return {
                'statusCode': 200,
                'headers': {'Content-Type': 'application/json'},
                'body': json.dumps(build_scoreboard_response()),
            }

    return {'statusCode': 400, 'body': 'Unknown interaction type'}
