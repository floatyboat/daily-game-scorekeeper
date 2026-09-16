"""Replay one archived day of the input channel through the commentary engine.

Local-only and read-only: fetches the channel's history and the DAY# archive,
rebuilds the streak state as it stood the evening before, then walks the day
minute by minute exactly as the two lambdas would -- the daily lambda's HOURLY
pass at the top of each hour, the sticky's STICKY pass every minute, the
morning board landing at post hour -- and prints what the bot would have
posted, when, and what it held back and why. Nothing is posted, nothing is
written. Run from the repository root:

    dotenv run -- python3 tools/replay_commentary.py                      # busiest day of the last 60
    dotenv run -- python3 tools/replay_commentary.py --day 2026-09-02
    dotenv run -- python3 tools/replay_commentary.py --guild <id> --days 90

The replay uses the guild's live config with commentary switched on and every
kind enabled, so it shows the whole menu; pass --overrides '{"nudge": false}'
to see it with some kinds off, --nudge-after 3 to try another gap, or
--midday-hour 15 to move the midday board.
"""
import argparse
import json
import os
import re
import sys
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT / 'tools'))

import commentary                                                    # noqa: E402
import store                                                         # noqa: E402
from backfill import fetch_history                                   # noqa: E402
from game_parser import (build_games, make_timestamp_checker,        # noqa: E402
                         match_message, scoring_players, board_heading)
from scoreboard import (make_session, parse_results, build_avatar_pool,   # noqa: E402
                        build_name_map, FLAG_IS_COMPONENTS_V2)

BOT_ID = str(os.getenv('DISCORD_BOT_ID') or '0')
UTC = timezone.utc


def ts(msg):
    return datetime.fromisoformat(msg['timestamp'])


def fold_history(days, through_day):
    """The aggregate partition and every player's partition as they stood once
    `through_day` was finalized, from the archive alone -- the in-memory half
    of store.rebuild_aggregates, so streaks and rosters are the real ones."""
    server = store.blank_agg()
    game_aggs, game_players, player_server, player_game = {}, {}, {}, {}
    for d in days:
        if d['day'] > through_day:
            break
        day, games = d['day'], d['games']
        prev = store.prev_day_str(day)
        scored = {k: {u for u, rec in sc.items() if int(rec.get('points') or 0) > 0}
                  for k, sc in games.items()}
        store.advance_streak(server, day, prev, any(scored.values()))
        for uid in {u for us in scored.values() for u in us}:
            store.advance_streak(player_server.setdefault(uid, store.blank_agg()), day, prev, True)
        for k, sc in games.items():
            store.advance_streak(game_aggs.setdefault(k, store.blank_agg()), day, prev,
                                 bool(scored[k]))
            game_players.setdefault(k, set()).update(sc)
            for uid in scored[k]:
                store.advance_streak(player_game.setdefault((uid, k), store.blank_agg()),
                                     day, prev, True)
    for agg in [server, *game_aggs.values(), *player_server.values(), *player_game.values()]:
        store.close_out_streak(agg, through_day)
    aggs = {store.SERVER_AGG_SK: server}
    for k, agg in game_aggs.items():
        aggs[store.game_agg_sk(k)] = {**agg, 'players': game_players[k]}
    per_player = {}
    for uid, agg in player_server.items():
        per_player.setdefault(uid, {})[store.SERVER_AGG_SK] = agg
    for (uid, k), agg in player_game.items():
        per_player.setdefault(uid, {})[store.game_agg_sk(k)] = agg
    return aggs, per_player


