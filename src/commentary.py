"""Commentary: what the bot says between the boards.

Two passes feed this module one Tick each -- today's live results and
everything already known about the day -- and get back at most ONE post.
Which post is decided by the registry at the bottom (TRIGGERS): a list of
Trigger entries in priority order, each a small pure function that looks at the
tick and says what it would announce, plus the words for it.

Adding a message kind is one Trigger entry: a `detect` returning the events it
sees, a `render` turning them into text (or a board), a `sample` so the
test-channel preview covers it, the cadence it runs on, and the key it is
switched by. The engine does the rest -- dedup against what the day has already
announced, the one-post-per-tick rule, the notification policy, the /setup
menu, persistence.

Cadences:
  HOURLY  the daily lambda's tick (lambda_function.commentary_tick): the
          scheduled kinds -- nudge, last call, midday board -- and the tie
          line, which is a nudge in spirit ("beat this and 1st is yours") and
          so waits for the hour rather than answering the result that made it.
  STICKY  the sticky pass, every minute (sticky_lambda.run_guild): the kinds
          that answer a result the moment it lands -- a first result, a lead
          change, a clean sweep. They post within a minute of the message
          that caused them, and the sticky reposts beneath them in the same
          pass. How a single result went is not commentary at all: the same
          pass reacts to the result itself (sticky_lambda.react_to_results).

Kinds:
  BODY    the message itself (nudge, last call). At most one body per tick:
          the first in registry order wins, the rest wait for a quieter hour.
  BOARD   a Components V2 board as the body (midday standings). Any flavor
          lines of the same tick go beneath it as one more text display.
  FLAVOR  one-liners (first result, lead change, clean sweep, ties). They ride
          under whatever body posts, or post on their own when no body is due.

How loud (Trigger.notify), quietest first: SILENT carries Discord's
suppress-notifications flag, so it lands without a push for anyone; NOTIFY
drops the flag but mentions nobody, so it notifies on each member's own channel
setting (the midday board and the tie line, which are once-a-day-ish and worth
noticing); PING also puts the people the render names into allowed_mentions
(the last call and the nudge). A composed post takes the LOUDEST of the kinds
that fired into it (see loudest), since one message carries one flag.

Two rules gate every kind, whatever its cadence (see blocked): nothing posts
before the day's board has (the morning board opens the conversation), and a
quiet rule over the bot's own messages since anyone else last spoke -- the
sticky aside. How much of that holds a kind back is its `waits`: ANY (the
default) waits while there is any of it, so the bot never stacks two messages
in a row; OWN (the nudge) waits only while one of them is its own kind, so no
other post holds a nudge back but two nudges never stack; NEVER (the midday
board, the last call) waits on nothing -- a scheduled post is not the bot
talking twice, and a one-liner an hour earlier must not push it back. The last
call needs that twice over: its server-streak branch only fires on a day nobody
has played, which is exactly the day no one clears the morning board out of
`unanswered`, so ANY would hold it back on every day it has something to say.

Events are plain dicts with an `id`. The state, shared by both passes, keeps
the ids of everything announced (nothing is said twice) and the standings of
the last pass (so lead changes can be seen). A trigger with `once=True` has a
single event per day whose id is its key, and is not even asked again once
that has gone out -- what keeps the last call from re-reading every player's
streaks on the ticks after it fired.

Pure: no Discord, no DynamoDB. The two passes gather the inputs, make_tick
assembles them, evaluate() decides, scoreboard.send_commentary posts and
store.record_commentary persists what to_record() hands back.
"""

import hashlib
import math
from dataclasses import dataclass, field
from datetime import timedelta
from functools import cached_property

from dateutil import parser as dateutil_parser

from game_parser import (
    build_games, format_scoreboard_components, game_link_button, game_sort_key,
    is_poop, over_budget, rotation_points_base, score_sort_key, scoring_players,
    shown_streak, total_points, SCORING_OFF, SCORING_PLACEMENT,
)
from scoreboard import (
    LOUDNESS, MAX_ACTION_ROWS, MAX_BUTTONS_PER_ROW, MIDDAY_TITLE, NOTIFY, PING,
    SILENT, build_name_map, is_sticky_message, reference_date,
)
import store

HOURLY, STICKY = 'hourly', 'sticky'
BODY, BOARD, FLAVOR = 'body', 'board', 'flavor'
# How much of the bot's own unanswered talking holds a kind back (Trigger.waits).
ANY, OWN, NEVER = 'any', 'own', 'never'

# The window: from an hour after day start (nobody has played at 03:01) until
# results stop counting. The board gate normally opens later than this.
LEAD_IN = timedelta(hours=1)

# Nudge: how close to the close before the last call owns the channel instead
# of the nudge. How long since a player's last result before they are asked
# back is the guild's commentary_nudge_after_hours.
NUDGE_CUTOFF = timedelta(hours=1)

