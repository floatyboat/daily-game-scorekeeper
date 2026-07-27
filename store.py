"""DynamoDB persistence for streaks, per-game player stats, and daily archives.

One table, generic PK/SK string keys, no GSIs. Item catalog (see SPEC.md):

    PK                        SK                contents
    GUILD#<gid>               CONFIG            per-server settings + last_finalized_day
    GUILD#<gid>               DAY#<YYYY-MM-DD>  the day's parsed results, JSON-frozen
    GUILD#<gid>               AGG#SERVER        overall server streak (any game played)
    GUILD#<gid>               AGG#GAME#<key>    per-game server streak + player sets
    GUILD#<gid>#PLAYER#<uid>  AGG#GAME#<key>    per-player-per-game streak + totals

DAY# items are plain overwrites (same parse -> same item) and are the source of
truth: rebuild_aggregates() recomputes every aggregate from them from scratch.

Aggregates carry `finalized_through` (last day folded in) and every incremental
aggregate write is conditioned on it being older than the day being folded, so
a double-fired or crashed-and-retried daily run can never double-increment a
streak: re-processing a day is a per-item no-op. This per-item guard (rather
than one run-level claim) also means a run that dies halfway resumes cleanly --
the retry updates exactly the items the first attempt didn't reach.
"""
import json
import os
from datetime import datetime, timedelta

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

TABLE_NAME = os.getenv('TABLE_NAME') or 'daily-game-tracker'
AWS_REGION = os.getenv('AWS_REGION') or 'us-east-1'

DAY_FMT = '%Y-%m-%d'
CONFIG_SK = 'CONFIG'
SERVER_AGG_SK = 'AGG#SERVER'

_table = None


def table():
    # Lazy so importing this module never requires AWS credentials.
    global _table
    if _table is None:
        _table = boto3.resource('dynamodb', region_name=AWS_REGION).Table(TABLE_NAME)
    return _table


# --- Keys and day arithmetic ---------------------------------------------------

def guild_pk(guild_id):
    return f'GUILD#{guild_id}'


def player_pk(guild_id, user_id):
    return f'GUILD#{guild_id}#PLAYER#{user_id}'


def game_agg_sk(game_key):
    return f'AGG#GAME#{game_key}'


def day_sk(day):
    return f'DAY#{day}'


def day_str(dt):
    return dt.strftime(DAY_FMT)


def prev_day_str(day):
    return day_str(datetime.strptime(day, DAY_FMT) - timedelta(days=1))


def next_day_str(day):
    return day_str(datetime.strptime(day, DAY_FMT) + timedelta(days=1))


# --- Streak core ----------------------------------------------------------------
# One pure function drives both the daily incremental update and the full
# rebuild, so the two paths cannot drift apart.

def blank_agg():
    return {'current_streak': 0, 'best_streak': 0, 'last_played_day': None,
            'broken_streak': 0, 'broken_day': None, 'total_plays': 0}


def advance_streak(agg, day, prev_day, played):
    """Fold one day into a streak aggregate, in place.

    Continuation keys on last_played_day == prev_day, so gap days never need
    explicit replay -- the next played day resets to 1 on its own. Re-folding
    an already-recorded played day is a no-op.
    """
    if played:
        if agg['last_played_day'] == day:
            return
        streak_alive = agg['last_played_day'] == prev_day
        agg['current_streak'] = agg['current_streak'] + 1 if streak_alive else 1
        agg['best_streak'] = max(agg['best_streak'], agg['current_streak'])
        agg['last_played_day'] = day
        agg['total_plays'] += 1
    elif agg['current_streak'] and agg['last_played_day'] == prev_day:
        # A streak alive through yesterday died today: remember what it was so
        # the scoreboard can announce the break (Phase 2's streak-ended line).
        agg['broken_streak'] = agg['current_streak']
        agg['broken_day'] = day
        agg['current_streak'] = 0


