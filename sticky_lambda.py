import json
import os
from datetime import datetime
from zoneinfo import ZoneInfo
from collections import defaultdict

from game_parser import (
    compute_puzzle_numbers, build_game_regexes,
    match_message, make_timestamp_checker,
)
from scoreboard import (
    DISCORD_API_BASE, FLAG_SUPPRESS_EMBEDS, FLAG_SUPPRESS_NOTIFICATIONS,
    make_session, fetch_messages, reference_date, is_scoreboard_message,
)

DISCORD_BOT_ID = os.getenv('DISCORD_BOT_ID') or 0
DISCORD_BOT_TOKEN = os.getenv('DISCORD_BOT_TOKEN')
INPUT_CHANNEL_ID = os.getenv('INPUT_CHANNEL_ID')
TEST_CHANNEL_ID = os.getenv('TEST_CHANNEL_ID')

TIMEZONE = ZoneInfo(os.getenv('TIMEZONE') or 'UTC')
TIME_WINDOW_HOURS = int(os.getenv('TIME_WINDOW_HOURS') or 24)
HOURS_AFTER_MIDNIGHT = int(os.getenv('HOURS_AFTER_MIDNIGHT') or 0)

_session = make_session(DISCORD_BOT_TOKEN)


PLAY_BUTTON_CUSTOM_ID = 'sticky_play'
SCORES_BUTTON_CUSTOM_ID = 'sticky_scores'


def build_sticky_components(yesterday_url=None):
    buttons = [
        {'type': 2, 'style': 1, 'label': 'Play', 'custom_id': PLAY_BUTTON_CUSTOM_ID},
        {'type': 2, 'style': 2, 'label': 'Scores', 'custom_id': SCORES_BUTTON_CUSTOM_ID},
    ]
    if yesterday_url:
        buttons.append({'type': 2, 'style': 5, 'label': 'Yesterday', 'url': yesterday_url})
    return [{'type': 1, 'components': buttons}]


def send_sticky(channel_id, content, components):
    payload = {
        'content': content,
        'components': components,
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


def suppress_embeds(channel_id, message):
    """Strip URL previews on a matched game-score message.

    No-op when the message has no embeds or already has the flag set. Requires
    MANAGE_MESSAGES for messages the bot didn't author; failures are swallowed
    so a missing perm or since-deleted message doesn't kill the run.
    """
    if not message.get('embeds'):
        return False
    flags = message.get('flags') or 0
    if flags & FLAG_SUPPRESS_EMBEDS:
        return False
    url = f'{DISCORD_API_BASE}/channels/{channel_id}/messages/{message["id"]}'
    r = _session.patch(url, json={'flags': flags | FLAG_SUPPRESS_EMBEDS})
    return r.ok


def find_sticky(messages):
    """Locate the bot's most recent non-scoreboard message — that's our sticky.

    The daily scoreboard post and the sticky can share a channel, so we skip
    scoreboard messages (identified by the v2-components flag) when scanning.
    /play replies are ephemeral and never appear in fetch_messages.
    """
    for msg in messages:
        if is_scoreboard_message(msg):
            continue
        author = msg.get('author', {})
        if DISCORD_BOT_ID and author.get('id') == str(DISCORD_BOT_ID):
            return msg
        if not DISCORD_BOT_ID and author.get('bot'):
            return msg
    return None


def find_latest_scoreboard_id(messages):
    for msg in messages:
        if is_scoreboard_message(msg):
            return msg['id']
    return None


def count_unique_players(results):
    players = set()
    for game_scores in results.values():
        players.update(game_scores.keys())
    return len(players)


def _sticky_is_current(sticky, content, want_url):
    if sticky.get('content', '') != content:
        return False
    btns = [c for row in (sticky.get('components') or [])
            for c in row.get('components', [])]
    if not any(c.get('custom_id') == PLAY_BUTTON_CUSTOM_ID for c in btns):
        return False
    existing_url = next((c.get('url') for c in btns if c.get('style') == 5), None)
    return existing_url == want_url


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
    yesterday's stats, and URL comparison catches the case where the daily
    scoreboard just posted and the Yesterday link is now stale.
    """
    sticky = find_sticky(channel_messages)
    content = build_sticky_content(results)

    yesterday_url = None
    scoreboard_id = find_latest_scoreboard_id(channel_messages)
    if scoreboard_id:
        # Discord's client routes by channel_id/message_id; the guild slot
        # accepts @me even for guild messages.
        yesterday_url = f'https://discord.com/channels/@me/{channel_id}/{scoreboard_id}'
    components = build_sticky_components(yesterday_url)

    if (sticky and channel_messages
            and channel_messages[0]['id'] == sticky['id']
            and _sticky_is_current(sticky, content, yesterday_url)):
        return 'unchanged'

    if sticky:
        delete_message(channel_id, sticky['id'])

    send_sticky(channel_id, content, components)
    return 'reposted' if sticky else 'created'


def lambda_handler(event, context):
    is_test = isinstance(event, dict) and 'test' in event

    # Active window: [HOURS_AFTER_MIDNIGHT, midnight) in TIMEZONE. The daily
    # scoreboard fires at HOURS_AFTER_MIDNIGHT and posts yesterday's summary;
    # before that point we'd be tracking the previous day's already-finalized
    # leaders, so stay dormant until it runs. Test events bypass this guard.
    if not is_test and datetime.now(TIMEZONE).hour < HOURS_AFTER_MIDNIGHT:
        return {'statusCode': 200, 'body': json.dumps('Outside active window')}

    today = reference_date(datetime.now(), TIMEZONE, HOURS_AFTER_MIDNIGHT)
    puzzle_numbers = compute_puzzle_numbers(today)
    game_regexes = build_game_regexes(puzzle_numbers)
    checker = make_timestamp_checker(today, TIMEZONE, HOURS_AFTER_MIDNIGHT, TIME_WINDOW_HOURS)

    # Operate end-to-end on a single channel: counts and the sticky live
    # together. Test events redirect to TEST_CHANNEL_ID so local runs never
    # touch real user messages.
    channel_id = TEST_CHANNEL_ID if is_test else INPUT_CHANNEL_ID
    messages = fetch_messages(_session, channel_id, limit=200)

    results = defaultdict(dict)
    suppressed = 0
    for msg in messages:
        entries = match_message(msg, game_regexes, checker)
        if not entries:
            continue
        if suppress_embeds(channel_id, msg):
            suppressed += 1
        for game_key, score, metadata, uid_override in entries:
            user_id = uid_override or msg.get('interaction_metadata', {}).get('user', {}).get('id') or msg['author']['id']
            results[game_key][user_id] = score
            puzzle_numbers.update(metadata)

    action = update_sticky(channel_id, messages, results)

    return {'statusCode': 200, 'body': json.dumps(f'Sticky: {action} (embeds suppressed: {suppressed})')}


if __name__ == '__main__':
    print(lambda_handler({'test': True}, None))