# Last call: a streak is worth a ping once it is worth showing -- the same
# shown_streak floor (MINIMUM_STREAK) the board, the buttons and the sticky
# spend, so the ping never names a number no surface would print. The message
# names at most this many people (and reads at most this many partitions).
LAST_CALL_MAX_NAMES = 10
LAST_CALL_MAX_PLAYERS = 40

# Lead change: a "lead" of one point is noise.
LEAD_MIN_POINTS = 2

# Clean sweep: how much of the day it takes. The bar is a share of the day's
# scored games, not a count of results, so a five-game rotation asks for three
# and a sixteen-game board asks for nine; SWEEP_MIN_GAMES is the floor under
# that, so a three-game rotation still has to be swept outright rather than
# announced off two results.
SWEEP_MIN_GAMES = 3

# The metrics a tie can be talked about in -- "games that take guesses", where
# a better score is a smaller count and beating the tie is a clear ask.
TIE_METRICS = ('guesses', 'connections', 'cryptic')


@dataclass
class Tick:
    """Everything one pass knows. Assembled by make_tick."""
    cfg: dict
    now: object          # aware datetime, guild-local
    today: object        # naive midnight of the scoring day (reference_date)
    day: str
    start: object        # aware datetime: when the scoring day opened
    close: object        # aware datetime: when results stop counting
    results: dict        # {game_key: {uid: score}} today so far
    last_post: dict      # {uid: aware datetime of their latest counted result}
    games: list          # built Game list, enabled games only
    puzzle_numbers: dict
    rotation: object     # today's key list, or None when unrestricted
    streaks: object      # gather_streaks bundle, or None
    known_players: set   # every uid with a finalized result in this guild, ever
    player_aggs: object  # callable(uid) -> {SK: item}, memoized by the caller
    names: dict          # {uid: display name}, the board's mention fallback
    state: dict          # {'announced': [...], 'standings': [[uid, pts], ...]}
    board_posted: bool   # the board covering yesterday has gone out (or there is none)
    unanswered: list     # the bot's own messages since anyone else spoke, newest first

    @property
    def hour(self):
        return self.now.hour

    @cached_property
    def scored(self):
        """The games that score today, in the app-wide order."""
        rot = set(self.rotation) if self.rotation else None
        games = [g for g in self.games if rot is None or g.key in rot]
        return sorted(games, key=lambda g: game_sort_key(g, self.results, self.streaks))

    @cached_property
    def by_key(self):
        return {g.key: g for g in self.games}

    @cached_property
    def scorers(self):
        return scoring_players(self.results, self.games, self.cfg['minimum_players'])

    @cached_property
    def pool(self):
        """What first place pays on the placement scale right now."""
        return rotation_points_base(self.results, self.scored, self.cfg['minimum_players'])

    @cached_property
    def totals(self):
        return total_points(self.results, self.games, self.cfg['minimum_players'],
                            self.rotation, self.cfg['scoring'])

    def streak_of(self, game_key):
        return ((self.streaks or {}).get('games') or {}).get(game_key, 0)


def unanswered_posts(messages, bot_id):
    """The bot's own messages since the newest one from anybody else, newest
    first -- a board, an announcement, a commentary post -- and empty when
    someone else spoke last. The sticky is skipped because it is always the
    newest message in a channel that has one."""
    run = []
    for msg in messages:
        if is_sticky_message(msg, bot_id):
            continue
        if (msg.get('author') or {}).get('id') != str(bot_id):
            break
        run.append(msg)
    return run


def make_tick(cfg, now, messages, results, puzzle_numbers, times, streaks, aggs,
              player_aggs, state, bot_id):
    """Assemble a Tick from what a pass has already fetched and parsed. Pure:
    the day arithmetic, the roster off the aggregate partition, and the two
    gate flags, so both passes build exactly the same view of the day."""
    tz = now.tzinfo
    today = reference_date(now, tz, cfg['hours_after_midnight'])
    day = store.day_str(today)
    start = today.replace(hour=cfg['hours_after_midnight'], tzinfo=tz)
    close = start + timedelta(hours=cfg['time_window_hours'])
    known = {str(uid) for sk, item in (aggs or {}).items()
             if sk.startswith(store.GAME_AGG_PREFIX)
             for uid in (item.get('players') or ())}
    board_posted = (not cfg['daily_enabled']
                    or (cfg['last_posted_day'] or '') >= store.prev_day_str(day))
    return Tick(
        cfg=cfg, now=now, today=today, day=day, start=start, close=close,
        results=results,
        last_post={uid: dateutil_parser.isoparse(ts).astimezone(tz)
                   for uid, ts in (times or {}).items()},
        games=build_games(puzzle_numbers, cfg['game_overrides']),
        puzzle_numbers=puzzle_numbers,
        rotation=store.current_rotation(cfg, day), streaks=streaks,
        known_players=known, player_aggs=player_aggs or (lambda uid: {}),
        names=build_name_map(messages), state=state or {},
        board_posted=board_posted, unanswered=unanswered_posts(messages, bot_id))


