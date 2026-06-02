import base64
import json
import os
from datetime import datetime
from zoneinfo import ZoneInfo
from nacl.signing import VerifyKey
from nacl.exceptions import BadSignatureError

from game_parser import build_games, compute_puzzle_numbers, format_scoreboard_components, make_timestamp_checker
from scoreboard import make_session, fetch_messages, reference_date, parse_results, build_avatar_pool

DISCORD_PUBLIC_KEY = os.getenv('DISCORD_PUBLIC_KEY', '')
DISCORD_BOT_TOKEN = os.getenv('DISCORD_BOT_TOKEN')
WORDLE_BOT_ID = os.getenv('WORDLE_BOT_ID')
TIMEZONE = ZoneInfo(os.getenv('TIMEZONE') or 'UTC')
TIME_WINDOW_HOURS = int(os.getenv('TIME_WINDOW_HOURS') or 24)
HOURS_AFTER_MIDNIGHT = int(os.getenv('HOURS_AFTER_MIDNIGHT') or 0)
MINIMUM_PLAYERS = int(os.getenv('MINIMUM_PLAYERS') or 1)

_session = make_session(DISCORD_BOT_TOKEN)


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


def fetch_today_results(channel_id):
    """Fetch one page of channel history and parse today's game results.

    Shared by the Scores and Play buttons so both reflect the same live view of
    the channel they were clicked in. Single page (limit=100) keeps the call
    under Discord's 3-second interaction-response budget; the daily summary
    lambda is the source of truth for the full archive, this is a live preview.

    Returns (results, puzzle_numbers, today).
    """
    today = reference_date(datetime.now(), TIMEZONE, HOURS_AFTER_MIDNIGHT)
    messages = fetch_messages(_session, channel_id, limit=100)
    checker = make_timestamp_checker(today, TIMEZONE, HOURS_AFTER_MIDNIGHT, TIME_WINDOW_HOURS)
    avatar_pool = build_avatar_pool(_session, messages, checker, WORDLE_BOT_ID)
    results, puzzle_numbers = parse_results(
        messages, today, TIMEZONE, HOURS_AFTER_MIDNIGHT, TIME_WINDOW_HOURS,
        wordle_bot_id=WORDLE_BOT_ID, avatar_hashes=avatar_pool,
    )
    return results, puzzle_numbers, today


def build_scoreboard_response(channel_id):
    """Build today's scoreboard as an ephemeral Components V2 reply."""
    results, puzzle_numbers, today = fetch_today_results(channel_id)

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


def build_play_response(channel_id):
    """Build an ephemeral message with link buttons for all tracked games.

    Buttons are ordered by how many people have already played each game today
    (descending), then alphabetically by title. A game played by at least one
    person gets a "(count)" suffix; games nobody has played yet sort last with
    no suffix, so the most active games surface first.
    """
    try:
        results, puzzle_numbers, _ = fetch_today_results(channel_id)
    except Exception:
        # Counts are a nice-to-have; never let a fetch/parse hiccup block the
        # core Play action. Fall back to today's games with no counts.
        results, puzzle_numbers = {}, compute_puzzle_numbers(datetime.utcnow())

    games = build_games(puzzle_numbers)

    def player_count(game):
        return len(results.get(game.key, {}))

    games.sort(key=lambda g: (-player_count(g), g.title.lower()))

    buttons = []
    for g in games:
        count = player_count(g)
        label = f"{g.emoji} {g.title} ({count})" if count else f"{g.emoji} {g.title}"
        buttons.append({"type": 2, "style": 5, "label": label, "url": g.url})

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
                'body': json.dumps(build_play_response(body['channel_id'])),
            }

    # MESSAGE_COMPONENT (type 3) — sticky buttons
    if body.get('type') == 3:
        custom_id = body.get('data', {}).get('custom_id')
        if custom_id == 'sticky_play':
            return {
                'statusCode': 200,
                'headers': {'Content-Type': 'application/json'},
                'body': json.dumps(build_play_response(body['channel_id'])),
            }
        if custom_id == 'sticky_scores':
            return {
                'statusCode': 200,
                'headers': {'Content-Type': 'application/json'},
                'body': json.dumps(build_scoreboard_response(body['channel_id'])),
            }

    return {'statusCode': 400, 'body': 'Unknown interaction type'}


if __name__ == '__main__':
    import sys, re
    # Fixtures use ${VAR} placeholders for installation-specific values
    # (e.g. channel_id) so the handler can stay env-free.
    fixture = sys.argv[1] if len(sys.argv) > 1 else 'test_events/Interaction/interaction_sticky_scores.json'
    with open(fixture) as f:
        raw = f.read()
    raw = re.sub(r'\$\{(\w+)\}', lambda m: os.environ[m.group(1)], raw)
    print(lambda_handler(json.loads(raw), None))