def streak_bundle(aggs, day, results, games, minimum_players, players_30d):
    """What scoreboard.gather_streaks would hand back, off the folded history."""
    scorers = scoring_players(results, games, minimum_players)
    bundle = {'games': {}, 'broken': {}, 'players_30d': {}, 'players_total': {},
              'server': store.display_streak(aggs.get(store.SERVER_AGG_SK), day,
                                             any(scorers.values()))}
    for g in games:
        item = aggs.get(store.game_agg_sk(g.key))
        bundle['games'][g.key] = store.display_streak(item, day, bool(scorers.get(g.key)))
        bundle['players_30d'][g.key] = len(players_30d.get(g.key, ()))
        bundle['players_total'][g.key] = len((item or {}).get('players') or ())
    return bundle


def bot_message(now, content='', components=None, board=False):
    """A stand-in for a message the bot would have posted, for the quiet gate."""
    msg = {'id': f'sim-{int(now.timestamp())}-{len(content)}',
           'author': {'id': BOT_ID, 'bot': True, 'username': 'scoreboard'},
           'timestamp': now.astimezone(UTC).isoformat(),
           'content': content, 'components': components or [], 'flags': 0}
    if board:
        msg['flags'] = FLAG_IS_COMPONENTS_V2
    return msg


def describe(entries, games_by_key, names):
    parts = []
    for key, score, _, uid in entries:
        g = games_by_key.get(key)
        who = f"{names.get(uid, uid)}: " if uid else ''
        parts.append(f"{who}{g.emoji if g else ''} {g.title if g else key} {score}")
    return ' · '.join(parts)


def pretty(text, names):
    return re.sub(r'<@(\d+)>', lambda m: '@' + names.get(m.group(1), m.group(1)), text)