@dataclass
class Rendered:
    """What a trigger's render hands back; the engine merges these."""
    content: str = ''                               # BODY text
    lines: list = field(default_factory=list)       # FLAVOR one-liners
    buttons: list = field(default_factory=list)     # BODY link buttons
    mentions: list = field(default_factory=list)    # BODY: who this is for
    components: list = None                         # BOARD components


@dataclass
class Post:
    """One message, ready for scoreboard.send_commentary."""
    kind: str
    content: str = ''
    components: list = field(default_factory=list)
    mentions: list = field(default_factory=list)    # only these get notified
    event_ids: list = field(default_factory=list)   # marked announced once sent
    board: bool = False
    notify: str = SILENT                            # SILENT, NOTIFY or PING


@dataclass(frozen=True)
class Trigger:
    """One message kind -- THE place to add or change what the bot says.

        key       config override key (commentary_overrides) and state key
        label     what /setup commentary's menu shows (<= 100 chars)
        describe  the menu's description line
        kind      BODY, BOARD or FLAVOR (see the module docstring)
        cadence   HOURLY (the daily lambda's tick) or STICKY (every minute)
        detect    callable(tick) -> [event]; pure, may read tick.state
        render    callable(events, tick) -> Rendered
        sample    callable(tick) -> [event]; fabricated, for the preview
        notify    how loudly it arrives: SILENT (no push), NOTIFY (no mention,
                  but no suppress flag either) or PING (BODY only, which also
                  notifies the users the render names as mentions)
        default   the coded state before any /setup choice, like GameSpec.disabled:
                  every kind ships on except the nudge
        once      one event per day, id == key; not re-detected once announced
        waits     which of the bot's unanswered messages hold this kind back (see
                  blocked): ANY, any of them; OWN, only this kind's own posts,
                  told apart by `headings`; NEVER, none
        headings  the openings this kind's posts start with, for OWN
    """
    key: str
    label: str
    describe: str
    kind: str
    cadence: str
    detect: object
    render: object
    sample: object
    notify: str = SILENT
    default: bool = True
    once: bool = False
    waits: str = ANY
    headings: tuple = ()


# --- Shared bits ----------------------------------------------------------------

def mention(uid):
    return f'<@{uid}>'


def join_names(uids):
    names = [mention(u) for u in uids]
    if len(names) <= 1:
        return ''.join(names)
    return ', '.join(names[:-1]) + ' and ' + names[-1]


def pts(n):
    return f"{n} pt{'' if n == 1 else 's'}"


def pick(tick, key, options):
    """One of `options`, stable for the day: a retried tick says the same thing."""
    digest = hashlib.md5(f'{tick.day}:{key}'.encode()).hexdigest()
    return options[int(digest, 16) % len(options)]


def game_tag(game):
    return f'{game.emoji} **{game.title}**'


def on_offer(tick, game):
    """The most a fresh first place in `game` pays right now, or None with
    points off. Placement pays the day's pool whatever the game; per-game pays
    one more than the players already in it."""
    mode = tick.cfg['scoring']
    if mode == SCORING_OFF:
        return None
    if mode == SCORING_PLACEMENT:
        return max(tick.pool, 1)
    return len(tick.results.get(game.key) or {}) + 1


def one_win(tick):
    """The most a single first place pays right now -- the yardstick a lead has
    to clear before it is worth announcing."""
    if tick.cfg['scoring'] == SCORING_PLACEMENT:
        return max(tick.pool, 1)
    return max([len(tick.results.get(g.key) or {}) for g in tick.scored] or [1])


def short_score(game, score):
    """A tie's score in a few characters, for the metrics ties are talked about in."""
    if game.metric == 'guesses':
        return f'{score}/{game.total}' if game.total else str(score)
    if game.metric == 'connections':
        mistakes = score[0]
        return 'no mistakes' if mistakes == 0 else f"{mistakes} mistake{'' if mistakes == 1 else 's'}"
    if game.metric == 'cryptic':
        hints = score[1]
        return 'no hints' if hints == 0 else f"{hints} hint{'' if hints == 1 else 's'}"
    return str(score)


def beatable(game, score):
    """Is there a better score than this one? A tie at the best possible score
    is not an opportunity for anyone."""
    if game.metric == 'guesses':
        return score > 1
    if game.metric == 'connections':
        return score[0] > 0
    if game.metric == 'cryptic':
        return score[1] > 0
    return False


def standings(tick):
    """[[uid, points], ...] best first -- the snapshot the state carries."""
    return [[uid, p] for uid, p in sorted(tick.totals.items(), key=lambda kv: (-kv[1], kv[0]))]


