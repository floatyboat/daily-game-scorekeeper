import json
import os
import traceback
from datetime import datetime
from zoneinfo import ZoneInfo
from collections import defaultdict

from game_parser import (
    compute_puzzle_numbers, build_games,
    match_message, make_timestamp_checker, STREAK_MIN,
)
from scoreboard import (
    DISCORD_API_BASE, FLAG_SUPPRESS_EMBEDS, FLAG_SUPPRESS_NOTIFICATIONS,
    make_session, fetch_messages, reference_date, is_scoreboard_message,
    is_sticky_message, build_avatar_pool, safe_guild_id, gather_streaks,
    PLAY_BUTTON_CUSTOM_ID, SCORES_BUTTON_CUSTOM_ID, STICKY_HEADING,
)
import store

# Global bot identity only -- per-server settings come from each guild's
# config item (see store.CONFIG_DEFAULTS), managed by /setup.
DISCORD_BOT_ID = os.getenv('DISCORD_BOT_ID') or 0
DISCORD_BOT_TOKEN = os.getenv('DISCORD_BOT_TOKEN')

_session = make_session(DISCORD_BOT_TOKEN)


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


def find_stickies(messages):
    """Every bot sticky in the channel, newest first (normally exactly one).

    Returning *all* matches rather than just the newest is what lets
    update_sticky collapse back to a single sticky. A scheduled run that
    double-fires can briefly post two stickies; a single-match scan would then
    delete only the newer one on each later run and orphan the older "No scores
    yet today" post indefinitely. /play replies are ephemeral and never appear
    in fetch_messages.
    """
    return [m for m in messages if is_sticky_message(m, DISCORD_BOT_ID)]


def find_latest_scoreboard_id(messages):
    for msg in messages:
        if is_scoreboard_message(msg):
            return msg['id']
    return None


def _sticky_is_current(sticky, content, want_url):
    if sticky.get('content', '') != content:
        return False
    btns = [c for row in (sticky.get('components') or [])
            for c in row.get('components', [])]
    if not any(c.get('custom_id') == PLAY_BUTTON_CUSTOM_ID for c in btns):
        return False
    existing_url = next((c.get('url') for c in btns if c.get('style') == 5), None)
    return existing_url == want_url


def build_sticky_content(results, server_streak=0):
    # Distinct games that have at least one score, then every play logged
    # against them (each player x game result counts once).
    game_count = sum(1 for scores in results.values() if scores)
    play_count = sum(len(scores) for scores in results.values())
    flair = f' · \U0001F525{server_streak}' if server_streak >= STREAK_MIN else ''
    if play_count == 0:
        # Flair stays on the empty state on purpose: "no scores yet, the
        # server streak is on the line" is the strongest nudge of the day.
        return f"{STICKY_HEADING}\nNo scores yet today{flair}"
    g = 'game' if game_count == 1 else 'games'
    p = 'play' if play_count == 1 else 'plays'
    return f"{STICKY_HEADING}\n{game_count} {g} · {play_count} {p} today{flair}"


def update_sticky(channel_id, channel_messages, results, server_streak=0,
                  link_yesterday=True):
    """Maintain exactly one sticky at the bottom of channel_id.

    No-op only when a single sticky is already the most recent message AND its
    content matches what we'd render now — content comparison catches the
    day-transition case where the sticky is still at the bottom but shows
    yesterday's stats, and URL comparison catches the case where the daily
    scoreboard just posted and the Yesterday link is now stale.

    link_yesterday=False (guild has the daily scoreboard disabled) drops the
    Yesterday button even when an old board is still in the channel — the
    freshest link would only ever point at a stale day.

    Otherwise delete *every* existing sticky before posting a fresh one. The
    morning scoreboard de-positions the sticky and staleness forces a repost; a
    double-fire of that run leaves two stickies, and deleting only the newest
    (the old behavior) orphaned the older "No scores yet today" post forever.
    Deleting all matches, plus the post-write sweep below, collapses any such
    duplicates back to one.
    """
    stickies = find_stickies(channel_messages)
    content = build_sticky_content(results, server_streak)

    yesterday_url = None
    if link_yesterday:
        scoreboard_id = find_latest_scoreboard_id(channel_messages)
        if scoreboard_id:
            # Discord's client routes by channel_id/message_id; the guild slot
            # accepts @me even for guild messages.
            yesterday_url = f'https://discord.com/channels/@me/{channel_id}/{scoreboard_id}'
    components = build_sticky_components(yesterday_url)

    if (len(stickies) == 1 and channel_messages
            and channel_messages[0]['id'] == stickies[0]['id']
            and _sticky_is_current(stickies[0], content, yesterday_url)):
        return 'unchanged'

    for old in stickies:
        delete_message(channel_id, old['id'])

    send_sticky(channel_id, content, components)

    # Close the double-fire window: a concurrent run can post a second sticky in
    # parallel with ours. Re-read the tail and drop everything but the newest so
    # the channel converges to one — both runs agree on "keep newest", and
    # delete_message swallows the 404 when the other already removed it.
    extra = find_stickies(fetch_messages(_session, channel_id, limit=10))
    for dup in extra[1:]:
        delete_message(channel_id, dup['id'])

    if not stickies:
        return 'created'
    return 'collapsed' if len(stickies) > 1 else 'reposted'