def replay(session, cfg, day, archive, history, overrides, nudge_after, midday_hour):
    tz = ZoneInfo(cfg['timezone'])
    today = datetime.strptime(day, store.DAY_FMT)
    start = today.replace(hour=cfg['hours_after_midnight'], tzinfo=tz)
    close = start + timedelta(hours=cfg['time_window_hours'])
    post_at = today.replace(hour=store.post_hour(cfg), tzinfo=tz)
    prev_day = store.prev_day_str(day)
    day_item = next((d for d in archive if d['day'] == day), None)
    # The archive records the rotation that governed a finalized day; an open
    # day (today) still has its draw in the live config.
    rotation = sorted((day_item or {}).get('rotation') or store.current_rotation(cfg, day) or [])
    now_real = datetime.now(tz)

    sim_cfg = {**cfg, 'commentary_enabled': True, 'commentary_overrides': overrides,
               'rotation_day': day, 'rotation_games': rotation,
               'rotation_prev_day': None, 'rotation_prev_games': [],
               'last_finalized_day': prev_day}
    if nudge_after:
        sim_cfg['commentary_nudge_after_hours'] = nudge_after
    if midday_hour is not None:
        sim_cfg['commentary_midday_hour'] = midday_hour
    aggs, per_player = fold_history(archive, prev_day)
    since = store.day_str(today - timedelta(days=30))
    players_30d = {}
    for d in archive:
        if since <= d['day'] <= prev_day:
            for k, sc in d['games'].items():
                players_30d.setdefault(k, set()).update(sc)

    stream = sorted((m for m in history if start - timedelta(hours=6) <= ts(m) < close), key=ts)
    checker = make_timestamp_checker(today, tz, cfg['hours_after_midnight'],
                                     cfg['time_window_hours'])
    pool = build_avatar_pool(session, stream, checker, cfg['guild_id'])
    names = build_name_map(stream)

    print(f"\nReplaying {day} for guild {cfg['guild_id']} ({cfg['timezone']}): day "
          f"{start:%H:%M} to {close:%H:%M}, board at {post_at:%H:%M}, midday "
          f"{sim_cfg['commentary_midday_hour']:02d}:00, last call "
          f"{sim_cfg['commentary_last_call_hours']}h before close, nudge after "
          f"{sim_cfg['commentary_nudge_after_hours']}h, scoring {sim_cfg['scoring']}")
    print(f"Rotation that day: {', '.join(rotation) if rotation else 'none (unrestricted)'}")
    if day_item:
        n_results = sum(len(sc) for sc in day_item['games'].values())
        players = {u for sc in day_item['games'].values() for u in sc}
        print(f"Archive: {n_results} results across {len(day_item['games'])} games by "
              f"{len(players)} players")
    print()

    state = {'announced': [], 'standings': []}
    visible, idx, board_posted = [], 0, False
    posts, held_seen = [], set()
    minute = start

    def log(when, tag, text):
        print(f"{when:%H:%M}  {tag:<8} {pretty(text, names)}")

    while minute < close:
        while idx < len(stream) and ts(stream[idx]) <= minute:
            msg = stream[idx]
            idx += 1
            visible.insert(0, msg)
            pn_probe = parse_results([], today, tz, cfg['hours_after_midnight'],
                                     cfg['time_window_hours'])[1]
            games_now = build_games(pn_probe, cfg['game_overrides'])
            entries = match_message(msg, games_now, checker, avatar_hashes=pool)
            author = msg['author']['id']
            if ts(msg) < start:
                continue    # carried for the parse (late shares), not part of the day's story
            if entries:
                log(ts(msg).astimezone(tz), 'result',
                    f"{names.get(author, author)}: "
                    f"{describe(entries, {g.key: g for g in games_now}, names)}")
            elif author != BOT_ID:
                log(ts(msg).astimezone(tz), 'chat',
                    f"{names.get(author, author)}: {(msg.get('content') or '')[:50]!r}")
        if not board_posted and minute >= post_at:
            visible.insert(0, bot_message(minute, board_heading('Daily Game Scoreboard'),
                                          board=True))
            board_posted = True
            log(minute, 'BOARD', "yesterday's scoreboard posts (the bot spoke last from here)")

        cfg_now = {**sim_cfg,
                   'last_posted_day': prev_day if board_posted else store.prev_day_str(prev_day)}
        for cadence in ([commentary.HOURLY, commentary.STICKY] if minute.minute == 0
                        else [commentary.STICKY]):
            times = {}
            results, pn = parse_results(
                visible, today, tz, cfg['hours_after_midnight'], cfg['time_window_hours'],
                avatar_hashes=pool, game_overrides=cfg['game_overrides'], times=times)
            games = build_games(pn, cfg['game_overrides'])
            streaks = streak_bundle(aggs, day, results, games, cfg['minimum_players'],
                                    players_30d)
            tick = commentary.make_tick(cfg_now, minute, visible, results, pn, times, streaks,
                                        aggs, lambda u: per_player.get(u, {}), state, BOT_ID)
            post = commentary.evaluate(tick, cadence)
            ids, snapshot = commentary.to_record(tick, post, sent=post is not None)
            state = {'announced': sorted(set(state['announced']) | set(ids)),
                     'standings': snapshot if snapshot is not None else state['standings']}
            reason = commentary.blocked(tick)
            if not post and reason and reason != 'outside the window':
                would = commentary.evaluate(replace(tick, unanswered=[], board_posted=True),
                                            cadence)
                if would and (cadence, tuple(would.event_ids)) not in held_seen:
                    held_seen.add((cadence, tuple(would.event_ids)))
                    log(minute, 'held', f"{cadence} {would.kind} waits: {reason}")
            if post:
                posts.append((minute, cadence, post))
                if post.board:
                    log(minute, cadence.upper(),
                        f"{post.kind}: standings board, {loudness(post, names)}")
                    for comp in post.components:
                        for child in [comp] + list(comp.get('components') or []):
                            if child.get('type') == 10:
                                for line in child['content'].split('\n'):
                                    print(f"                  {pretty(line, names)}")
                    visible.insert(0, bot_message(minute, '', post.components))
                else:
                    log(minute, cadence.upper(), f"{post.kind} ({loudness(post, names)})")
                    for line in post.content.split('\n'):
                        print(f"                  {pretty(line, names)}")
                    buttons = [b['label'] for row in post.components
                               for b in row.get('components', [])]
                    if buttons:
                        print(f"                  [buttons: {' | '.join(buttons)}]")
                    visible.insert(0, bot_message(minute, post.content))
        minute += timedelta(minutes=1)
        if minute > now_real >= minute - timedelta(minutes=1):
            log(now_real, 'NOW', 'the day is still open; what follows assumes nobody else posts')

    print()
    kinds = {}
    for _, cadence, post in posts:
        kinds[post.kind] = kinds.get(post.kind, 0) + 1
    humans = sum(1 for m in stream if m['author']['id'] != BOT_ID and start <= ts(m) < close)
    pinged = sum(1 for _, _, p in posts if p.notify == commentary.PING and p.mentions)
    notifying = sum(1 for _, _, p in posts if p.notify == commentary.NOTIFY)
    print(f"Summary: {len(posts)} commentary posts ({', '.join(f'{k} x{n}' for k, n in kinds.items()) or 'none'}) "
          f"against {humans} human messages; "
          f"{pinged} pinged someone, {notifying} notified without naming anyone, "
          f"{len(posts) - pinged - notifying} landed silent.")


