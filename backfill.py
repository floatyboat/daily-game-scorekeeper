"""Backfill the DAY# archive from Discord channel history, then rebuild aggregates.

Local-only tooling (never deployed). Seeds streaks and player stats with their
true historical values instead of starting from zero (SPEC.md Phase 1):

    dotenv run -- python3 backfill.py --days 120     # limited window (default)
    dotenv run -- python3 backfill.py --all          # entire channel history
    dotenv run -- python3 backfill.py --rebuild-only # aggregates from existing DAY items

Idempotent: DAY# writes are overwrites and the rebuild recomputes every
aggregate from scratch, so re-running or extending the window later converges.
Avoid running concurrently with the daily finalize (shortly after
HOURS_AFTER_MIDNIGHT guild-local time).
"""
import argparse
import os
import time
from datetime import datetime, timedelta

from dotenv import load_dotenv

load_dotenv()

# Env is read at import time by lambda_function (and below), so load_dotenv()
# must run before these imports.
import store
from game_parser import make_timestamp_checker, build_games, compute_points
from scoreboard import (
    DISCORD_API_BASE, make_session, parse_results, build_avatar_pool,
    get_channel_guild_id, reference_date,
)
from lambda_function import (
    env_config_defaults, TIMEZONE, HOURS_AFTER_MIDNIGHT,
    TIME_WINDOW_HOURS, MINIMUM_PLAYERS,
)

DISCORD_BOT_TOKEN = os.getenv('DISCORD_BOT_TOKEN')
INPUT_CHANNEL_ID = os.getenv('INPUT_CHANNEL_ID')
WORDLE_BOT_ID = os.getenv('WORDLE_BOT_ID')

# Identifier-carrying games (Wordle #1,500) attribute to their puzzle's day no
# matter when they were posted, so each day's parse can't use a strict
# timestamp bucket. It instead sees this many days of messages after the day --
# late shares beyond the grace window are dropped, which bounds a deep
# backfill's cost at (messages x grace) instead of (messages x total days).
LATE_GRACE_DAYS = 7


def fetch_history(session, channel_id, cutoff=None):
    """All channel messages newest-first, back to cutoff (aware dt) or channel start.

    Unlike scoreboard.fetch_messages this paces itself and honors 429s, since a
    deep backfill can be hundreds of pages.
    """
    messages, before = [], None
    while True:
        url = f'{DISCORD_API_BASE}/channels/{channel_id}/messages?limit=100'
        if before:
            url += f'&before={before}'
        r = session.get(url)
        if r.status_code == 429:
            time.sleep(float(r.json().get('retry_after', 1)) + 0.1)
            continue
        r.raise_for_status()
        page = r.json()
        if not page:
            break
        messages += page
        before = page[-1]['id']
        oldest = datetime.fromisoformat(page[-1]['timestamp'])
        print(f'\r  fetched {len(messages)} messages (back to {oldest:%Y-%m-%d})',
              end='', flush=True)
        if len(page) < 100 or (cutoff and oldest < cutoff):
            break
        time.sleep(0.3)
    print()
    return messages


def backfill_days(session, guild_id, messages, start_dt, through_dt):
    """Parse and archive each day in [start_dt, through_dt]. Returns days written."""
    stamped = [(datetime.fromisoformat(m['timestamp']), m) for m in messages]
    written = 0
    day_dt = start_dt
    while day_dt <= through_dt:
        lo = (day_dt - timedelta(days=1)).replace(tzinfo=TIMEZONE)
        hi = (day_dt + timedelta(days=1 + LATE_GRACE_DAYS)).replace(tzinfo=TIMEZONE)
        day_msgs = [m for ts, m in stamped if lo <= ts < hi]
        if day_msgs:
            checker = make_timestamp_checker(day_dt, TIMEZONE, HOURS_AFTER_MIDNIGHT,
                                             TIME_WINDOW_HOURS)
            pool = build_avatar_pool(session, day_msgs, checker, WORDLE_BOT_ID)
            results, puzzle_numbers = parse_results(
                day_msgs, day_dt, TIMEZONE, HOURS_AFTER_MIDNIGHT, TIME_WINDOW_HOURS,
                wordle_bot_id=WORDLE_BOT_ID, avatar_hashes=pool,
            )
            if any(results.values()):
                games = build_games(puzzle_numbers)
                points = {g.key: compute_points(results, [g], MINIMUM_PLAYERS)
                          for g in games}
                day = store.day_str(day_dt)
                store.write_day(guild_id, day, results, points, puzzle_numbers)
                written += 1
                total = sum(len(v) for v in results.values())
                played = sum(1 for v in results.values() if v)
                print(f'  {day}: {total} results across {played} games')
        day_dt += timedelta(days=1)
    return written


def print_summary(summary):
    server = summary['server']
    print(f"\nRebuilt from {summary['days']} archived days "
          f"({summary['player_aggs']} player-game aggregates)")
    print(f"  server streak: {server['current_streak']} (best {server['best_streak']}, "
          f"{server['total_plays']} active days)")
    ranked = sorted(summary['games'].items(),
                    key=lambda kv: (-kv[1]['agg']['current_streak'], kv[0]))
    for key, g in ranked:
        agg = g['agg']
        print(f"  {key:<18} streak {agg['current_streak']:>4} (best {agg['best_streak']:>4})"
              f"  players {g['players']:>3} (30d {g['players_30d']:>3})"
              f"  play-days {agg['total_plays']}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument('--days', type=int, default=120,
                      help='how many days back to backfill (default 120)')
    mode.add_argument('--all', action='store_true', help='entire channel history')
    mode.add_argument('--rebuild-only', action='store_true',
                      help='skip Discord entirely; recompute aggregates from DAY items')
    args = ap.parse_args()

    session = make_session(DISCORD_BOT_TOKEN)
    guild_id = get_channel_guild_id(session, INPUT_CHANNEL_ID)
    if not guild_id:
        raise SystemExit('input channel has no guild_id (is it a DM?)')

    # Last closed scoring day; today's still-open window is never archived.
    through_dt = reference_date(datetime.now(TIMEZONE), TIMEZONE,
                                HOURS_AFTER_MIDNIGHT, days_back=1)
    through_day = store.day_str(through_dt)
    store.ensure_config(guild_id, env_config_defaults())

    if not args.rebuild_only:
        if args.all:
            print('fetching entire channel history...')
            messages = fetch_history(session, INPUT_CHANNEL_ID)
            if not messages:
                raise SystemExit('channel has no messages')
            oldest = min(datetime.fromisoformat(m['timestamp']) for m in messages)
            start_dt = (oldest.astimezone(TIMEZONE) - timedelta(days=1)) \
                .replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
        else:
            start_dt = through_dt - timedelta(days=args.days - 1)
            cutoff = start_dt.replace(tzinfo=TIMEZONE)
            print(f'fetching history back to {store.day_str(start_dt)}...')
            messages = fetch_history(session, INPUT_CHANNEL_ID, cutoff)
        print(f'parsing {store.day_str(start_dt)} .. {through_day}')
        written = backfill_days(session, guild_id, messages, start_dt, through_dt)
        print(f'archived {written} days with plays')

    summary = store.rebuild_aggregates(guild_id, through_day)
    store.set_last_finalized(guild_id, through_day)
    print_summary(summary)


if __name__ == '__main__':
    main()
