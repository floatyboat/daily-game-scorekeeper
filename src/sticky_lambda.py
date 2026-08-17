import json
import os
import time
import traceback
from datetime import datetime
from zoneinfo import ZoneInfo
from collections import defaultdict

from game_parser import (
    compute_puzzle_numbers, build_games, top_game_buttons,
    match_message, make_timestamp_checker, STREAK_MIN,
)
from scoreboard import (
    DISCORD_API_BASE, FLAG_SUPPRESS_EMBEDS, FLAG_SUPPRESS_NOTIFICATIONS,
    make_session, fetch_messages, reference_date, is_scoreboard_message,
    is_sticky_message, build_avatar_pool, safe_guild_id, gather_streaks,
    PLAY_BUTTON_CUSTOM_ID, MORE_BUTTON_CUSTOM_ID, SCORES_BUTTON_CUSTOM_ID,
    STICKY_HEADING,
)
import store

# Global bot identity only -- per-server settings come from each guild's
# config item (see store.CONFIG_DEFAULTS), managed by /setup.
DISCORD_BOT_ID = os.getenv('DISCORD_BOT_ID') or 0
DISCORD_BOT_TOKEN = os.getenv('DISCORD_BOT_TOKEN')

_session = make_session(DISCORD_BOT_TOKEN)


def build_sticky_components(yesterday_url=None, game_buttons=(), show_more=False):
    """The sticky's rows: the action row, then the top-games shortcut row.

    game_buttons are the leading games in the app-wide order, so the row is the
    head of the Play list surfaced a tap earlier. Empty -- the guild's
    sticky_games is 0 (the default), or it has no games enabled -- just drops
    the row; Discord rejects an action row with no components.

    show_more adds the More button (the `/play all:true` view, everything
    tracked rather than today's draw) last, after the everyday buttons. Only
    worth a slot while a rotation is actually narrowing Play -- unrestricted,
    the two buttons would open the same list -- so run_guild gates it on that.
    """
    buttons = [
        {'type': 2, 'style': 1, 'label': 'Play', 'custom_id': PLAY_BUTTON_CUSTOM_ID},
        {'type': 2, 'style': 2, 'label': 'Scores', 'custom_id': SCORES_BUTTON_CUSTOM_ID},
    ]
    if yesterday_url:
        buttons.append({'type': 2, 'style': 5, 'label': 'Yesterday', 'url': yesterday_url})
    if show_more:
        buttons.append({'type': 2, 'style': 2, 'label': 'More',
                        'custom_id': MORE_BUTTON_CUSTOM_ID})
    rows = [{'type': 1, 'components': buttons}]
    if game_buttons:
        rows.append({'type': 1, 'components': list(game_buttons)})
    return rows


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

    Per-guild, via the `suppress_embeds` config field -- run_guild owns that
    gate and this stays the mechanism. No-op when the message has no embeds or
    already has the flag set. Requires MANAGE_MESSAGES for messages the bot
    didn't author; failures are swallowed so a missing perm or since-deleted
    message doesn't kill the run.
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


def _button_identity(rows):
    """Every button in row order, reduced to what we actually render.

    Discord echoes components back with extra server-set fields (component ids
    and the like), so compare this projection rather than the raw dicts.
    """
    return [(c.get('custom_id'), c.get('label'), c.get('url'))
            for row in (rows or []) for c in row.get('components', [])]


