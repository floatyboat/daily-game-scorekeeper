import json
import os
import time
import traceback
from datetime import datetime
from zoneinfo import ZoneInfo

from game_parser import (format_scoreboard_components, make_timestamp_checker,
                         build_games, points_per_game, compute_puzzle_numbers,
                         next_rotation, game_sort_key, game_link_button,
                         GAME_SPECS, spec_enabled)
from scoreboard import (
    DISCORD_API_BASE, make_session, fetch_messages, reference_date,
    parse_results, build_avatar_pool, is_scoreboard_message, gather_streaks,
    rotation_context, FLAG_SUPPRESS_EMBEDS, FLAG_SUPPRESS_NOTIFICATIONS,
    MAX_BUTTONS_PER_ROW, MAX_ACTION_ROWS,
)
import store

# Global bot identity only -- every per-server setting (channels, timezone,
# schedule, games) lives in the guild's config item and is managed by /setup.
DISCORD_BOT_TOKEN = os.getenv('DISCORD_BOT_TOKEN')

_session = make_session(DISCORD_BOT_TOKEN)


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


def persist_results(cfg, results, puzzle_numbers, ref_date, games, rotation=None):
    """SPEC.md write path: freeze the day and fold streak aggregates.

    Runs BEFORE the scoreboard renders (the board displays the exact streaks
    this fold produces, including break callouts) and never raises --
    persistence problems must not break the user-facing post, which simply
    goes out streak-less. Test invocations parse the same real input channels
    and every store write is idempotent, so they persist too, which keeps
    this path covered by the standard post-change test event.
    """
    try:
        day = store.day_str(ref_date)
        # Doubles as the streak-eligibility signal: finalize_day counts a play
        # only where points landed.
        points_by_game = points_per_game(results, games, cfg['minimum_players'])
        archived = store.write_day(cfg['guild_id'], day, results, points_by_game,
                                   puzzle_numbers, rotation)
        stats = store.finalize_day(cfg['guild_id'], day, results, points_by_game,
                                   [g.key for g in games])
        return (f'store: day={day} archived={archived} '
                f'aggs updated={stats["updated"]} skipped={stats["skipped"]}')
    except Exception as e:
        return f'store: FAILED {type(e).__name__}: {e}'


def announce_rotation(channel_id, rotation, games, streaks):
    """Post the "Today's games" follow-up under the board: a bare header over
    the new rotation as link buttons in the app-wide order. The buttons carry
    the emoji-title labels themselves, so the content repeats none of them.

    Deliberately a plain message, NOT flag 32768: is_scoreboard_message() keys
    on that flag, so a components-v2 follow-up would hijack the sticky's
    Yesterday link and the posted-today dedup scan. A plain message still
    carries link-button rows, and none of them is custom_id 'sticky_play', so
    the sticky pass never mistakes it for a sticky either. Silent and
    un-embedded -- the pinned board sits directly above -- and never pinned.
    """
    rot = set(rotation)
    todays = sorted((g for g in games if g.key in rot),
                    key=lambda g: game_sort_key(g, {}, streaks))
    if not todays:
        return
    game_streaks = (streaks or {}).get('games', {})
    buttons = [game_link_button(g, game_streaks.get(g.key, 0)) for g in todays]
    rows = [{'type': 1, 'components': buttons[i:i + MAX_BUTTONS_PER_ROW]}
            for i in range(0, len(buttons), MAX_BUTTONS_PER_ROW)][:MAX_ACTION_ROWS]
    response = _session.post(f'{DISCORD_API_BASE}/channels/{channel_id}/messages', json={
        'content': "\U0001F3AE **Today's games:**",
        'components': rows,
        'flags': FLAG_SUPPRESS_EMBEDS | FLAG_SUPPRESS_NOTIFICATIONS,
        'allowed_mentions': {'parse': []},
    })
    response.raise_for_status()


