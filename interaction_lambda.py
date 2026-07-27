import base64
import json
import os
import random
from datetime import datetime
from zoneinfo import ZoneInfo
from nacl.signing import VerifyKey
from nacl.exceptions import BadSignatureError

from game_parser import (
    build_games, compute_puzzle_numbers, format_scoreboard_components,
    make_timestamp_checker, game_sort_key, STREAK_MIN,
)
from scoreboard import (
    make_session, fetch_messages, reference_date, parse_results, build_avatar_pool,
    safe_guild_id, gather_streaks,
    PLAY_BUTTON_CUSTOM_ID, SCORES_BUTTON_CUSTOM_ID,
)

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
    today = reference_date(datetime.now(TIMEZONE), TIMEZONE, HOURS_AFTER_MIDNIGHT)
    messages = fetch_messages(_session, channel_id, limit=100)
    checker = make_timestamp_checker(today, TIMEZONE, HOURS_AFTER_MIDNIGHT, TIME_WINDOW_HOURS)
    avatar_pool = build_avatar_pool(_session, messages, checker, WORDLE_BOT_ID)
    results, puzzle_numbers = parse_results(
        messages, today, TIMEZONE, HOURS_AFTER_MIDNIGHT, TIME_WINDOW_HOURS,
        wordle_bot_id=WORDLE_BOT_ID, avatar_hashes=avatar_pool,
    )
    return results, puzzle_numbers, today


def build_scoreboard_response(channel_id, guild_id=None):
    """Build today's scoreboard as an ephemeral Components V2 reply.

    Streaks ride along when the store is reachable: live views show a streak
    kept alive today as current + 1 (SPEC.md), so the board updates the moment
    someone plays.
    """
    results, puzzle_numbers, today = fetch_today_results(channel_id)

    streaks = gather_streaks(guild_id, today, results,
                             [g.key for g in build_games(puzzle_numbers)])
    components = format_scoreboard_components(
        results, today, puzzle_numbers,
        title="Today's Scores", minimum_players=MINIMUM_PLAYERS, streaks=streaks,
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


def interaction_user_id(body):
    """ID of the user who triggered an interaction.

    Discord nests the acting user under `member.user` for guild interactions and
    promotes it to a top-level `user` in DMs, so check both. Returns None when
    neither is present (e.g. a bare test fixture), which callers treat as
    "unknown user" and fall back to listing every game.
    """
    member = body.get('member') or {}
    return (member.get('user') or body.get('user') or {}).get('id')


def interaction_guild_id(body):
    """guild_id of the interaction, for streak lookups.

    Guild interactions carry it directly; local test fixtures (and DMs) don't,
    so fall back to resolving the channel via the API (cached per process).
    None -- a channel with no guild -- just renders without streaks.
    """
    return body.get('guild_id') or safe_guild_id(_session, body.get('channel_id'))


def unplayed_games(channel_id, user_id=None, guild_id=None):
    """Today's tracked games the presser hasn't logged yet, plus the live
    results and streak bundle backing them.

    Shared by the Play and Random buttons so both work off the same live view
    of the channel. When user_id is known, games that user has already logged
    today are dropped, making the result personal to whoever pressed; with no
    user_id (an unidentifiable presser) every game is returned. results and
    the gather_streaks() bundle (or None) cover ALL games, so counts and
    streak numbers reflect the whole server, not just the presser's remainder.
    """
    today = None
    try:
        results, puzzle_numbers, today = fetch_today_results(channel_id)
    except Exception:
        # Counts are a nice-to-have; never let a fetch/parse hiccup block the
        # core action. Fall back to today's games with no counts or streaks.
        results, puzzle_numbers = {}, compute_puzzle_numbers(datetime.utcnow())

    games = build_games(puzzle_numbers)

    streaks = None
    if today is not None:
        streaks = gather_streaks(guild_id, today, results,
                                 [g.key for g in games], include_players=False)

    if user_id is not None:
        games = [g for g in games if user_id not in results.get(g.key, {})]

    return games, results, streaks


ALL_PLAYED_MESSAGE = "\U0001F389 You've played every tracked game today!"


def build_play_response(channel_id, user_id=None, guild_id=None):
    """Build an ephemeral message with link buttons for tracked games.

    When user_id is known, only games that user hasn't logged today are shown,
    so the Play list is personal to whoever pressed the button. Buttons follow
    the app-wide game ordering (game_sort_key, same as scoreboard sections):
    today's live count, then active server streak, then all-time distinct
    players, then title. Labels get a "(count)" suffix once someone has played
    today and a fire-streak suffix while the game's server streak is alive.
    With no user_id (an unidentifiable presser) every game is listed; with no
    reachable store the order falls back to live count then title.
    """
    games, results, streaks = unplayed_games(channel_id, user_id, guild_id)
    game_streaks = (streaks or {}).get('games', {})

    games.sort(key=lambda g: game_sort_key(g, results, streaks))

    buttons = []
    for g in games:
        count = len(results.get(g.key) or {})
        label = f"{g.emoji} {g.title} ({count})" if count else f"{g.emoji} {g.title}"
        streak = game_streaks.get(g.key, 0)
        if streak >= STREAK_MIN:
            label += f" \U0001F525{streak}"
        buttons.append({"type": 2, "style": 5, "label": label, "url": g.url})

    action_rows = []
    for i in range(0, len(buttons), 5):
        action_rows.append({"type": 1, "components": buttons[i:i + 5]})

    # A "surprise me" shortcut: one random unplayed game as its own grey link
    # button on its own row above the list. Resolved here, at click time, so the
    # link points straight at a game this user hasn't logged — one tap, no
    # follow-up.
    if games:
        pick = random.choice(games)
        action_rows.insert(0, {"type": 1, "components": [
            {"type": 2, "style": 5, "label": "\U0001F52E Random", "url": pick.url},
        ]})

    # Filtering can empty the list once a user has logged everything today.
    content = "Pick a game to play!" if action_rows else ALL_PLAYED_MESSAGE

    return {
        "type": 4,
        "data": {
            "flags": 64,
            "content": content,
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
                'body': json.dumps(build_play_response(
                    body['channel_id'], interaction_user_id(body), interaction_guild_id(body))),
            }

    # MESSAGE_COMPONENT (type 3) — sticky buttons
    if body.get('type') == 3:
        custom_id = body.get('data', {}).get('custom_id')
        if custom_id == PLAY_BUTTON_CUSTOM_ID:
            return {
                'statusCode': 200,
                'headers': {'Content-Type': 'application/json'},
                'body': json.dumps(build_play_response(
                    body['channel_id'], interaction_user_id(body), interaction_guild_id(body))),
            }
        if custom_id == SCORES_BUTTON_CUSTOM_ID:
            return {
                'statusCode': 200,
                'headers': {'Content-Type': 'application/json'},
                'body': json.dumps(build_scoreboard_response(
                    body['channel_id'], interaction_guild_id(body))),
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
