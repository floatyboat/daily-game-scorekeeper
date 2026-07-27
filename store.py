"""DynamoDB persistence for streaks, per-game player stats, and daily archives.

One table, generic PK/SK string keys, no GSIs. Item catalog (see SPEC.md):

    PK                        SK                contents
    GUILDS                    GUILD#<gid>       per-server config + run markers
    GUILD#<gid>               DAY#<YYYY-MM-DD>  the day's parsed results, JSON-frozen
    GUILD#<gid>               AGG#SERVER        overall server streak (any game played)
    GUILD#<gid>               AGG#GAME#<key>    per-game server streak + player sets
    GUILD#<gid>#PLAYER#<uid>  AGG#SERVER        per-player overall streak (any game)
    GUILD#<gid>#PLAYER#<uid>  AGG#GAME#<key>    per-player-per-game streak + totals

All configs share one partition (GUILDS) so the scheduled lambdas can load every
guild with a single small Query each tick -- a Scan would read the whole table
(every DAY/AGG item) once a minute, which the 5-RCU budget cannot absorb.
Onboarding is automatic: /setup writes the item, the next tick picks it up.

This module also owns the per-server config *schema* (CONFIG_FIELDS below): one
declaration per setting, from which the defaults, the stored-value coercion, the
/setup slash-command options, and the handler that writes them back all derive.

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
from dataclasses import dataclass
from datetime import datetime, timedelta

import boto3
from boto3.dynamodb.conditions import Key
from botocore.config import Config
from botocore.exceptions import ClientError

TABLE_NAME = os.getenv('TABLE_NAME') or 'daily-game-tracker'
AWS_REGION = os.getenv('AWS_REGION') or 'us-east-1'

DAY_FMT = '%Y-%m-%d'
GUILDS_PK = 'GUILDS'
SERVER_AGG_SK = 'AGG#SERVER'
GAME_AGG_PREFIX = 'AGG#GAME#'

# --- Per-server configuration schema -------------------------------------------
# The table is the ONLY source of per-server config -- env vars configure
# nothing per-server.
#
# Discord option types, for the fields /setup exposes as slash-command options.
OPT_SUB_COMMAND, OPT_STRING, OPT_INTEGER, OPT_BOOLEAN, OPT_USER, OPT_CHANNEL = 1, 3, 4, 5, 6, 7


@dataclass(frozen=True)
class ConfigField:
    """One per-server setting, declared once and derived from everywhere.

        name      config item attribute, and the key callers read off a cfg dict
        default   value for a guild that never set it
        coerce    stored/incoming value -> the type callers expect. DynamoDB
                  hands numbers back as Decimal, and Discord hands option values
                  back as whatever JSON had; this is the one place that is fixed.
        group     the /setup subcommand exposing this field ('time'/'limits'),
                  or None for settings with a surface of their own (the channel
                  subcommands, the on/off toggles, /games, the run markers)
        option    slash-command option name when it differs from `name`
        opt_type  Discord option type
        describe  option description shown in Discord's picker
        minimum   option bounds Discord enforces before the interaction is sent
        maximum

    Same bargain as GameSpec in game_parser.py: adding a setting is one entry
    here, not four coordinated edits across three files that silently no-op when
    they drift. register_commands.py registers `group` fields straight off this
    table and interaction_lambda.handle_setup reads them back the same way, so
    an option name cannot exist on one side only.
    """
    name: str
    default: object = None
    coerce: object = None
    group: str = None
    option: str = None
    opt_type: int = OPT_INTEGER
    describe: str = ''
    minimum: int = None
    maximum: int = None

    @property
    def option_name(self):
        return self.option or self.name


def _overrides(value):
    """game_overrides holds only explicit deviations from each GameSpec's coded
    default, so a newly added game reaches every guild with its own default
    rather than a frozen snapshot of an old /games submission."""
    return {k: bool(v) for k, v in (value or {}).items()}


CONFIG_FIELDS = [
    # Channels: their own /setup subcommands (native picker + raw-ID fallback).
    ConfigField('input_channel_id'),    # scores are read + sticky lives here
    ConfigField('output_channel_id'),   # daily scoreboard posts here

    # /setup time
    ConfigField('timezone', default='UTC', group='time', opt_type=OPT_STRING,
                describe='IANA name, e.g. America/New_York'),
    ConfigField('hours_after_midnight', default=0, coerce=int, group='time',
                option='day_start_hour', minimum=0, maximum=23,
                describe='Hour the scoring day starts (default 0)'),
    ConfigField('post_hour', coerce=int, group='time', minimum=0, maximum=23,
                describe='Local hour the scoreboard posts (default: day start hour)'),
    ConfigField('time_window_hours', default=24, coerce=int, group='time',
                option='window_hours', minimum=1, maximum=24,
                describe='Hours submissions stay open each day (default 24)'),

    # /setup limits
    ConfigField('minimum_players', default=1, coerce=int, group='limits', minimum=1,
                describe='Hide games with fewer players than this (default 1)'),
    ConfigField('hundreds_of_messages', default=1, coerce=int, group='limits',
                option='message_volume', minimum=1, maximum=8,
                describe='Hundreds of messages/day in the input channel (default 1)'),
    ConfigField('wordle_bot_id', coerce=str, group='limits', option='wordle_bot',
                opt_type=OPT_USER,
                describe='The official Wordle bot (enables image results)'),

    # Toggles (/setup daily, /setup sticky) and the game menu (/games).
    ConfigField('daily_enabled', default=True, coerce=bool, opt_type=OPT_BOOLEAN),
    ConfigField('sticky_enabled', default=True, coerce=bool, opt_type=OPT_BOOLEAN),
    ConfigField('game_overrides', default={}, coerce=_overrides),

    # Run markers, written by the daily lambda. last_posted_day is the post
    # gate; last_finalized_day is diagnostic only -- nothing reads it, it just
    # records how far aggregates have been folded (see set_last_finalized).
    ConfigField('last_finalized_day'),
    ConfigField('last_posted_day'),
]

CONFIG_DEFAULTS = {f.name: f.default for f in CONFIG_FIELDS}


def setup_options(group):
    """The /setup subcommand's fields, in declaration order."""
    return [f for f in CONFIG_FIELDS if f.group == group]