def scoreboard_posted_today(cfg, tz, now_local, input_messages):
    """True when today's board is already in the output channel.

    Belt and braces under the hourly schedule: last_posted_day is the primary
    gate, this catches a marker lost to a partial failure (or a manual post)
    by looking for a components-v2 scoreboard posted today, guild-local.
    """
    if cfg['output_channel_id'] == cfg['input_channel_id']:
        candidates = input_messages
    else:
        candidates = fetch_messages(_session, cfg['output_channel_id'], limit=10)
    for msg in candidates[:10]:
        if not is_scoreboard_message(msg):
            continue
        if datetime.fromisoformat(msg['timestamp']).astimezone(tz).date() == now_local.date():
            return True
    return False


def process_guild(cfg, is_test, test_channel_id):
    """Post one guild's daily scoreboard if it is due. Returns a summary string.

    Real runs gate on the guild's local post hour and last_posted_day, so the
    hourly schedule fires this at each guild's own morning exactly once. Test
    runs skip the gates, post to the test channel, never pin, and never
    advance last_posted_day -- but they still persist (idempotently), keeping
    the store path covered by the routine post-change test event.
    """
    gid = cfg['guild_id']
    t0 = time.time()

    def note(msg):
        print(f'[guild {gid} t+{time.time() - t0:.2f}s] {msg}')

    if not cfg['daily_enabled']:
        return 'daily scoreboard disabled'
    if not cfg['input_channel_id'] or not (cfg['output_channel_id'] or is_test):
        return 'channels not configured (run /setup)'

    tz = ZoneInfo(cfg['timezone'])
    now_local = datetime.now(tz)
    yesterday = reference_date(now_local, tz, cfg['hours_after_midnight'], days_back=1)
    day = store.day_str(yesterday)
    today_day = store.next_day_str(day)
    # The rotation that governed the day being scored (or None: unrestricted).
    # Before the first draw ever lands, and after any gap, this is None -- the
    # board scores everything and the rotation starts from today instead.
    overrides, rotation, off_mode = rotation_context(cfg, day)

    if not is_test:
        post_hour = store.post_hour(cfg)
        if now_local.hour < post_hour:
            return f'waiting for {post_hour:02d}:00 local'
        if cfg['last_posted_day'] and cfg['last_posted_day'] >= day:
            return f'already posted {day}'

    messages = fetch_messages(_session, cfg['input_channel_id'],
                              limit=cfg['hundreds_of_messages'] * 100)
    note(f'fetched {len(messages)} messages')
    if not messages:
        return 'no messages in input channel'

    if not is_test and scoreboard_posted_today(cfg, tz, now_local, messages):
        store.set_last_posted(gid, day)   # heal the marker so later ticks skip cheaply
        # The rotation draw rides the post, so heal it the same way: a stale
        # rotation_day means the crashed (or manual) post never reached the
        # announce+persist tail. Persist-first here, the reverse of the main
        # path: the loss window then costs a duplicate announcement instead of
        # a scored day nobody was told about -- the cheaper mistake -- and
        # set_rotation's monotonic condition makes a double-fired heal a no-op.
        if cfg['rotation_enabled'] and (cfg['rotation_day'] or '') < today_day:
            enabled_keys = [s.key for s in GAME_SPECS
                            if spec_enabled(s, cfg['game_overrides'])]
            heal_rot = next_rotation(enabled_keys, cfg['rotation_count'], 'random',
                                     cfg['rotation_min_players'], None, {})
            if heal_rot:
                store.set_rotation(gid, today_day, heal_rot)
                announce_rotation(cfg['output_channel_id'], heal_rot,
                                  build_games(compute_puzzle_numbers(yesterday),
                                              cfg['game_overrides']), None)
        return f'scoreboard already in channel; marked {day} posted'

    checker = make_timestamp_checker(yesterday, tz, cfg['hours_after_midnight'],
                                     cfg['time_window_hours'])
    avatar_pool = build_avatar_pool(_session, messages, checker, gid)
    note(f'avatar pool has {len(avatar_pool)} users')

    results, puzzle_numbers = parse_results(
        messages, yesterday, tz, cfg['hours_after_midnight'], cfg['time_window_hours'],
        avatar_hashes=avatar_pool, game_overrides=overrides,
    )
    note(f'parsed {sum(len(v) for v in results.values())} game results')

    games = build_games(puzzle_numbers, overrides)
    note(persist_results(cfg, results, puzzle_numbers, yesterday, games, rotation))

    streaks = gather_streaks(gid, yesterday, results, games, cfg['minimum_players'])
    components = format_scoreboard_components(results, yesterday, puzzle_numbers,
                                              minimum_players=cfg['minimum_players'],
                                              streaks=streaks,
                                              game_overrides=overrides,
                                              rotation=rotation, rotation_off=off_mode)

    # Draw the new day's rotation before posting, so its announcement can ride
    # directly under the board. Yesterday's list seeds the swap only when it
    # actually governed the day just scored; anything stale means a fresh draw.
    next_rot = None
    if cfg['rotation_enabled']:
        enabled_keys = [s.key for s in GAME_SPECS
                        if spec_enabled(s, cfg['game_overrides'])]
        if cfg['rotation_day'] == today_day:
            # Re-run after a crash that got past set_rotation: keep the draw.
            next_rot = [k for k in cfg['rotation_games'] if k in set(enabled_keys)]
        else:
            prev = cfg['rotation_games'] if cfg['rotation_day'] == day else None
            next_rot = next_rotation(enabled_keys, cfg['rotation_count'],
                                     cfg['rotation_mode'], cfg['rotation_min_players'],
                                     prev, results)

    channel = test_channel_id if is_test else cfg['output_channel_id']
    response = send_message(channel, components=components)
    note('posted scoreboard')
    if next_rot:
        # Buttons need the full enabled list -- under off mode 'skipped' the
        # parse universe (games) was narrowed to yesterday's rotation.
        announce_games = (games if off_mode != 'skipped'
                          else build_games(puzzle_numbers, cfg['game_overrides']))
        announce_rotation(channel, next_rot, announce_games, streaks)
        note('announced rotation')

    if is_test:
        return f'TEST: posted {day} scoreboard to {channel}'
    pin_message(channel, response['id'])
    if next_rot:
        store.set_rotation(gid, today_day, next_rot)
    store.set_last_posted(gid, day)
    return f'posted {day}'