def _sticky_is_current(sticky, content, components):
    """True when the live sticky already renders exactly what we'd post now.

    Content plus every button, so a stale Yesterday link, a reshuffled or
    restreaked shortcut row, a row an admin has just resized or switched off,
    a More button today's rotation has just introduced (or a lapsed rotation
    has dropped), and a sticky posted before any of these buttons existed all
    force a repost.
    """
    if sticky.get('content', '') != content:
        return False
    return _button_identity(sticky.get('components')) == _button_identity(components)


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
                  link_yesterday=True, game_buttons=(), show_more=False):
    """Maintain exactly one sticky at the bottom of channel_id.

    No-op only when a single sticky is already the most recent message AND both
    its content and its buttons match what we'd render now — content comparison
    catches the day-transition case where the sticky is still at the bottom but
    shows yesterday's stats, and button comparison catches a Yesterday link gone
    stale behind a freshly posted scoreboard, or a shortcut row the day's plays
    have since reordered (or an admin has resized via /setup sticky).

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
    components = build_sticky_components(yesterday_url, game_buttons, show_more)

    if (len(stickies) == 1 and channel_messages
            and channel_messages[0]['id'] == stickies[0]['id']
            and _sticky_is_current(stickies[0], content, components)):
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


# One entry per guild whose last pass ended settled; run_guild's probe uses it
# to skip the full pass while nothing has moved. Process-lifetime state: the
# every-minute schedule keeps this container warm, so entries usually survive
# from one tick to the next and the common case collapses to one tiny fetch.
_probe_state = {}   # guild_id -> {'fingerprint', 'newest_id', 'expires'}
PROBE_MAX_AGE = 600

# Don't start another guild with less than this left on the clock; a typical
# pass is well under it, so the margin only ever trims the pathological runs.
DEADLINE_MARGIN_MS = 8000


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

    # Probe short-circuit: when the last full pass left the sticky settled and
    # neither the date nor the config has moved, a single-message fetch proving
    # "the newest message is still the settled sticky" also proves the parse
    # could not have changed -- no new plays, same window, same games -- so the
    # 200-message fetch, the regex pass, and the streak read are all skipped.
    # Bounded by PROBE_MAX_AGE so what a head probe can't see (an edit or
    # deletion of an older message, a changed avatar) still heals within
    # minutes rather than waiting on the next new message.
    gid = cfg['guild_id']
    fingerprint = f"{store.day_str(today)} {json.dumps(cfg, sort_keys=True, default=str)}"
    state = None if force else _probe_state.get(gid)
    if state and state['fingerprint'] == fingerprint \
            and state['expires'] > time.monotonic():
        probe = fetch_messages(_session, channel_id, limit=1)
        if probe and probe[0]['id'] == state['newest_id']:
            return 'unchanged (probe)'
    _probe_state.pop(gid, None)

    rotation = store.current_rotation(cfg, store.day_str(today))
    puzzle_numbers = compute_puzzle_numbers(today)
    games = build_games(puzzle_numbers, cfg['game_overrides'])
    checker = make_timestamp_checker(today, tz, cfg['hours_after_midnight'],
                                     cfg['time_window_hours'])

    messages = fetch_messages(_session, channel_id, limit=200)
    avatar_pool = build_avatar_pool(_session, messages, checker, cfg['guild_id'])

    results = defaultdict(dict)
    suppressed = 0
    for msg in messages:
        entries = match_message(msg, games, checker, avatar_hashes=avatar_pool)
        if not entries:
            continue
        if cfg['suppress_embeds'] and suppress_embeds(channel_id, msg):
            suppressed += 1
        for game_key, score, metadata, uid_override in entries:
            user_id = uid_override or msg.get('interaction_metadata', {}).get('user', {}).get('id') or msg['author']['id']
            results[game_key][user_id] = score
            puzzle_numbers.update(metadata)

    # Server-wide streak flair, bare fire+number at the end of the content
    # line -- kept alive today (live +1) or still extendable from yesterday.
    # Fail-open: no store, no flair.
    streaks = gather_streaks(cfg['guild_id'], today, results, games,
                             cfg['minimum_players'], include_players=False)
    server_streak = (streaks or {}).get('server', 0)

    # Shortcut row: the head of the Play list, same ordering and labels. Off by
    # default (sticky_games 0), and then the ordering pass never runs -- a
    # guild that doesn't want the row pays nothing to rank games for it. Only
    # rotation games: the row mirrors what Play lists. The content counts stay
    # unfiltered -- every play counts, on or off rotation.
    rot = set(rotation) if rotation is not None else None
    # More is the way back out to the games today's draw left behind, so it
    # rides on the same condition that narrows Play in the first place.
    show_more = rot is not None
    game_buttons = ()
    if cfg['sticky_games']:
        row_games = games if rot is None else [g for g in games if g.key in rot]
        game_buttons = top_game_buttons(row_games, results, streaks, cfg['sticky_games'])

    action = update_sticky(channel_id, messages, results, server_streak,
                           link_yesterday=cfg['daily_enabled'],
                           game_buttons=game_buttons, show_more=show_more)
    if action == 'unchanged' and not force:
        # 'unchanged' guarantees messages[0] is the single, settled sticky.
        _probe_state[gid] = {'fingerprint': fingerprint,
                             'newest_id': messages[0]['id'],
                             'expires': time.monotonic() + PROBE_MAX_AGE}
    note = f' (embeds suppressed: {suppressed})' if cfg['suppress_embeds'] else ''
    return f'{action}{note}'


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

    configs = [cfg for cfg in store.all_configs()
               if cfg['sticky_enabled'] and cfg['input_channel_id']]
    # A different starting guild each minute: if a run ever runs out of time,
    # the deferral below lands on different guilds each tick instead of
    # deterministically starving the tail of the partition order.
    if len(configs) > 1:
        offset = int(time.time() // 60) % len(configs)
        configs = configs[offset:] + configs[:offset]

    summary = {}
    for i, cfg in enumerate(configs):
        gid = cfg['guild_id']
        if context is not None \
                and context.get_remaining_time_in_millis() < DEADLINE_MARGIN_MS:
            # Stop cleanly rather than letting Lambda kill the run mid-guild;
            # the start-offset rotation above spreads the deferral around.
            print(f'sticky: out of time, deferring {len(configs) - i} guild(s)')
            for later in configs[i:]:
                summary[later['guild_id']] = 'deferred: out of time'
            break
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