def close_out_streak(agg, through_day):
    """After a replay, settle a streak that isn't alive through through_day.

    Mirrors what the daily not-played updates would have recorded: the streak
    broke the day after its last play.
    """
    if agg['current_streak'] and agg['last_played_day'] and agg['last_played_day'] < through_day:
        agg['broken_streak'] = agg['current_streak']
        agg['broken_day'] = next_day_str(agg['last_played_day'])
        agg['current_streak'] = 0


# --- Item marshalling -----------------------------------------------------------

def _agg_to_item(pk, sk, agg, finalized_through, players=None, extra=None):
    item = {
        'PK': pk, 'SK': sk,
        'current_streak': agg['current_streak'],
        'best_streak': agg['best_streak'],
        'total_plays': agg['total_plays'],
        'finalized_through': finalized_through,
    }
    if agg['last_played_day']:
        item['last_played_day'] = agg['last_played_day']
    if agg['broken_day']:
        item['broken_streak'] = agg['broken_streak']
        item['broken_day'] = agg['broken_day']
    if players:
        item['players'] = set(players)   # -> DynamoDB string set
    if extra:
        item.update(extra)
    return item


def _agg_from_item(item):
    """(agg, players) from a stored item; blank when item is None."""
    agg = blank_agg()
    players = set()
    if item:
        agg['current_streak'] = int(item.get('current_streak', 0))
        agg['best_streak'] = int(item.get('best_streak', 0))
        agg['last_played_day'] = item.get('last_played_day')
        agg['broken_streak'] = int(item.get('broken_streak', 0))
        agg['broken_day'] = item.get('broken_day')
        agg['total_plays'] = int(item.get('total_plays', 0))
        players = set(item.get('players') or [])
    return agg, players


# --- Reads ----------------------------------------------------------------------

def _query_all(**kwargs):
    resp = table().query(**kwargs)
    items = resp['Items']
    while 'LastEvaluatedKey' in resp:
        resp = table().query(ExclusiveStartKey=resp['LastEvaluatedKey'], **kwargs)
        items += resp['Items']
    return items


def query_aggs(pk):
    """All AGG# items in one partition, as {SK: item}."""
    items = _query_all(KeyConditionExpression=Key('PK').eq(pk) & Key('SK').begins_with('AGG#'))
    return {it['SK']: it for it in items}


def fetch_days(guild_id, start_day, end_day):
    """Decoded DAY# items for start..end inclusive, ascending.

    A day with no plays is never written, so absence means "no plays".
    Each entry is {'day', 'games': {game: {uid: {'score', 'points'}}}, 'puzzles'}.
    """
    items = _query_all(KeyConditionExpression=Key('PK').eq(guild_pk(guild_id)) &
                       Key('SK').between(day_sk(start_day), day_sk(end_day)))
    return [{'day': it['day'], **json.loads(it['data'])} for it in items]


def get_config(guild_id):
    resp = table().get_item(Key={'PK': guild_pk(guild_id), 'SK': CONFIG_SK})
    return resp.get('Item')


# --- Writes ---------------------------------------------------------------------

def ensure_config(guild_id, defaults):
    """Create the guild CONFIG item if absent; return the stored config.

    Never overwrites an existing config -- Phase 3 makes the table the source
    of truth for these values, so env vars only seed, they don't win.
    """
    item = {'PK': guild_pk(guild_id), 'SK': CONFIG_SK, 'guild_id': str(guild_id)}
    item.update({k: v for k, v in defaults.items() if v is not None})
    try:
        table().put_item(Item=item, ConditionExpression='attribute_not_exists(PK)')
        return item
    except ClientError as e:
        if e.response['Error']['Code'] != 'ConditionalCheckFailedException':
            raise
        return get_config(guild_id)