def loudness(post, names):
    """How the post would land, read off the post itself rather than guessed
    from its mentions: 'silent', 'notify', or who it pings."""
    if post.notify == commentary.PING and post.mentions:
        return f"pings {', '.join(names.get(u, u) for u in post.mentions)}"
    return post.notify


def busiest_day(archive):
    return max(archive, key=lambda d: (sum(len(sc) for sc in d['games'].values()), d['day']))['day']


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('--guild', help='guild id (needed when several are configured)')
    ap.add_argument('--day', help='YYYY-MM-DD in the guild\'s scoring days (default: busiest)')
    ap.add_argument('--days', type=int, default=60, help='archive window to pick from')
    ap.add_argument('--overrides', default='{}', help='JSON commentary_overrides to apply')
    ap.add_argument('--nudge-after', type=int, default=0, help='override the nudge gap (hours)')
    ap.add_argument('--midday-hour', type=int, help='override the midday board hour (0-23)')
    args = ap.parse_args()

    configs = [c for c in store.all_configs() if c['input_channel_id']]
    if args.guild:
        configs = [c for c in configs if c['guild_id'] == str(args.guild)]
    if len(configs) != 1:
        raise SystemExit(f'{len(configs)} configured guilds; pass --guild: '
                         f"{[c['guild_id'] for c in configs]}")
    cfg = configs[0]
    tz = ZoneInfo(cfg['timezone'])
    today = store.day_str(datetime.now(tz))
    since = store.day_str(datetime.now(tz) - timedelta(days=args.days + 45))
    archive = store.fetch_days(cfg['guild_id'], since, today)
    candidates = [d for d in archive if d['day'] < today
                  and d['day'] >= store.day_str(datetime.now(tz) - timedelta(days=args.days))]
    if not candidates:
        raise SystemExit('no archived days in the window')
    day = args.day or busiest_day(candidates)
    if not any(d['day'] == day for d in archive):
        print(f'note: {day} has no archived results; replaying the channel anyway')

    session = make_session(os.getenv('DISCORD_BOT_TOKEN'))
    start = datetime.strptime(day, store.DAY_FMT).replace(hour=cfg['hours_after_midnight'],
                                                          tzinfo=tz)
    history = fetch_history(session, cfg['input_channel_id'],
                            cutoff=start - timedelta(hours=6))
    # Every kind on unless the caller says otherwise, including the ones that ship
    # off (the nudge): the point of a replay is seeing the whole menu.
    overrides = {t.key: True for t in commentary.TRIGGERS}
    overrides.update(json.loads(args.overrides))
    replay(session, cfg, day, archive, history, overrides, args.nudge_after,
           args.midday_hour)


if __name__ == '__main__':
    main()