def sole_first(tick, game):
    """The one player alone at the top of `game` (non-poop), or None."""
    scores = {u: s for u, s in (tick.results.get(game.key) or {}).items()
              if not is_poop(game.metric, s, game.total)}
    if not scores:
        return None
    best = min(scores.values(), key=lambda s: score_sort_key(game.metric, s))
    top = [u for u, s in scores.items() if s == best]
    return top[0] if len(top) == 1 else None


def sample_players(tick, n):
    """Real people for the preview: today's players first, then anyone the
    guild has ever scored. '0' renders as an unknown user when a guild has
    nobody at all."""
    seen = []
    for scores in tick.results.values():
        for uid in scores:
            if uid not in seen:
                seen.append(uid)
    for uid in sorted(tick.known_players):
        if uid not in seen:
            seen.append(uid)
    while len(seen) < n:
        seen.append('0')
    return seen[:n]


def sample_games(tick, n, metrics=None):
    """Real games for the preview: today's scored games first."""
    pool = [g for g in tick.scored if not metrics or g.metric in metrics]
    pool += [g for g in tick.games if g not in pool and (not metrics or g.metric in metrics)]
    return pool[:n]


# --- Nudge: points still on the table ---------------------------------------------

# Every opening a nudge can have. pick() chooses one a day, and blocked() tells a
# nudge apart from the bot's other posts by them (Trigger.headings), so a new
# phrasing goes here and nowhere else.
NUDGE_HEADINGS = (
    '\U0001F3AF **Points still on the table**',
    '\U0001FA9C **Room to climb today**',
    '\U0001F3AF **Not done yet?**',
)

def detect_nudge(tick):
    if tick.close - tick.now <= NUDGE_CUTOFF:
        return []
    gap = timedelta(hours=tick.cfg['commentary_nudge_after_hours'])
    events = []
    for uid, last in sorted(tick.last_post.items()):
        if tick.now - last < gap:
            continue
        played = [g for g in tick.scored if uid in (tick.results.get(g.key) or {})]
        left = [g for g in tick.scored if uid not in (tick.results.get(g.key) or {})]
        if not played or not left:
            continue
        events.append({'id': f'nudge:{uid}', 'uid': uid, 'games': [g.key for g in left],
                       'offer': {g.key: on_offer(tick, g) for g in left}})
    return events


def render_nudge(events, tick):
    head = pick(tick, 'nudge', NUDGE_HEADINGS)
    lines, union = [head], []
    placement = tick.cfg['scoring'] == SCORING_PLACEMENT
    for e in events:
        games = [tick.by_key[k] for k in e['games'] if k in tick.by_key]
        for g in games:
            if g.key not in union:
                union.append(g.key)
        emojis = ' '.join(g.emoji for g in games)
        n = len(games)
        offers = [v for v in e['offer'].values() if v]
        tail = ''
        if offers:
            tail = (f' — up to **{pts(max(offers))}** each' if placement
                    else f' — up to **{pts(sum(offers))}** between them')
        lines.append(f"{mention(e['uid'])} — {n} still open ({emojis}){tail}")
    buttons = [game_link_button(tick.by_key[k], tick.streak_of(k)) for k in union]
    return Rendered(content='\n'.join(lines), buttons=buttons,
                    mentions=[e['uid'] for e in events])


def sample_nudge(tick):
    uid = sample_players(tick, 1)[0]
    games = sample_games(tick, 2)
    return [{'id': f'nudge:{uid}', 'uid': uid, 'games': [g.key for g in games],
             'offer': {g.key: on_offer(tick, g) for g in games}}]


# --- Last call: streaks on the line ---------------------------------------------

def _at_risk(tick):
    """[{uid, server, games: [(key, streak)]}] for everyone with a streak that
    dies at the close unless they play. Sorted by the longest streak at stake."""
    played_any = {uid for uids in tick.scorers.values() for uid in uids}
    enabled = {g.key for g in tick.games}
    risks = []
    for uid in sorted(tick.known_players)[:LAST_CALL_MAX_PLAYERS]:
        aggs = tick.player_aggs(uid) or {}
        server = 0
        if uid not in played_any:
            server = shown_streak(
                store.display_streak(aggs.get(store.SERVER_AGG_SK), tick.day, False))
        games = []
        for sk, item in aggs.items():
            if not sk.startswith(store.GAME_AGG_PREFIX):
                continue
            key = store.game_key_from_sk(sk)
            if key not in enabled or uid in tick.scorers.get(key, ()):
                continue
            n = shown_streak(store.display_streak(item, tick.day, False))
            if n:
                games.append((key, n))
        if server or games:
            games.sort(key=lambda kn: -kn[1])
            risks.append({'uid': uid, 'server': server, 'games': games})
    risks.sort(key=lambda r: -max([r['server']] + [n for _, n in r['games']]))
    return risks