def write_day(guild_id, day, results, points_by_game, puzzle_numbers):
    """Freeze one day's parsed results as the durable archive item.

    Points are stored per player per game so historical rollups survive future
    scoring-rule changes. Returns False (and writes nothing) on a no-play day.
    """
    games = {}
    for game_key, scores in results.items():
        if not scores:
            continue
        pts = points_by_game.get(game_key, {})
        games[game_key] = {uid: {'score': score, 'points': pts.get(uid, 0)}
                           for uid, score in scores.items()}
    if not games:
        return False
    players = {uid for scores in games.values() for uid in scores}
    table().put_item(Item={
        'PK': guild_pk(guild_id), 'SK': day_sk(day), 'day': day,
        'player_count': len(players),
        'data': json.dumps({'games': games, 'puzzles': puzzle_numbers}, default=str),
    })
    return True


def _put_guarded(item, day, stats):
    """Write an aggregate unless this day is already folded into it."""
    try:
        table().put_item(
            Item=item,
            ConditionExpression='attribute_not_exists(finalized_through) OR finalized_through < :d',
            ExpressionAttributeValues={':d': day},
        )
        stats['updated'] += 1
    except ClientError as e:
        if e.response['Error']['Code'] != 'ConditionalCheckFailedException':
            raise
        stats['skipped'] += 1


def finalize_day(guild_id, day, results, points_by_game, game_keys):
    """Fold one finalized day into every aggregate. Safe to re-run.

    game_keys is the enabled-game universe for the guild: games in it with no
    results get their streak-break bookkeeping; games outside it (disabled) are
    left untouched.
    """
    prev_day = prev_day_str(day)
    stats = {'updated': 0, 'skipped': 0}
    gpk = guild_pk(guild_id)
    existing = query_aggs(gpk)

    # Overall server streak: any game played today keeps it alive.
    agg, _ = _agg_from_item(existing.get(SERVER_AGG_SK))
    advance_streak(agg, day, prev_day, any(results.get(k) for k in game_keys))
    if agg['total_plays'] or existing.get(SERVER_AGG_SK):
        _put_guarded(_agg_to_item(gpk, SERVER_AGG_SK, agg, day), day, stats)

    # Per-game server streaks + all-time player sets.
    for game_key in game_keys:
        uids = set(results.get(game_key) or {})
        sk = game_agg_sk(game_key)
        agg, players = _agg_from_item(existing.get(sk))
        if not uids and not existing.get(sk):
            continue   # never played: nothing to record yet
        advance_streak(agg, day, prev_day, bool(uids))
        players |= uids
        extra = {}
        if existing.get(sk) and 'players_30d' in existing[sk]:
            extra['players_30d'] = int(existing[sk]['players_30d'])   # refreshed below
        _put_guarded(_agg_to_item(gpk, sk, agg, day, players=players, extra=extra), day, stats)

    # Per-player-per-game streaks and points. Players who didn't play are left
    # alone on purpose: best_streak is maintained on the way up and the display
    # layer treats a stale last_played_day as a broken streak, so their next
    # play resets correctly without us touching every known player daily.
    for uid in {u for k in game_keys for u in (results.get(k) or {})}:
        ppk = player_pk(guild_id, uid)
        theirs = query_aggs(ppk)
        for game_key in game_keys:
            if uid not in (results.get(game_key) or {}):
                continue
            sk = game_agg_sk(game_key)
            item = theirs.get(sk)
            agg, _ = _agg_from_item(item)
            advance_streak(agg, day, prev_day, True)
            points = int(points_by_game.get(game_key, {}).get(uid, 0))
            points_sum = int(item.get('points_sum', 0)) if item else 0
            _put_guarded(_agg_to_item(ppk, sk, agg, day,
                                      extra={'points_sum': points_sum + points}), day, stats)

    refresh_players_30d(guild_id, day, game_keys)
    set_last_finalized(guild_id, day)
    return stats