def post_hour(cfg):
    """Guild-local hour the daily board posts and the sticky wakes.

    Unset means "as soon as the scoring day has rolled over", i.e. the day-start
    hour -- written down here once instead of in each of the four callers that
    used to spell out the fallback.
    """
    return cfg['hours_after_midnight'] if cfg['post_hour'] is None else cfg['post_hour']


_resource = None
_table = None


def _dynamodb():
    # Lazy so importing this module never requires AWS credentials. Tight
    # timeouts: interactive callers live inside Discord's 3-second deadline,
    # and the daily lambda must never hang on a store outage.
    global _resource
    if _resource is None:
        _resource = boto3.resource('dynamodb', region_name=AWS_REGION, config=Config(
            connect_timeout=2, read_timeout=3,
            retries={'max_attempts': 2, 'mode': 'standard'}))
    return _resource


def table():
    global _table
    if _table is None:
        _table = _dynamodb().Table(TABLE_NAME)
    return _table


# --- Keys and day arithmetic ---------------------------------------------------

def guild_pk(guild_id):
    return f'GUILD#{guild_id}'


def config_sk(guild_id):
    return f'GUILD#{guild_id}'


def player_pk(guild_id, user_id):
    return f'GUILD#{guild_id}#PLAYER#{user_id}'


def game_agg_sk(game_key):
    return f'{GAME_AGG_PREFIX}{game_key}'


def game_key_from_sk(sk):
    return sk[len(GAME_AGG_PREFIX):]


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


def display_streak(item, day, played):
    """Streak to display for reference day `day`, given whether the subject
    played on that day (per the parse feeding the view).

    Returns the same number on both sides of the daily finalize:
      - finalized view (`day` already folded in): last_played_day == day and
        the stored current_streak already counts `day`.
      - live view (folded through an earlier day): a play on `day` extends a
        streak alive through the day before by one, or starts a new one at 1.
      - not played: the streak shows only while it can still be extended
        (alive through the day before `day`); anything staler renders 0 even
        before the break is finalized (SPEC.md "active" rule).
    """
    if not item:
        return 1 if played else 0
    last = item.get('last_played_day')
    current = int(item.get('current_streak') or 0)
    if played:
        if last == day:
            return current
        if last == prev_day_str(day):
            return current + 1
        return 1
    return current if last == prev_day_str(day) else 0


def broken_streak_on(item, day):
    """Length of a streak recorded as ending on `day`, else 0.

    Drives the scoreboard's "streak ended" callout, which appears only on the
    day the break was finalized and disappears on its own the next day.
    """
    if item and item.get('broken_day') == day:
        return int(item.get('broken_streak') or 0)
    return 0


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


def batch_get(keys):
    """Items for explicit {'PK','SK'} key dicts (caller must dedupe).

    Chunked to BatchGetItem's 100-key limit. Unprocessed keys are retried a
    few times, then dropped: display callers treat a missing item as a blank
    aggregate, so a throttled read degrades the view instead of erroring.
    """
    items = []
    for i in range(0, len(keys), 100):
        request = {TABLE_NAME: {'Keys': keys[i:i + 100]}}
        for _ in range(4):
            resp = _dynamodb().batch_get_item(RequestItems=request)
            items += resp.get('Responses', {}).get(TABLE_NAME, [])
            request = resp.get('UnprocessedKeys') or {}
            if not request.get(TABLE_NAME, {}).get('Keys'):
                break
    return items


def fetch_days(guild_id, start_day, end_day):
    """Decoded DAY# items for start..end inclusive, ascending.

    A day with no plays is never written, so absence means "no plays".
    Each entry is {'day', 'games': {game: {uid: {'score', 'points'}}}, 'puzzles'}.
    """
    items = _query_all(KeyConditionExpression=Key('PK').eq(guild_pk(guild_id)) &
                       Key('SK').between(day_sk(start_day), day_sk(end_day)))
    return [{'day': it['day'], **json.loads(it['data'])} for it in items]


