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
          change, a clean sweep, a streak milestone. They post within a minute
          of the message that caused them, and the sticky reposts beneath them
          in the same pass. How a single result went is not commentary at all:
          the same pass reacts to the result itself
          (sticky_lambda.react_to_results).

Kinds:
  BODY    the message itself (the nudge). At most one body (or board) per
          tick: the first in registry order wins, the rest wait for a quieter
          hour.
  BOARD   a Components V2 board as the body (the last call, the midday
          standings). Any flavor lines of the same tick go beneath it as one
          more text display.
  FLAVOR  one-liners (first result, lead change, clean sweep, streak
          milestones, ties). They ride under whatever body posts, or post on
          their own when no body is due.

Replies: an event can name the message it answers (`reply_to`), and its post
then goes out as a Discord reply to that message: a welcome answers the
newcomer's first result, a streak milestone the result that reached it. A lead
line answers nothing (see detect_lead).
A post replies to one message or to none, and the first event decides which:
an event that does not fit waits for a later pass (compose).

How loud (Trigger.notify), quietest first: SILENT carries Discord's
suppress-notifications flag, so it lands without a push for anyone; NOTIFY
drops the flag but mentions nobody, so it notifies on each member's own channel
setting (the midday board and the tie line, which are once-a-day-ish and worth
noticing); PING also puts the people the render names into allowed_mentions
(the last call, the nudge and the welcome). A composed post takes the LOUDEST
of the kinds that fired into it (see loudest), since one message carries one
flag, and a reply lands at NOTIFY at least: it answers somebody's result, so
it never goes out silent.

Two rules gate every kind, whatever its cadence (see blocked): nothing posts
before the day's board has (the morning board opens the conversation), and a
quiet rule over the bot's own messages since anyone else last spoke -- the
sticky aside. How much of that holds a kind back is its `waits`: ANY (the
default) waits while there is any of it, so the bot never stacks two messages
in a row; OWN (the nudge) waits only while one of them is its own kind, so no
other post holds a nudge back but two nudges never stack; NEVER (the midday
board, the last call, the streak milestones) waits on nothing -- a scheduled
post is not the bot talking twice, and a one-liner an hour earlier must not
push it back. The last call needs that twice over: a day nobody has played puts
every server streak on the line, and that is exactly the day no one clears the
morning board out of `unanswered`, so ANY would hold it back on the day it has
the most to say. A streak milestone answers one result, so it must land beside
it rather than wait behind the reply to someone else's.

Events are plain dicts with an `id`. The state, shared by both passes, is the
set of ids announced today: nothing is said twice, and a kind that has to
remember more than that keeps it in its ids -- a lead change's id names who
took the lead, which is how the trigger knows the last leader it announced.
A trigger with `once=True` has a single event per day whose id is its key,
and is not even asked again once that has gone out -- what keeps the last
call from re-reading every player's streaks on the ticks after it fired.