def detect_last_call(tick):
    hours = tick.cfg['commentary_last_call_hours']
    if not hours:
        return []
    left = tick.close - tick.now
    if left > timedelta(hours=hours):
        return []
    played_any = any(tick.scorers.values())
    server = 0 if played_any else shown_streak((tick.streaks or {}).get('server', 0))
    risks = _at_risk(tick)[:LAST_CALL_MAX_NAMES]
    if not risks and not server:
        return []
    return [{'id': 'last_call', 'hours_left': max(1, math.ceil(left / timedelta(hours=1))),
             'risks': risks, 'server_streak': server}]


def render_last_call(events, tick):
    e = events[0]
    h = e['hours_left']
    left = f"{h} hour{'' if h == 1 else 's'} left"
    head = pick(tick, 'last_call', [
        f'⏰ **Last call: {left}!**',
        f'⏰ **{left}, streaks on the line**',
        f'⏰ **Closing soon: {left}**',
    ])
    lines = [head]
    for r in e['risks']:
        parts = []
        if r['server']:
            parts.append(f"\U0001F525 {r['server']}-day streak")
        for key, n in r['games']:
            g = tick.by_key.get(key)
            if g:
                parts.append(f'{g.emoji} {g.title} \U0001F525{n}')
        lines.append(f"{mention(r['uid'])}: {' · '.join(parts)}")
    if e['server_streak']:
        lines.append(f"-# Nobody has scored yet today, and the server's "
                     f"{e['server_streak']}-day streak needs one result.")
    return Rendered(content='\n'.join(lines), mentions=[r['uid'] for r in e['risks']])


def sample_last_call(tick):
    a, b = sample_players(tick, 2)
    games = sample_games(tick, 2)
    risks = [{'uid': a, 'server': 12, 'games': [(games[0].key, 8)] if games else []},
             {'uid': b, 'server': 0, 'games': [(games[-1].key, 3)] if games else []}]
    return [{'id': 'last_call', 'hours_left': 3, 'risks': risks, 'server_streak': 0}]


# --- Midday: the standings so far, as a board -------------------------------------

def detect_midday(tick):
    if tick.hour < tick.cfg['commentary_midday_hour']:
        return []
    # Standings, so somebody has to be standing: this is exactly what
    # format_points_summary prints, so the kind stays quiet on the days the
    # board it would post is empty -- scoring off, or a day whose only results
    # so far are poops.
    if not any(p > 0 for p in tick.totals.values()):
        return []
    return [{'id': 'midday'}]


def render_midday(events, tick):
    cfg = tick.cfg
    # The standings alone (standings_only), not a second full board: who is
    # ahead is the whole message, and the per-game breakdown is one tap away on
    # the sticky's Scores button, which renders the morning board's layout
    # live. Points still come from the rotation alone, as on the morning board.
    components = format_scoreboard_components(
        tick.results, tick.today, tick.puzzle_numbers, title=MIDDAY_TITLE,
        minimum_players=cfg['minimum_players'], streaks=tick.streaks,
        game_overrides=cfg['game_overrides'], rotation=tick.rotation,
        names=tick.names, scoring=cfg['scoring'], standings_only=True, live=True)
    hint = midday_hint(tick)
    if hint:
        with_hint = components + [{'type': 10, 'content': hint}]
        if not over_budget(with_hint):
            components = with_hint
    return Rendered(components=components)


def midday_hint(tick):
    """The line under the midday standings pointing at the full breakdown, or
    '' when there is nothing to point at.

    Counts what the Scores button would actually show this guild -- the
    morning board's layout, so the rotation plus whatever rotation_off_mode
    lets through -- rather than everything parsed, so the numbers match the
    view the line is sending people to. No sticky means no button, and the
    pointer is dropped rather than made up: there is no other live scores view.
    """
    cfg = tick.cfg
    if not cfg['sticky_enabled']:
        return ''
    rot = set(tick.rotation) if tick.rotation else None
    shown = [g for g in tick.games
             if len(tick.results.get(g.key) or {}) >= cfg['minimum_players']
             and (rot is None or g.key in rot or cfg['rotation_off_mode'] == 'shown')]
    if not shown:
        return ''
    n = sum(len(tick.results[g.key]) for g in shown)
    return (f"-# {n} result{'' if n == 1 else 's'} in {len(shown)} game"
            f"{'' if len(shown) == 1 else 's'} so far: tap Scores on the sticky "
            "to see them all.")


def sample_midday(tick):
    return [{'id': 'midday'}]


# --- First result ever ------------------------------------------------------------

def detect_first_play(tick):
    # known_players is folded at finalize, which runs at post hour: until
    # yesterday is folded, someone who started yesterday still looks new.
    if store.prev_day_str(tick.day) > (tick.cfg['last_finalized_day'] or ''):
        return []
    seen = {uid for scores in tick.results.values() for uid in scores}
    return [{'id': f'first:{uid}', 'uid': uid} for uid in sorted(seen - tick.known_players)]


def render_first_play(events, tick):
    return Rendered(lines=[pick(tick, f"first:{e['uid']}", [
        f"\U0001F389 First result from {mention(e['uid'])}, welcome to the board!",
        f"\U0001F389 {mention(e['uid'])} is on the board for the first time!",
    ]) for e in events])