def lambda_handler(event, context):
    """Hourly tick: post the daily scoreboard for every guild that is due.

    The guild list comes from the table each invocation, so a server onboarded
    via /setup is picked up with no deploy or schedule change. Event keys:
      test             any value: post to the test channel, skip gates, no pin
      test_channel_id  overrides the TEST_CHANNEL_ID env for this run
      guild_id         only process this guild
    """
    event = event if isinstance(event, dict) else {}
    is_test = 'test' in event
    test_channel_id = event.get('test_channel_id') or os.getenv('TEST_CHANNEL_ID')
    if is_test and not test_channel_id:
        return {'statusCode': 400,
                'body': json.dumps('test mode needs test_channel_id in the event '
                                   'or TEST_CHANNEL_ID in the env')}

    configs = store.all_configs()
    if event.get('guild_id'):
        configs = [c for c in configs if c['guild_id'] == str(event['guild_id'])]

    summary = {}
    for cfg in configs:
        gid = cfg['guild_id']
        try:
            summary[gid] = process_guild(cfg, is_test, test_channel_id)
        except Exception as e:
            traceback.print_exc()
            summary[gid] = f'FAILED {type(e).__name__}: {e}'
        print(f'guild {gid}: {summary[gid]}')

    if not summary:
        summary = 'no guilds configured'
    return {'statusCode': 200, 'body': json.dumps(summary)}


if __name__ == '__main__':
    print(lambda_handler({'test': True}, None))