Pure: no Discord, no DynamoDB. The two passes gather the inputs, make_tick
assembles them, evaluate() decides, scoreboard.send_commentary posts and
store.record_commentary persists the post's event ids.
"""

import hashlib
from collections import Counter
from dataclasses import dataclass, field
from datetime import timedelta
from functools import cached_property

from dateutil import parser as dateutil_parser

from game_parser import (
    build_games, format_scoreboard_components, format_travle_score, game_link_button,
    game_sort_key, is_poop, over_budget, rotation_points_base, score_sort_key,
    scoring_players, shown_streak, total_points, HEADER_COLOR, HIGHER_IS_BETTER,
    SCORES_COLOR, SCORING_OFF, SCORING_PLACEMENT,
)
from scoreboard import (
    LAST_CALL_HEADING, LOUDNESS, MAX_ACTION_ROWS, MAX_BUTTONS_PER_ROW, MIDDAY_TITLE,
    NOTIFY, PING, SILENT, build_name_map, is_sticky_message, reference_date,
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

# Streak milestones: the lengths a result is congratulated for, then every
# STREAK_MILESTONE_EVERY days past the last of them. From STREAK_WOW days on
# the line opens with "Wow!".
STREAK_MILESTONES = (3, 7, 14, 30, 50)
STREAK_MILESTONE_EVERY = 25
STREAK_WOW = 7

# Clean sweep: how much of the day it takes. The bar is a share of the day's
# scored games, not a count of results, so a five-game rotation asks for three
# and a sixteen-game board asks for nine; SWEEP_MIN_GAMES is the floor under
# that, so a three-game rotation still has to be swept outright rather than
# announced off two results.
SWEEP_MIN_GAMES = 3

# The metrics a tie can be talked about in, where beating the tie is a clear
# ask: fewer guesses, mistakes or hints, a travle closer to a Perfect, a higher
# score short of the game's ceiling, or a golf tally above 0 (beatable says
# where each tops out).
TIE_METRICS = ('guesses', 'connections', 'cryptic', 'travle', 'score', 'golf')


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
    posts: list          # (msg, game_key, uid, score) per result matched, oldest first
    games: list          # built Game list, enabled games only
    puzzle_numbers: dict
    rotation: object     # today's key list, or None when unrestricted
    streaks: object      # gather_streaks bundle, or None
    known_players: set   # every uid with a finalized result in this guild, ever
    player_aggs: object  # callable(uid) -> {SK: item}, memoized by the caller
    names: dict          # {uid: display name}, the board's mention fallback
    state: dict          # {'announced': [event ids that have gone out today]}
    board_posted: bool   # the board covering yesterday has gone out (or there is none)
    unanswered: list     # the bot's own messages since anyone else spoke, newest first

    @property
    def hour(self):
        return self.now.hour

    @cached_property
    def last_post(self):
        """{uid: aware datetime of their latest result}. Discord's timestamps
        share one format, so the newest is simply the greatest string."""
        latest = {}
        for msg, _, uid, _ in self.posts:
            latest[uid] = max(latest.get(uid, ''), msg['timestamp'])
        return {uid: dateutil_parser.isoparse(ts).astimezone(self.now.tzinfo)
                for uid, ts in latest.items()}

    @cached_property
    def first_posts(self):
        """{uid: id of the message holding their first result today}, in the
        order they first posted."""
        first = {}
        for msg, _, uid, _ in self.posts:
            first.setdefault(uid, msg['id'])
        return first

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

    @cached_property
    def games_played(self):
        """{uid: how many of today's scored games they have a result in}, counting
        only games that pay anyone yet (minimum_players): the games their points
        could have come from. A poop counts, since they had their go at it."""
        floor = self.cfg['minimum_players']
        fields = (self.results.get(g.key) or {} for g in self.scored)
        return Counter(uid for scores in fields if len(scores) >= floor for uid in scores)

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


def make_tick(cfg, now, messages, results, puzzle_numbers, posts, streaks, aggs,
              player_aggs, state, bot_id):
    """Assemble a Tick from what a pass has already fetched and parsed. Pure:
    the day arithmetic, the roster off the aggregate partition, and the two
    gate flags, so both passes build exactly the same view of the day. posts
    arrive newest first, as parse_results fills them."""
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
        results=results, posts=list(reversed(posts or ())),
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
    mentions: list = field(default_factory=list)    # who a PING kind notifies
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
    reply_to: str = None                            # the message it answers, if any


@dataclass(frozen=True)
class Trigger:
    """One message kind -- THE place to add or change what the bot says.

        key       config override key (commentary_overrides) and state key
        label     what the menu on /setup's Commentary form shows (<= 100 chars)
        describe  the menu's description line
        kind      BODY, BOARD or FLAVOR (see the module docstring)
        cadence   HOURLY (the daily lambda's tick) or STICKY (every minute)
        detect    callable(tick) -> [event]; pure, may read tick.state
        render    callable(events, tick) -> Rendered
        sample    callable(tick) -> [event]; fabricated, for the preview
        notify    how loudly it arrives: SILENT (no push), NOTIFY (no mention,
                  but no suppress flag either) or PING (which also notifies
                  the users its render lists as mentions). A post that
                  replies to a result lands at NOTIFY at least (loudest).
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
    if game.metric == 'travle':
        # The in-order checks break ties at the same +N, so they are part of
        # what is tied: the line reads like the board lines it names.
        return format_travle_score(score)
    return str(score)   # score, golf: the bare number, as the board line prints it


def beatable(game, score):
    """Is there a better score than this one? A tie at the best possible score
    is not an opportunity for anyone, and a game somebody has already topped
    cannot be won outright by anyone still to play it (still_winnable)."""
    if game.metric == 'guesses':
        return score > 1
    if game.metric == 'connections':
        return score[0] > 0
    if game.metric == 'cryptic':
        return score[1] > 0
    if game.metric == 'travle':
        return score[0] >= 0    # anything short of a Perfect, tier -1
    if game.metric in HIGHER_IS_BETTER:
        # The ceiling is the ace (performance_tier); a game without one has
        # no top to reach, so any score can be beaten.
        return not game.total or (score if game.metric == 'score' else score[0]) < game.total
    if game.metric == 'golf':
        return score > 0
    # A clock or an estimate (time, timed_win, fermi) has no top a result can
    # reach: there is always a faster solve or a closer guess.
    return True


def standings(tick):
    """[[uid, points], ...] best first."""
    return [[uid, p] for uid, p in sorted(tick.totals.items(), key=lambda kv: (-kv[1], kv[0]))]


def top_of(tick, game):
    """(best score, [players on it]) in `game` so far, poops aside, or
    (None, []) while nobody has a result that counts. The one reading of who
    is 1st that the sweep and the tie share."""
    scores = {u: s for u, s in (tick.results.get(game.key) or {}).items()
              if not is_poop(game.metric, s, game.total)}
    if not scores:
        return None, []
    best = min(scores.values(), key=lambda s: score_sort_key(game.metric, s))
    return best, sorted(u for u, s in scores.items() if s == best)


def sole_first(tick, game):
    """The one player alone at the top of `game` (non-poop), or None."""
    _, top = top_of(tick, game)
    return top[0] if len(top) == 1 else None


def still_winnable(tick, game):
    """Can a player who has not posted `game` yet still finish alone in 1st in
    it? Not once the best result in is the best the game allows (beatable):
    matching it would only tie."""
    best, _ = top_of(tick, game)
    return best is None or beatable(game, best)


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


def folded_through_yesterday(tick):
    """Is yesterday in the aggregates yet? It folds at finalize, which runs at
    post hour; until then the all-time player sets miss yesterday's newcomers
    and every streak reads a day short."""
    return store.prev_day_str(tick.day) <= (tick.cfg['last_finalized_day'] or '')


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

def _stake(overall, games):
    """What the last call names for one player or for the server, or None
    when nothing is on the line: {overall, games: [(key, streak)]} holding
    the game streaks, longest first, or else the overall day streak. Never
    both: one result in a game saves that game's streak and the day streak
    alike, so naming the two would say the same thing twice."""
    if games:
        return {'overall': 0, 'games': sorted(games, key=lambda kn: (-kn[1], kn[0]))}
    return {'overall': overall, 'games': []} if overall else None


def _server_at_risk(tick):
    """The server's own streaks that die at the close unless somebody scores,
    as a _stake.

    These are the board's numbers: the day streak under its heading, at risk
    only while nobody has scored, and the fire on each game title,
    off-rotation included. The streak bundle already reads a game nobody has
    scored today as the streak alive through yesterday, which is exactly the
    one on the line. Unlike a player's, a server streak is never spent by a
    poop: anyone who has not posted can still carry it.
    """
    streaks = tick.streaks or {}
    overall = 0 if any(tick.scorers.values()) else shown_streak(streaks.get('server', 0))
    games = [(key, n) for key, n in (streaks.get('games') or {}).items()
             if shown_streak(n) and not tick.scorers.get(key)]
    return _stake(overall, games)


def _players_at_risk(tick):
    """[{uid, overall, games}] for everyone with a streak that dies at the
    close unless they play, each a _stake plus its player. Sorted by the
    longest streak at stake.

    A streak is at risk only while the player can still do something about it,
    which is why POSTING closes a game here and SCORING keeps a streak alive
    everywhere else. A poop keeps nothing alive, but the puzzle is spent: that
    per-game streak is already gone, not on the line, so pinging it would ask
    for a result the player cannot produce. Their overall streak survives a
    poop -- any other game can still carry it -- so it stays at risk until
    every game they could post is posted.
    """
    scored_any = {uid for uids in tick.scorers.values() for uid in uids}
    enabled = {g.key for g in tick.games}
    posted = {key: set(tick.results.get(key) or {}) for key in enabled}
    risks = []
    for uid in sorted(tick.known_players)[:LAST_CALL_MAX_PLAYERS]:
        aggs = tick.player_aggs(uid) or {}
        overall = 0
        if uid not in scored_any and any(uid not in uids for uids in posted.values()):
            overall = shown_streak(
                store.display_streak(aggs.get(store.SERVER_AGG_SK), tick.day, False))
        games = []
        for sk, item in aggs.items():
            if not sk.startswith(store.GAME_AGG_PREFIX):
                continue
            key = store.game_key_from_sk(sk)
            if key not in enabled or uid in posted[key]:
                continue
            n = shown_streak(store.display_streak(item, tick.day, False))
            if n:
                games.append((key, n))
        stake = _stake(overall, games)
        if stake:
            risks.append({'uid': uid, **stake})
    risks.sort(key=lambda r: -max([r['overall']] + [n for _, n in r['games']]))
    return risks


def detect_last_call(tick):
    hours = tick.cfg['commentary_last_call_hours']
    if not hours or tick.close - tick.now > timedelta(hours=hours):
        return []
    server = _server_at_risk(tick)
    players = _players_at_risk(tick)[:LAST_CALL_MAX_NAMES]
    if not server and not players:
        return []
    return [{'id': 'last_call', 'server': server, 'players': players}]


def render_last_call(events, tick):
    """The last call as a board in two blocks, like the daily one: the fixed
    heading with the day streak, then the game streaks, those with no player
    tied to them first and then the games players are named in, each longest
    first. No countdown: when it posts is the guild's
    commentary_last_call_hours, so the post only says what is at stake.

    One line per streak. The fire after a game's name is the server's streak,
    as on the board's game titles, there only while that streak is on the
    line. After it come the players whose own streak there is on the line,
    each pinged: a bare name when theirs is as long as the server's, since
    they scored on every day of it and the number is already on the line, and
    their own number when it is shorter."""
    e = events[0]
    server = e['server'] or {'overall': 0, 'games': []}
    fires = dict(server['games'])           # game key: the server's streak
    if server['overall']:
        fires[None] = server['overall']     # None: the day streak
    named = {}                              # the same keys: [(uid, their streak)]
    for p in e['players']:
        for key, n in p['games'] or [(None, p['overall'])]:
            named.setdefault(key, []).append((p['uid'], n))

    def line(key):
        tag = '\U0001F4C5 **Day streak**' if key is None else game_tag(tick.by_key[key])
        fire = fires.get(key, 0)
        if fire:
            tag += f' \U0001F525{fire}'
        who = ' · '.join(mention(uid) if n == fire else f'{mention(uid)} \U0001F525{n}'
                         for uid, n in sorted(named.get(key, ()), key=lambda un: -un[1]))
        return f'{tag}: {who}' if who else tag

    def order(key):
        longest = max([fires.get(key, 0)] + [n for _, n in named.get(key, ())])
        return key in named, -longest, tick.by_key[key].title.lower()

    def block(color, lines):
        return {'type': 17, 'accent_color': color,
                'components': [{'type': 10, 'content': '\n'.join(lines)}]}

    keys = fires.keys() | named.keys()
    heading = [LAST_CALL_HEADING] + ([line(None)] if None in keys else [])
    games = [line(key) for key in sorted(keys - {None}, key=order)]
    components = [block(HEADER_COLOR, heading)]
    if games:
        components.append(block(SCORES_COLOR, games))
    return Rendered(components=components, mentions=[p['uid'] for p in e['players']])


def sample_last_call(tick):
    """Every kind of line: a day streak, a server streak with no player tied
    to it, and a server streak one player has carried all along (a bare name)
    with a shorter streak behind theirs."""
    a, b, c = sample_players(tick, 3)
    keys = [g.key for g in sample_games(tick, 2)]
    # With no game to put them on, a and b fall back to day streaks (_stake).
    players = [{'uid': a, **_stake(15, [(key, 15) for key in keys[:1]])},
               {'uid': b, **_stake(4, [(key, 4) for key in keys[:1]])},
               {'uid': c, **_stake(5, [])}]
    server = _stake(0, list(zip(keys, (15, 12))))
    return [{'id': 'last_call', 'server': server, 'players': players}]


# --- Midday: the standings so far, as a board -------------------------------------

def before_midday(tick):
    """Is the midday standings hour still to come? The hour the midday board
    waits for, and the line between an early lead and a plain one."""
    return tick.hour < tick.cfg['commentary_midday_hour']


def detect_midday(tick):
    if before_midday(tick):
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
    """Everyone with a result today and none ever finalized here, in the order
    they first posted, each answering that first result."""
    # Until yesterday is folded, someone who started yesterday still looks new.
    if not folded_through_yesterday(tick):
        return []
    return [{'id': f'first:{uid}', 'uid': uid, 'reply_to': msg_id}
            for uid, msg_id in tick.first_posts.items() if uid not in tick.known_players]


def render_first_play(events, tick):
    return Rendered(lines=[pick(tick, f"first:{e['uid']}", [
        f"\U0001F389 First result from {mention(e['uid'])}, welcome to the board!",
        f"\U0001F389 {mention(e['uid'])} is on the board for the first time!",
    ]) for e in events], mentions=[e['uid'] for e in events])


def sample_first_play(tick):
    uid = sample_players(tick, 1)[0]
    return [{'id': f'first:{uid}', 'uid': uid}]


# --- Lead change ------------------------------------------------------------------

def lead_id(n, uid):
    """The id of the day's n-th lead announcement. It names who took the lead,
    so the day's announced ids double as the record of the last leader
    announced (announced_leads) and the trigger needs no state of its own."""
    return f'lead:{n}:{uid}'


def announced_leads(state):
    """[(n, uid)] for every lead announced today, in the order they went out."""
    leads = []
    for event_id in state.get('announced') or ():
        parts = event_id.split(':')
        if len(parts) == 3 and parts[0] == 'lead' and parts[1].isdigit():
            leads.append((int(parts[1]), parts[2]))
    return sorted(leads)


def detect_lead(tick):
    """Someone takes sole control of the lead: one player alone on top, with
    somebody behind them, who is not the leader the day's latest lead
    announcement named. There is no points floor: every change at the top is
    news, however small the margin.

    The day's first leader has one more bar to clear. Points pile up with
    every game played, so early on the standings rank who has played most,
    not who is playing best: someone who posts four results before anyone
    else would lead the moment a second player posts one. So until a lead has
    been announced, the lead must be contested: everyone else, between them,
    has played at least as many of today's scored games as the leader (a
    leader on 3 is contested by one player on 1 and another on 2). Someone
    who passes the early leader on fewer games clears that at once, since the
    player they passed has already played more. Once the day has a leader,
    every change is announced as it happens.

    The yardstick is the last ANNOUNCEMENT, not the last pass. Compared with
    the previous pass's standings, a tie at the top resolving in one player's
    favour looked like no change at all -- that player was already among the
    leaders -- and since a shared lead is never announced, taking the lead out
    of a tie was the one lead change the bot could not see. Judged against the
    last announcement, a leader who is briefly caught and pulls clear again is
    not announced twice, and a change that could not post when it happened
    (before the board, say) is said once it can rather than lost.

    The line replies to no result and goes out silent: a lead is news about
    the standings, not about one result, and the result that changes it is
    not always the leader's (on the placement scale every newcomer raises
    what each place pays)."""
    now = standings(tick)
    if len(now) < 2 or now[1][1] == now[0][1]:
        return []
    leader, points = now[0]
    leads = announced_leads(tick.state)
    if leads and leads[-1][1] == leader:
        return []
    chasers = [[uid, p] for uid, p in now[1:] if p == now[1][1]]
    played = tick.games_played
    if not leads and played[leader] > sum(played.values()) - played[leader]:
        return []
    n = len(leads) + 1
    return [{'id': lead_id(n, leader), 'n': n, 'leader': leader, 'points': points,
             'chasers': chasers, 'early': n == 1 and before_midday(tick)}]


def render_lead(events, tick):
    """The day's first line says who is in front, since nothing has changed
    hands yet: an early lead before the midday standings, a plain one after
    them, because the games check can hold the first lead back until late in
    the day. Every later line names someone new (detect_lead skips the last
    leader announced), so it says they took it."""
    lines = []
    for e in events:
        who, points = mention(e['leader']), pts(e['points'])
        if e['early']:
            options = [f"\U0001F451 {who} is out to an early lead with {points}",
                       f"\U0001F451 {who} jumps out to an early lead with {points}"]
        elif e['n'] == 1:
            options = [f"\U0001F451 {who} leads with {points}",
                       f"\U0001F451 {who} is out in front with {points}"]
        else:
            options = [f"\U0001F451 {who} takes the lead with {points}",
                       f"\U0001F451 New leader: {who} on {points}"]
        line = pick(tick, e['id'], options)
        chasers = ', '.join(f'{mention(uid)} {p}' for uid, p in e['chasers'])
        lines.append(f'{line} ({chasers})' if chasers else line)
    return Rendered(lines=lines)


def sample_lead(tick):
    """Two lines: the day's early lead, then the lead changing hands."""
    a, b = sample_players(tick, 2)
    return [{'id': lead_id(1, a), 'n': 1, 'leader': a, 'points': 9, 'chasers': [[b, 7]],
             'early': True},
            {'id': lead_id(2, b), 'n': 2, 'leader': b, 'points': 14, 'chasers': [[a, 12]],
             'early': False}]


# --- Clean sweep: alone in 1st in every game, or still on course for it ---------
# How a single result went (an ace, a flawless grid, no hints) is the sticky's
# reaction on that result (sticky_lambda.react_to_results), not a line here; a
# sweep spans the day's games, so it stays commentary.

def can_still_sweep(tick, uid):
    """Can `uid` still finish alone in 1st in every game on today's slate?

    Only while nothing is lost. A game they have posted they must lead
    outright: their result is final, so a loss or a tie for 1st never comes
    back, which is what the old "1st in 3 of 5" line never checked. A game
    only they have posted is theirs for now, since nobody else has had a go
    at it. A game they have not posted must still be winnable (still_winnable)."""
    for g in tick.scored:
        if uid in (tick.results.get(g.key) or {}):
            if sole_first(tick, g) != uid:
                return False
        elif not still_winnable(tick, g):
            return False
    return True


def detect_sweep(tick):
    """A clean sweep in the making, then the clean sweep: one player alone in
    1st in most of the day's scored games while they can still be alone in 1st
    in all of them (can_still_sweep), and again once they are.

    "Most" is measured against the whole slate (tick.scored), not against the
    games played so far: being in front of the only two results anyone has
    posted at 9am is not a sweep, it is the first two results. Only contested
    games count toward it, since leading a game nobody else has played is not
    beating anybody. A strict majority of the slate can only ever belong to
    one player, so this never has to break a tie between two people holding
    the same number of games.

    Two events, each said once a day: `sweep:<uid>` while part of the slate is
    still open, then `sweep:<uid>:all` once they lead every game on it.
    Separate ids, so the finished sweep still gets its line after the one that
    saw it coming; a slate small enough that the bar is all of it only ever
    gets the finished line."""
    floor = max(2, tick.cfg['minimum_players'])
    contested = [g for g in tick.scored if len(tick.results.get(g.key) or {}) >= floor]
    firsts = [f for f in (sole_first(tick, g) for g in contested) if f]
    if not firsts:
        return []
    wins = {uid: firsts.count(uid) for uid in set(firsts)}
    uid = max(sorted(wins), key=wins.get)
    n, of = wins[uid], len(tick.scored)
    if n < SWEEP_MIN_GAMES or n * 2 <= of or not can_still_sweep(tick, uid):
        return []
    done = n == of
    return [{'id': f"sweep:{uid}{':all' if done else ''}", 'uid': uid, 'n': n, 'of': of,
             'done': done}]


def render_sweep(events, tick):
    """The finished sweep, or one on its way with how many games are still open."""
    lines = []
    for e in events:
        who, key = mention(e['uid']), f"sweep:{e['id']}"
        if e['done']:
            line = pick(tick, key, [
                f"\U0001F9F9 {who} is 1st in all {e['n']} of today's games, a clean sweep",
                f"\U0001F9F9 Clean sweep: {who} is 1st in every game today, all {e['n']} of them",
            ])
        else:
            left = e['of'] - e['n']
            line = pick(tick, key, [
                f"\U0001F9F9 {who} is 1st in {e['n']} of today's {e['of']} games "
                f"with {left} still open, a clean sweep in the making",
                f"\U0001F9F9 A clean sweep in the making: {who} is 1st in {e['n']} of "
                f"{e['of']} games, {left} to go",
            ])
        lines.append(line)
    return Rendered(lines=lines)


def sample_sweep(tick):
    """Both lines a sweep can get: on its way at the bar, then finished. A
    slate small enough that the bar is all of it only ever gets the second."""
    uid = sample_players(tick, 1)[0]
    of = max(len(tick.scored), SWEEP_MIN_GAMES)
    n = max(SWEEP_MIN_GAMES, of // 2 + 1)
    making = [{'id': 'sweep:sample', 'uid': uid, 'n': n, 'of': of, 'done': False}]
    return (making if n < of else []) + [
        {'id': 'sweep:sample:all', 'uid': uid, 'n': of, 'of': of, 'done': True}]


# --- Streak milestones: a reply to the result that reached one ------------------

def is_milestone(n):
    """Is an n-day streak one to congratulate? 3, 7, 14, 30, 50, then every 25
    days, and never a streak the board would not print (shown_streak)."""
    if not shown_streak(n):
        return False
    return n in STREAK_MILESTONES or (
        n > STREAK_MILESTONES[-1] and n % STREAK_MILESTONE_EVERY == 0)


def detect_streak(tick):
    """Every streak today's results carry to a milestone (is_milestone): a
    player's day streak, at their first scoring result of the day, and their
    streak in each game they scored, at the result that counts there. Each
    event answers that result.

    Streaks advance on SCORING, as they fold at finalize, so a poop extends
    nothing and neither does a game still short of minimum_players. Until
    yesterday is folded every streak reads a day short, so nothing is said
    before then (folded_through_yesterday)."""
    if not folded_through_yesterday(tick):
        return []
    holders = {}                            # message id: whose results it holds
    for msg, _, uid, _ in tick.posts:
        holders.setdefault(msg['id'], set()).add(uid)
    events, seen = [], set()
    for msg, key, uid, _ in tick.posts:     # oldest first
        if uid not in tick.scorers.get(key, ()):
            continue
        aggs = tick.player_aggs(uid) or {}
        for streak in (None, key):          # None: the day streak
            if (uid, streak) in seen:
                continue
            seen.add((uid, streak))
            sk = store.SERVER_AGG_SK if streak is None else store.game_agg_sk(streak)
            n = store.display_streak(aggs.get(sk), tick.day, True)
            if is_milestone(n):
                events.append({'id': f"streak:{uid}:{streak or 'day'}", 'uid': uid,
                               'game': streak, 'n': n, 'reply_to': msg['id'],
                               'shared': len(holders[msg['id']]) > 1})
    return events


def article(n):
    """The article before a number read aloud: an 800-day streak, a 75-day
    one. Of the milestones, only those starting with 8 open on a vowel."""
    return 'an' if str(n).startswith('8') else 'a'


def and_list(items):
    """`a`, `a and b`, `a, b and c`."""
    return items[0] if len(items) == 1 else f"{', '.join(items[:-1])} and {items[-1]}"


def streak_phrase(tick, e):
    """A milestone as the line names it: `a 14-day streak`, or
    `a 7-day 📗 **Wordle** streak` for a game's."""
    game = f"{game_tag(tick.by_key[e['game']])} " if e['game'] else ''
    return f"{article(e['n'])} {e['n']}-day {game}streak"


def render_streak(events, tick):
    """One line per player, naming every milestone their result reached, the
    day streak first, then a gray pointer at /stats. "You're" when the message
    answered is theirs alone, their name when it is a shared grid, where "you"
    could be anyone in it. From STREAK_WOW days on, the line opens with "Wow!".

    A day streak as long as a game streak the same result reached is left
    out: they played that game on every day of it, so it would say the same
    thing twice (on a one-game server, it always would)."""
    reached = {}
    for e in events:
        reached.setdefault(e['uid'], []).append(e)
    lines = []
    for uid, streaks in reached.items():
        game_lengths = {e['n'] for e in streaks if e['game']}
        streaks = sorted((e for e in streaks if e['game'] or e['n'] not in game_lengths),
                         key=lambda e: (e['game'] is not None, -e['n']))
        wow = 'Wow! ' if max(e['n'] for e in streaks) >= STREAK_WOW else ''
        who = f'{mention(uid)} is' if any(e.get('shared') for e in streaks) else "You're"
        phrases = and_list([streak_phrase(tick, e) for e in streaks])
        lines.append(f'{wow}{who} on {phrases}! Keep it up!')
    lines.append('-# `/stats` shows all your streaks')
    return Rendered(lines=lines)


def sample_streak(tick):
    """A day streak and a game streak reaching milestones on the same result."""
    uid = sample_players(tick, 1)[0]
    return [{'id': 'streak:sample:day', 'uid': uid, 'game': None, 'n': 14}] + [
        {'id': f'streak:sample:{g.key}', 'uid': uid, 'game': g.key, 'n': 7}
        for g in sample_games(tick, 1)]


# --- Tie for first, in a game where it can be broken ---------------------------------

def detect_tie(tick):
    events = []
    for g in tick.scored:
        if g.metric not in TIE_METRICS:
            continue
        best, tied = top_of(tick, g)
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
    # A score game's scale is its own (5 greens, 100%, 5000 points), so its
    # sample sits at four fifths of the ceiling rather than at a fixed number.
    # A golf tally has no ceiling to take a share of; it counts down to 0.
    score = {'guesses': 4, 'connections': (1, 4), 'cryptic': (2, 2, 0, 0, 0),
             'travle': (0, 1, 0, -3), 'golf': 40,
             'score': g.total * 4 // 5}[g.metric]
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
    # precisely when this kind has the most to say -- every server streak is
    # on the line.
    Trigger('last_call', 'Last call', 'Streaks about to break, a few hours before the close',
            BOARD, HOURLY, detect_last_call, render_last_call, sample_last_call,
            notify=PING, once=True, waits=NEVER),
    Trigger('midday', 'Midday standings', 'The points standings so far, once, at the midday hour',
            BOARD, HOURLY, detect_midday, render_midday, sample_midday, once=True,
            notify=NOTIFY, waits=NEVER),
    # Off until its cadence is settled: a nudge per player, each on the hour that
    # player goes quiet, put four pinging posts into a 40-message day (the
    # 2026-09-15 replay). A server that wants it ticks it on /setup's Commentary form.
    Trigger('nudge', 'Points nudge', 'Ask someone back for the games they left open',
            BODY, HOURLY, detect_nudge, render_nudge, sample_nudge, notify=PING,
            default=False, waits=OWN, headings=NUDGE_HEADINGS),
    # The one flavor line that pings: it is written to one person, who has no
    # reason yet to be watching the channel.
    Trigger('first_play', 'First result',
            "A welcome under someone's first ever result, pinging them",
            FLAVOR, STICKY, detect_first_play, render_first_play, sample_first_play,
            notify=PING),
    Trigger('lead', 'Lead changes',
            'The first leader once the rest have played as many games between them, then every change',
            FLAVOR, STICKY, detect_lead, render_lead, sample_lead),
    Trigger('sweep', 'Clean sweep',
            'A clean sweep in the making (1st in most games, none lost), then the sweep',
            FLAVOR, STICKY, detect_sweep, render_sweep, sample_sweep),
    # waits=NEVER: each line answers one result and has to land beside it. Two
    # players reaching milestones in one pass answer different results, and
    # under ANY the second would wait behind the first reply until somebody
    # else spoke, which can be hours after the result it congratulates.
    Trigger('streak', 'Streak milestones',
            'A reply to a result that reaches a 3, 7, 14, 30 or 50-day streak, then every 25',
            FLAVOR, STICKY, detect_streak, render_streak, sample_streak, waits=NEVER),
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
    announced until the caller records the post."""
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


def loudest(triggers, reply=False):
    """The level a composed post goes out at: the loudest of the kinds whose
    words are actually IN it. A quiet flavor line riding under a pinging body is
    already part of a message that pings, and a NOTIFY line under a SILENT body
    would be suppressed by it, so the message takes the loudest of its parts
    rather than the body's alone.

    Only the parts that landed count. A losing body (the second BODY of a tick,
    flavor lines dropped for the board's budget, or a line that does not
    answer the same message as the post) is left out of the post entirely and
    waits for a later pass, so it must not raise the level of a
    message it contributed nothing to -- which is how the midday board briefly
    went out as a ping on an hour the nudge also fired.

    A reply is never silent: it answers somebody's result, so it lands at
    NOTIFY at least, whatever its kinds would say on their own. It still pings
    nobody by itself (send_commentary turns replied_user off), so the player
    it answers hears it on their own channel setting, like everyone else."""
    levels = [t.notify for t in triggers] + ([NOTIFY] if reply else [])
    return max(levels, key=LOUDNESS.get, default=SILENT)


def compose(found, tick):
    """One post out of everything that fired: the first body (or board) plus
    every flavor line. Shared with the preview, which composes one kind at a time.

    A post is a reply to one message or a plain post, and the first event, in
    registry order, decides which (`reply_to`). Only the events that answer
    the same message, or for a plain post none, go in it. The rest are left
    out, to fire again on a later pass as a losing body does. So a reply
    carries nothing but what answers its message: a line about somebody else
    (the lead, a sweep) riding in it would read as being about the player it
    answers, and would land as loudly as the reply."""
    reply_to = found[0][1][0].get('reply_to')
    found = [(t, [e for e in events if e.get('reply_to') == reply_to])
             for t, events in found]
    found = [(t, events) for t, events in found if events]
    body = next(((t, ev) for t, ev in found if t.kind != FLAVOR), None)
    flavors = [(t, ev, t.render(ev, tick)) for t, ev in found if t.kind == FLAVOR]
    lines = [line for _, _, rendered in flavors for line in rendered.lines]
    if body is None:
        return make_post('flavor', flavors, content='\n'.join(lines))
    trigger, events = body
    rendered = trigger.render(events, tick)
    if trigger.kind == BOARD:
        components = list(rendered.components)
        if lines:
            # The lines ride under the board as one text display -- unless
            # that breaks the board's budget, in which case they wait: only
            # the board's own id is announced, so they fire again next pass.
            with_lines = components + [{'type': 10, 'content': '\n'.join(lines)}]
            if over_budget(with_lines):
                flavors = []
            else:
                components = with_lines
        return make_post(trigger.key, [(trigger, events, rendered)] + flavors,
                         components=components, board=True)
    content = rendered.content + ('\n' + '\n'.join(lines) if lines else '')
    return make_post(trigger.key, [(trigger, events, rendered)] + flavors,
                     content=content, components=button_rows(rendered.buttons))


def make_post(kind, parts, **fields):
    """The Post for the kinds that made it into one message -- `parts`, as
    (trigger, events, rendered) -- with everything that follows from which
    ones did: the ids it announces, the message it answers, how loudly it
    lands (never silent when it answers one) and who it pings (the mentions
    of its PING kinds)."""
    events = [e for _, evs, _ in parts for e in evs]
    reply_to = next((e['reply_to'] for e in events if e.get('reply_to')), None)
    return Post(kind=kind, event_ids=[e['id'] for e in events],
                notify=loudest([t for t, _, _ in parts], reply=bool(reply_to)),
                mentions=[uid for t, _, r in parts if t.notify == PING for uid in r.mentions],
                reply_to=reply_to,
                **fields)


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
