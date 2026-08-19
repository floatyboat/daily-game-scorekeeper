import json
import os
import sys
import time
import traceback
from datetime import datetime
from zoneinfo import ZoneInfo

from game_parser import (format_scoreboard_components, make_timestamp_checker,
                         build_games, points_per_game, compute_puzzle_numbers,
                         next_rotation, game_sort_key, game_link_button,
                         scoring_players, GAME_SPECS, spec_enabled)
from scoreboard import (
    DISCORD_API_BASE, make_session, fetch_messages, reference_date,
    parse_results, build_avatar_pool, build_name_map, is_scoreboard_message,
    gather_streaks,
    FLAG_SUPPRESS_EMBEDS, FLAG_SUPPRESS_NOTIFICATIONS, FLAG_IS_COMPONENTS_V2,
    MAX_BUTTONS_PER_ROW, MAX_ACTION_ROWS,
)
import store

# Global bot identity only -- every per-server setting (channels, timezone,
# schedule, games) lives in the guild's config item and is managed by /setup.
DISCORD_BOT_TOKEN = os.getenv('DISCORD_BOT_TOKEN')

_session = make_session(DISCORD_BOT_TOKEN)


def send_message(channel_id, components):
    url = f'{DISCORD_API_BASE}/channels/{channel_id}/messages'
    payload = {
        'components': components,
        'flags': FLAG_IS_COMPONENTS_V2,
        'allowed_mentions': {'parse': ['users']},
    }
    response = _session.post(url, json=payload)
    response.raise_for_status()

    return response.json()


def pin_message(channel_id, message_id):
    url = f'{DISCORD_API_BASE}/channels/{channel_id}/messages/pins/{message_id}'
    _session.put(url)


def persist_results(cfg, results, puzzle_numbers, ref_date, games, rotation=None,
                    write=True):
    """SPEC.md write path: freeze the day and fold streak aggregates.

    Runs BEFORE the scoreboard renders (the board displays the exact streaks
    this fold produces, including break callouts) and never raises --
    persistence problems must not break the user-facing post, which simply
    goes out streak-less.

    write=False does everything except touch the table. A throwaway parse must
    never land on real history: write_day's put_item is unconditional, and a
    parse made after the input channel has scrolled past the fetch window sees
    fewer messages than the run that first wrote the day -- overwriting a
    complete archive with a partial one. (The aggregates were always safe;
    _put_guarded's finalized_through condition rejects a replay.) The scoring
    fold still runs either way, so the parse -> points path stays covered by
    the routine post-change test event.
    """
    try:
        day = store.day_str(ref_date)
        # Doubles as the streak-eligibility signal: finalize_day counts a play
        # only where points landed.
        points_by_game = points_per_game(results, games, cfg['minimum_players'])
        if not write:
            n_scored = sum(1 for pts in points_by_game.values() if pts)
            return f'store: dry run, would write day={day} ({n_scored} scored games)'
        archived = store.write_day(cfg['guild_id'], day, results, points_by_game,
                                   puzzle_numbers, rotation)
        stats = store.finalize_day(cfg['guild_id'], day, results, points_by_game,
                                   [g.key for g in games])
        return (f'store: day={day} archived={archived} '
                f'aggs updated={stats["updated"]} skipped={stats["skipped"]}')
    except Exception as e:
        return f'store: FAILED {type(e).__name__}: {e}'