def sample_first_play(tick):
    uid = sample_players(tick, 1)[0]
    return [{'id': f'first:{uid}', 'uid': uid}]


# --- Lead change ------------------------------------------------------------------

def detect_lead(tick):
    """A new sole leader, once they are clear of a single win's worth of points
    and at most once an hour. Replaying real days showed the lead flipping on
    every result through the first two hours of play, at 2 to 6 points, and a
    shared lead flipping back within minutes -- none of it news. So: no shared
    leads, no lead a single first place could have bought, and the event id is
    the clock hour, which dedups a second change in the same hour away."""
    prev = tick.state.get('standings') or []
    now = standings(tick)
    if not prev or not now:
        return []
    top = now[0][1]
    leaders = [uid for uid, p in now if p == top]
    if len(leaders) != 1 or top <= one_win(tick) or top < LEAD_MIN_POINTS:
        return []
    prev_top = prev[0][1]
    prev_leaders = [uid for uid, p in prev if p == prev_top]
    if leaders[0] in prev_leaders:
        return []
    return [{'id': f'lead:{tick.hour}', 'leaders': leaders, 'points': top,
             'previous': [u for u in prev_leaders if u not in leaders], 'now': dict(now)}]


def render_lead(events, tick):
    lines = []
    for e in events:
        if len(e['leaders']) == 1:
            line = pick(tick, f"lead:{e['id']}", [
                f"\U0001F451 {mention(e['leaders'][0])} takes the lead with {pts(e['points'])}",
                f"\U0001F451 New leader: {mention(e['leaders'][0])} on {pts(e['points'])}",
            ])
        else:
            line = f"\U0001F451 {join_names(e['leaders'])} now share the lead at {pts(e['points'])}"
        chasers = [f"{mention(u)} {e['now'].get(u, 0)}" for u in e['previous']]
        if chasers:
            line += f" ({', '.join(chasers)})"
        lines.append(line)
    return Rendered(lines=lines)


def sample_lead(tick):
    a, b = sample_players(tick, 2)
    return [{'id': 'lead:sample', 'leaders': [a], 'points': 9, 'previous': [b],
             'now': {a: 9, b: 7}}]


# --- Clean sweep: one player alone at the top of most of the day's games ---------
# How a single result went (an ace, a flawless grid, no hints) is the sticky's
# reaction on that result (sticky_lambda.react_to_results), not a line here; a
# sweep spans the day's games, so it stays commentary.

def detect_sweep(tick):
    """One player alone in 1st in most of the day's scored games.

    "Most" is measured against the whole slate (tick.scored), not against the
    games played so far: being in front of the only two results anyone has
    posted at 9am is not a sweep, it is the first two results, and the old
    two-contested-games floor announced exactly that. A strict majority of the
    slate can only ever belong to one player, so this never has to break a tie
    between two people holding the same number of games."""
    floor = max(2, tick.cfg['minimum_players'])
    contested = [g for g in tick.scored if len(tick.results.get(g.key) or {}) >= floor]
    firsts = [f for f in (sole_first(tick, g) for g in contested) if f]
    if not firsts:
        return []
    wins = {uid: firsts.count(uid) for uid in set(firsts)}
    uid = max(sorted(wins), key=wins.get)
    if wins[uid] < SWEEP_MIN_GAMES or wins[uid] * 2 <= len(tick.scored):
        return []
    return [{'id': f'sweep:{uid}', 'uid': uid, 'n': wins[uid], 'of': len(tick.scored),
             'clean': wins[uid] == len(contested)}]


def render_sweep(events, tick):
    """Three lines for three days: every game there is, every game anyone has
    finished yet, or simply most of them."""
    lines = []
    for e in events:
        who, key = mention(e['uid']), f"sweep:{e['id']}"
        if e['clean'] and e['n'] == e['of']:
            line = pick(tick, key, [
                f"\U0001F9F9 {who} is 1st in all {e['n']} of today's games, a clean sweep",
                f"\U0001F9F9 Clean sweep: {who} is 1st in every game today, all {e['n']} of them",
            ])
        elif e['clean']:
            line = pick(tick, key, [
                f"\U0001F9F9 {who} is 1st in every game played so far "
                f"({e['n']} of today's {e['of']}), a clean sweep in the making",
                f"\U0001F9F9 A clean sweep in the making: {who} is 1st in every game "
                f"anyone has finished, {e['n']} of {e['of']}",
            ])
        else:
            line = pick(tick, key, [
                f"\U0001F9F9 {who} is 1st in {e['n']} of today's {e['of']} games",
                f"\U0001F9F9 {who} has most of the day: 1st in {e['n']} of {e['of']} games",
            ])
        lines.append(line)
    return Rendered(lines=lines)