def _effective_config(item):
    """Stored config item -> plain dict with defaults filled and every value run
    through its field's coercion, so callers never see DynamoDB types (numbers
    come back as Decimal) or missing keys. Each field's `coerce` also hands back
    a fresh container, so the shared CONFIG_FIELDS defaults are never aliased
    into a caller's config."""
    cfg = {}
    for f in CONFIG_FIELDS:
        value = item.get(f.name)
        if value is None:
            value = f.default
        cfg[f.name] = f.coerce(value) if (f.coerce and value is not None) else value
    cfg['guild_id'] = str(item.get('guild_id') or '') or None
    return cfg


def default_config(guild_id=None):
    """Effective config for a guild with no stored item (e.g. an interaction
    from a server that never ran /setup) -- all defaults, nothing enabled to
    post anywhere because both channels are None."""
    return _effective_config({'guild_id': guild_id} if guild_id else {})


def get_config(guild_id):
    """Effective config for one guild, or None when it has never been set up."""
    resp = table().get_item(Key={'PK': GUILDS_PK, 'SK': config_sk(guild_id)})
    item = resp.get('Item')
    return _effective_config(item) if item else None


def all_configs():
    """Effective configs for every set-up guild -- one small Query. This is
    the fan-out source for both scheduled lambdas."""
    items = _query_all(KeyConditionExpression=Key('PK').eq(GUILDS_PK))
    return [_effective_config(it) for it in items]


# --- Writes ---------------------------------------------------------------------

def update_config(guild_id, updates):
    """Set config fields for a guild, creating the item on first use (/setup
    in a fresh server is the onboarding write). Unknown keys are rejected so a
    typo can't plant dead config."""
    bad = set(updates) - set(CONFIG_DEFAULTS)
    if bad:
        raise ValueError(f'unknown config fields: {sorted(bad)}')
    names = {f'#f{i}': k for i, k in enumerate(updates)}
    values = {f':v{i}': v for i, v in enumerate(updates.values())}
    sets = ', '.join(f'#f{i} = :v{i}' for i in range(len(updates)))
    table().update_item(
        Key={'PK': GUILDS_PK, 'SK': config_sk(guild_id)},
        UpdateExpression=f'SET guild_id = :gid, {sets}',
        ExpressionAttributeNames=names,
        ExpressionAttributeValues={':gid': str(guild_id), **values},
    )


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

        # Overall per-player streak: any game played today keeps it alive. This
        # is the number the scoreboard's points summary shows, so it has to be
        # its own aggregate -- it is not derivable from the per-game ones (a
        # player alternating games has no per-game streak but a long overall one).
        agg, _ = _agg_from_item(theirs.get(SERVER_AGG_SK))
        advance_streak(agg, day, prev_day, True)
        _put_guarded(_agg_to_item(ppk, SERVER_AGG_SK, agg, day), day, stats)

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


def _advance_marker(guild_id, field, day):
    """Monotonically advance a day marker on the guild's config item; a no-op
    for a guild whose config was deleted mid-run."""
    try:
        table().update_item(
            Key={'PK': GUILDS_PK, 'SK': config_sk(guild_id)},
            UpdateExpression=f'SET {field} = :d',
            ConditionExpression=f'attribute_exists(SK) AND '
                                f'(attribute_not_exists({field}) OR {field} < :d)',
            ExpressionAttributeValues={':d': day},
        )
    except ClientError as e:
        if e.response['Error']['Code'] != 'ConditionalCheckFailedException':
            raise


def set_last_finalized(guild_id, day):
    """Advance the finalize marker (last day folded into aggregates).

    Diagnostic only: nothing gates on this. Folding is made safe by the
    per-item `finalized_through` guard in _put_guarded(), and the daily post
    gates on last_posted_day -- this is here to answer "how far along is this
    guild?" without reading every aggregate.
    """
    _advance_marker(guild_id, 'last_finalized_day', day)


def set_last_posted(guild_id, day):
    """Advance the post marker (last day whose scoreboard went out for real).
    The hourly daily lambda gates on this, so test posts never touch it."""
    _advance_marker(guild_id, 'last_posted_day', day)


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
    player_server = {}

    for d in days:
        day, games = d['day'], d['games']
        prev = prev_day_str(day)
        advance_streak(server, day, prev, bool(games))
        for uid in {u for scores in games.values() for u in scores}:
            advance_streak(player_server.setdefault(uid, blank_agg()), day, prev, True)
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
    for agg in player_server.values():
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
        for uid, agg in player_server.items():
            batch.put_item(Item=_agg_to_item(
                player_pk(guild_id, uid), SERVER_AGG_SK, agg, through_day))

    return {
        'days': len(days),
        'server': server,
        'games': {k: {'agg': game_aggs[k], 'players': len(game_players[k]),
                      'players_30d': len(players_30d.get(k, ()))}
                  for k in game_aggs},
        'player_aggs': len(player_aggs),
        'players': len(player_server),
    }