def run_guild(cfg, force=False):
    """One guild's sticky pass: parse today's plays and settle the sticky.

    Active window: [post hour, midnight) guild-local. The daily scoreboard
    posts at the guild's post hour and summarizes yesterday; before that point
    we'd be tracking the previous day's already-finalized leaders, so stay
    dormant until it has had its chance. force (test runs) bypasses the guard.
    """
    channel_id = cfg['input_channel_id']
    tz = ZoneInfo(cfg['timezone'])
    now_local = datetime.now(tz)

    if not force and now_local.hour < store.post_hour(cfg):
        return 'outside active window'

    today = reference_date(now_local, tz, cfg['hours_after_midnight'])
    puzzle_numbers = compute_puzzle_numbers(today)
    games = build_games(puzzle_numbers, cfg['game_overrides'])
    checker = make_timestamp_checker(today, tz, cfg['hours_after_midnight'],
                                     cfg['time_window_hours'])

    messages = fetch_messages(_session, channel_id, limit=200)
    avatar_pool = build_avatar_pool(_session, messages, checker, cfg['wordle_bot_id'])

    results = defaultdict(dict)
    suppressed = 0
    for msg in messages:
        entries = match_message(msg, games, checker,
                                wordle_bot_id=cfg['wordle_bot_id'], avatar_hashes=avatar_pool)
        if not entries:
            continue
        if suppress_embeds(channel_id, msg):
            suppressed += 1
        for game_key, score, metadata, uid_override in entries:
            user_id = uid_override or msg.get('interaction_metadata', {}).get('user', {}).get('id') or msg['author']['id']
            results[game_key][user_id] = score
            puzzle_numbers.update(metadata)

    # Server-wide streak flair, bare fire+number at the end of the content
    # line -- kept alive today (live +1) or still extendable from yesterday.
    # Fail-open: no store, no flair.
    streaks = gather_streaks(cfg['guild_id'], today, results,
                             [g.key for g in games], include_players=False)
    server_streak = (streaks or {}).get('server', 0)

    action = update_sticky(channel_id, messages, results, server_streak,
                           link_yesterday=cfg['daily_enabled'])
    return f'{action} (embeds suppressed: {suppressed})'


def lambda_handler(event, context):
    """Frequent tick: settle the sticky for every guild with one enabled.

    The guild list comes from the table each invocation, so onboarding a
    server (/setup) needs no deploy or schedule change. Test events operate on
    the test channel with a default config so local runs never touch real user
    messages: {'test': true} plus optional 'channel_id' and any config-field
    overrides (e.g. 'daily_enabled': false to preview the linkless sticky).
    """
    event = event if isinstance(event, dict) else {}

    if 'test' in event:
        cfg = store.default_config()
        cfg.update({k: v for k, v in event.items() if k in store.CONFIG_DEFAULTS})
        cfg['input_channel_id'] = (event.get('channel_id')
                                   or cfg['input_channel_id']
                                   or os.getenv('TEST_CHANNEL_ID'))
        if not cfg['input_channel_id']:
            return {'statusCode': 400,
                    'body': json.dumps('test mode needs channel_id in the event '
                                       'or TEST_CHANNEL_ID in the env')}
        # A default config has no guild_id; resolve it from the test channel so
        # run_guild reads it off cfg exactly as it does for a stored config.
        cfg['guild_id'] = safe_guild_id(_session, cfg['input_channel_id'])
        result = run_guild(cfg, force=True)
        return {'statusCode': 200, 'body': json.dumps(f'Sticky (test): {result}')}

    summary = {}
    for cfg in store.all_configs():
        gid = cfg['guild_id']
        if not cfg['sticky_enabled'] or not cfg['input_channel_id']:
            continue
        try:
            summary[gid] = run_guild(cfg)
        except Exception as e:
            traceback.print_exc()
            summary[gid] = f'FAILED {type(e).__name__}: {e}'

    if not summary:
        summary = 'no guilds with a sticky to run'
    return {'statusCode': 200, 'body': json.dumps(summary)}


if __name__ == '__main__':
    print(lambda_handler({'test': True}, None))