def sample_sweep(tick):
    uid = sample_players(tick, 1)[0]
    of = max(len(tick.scored), SWEEP_MIN_GAMES)
    n = max(SWEEP_MIN_GAMES, of // 2 + 1)
    # The emblematic one: in front of everything played so far, and that is
    # already most of the day.
    return [{'id': 'sweep:sample', 'uid': uid, 'n': n, 'of': of, 'clean': True}]


# --- Tie for first, in a game where it can be broken ---------------------------------

def detect_tie(tick):
    events = []
    for g in tick.scored:
        if g.metric not in TIE_METRICS:
            continue
        scores = {u: s for u, s in (tick.results.get(g.key) or {}).items()
                  if not is_poop(g.metric, s, g.total)}
        if len(scores) < 2:
            continue
        best = min(scores.values(), key=lambda s: score_sort_key(g.metric, s))
        tied = sorted(u for u, s in scores.items() if s == best)
        if len(tied) < 2 or not beatable(g, best):
            continue
        events.append({'id': f'tie:{g.key}:{short_score(g, best)}', 'game': g.key,
                       'players': tied, 'score': best, 'payout': on_offer(tick, g)})
    return events


def render_tie(events, tick):
    lines = []
    for e in events:
        g = tick.by_key.get(e['game'])
        if not g:
            continue
        n = len(e['players'])
        score = short_score(g, e['score'])
        line = pick(tick, f"tie:{e['id']}", [
            f'\U0001FAA2 {game_tag(g)}: {n}-way tie at {score}, beat it and 1st is yours',
            f'\U0001FAA2 {n} players are level at {score} on {game_tag(g)}, room at the top',
        ])
        if e['payout']:
            line += f' ({pts(e["payout"])})'
        lines.append(line)
    return Rendered(lines=lines)


def sample_tie(tick):
    a, b = sample_players(tick, 2)
    games = sample_games(tick, 1, metrics=TIE_METRICS)
    if not games:
        return []
    g = games[0]
    score = {'guesses': 4, 'connections': (1, 4), 'cryptic': (2, 2, 0, 0)}[g.metric]
    return [{'id': 'tie:sample', 'game': g.key, 'players': [a, b], 'score': score,
             'payout': on_offer(tick, g)}]


# --- The registry: priority order -------------------------------------------------
# Bodies first, in the order they win a tick; flavor lines in the order they
# read beneath a body (the tie, an hourly flavor, reads beneath the nudge). To
# add a message kind, add an entry.

TRIGGERS = [
    # waits=NEVER for midday's reason and one of its own: where input and output
    # are one channel (the default a fresh server is pointed at), a quiet day
    # never clears the morning board out of `unanswered`, and a quiet day is
    # precisely when this kind has something to say -- detect_last_call counts a
    # server streak only when nobody has played.
    Trigger('last_call', 'Last call', 'Streaks about to break, a few hours before the close',
            BODY, HOURLY, detect_last_call, render_last_call, sample_last_call,
            notify=PING, once=True, waits=NEVER),
    Trigger('midday', 'Midday standings', 'The points standings so far, once, at the midday hour',
            BOARD, HOURLY, detect_midday, render_midday, sample_midday, once=True,
            notify=NOTIFY, waits=NEVER),
    # Off until its cadence is settled: a nudge per player, each on the hour that
    # player goes quiet, put four pinging posts into a 40-message day (the
    # 2026-09-15 replay). A server that wants it says so in /setup commentary.
    Trigger('nudge', 'Points nudge', 'Ask someone back for the games they left open',
            BODY, HOURLY, detect_nudge, render_nudge, sample_nudge, notify=PING,
            default=False, waits=OWN, headings=NUDGE_HEADINGS),
    Trigger('first_play', 'First result', "A welcome under someone's first ever result",
            FLAVOR, STICKY, detect_first_play, render_first_play, sample_first_play),
    Trigger('lead', 'Lead changes', 'When the top of the standings changes hands',
            FLAVOR, STICKY, detect_lead, render_lead, sample_lead),
    Trigger('sweep', 'Clean sweep', "One player alone in 1st in most of the day's scored games",
            FLAVOR, STICKY, detect_sweep, render_sweep, sample_sweep),
    Trigger('tie', 'Beatable ties', 'A tie for first that one better result would break',
            FLAVOR, HOURLY, detect_tie, render_tie, sample_tie, notify=NOTIFY),
]

TRIGGERS_BY_KEY = {t.key: t for t in TRIGGERS}


def trigger_enabled(trigger, overrides):
    """A trigger's effective state for one guild: its explicit override, else
    its default -- the same rule game_parser.spec_enabled applies to games."""
    return bool((overrides or {}).get(trigger.key, trigger.default))


def enabled_triggers(cfg, cadence=None):
    return [t for t in TRIGGERS if trigger_enabled(t, cfg['commentary_overrides'])
            and (cadence is None or t.cadence == cadence)]


# --- The engine -----------------------------------------------------------------

def blocked(tick, trigger=None):
    """Why `trigger` (or, with none given, anything at all) may not post this
    pass, or None. Both cadences. The window and the board are absolute; the
    quiet rule is each kind's own (Trigger.waits), and with no trigger given
    it is the strictest, ANY."""
    if not tick.start + LEAD_IN <= tick.now < tick.close:
        return 'outside the window'
    if not tick.board_posted:
        return 'board not posted yet'
    waits = trigger.waits if trigger else ANY
    if waits == ANY and tick.unanswered:
        return 'the bot posted last'
    if waits == OWN and any((m.get('content') or '').startswith(trigger.headings)
                            for m in tick.unanswered):
        return f'the last {trigger.key} is unanswered'
    return None


def button_rows(buttons):
    rows = [{'type': 1, 'components': buttons[i:i + MAX_BUTTONS_PER_ROW]}
            for i in range(0, len(buttons), MAX_BUTTONS_PER_ROW)]
    return rows[:MAX_ACTION_ROWS]


def evaluate(tick, cadence):
    """The one post this pass should make, or None. Pure: nothing is marked
    announced until to_record() sees the post went out."""
    announced = set(tick.state.get('announced') or ())
    found = []
    for trigger in enabled_triggers(tick.cfg, cadence):
        if blocked(tick, trigger) or (trigger.once and trigger.key in announced):
            continue
        events = [e for e in trigger.detect(tick) if e['id'] not in announced]
        if events:
            found.append((trigger, events))
    if not found:
        return None
    return compose(found, tick)


def loudest(triggers):
    """The level a composed post goes out at: the loudest of the kinds whose
    words are actually IN it. A quiet flavor line riding under a pinging body is
    already part of a message that pings, and a NOTIFY line under a SILENT body
    would be suppressed by it, so the message takes the loudest of its parts
    rather than the body's alone.

    Only the parts that landed count. A losing body (the second BODY of a tick,
    or flavor lines dropped for the board's budget) is left out of the post
    entirely and waits for a later pass, so it must not raise the level of a
    message it contributed nothing to -- which is how the midday board briefly
    went out as a ping on an hour the nudge also fired."""
    return max((t.notify for t in triggers), key=LOUDNESS.get, default=SILENT)


def compose(found, tick):
    """One post out of everything that fired: the first body (or board) plus
    every flavor line. Shared with the preview, which composes one kind at a time."""
    body = next(((t, ev) for t, ev in found if t.kind != FLAVOR), None)
    lines, ids, flavors = [], [], []
    for trigger, events in found:
        if trigger.kind != FLAVOR:
            continue
        lines += trigger.render(events, tick).lines
        ids += [e['id'] for e in events]
        flavors.append(trigger)
    if body is None:
        return Post(kind='flavor', content='\n'.join(lines), event_ids=ids,
                    notify=loudest(flavors))
    trigger, events = body
    rendered = trigger.render(events, tick)
    body_ids = [e['id'] for e in events]
    if trigger.kind == BOARD:
        components = list(rendered.components)
        if lines:
            # The lines ride under the board as one text display -- unless
            # that breaks the board's budget, in which case they wait: only
            # the board's own id is announced, so they fire again next pass.
            with_lines = components + [{'type': 10, 'content': '\n'.join(lines)}]
            if over_budget(with_lines):
                ids, flavors = [], []
            else:
                components = with_lines
        return Post(kind=trigger.key, components=components, event_ids=body_ids + ids,
                    board=True, notify=loudest([trigger] + flavors))
    content = rendered.content + ('\n' + '\n'.join(lines) if lines else '')
    return Post(kind=trigger.key, content=content, components=button_rows(rendered.buttons),
                mentions=list(rendered.mentions) if trigger.notify == PING else [],
                event_ids=body_ids + ids, notify=loudest([trigger] + flavors))


def to_record(tick, post, sent):
    """What a pass should persist: (announced ids, standings) -- the ids only
    when the post actually went out, the standings only when they moved. Both
    None/empty means nothing to write."""
    ids = list(post.event_ids) if post and sent else []
    now = standings(tick)
    snapshot = now if now != (tick.state.get('standings') or []) else None
    return ids, snapshot


def samples(tick):
    """One post per registered kind, from fabricated events over the live tick
    -- real players, real games -- so every message the bot can send is on
    screen before it is switched on. Overrides and gates are ignored: the
    preview shows the whole menu.

    Nobody is notified: every sample goes out SILENT with no allowed_mentions.
    The fabricated events name real players, so a pinging kind would ping them
    for a message that is only a preview -- and because overrides are ignored,
    that includes the nudge, which the server may not have switched on at all.
    The mention still renders as text; it just carries no allowed_mentions."""
    posts = []
    for trigger in TRIGGERS:
        events = trigger.sample(tick)
        if events:
            post = compose([(trigger, events)], tick)
            post.mentions = []
            post.notify = SILENT
            posts.append(post)
    return posts