def announce_rotation(channel_id, rotation, games, streaks):
    """Post "Today's games": a bare header over the new rotation as link buttons
    in the app-wide order. The buttons carry the emoji-title labels themselves,
    so the content repeats none of them.

    Goes out on any tick that draws the rotation, and on any tick that posts
    the board -- the post that notifies the channel, so the list stands next to
    it rather than being hours stale by the time anyone is looking. A guild
    whose post hour is later than its day start therefore gets it twice a day;
    one posting at day start (the default, post_hour falling back to
    hours_after_midnight) has both on one tick and gets it once.

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


def post_blocked(cfg, is_test, now_local, day):
    """Why the daily board is not posting this tick, or None when it is due.

    Real runs gate on the guild's local post hour and last_posted_day, so the
    hourly schedule fires the board at each guild's own morning exactly once.
    Test runs skip those two but still respect the daily_enabled switch, so a
    paused guild stays out of the test post the same way it stays out of the
    real one. Only the board is gated here -- the rotation stage runs whatever
    this returns.
    """
    if not cfg['daily_enabled']:
        return 'daily scoreboard disabled'
    if not (cfg['output_channel_id'] or is_test):
        return 'output channel not configured (run /setup)'
    if is_test:
        return None
    post_hour = store.post_hour(cfg)
    if now_local.hour < post_hour:
        return f'waiting for {post_hour:02d}:00 local'
    if cfg['last_posted_day'] and cfg['last_posted_day'] >= day:
        return f'already posted {day}'
    return None


def draw_rotation(cfg, is_test, day, today_day, results):
    """Draw and persist today's rotation. The list, or None with nothing to
    draw from.

    The day-start stage, deliberately independent of the board: it is due as
    soon as the scoring day has rolled over past the stored draw, so the hourly
    tick lands it at each guild's own day start -- hours before the board for a
    guild whose post hour is later, and at all for a guild that has the board
    switched off. On a tick carrying both stages it runs FIRST, so the day's
    games are settled whatever then happens to the post.

    `day` (the closed day) seeds swap mode: its participation is the earn-in
    signal, and its list seeds the swap only when that list actually governed
    it.

    Persists here; the announcement follows in process_guild, after the board.
    A crash between the two costs that announcement -- recoverable, since any
    later tick that posts the board announces the stored list -- where the
    reverse order would let the next tick draw a DIFFERENT set an hour into a
    day whose games have already been listed. Test runs never persist, so
    repeated test runs leave real rotation state alone (like last_posted_day,
    it only advances on a real run).
    """
    enabled_keys = [s.key for s in GAME_SPECS if spec_enabled(s, cfg['game_overrides'])]
    prev = cfg['rotation_games'] if cfg['rotation_day'] == day else None
    rotation = next_rotation(enabled_keys, cfg['rotation_count'], cfg['rotation_mode'],
                             cfg['rotation_min_players'], prev, results or {})
    if not rotation:
        return None
    if not is_test:
        store.set_rotation(cfg['guild_id'], today_day, rotation,
                           cfg['rotation_day'], cfg['rotation_games'])
    return rotation


def stored_rotation(cfg, today_day):
    """Today's already-drawn list, filtered to the games still enabled, or None
    when the stored draw is not today's (or has nothing left in it).

    What a tick that did not draw announces: the board's tick at a later post
    hour, and a test run over a day already drawn for real -- the live set
    rather than an invented one.
    """
    if cfg['rotation_day'] != today_day:
        return None
    enabled = {s.key for s in GAME_SPECS if spec_enabled(s, cfg['game_overrides'])}
    return [k for k in cfg['rotation_games'] if k in enabled] or None


def process_guild(cfg, is_test, test_channel_id, days_back=1):
    """Post one guild's daily scoreboard if it is due, then settle its rotation.

    Two stages on one hourly tick, sharing a single parse of the closed day:
    the rotation draw (draw_rotation: day start, independent of the board) and
    the board (post_blocked: post hour, last_posted_day). The draw goes first,
    and "Today's games" is announced after both -- on any tick that drew, and
    on any tick that posted the board, so a guild whose post hour is later gets
    it at day start and again under the scoreboard. Test runs skip the
    board's timing gates, post to the test channel, never pin, and write
    nothing at all: not last_posted_day, not the rotation, not the day archive.
    They read the real table and parse the real input channel, so what they
    render is exactly what a live run would -- they just leave no trace.

    days_back selects the day the board scores, counting back from the guild's
    current day: 1 (the default, and the only value the schedule ever uses) is
    the closed day. 0 scores today, which is a preview only -- the day is still
    open, so it is rendered but never persisted; see the guard below.
    """
    gid = cfg['guild_id']
    t0 = time.time()

    def note(msg):
        print(f'[guild {gid} t+{time.time() - t0:.2f}s] {msg}')

    if not cfg['input_channel_id']:
        return 'input channel not configured (run /setup)'

    tz = ZoneInfo(cfg['timezone'])
    now_local = datetime.now(tz)
    today = reference_date(now_local, tz, cfg['hours_after_midnight'])
    scored = reference_date(now_local, tz, cfg['hours_after_midnight'],
                            days_back=days_back)
    day = store.day_str(scored)
    today_day = store.day_str(today)
    # The rotation that governed the day being scored (or None: unrestricted).
    # Today's draw has usually already shifted it into the previous slot by now;
    # before the first draw ever lands, and after any gap, this is None -- the
    # board scores everything and the rotation starts from today instead.
    rotation = store.current_rotation(cfg, day)

    blocked = post_blocked(cfg, is_test, now_local, day)
    board_due = blocked is None
    # Monotonic in the day, exactly like set_rotation's condition: a stored draw
    # that already names today (or, after a timezone or day-start edit moved the
    # boundary, a later day) is left alone rather than redrawn hourly.
    draw_due = cfg['rotation_enabled'] and today_day > (cfg['rotation_day'] or '')
    # Test runs settle the rotation whatever the clock says: a throwaway draw on
    # a fresh day, the live set otherwise, and neither is ever written.
    rotation_due = draw_due or (cfg['rotation_enabled'] and is_test)
    # The rotation stage wants the same parse the board does, for two reasons.
    # Swap mode earns membership from the closed day's participation -- but only
    # when a rotation it can seed from actually governed that day. Every mode
    # needs it for the announcement's streak ordering: the aggregates are not
    # folded through the closed day until the board runs at post hour, which is
    # hours after this tick, so the parse is the only record of that day here.
    needs_counts = rotation_due
    if not board_due and not rotation_due:
        return blocked

    # One fetch and one parse of the closed day, shared by both stages.
    messages, results, streaks = None, None, None
    if board_due or needs_counts:
        messages = fetch_messages(_session, cfg['input_channel_id'],
                                  limit=cfg['hundreds_of_messages'] * 100)
        note(f'fetched {len(messages)} messages')

    if board_due:
        if not messages:
            board_due, blocked = False, 'no messages in input channel'
        elif not is_test and scoreboard_posted_today(cfg, tz, now_local, messages):
            # Heal a marker lost to a partial failure (or a manual post) so
            # later ticks skip cheaply. The rotation draw is not part of the
            # post any more, so it needs no healing of its own -- it lands
            # below on its own schedule. Checked before the avatar pool and the
            # parse, so a healed tick pays for neither.
            store.set_last_posted(gid, day)
            board_due = False
            blocked = f'scoreboard already in channel; marked {day} posted'

    if board_due or needs_counts:
        checker = make_timestamp_checker(scored, tz, cfg['hours_after_midnight'],
                                         cfg['time_window_hours'])
        avatar_pool = build_avatar_pool(_session, messages, checker, gid)
        note(f'avatar pool has {len(avatar_pool)} users')
        results, puzzle_numbers = parse_results(
            messages, scored, tz, cfg['hours_after_midnight'], cfg['time_window_hours'],
            avatar_hashes=avatar_pool, game_overrides=cfg['game_overrides'],
        )
        note(f'parsed {sum(len(v) for v in results.values())} game results')

    parts, response = [], None

    # Day start first: the draw lands and persists before the board goes out,
    # so a post that fails can never cost the day its rotation. Announcing is a
    # separate step below -- it has to sit UNDER the board in the channel, and
    # it wants the board's streak bundle when there is one.
    rotation_today = None
    if draw_due:
        rotation_today = draw_rotation(cfg, is_test, day, today_day, results)
        drew = (f'rotation {today_day}: {", ".join(rotation_today)}' if rotation_today
                else 'rotation: no games to draw from')
        note(drew)
        parts.append(drew)

    if board_due:
        games = build_games(puzzle_numbers, cfg['game_overrides'])
        # Two independent reasons to hold the write back: a test run must leave
        # the table exactly as it found it, and an open day has no business
        # being archived at all. Reads are unaffected -- gather_streaks below
        # still renders real streaks either way.
        note(persist_results(cfg, results, puzzle_numbers, scored, games, rotation,
                             write=not is_test and days_back >= 1))

        streaks = gather_streaks(gid, scored, results, games, cfg['minimum_players'])
        components = format_scoreboard_components(results, scored, puzzle_numbers,
                                                  minimum_players=cfg['minimum_players'],
                                                  streaks=streaks,
                                                  game_overrides=cfg['game_overrides'],
                                                  rotation=rotation,
                                                  rotation_off=cfg['rotation_off_mode'],
                                                  names=build_name_map(messages))
        board_channel = test_channel_id if is_test else cfg['output_channel_id']
        response = send_message(board_channel, components=components)
        note('posted scoreboard')
        parts.append(f'TEST: posted {day} scoreboard to {board_channel}' if is_test
                     else f'posted {day}')
    else:
        parts.append(blocked)

    # Two triggers, one per thing worth standing next to: the draw itself, and
    # the board -- the post that actually notifies the channel. A default config
    # puts both on one tick and so gets one post; a later post hour gets the
    # list at day start and again under the board. A board that never posts
    # (empty input channel, a marker healed from a manual post) costs only the
    # second, never the day-start one.
    announce_due = (cfg['rotation_enabled']
                    and (draw_due or response is not None or is_test))
    todays_rotation = (rotation_today or stored_rotation(cfg, today_day)) \
        if announce_due else None

    if todays_rotation:
        # Today's games, built for today: only the emoji, title and (static)
        # URL reach the buttons, but the day the header is about is this one.
        # Falls back to the input channel so a guild with the board off still
        # gets the announcement.
        channel = (test_channel_id if is_test
                   else cfg['output_channel_id'] or cfg['input_channel_id'])
        todays_games = build_games(compute_puzzle_numbers(today), cfg['game_overrides'])
        if streaks is None:
            # No board this tick, so no bundle to reuse: build the one the board
            # will build at post hour. It has to be the CLOSED day's bundle, not
            # today's. The fold runs at post hour, hours after this, so the
            # aggregates here still stop at the day before the closed one --
            # gathering for today would ask display_streak "alive through
            # yesterday?" of a table that has never seen yesterday and get 0 for
            # every game, collapsing the streak tier of the ordering below.
            # Gathering for `scored` lets it fold that day in from the parse.
            streaks = gather_streaks(gid, scored, results, todays_games,
                                     cfg['minimum_players'], include_players=False)
        if streaks:
            # ...then re-base that bundle from the closed day onto today. A
            # streak alive through the day BEFORE it still reads live there
            # (it was still extendable then), but nothing that went unscored
            # on the closed day carries a streak into this one. The board's own
            # bundle, on a tick that posted one, is gathered for that same
            # closed day, so it needs the identical re-basing -- hence a copy
            # here rather than a branch above.
            scorers = scoring_players(results, todays_games, cfg['minimum_players'])
            streaks = dict(streaks)
            streaks['games'] = {k: (v if scorers.get(k) else 0)
                                for k, v in streaks['games'].items()}
            if not any(scorers.values()):
                streaks['server'] = 0
        announce_rotation(channel, todays_rotation, todays_games, streaks)
        # The draw line above already lists the games on a tick that drew them.
        announced = f'announced {today_day}'
        if not draw_due:
            announced += f': {", ".join(todays_rotation)}'
        note(announced)
        parts.append(announced)

    # Pinning last keeps Discord's "pinned a message" notice below the
    # announcement, so the board and today's games stay adjacent.
    if response and not is_test:
        pin_message(cfg['output_channel_id'], response['id'])
        store.set_last_posted(gid, day)
    return '; '.join(parts)


def lambda_handler(event, context):
    """Hourly tick: for every guild, post the daily scoreboard if it is due and
    draw today's rotation if the day has rolled over.

    One rule per stage rather than one schedule per guild: the tick fires every
    hour and each guild's own timezone, day-start hour and post hour decide what
    happens on it. The guild list comes from the table each invocation, so a
    server onboarded via /setup is picked up with no deploy or schedule change.
    Event keys:
      test             any value: post to the test channel, skip gates, no
                       writes of any kind
      test_channel_id  overrides the TEST_CHANNEL_ID env for this run
      guild_id         only process this guild
      days_back        which day the board scores, counting back from the
                       guild's current day (default 1, the closed day). 0
                       scores today so far -- a preview, never persisted.
    """
    event = event if isinstance(event, dict) else {}
    is_test = 'test' in event
    test_channel_id = event.get('test_channel_id') or os.getenv('TEST_CHANNEL_ID')
    if is_test and not test_channel_id:
        return {'statusCode': 400,
                'body': json.dumps('test mode needs test_channel_id in the event '
                                   'or TEST_CHANNEL_ID in the env')}
    try:
        days_back = int(event.get('days_back', 1))
    except (TypeError, ValueError):
        return {'statusCode': 400,
                'body': json.dumps(f'days_back must be an integer, '
                                   f'got {event["days_back"]!r}')}
    if days_back < 0:
        return {'statusCode': 400,
                'body': json.dumps('days_back must be 0 or more')}

    configs = store.all_configs()
    if event.get('guild_id'):
        configs = [c for c in configs if c['guild_id'] == str(event['guild_id'])]

    summary = {}
    for cfg in configs:
        gid = cfg['guild_id']
        try:
            summary[gid] = process_guild(cfg, is_test, test_channel_id, days_back)
        except Exception as e:
            traceback.print_exc()
            summary[gid] = f'FAILED {type(e).__name__}: {e}'
        print(f'guild {gid}: {summary[gid]}')

    if not summary:
        summary = 'no guilds configured'
    return {'statusCode': 200, 'body': json.dumps(summary)}


if __name__ == '__main__':
    # No argument runs the plain test event, so the documented post-change
    # check stays `python3 src/lambda_function.py`. Pass a fixture path (or
    # inline JSON) to run any other one:
    #   dotenv run -- python3 src/lambda_function.py tests/events/daily/scoreboard_today.json
    arg = sys.argv[1] if len(sys.argv) > 1 else None
    if arg:
        event = json.loads(arg if arg.lstrip().startswith('{') else open(arg).read())
    else:
        event = {'test': True}
    print(lambda_handler(event, None))