def refresh_players_30d(guild_id, day, game_keys):
    """Recompute each game's rolling 30-day distinct-player count.

    Runs daily at finalize for every game with an aggregate item (not just
    today's played games -- the trailing window loses old days for everyone).
    Interactive reads then get the count from the aggregate for free.
    """
    start = day_str(datetime.strptime(day, DAY_FMT) - timedelta(days=29))
    counts = {}
    for d in fetch_days(guild_id, start, day):
        for game_key, scores in d['games'].items():
            counts.setdefault(game_key, set()).update(scores)
    for game_key in game_keys:
        n = len(counts.get(game_key, ()))
        try:
            table().update_item(
                Key={'PK': guild_pk(guild_id), 'SK': game_agg_sk(game_key)},
                UpdateExpression='SET players_30d = :n',
                ConditionExpression='attribute_exists(PK)',   # never create orphans
                ExpressionAttributeValues={':n': n},
            )
        except ClientError as e:
            if e.response['Error']['Code'] != 'ConditionalCheckFailedException':
                raise


def set_last_finalized(guild_id, day):
    """Advance the run-level marker on CONFIG (monotonic; Phase 3's hourly
    scheduler uses it to decide whether a guild still needs finalizing)."""
    try:
        table().update_item(
            Key={'PK': guild_pk(guild_id), 'SK': CONFIG_SK},
            UpdateExpression='SET last_finalized_day = :d',
            ConditionExpression='attribute_exists(SK) AND '
                                '(attribute_not_exists(last_finalized_day) OR last_finalized_day < :d)',
            ExpressionAttributeValues={':d': day},
        )
    except ClientError as e:
        if e.response['Error']['Code'] != 'ConditionalCheckFailedException':
            raise


def rebuild_aggregates(guild_id, through_day):
    """Recompute every aggregate from DAY# items (the source of truth).

    Replays played days in order; gaps need no explicit replay because streak
    continuation keys on last_played_day == prev_day. Streaks not alive through
    through_day are then closed out to match what daily updates would have
    recorded. Writes are unconditional overwrites -- don't run concurrently
    with a daily finalize. Returns a summary for display.
    """
    days = [d for d in fetch_days(guild_id, '0000-00-00', through_day)]

    server = blank_agg()
    game_aggs, game_players = {}, {}
    player_aggs, player_points = {}, {}

    for d in days:
        day, games = d['day'], d['games']
        prev = prev_day_str(day)
        advance_streak(server, day, prev, bool(games))
        for game_key, scores in games.items():
            agg = game_aggs.setdefault(game_key, blank_agg())
            advance_streak(agg, day, prev, True)
            game_players.setdefault(game_key, set()).update(scores)
            for uid, rec in scores.items():
                pagg = player_aggs.setdefault((uid, game_key), blank_agg())
                advance_streak(pagg, day, prev, True)
                player_points[(uid, game_key)] = (player_points.get((uid, game_key), 0)
                                                  + int(rec.get('points') or 0))

    close_out_streak(server, through_day)
    for agg in game_aggs.values():
        close_out_streak(agg, through_day)
    for agg in player_aggs.values():
        close_out_streak(agg, through_day)

    window_start = day_str(datetime.strptime(through_day, DAY_FMT) - timedelta(days=29))
    players_30d = {}
    for d in days:
        if d['day'] >= window_start:
            for game_key, scores in d['games'].items():
                players_30d.setdefault(game_key, set()).update(scores)

    gpk = guild_pk(guild_id)
    with table().batch_writer() as batch:
        batch.put_item(Item=_agg_to_item(gpk, SERVER_AGG_SK, server, through_day))
        for game_key, agg in game_aggs.items():
            batch.put_item(Item=_agg_to_item(
                gpk, game_agg_sk(game_key), agg, through_day,
                players=game_players[game_key],
                extra={'players_30d': len(players_30d.get(game_key, ()))}))
        for (uid, game_key), agg in player_aggs.items():
            batch.put_item(Item=_agg_to_item(
                player_pk(guild_id, uid), game_agg_sk(game_key), agg, through_day,
                extra={'points_sum': player_points[(uid, game_key)]}))

    return {
        'days': len(days),
        'server': server,
        'games': {k: {'agg': game_aggs[k], 'players': len(game_players[k]),
                      'players_30d': len(players_30d.get(k, ()))}
                  for k in game_aggs},
        'player_aggs': len(player_aggs),
    }
