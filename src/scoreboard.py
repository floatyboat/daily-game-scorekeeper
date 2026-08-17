"""Shared orchestration above game_parser: session, fetch, parse, dedup."""
import time

import requests
from datetime import timedelta
from collections import defaultdict

import store
from game_parser import (
    compute_puzzle_numbers, build_games, scoring_players,
    make_timestamp_checker, match_message, _avatar_ahash, WORDLE_BOT_ID,
)

DISCORD_API_BASE = 'https://discord.com/api/v10'

FLAG_SUPPRESS_EMBEDS         = 1 << 2    # 4
FLAG_EPHEMERAL               = 1 << 6    # 64
FLAG_SUPPRESS_NOTIFICATIONS  = 1 << 12   # 4096
FLAG_IS_COMPONENTS_V2        = 1 << 15   # 32768

# Custom IDs for the sticky's interactive buttons, and the sticky's heading.
# Defined here, in the module both lambdas already import, so the producer
# (sticky_lambda) and the consumers (interaction_lambda) share one source of
# truth instead of duplicating the literal strings and silently drifting apart.
PLAY_BUTTON_CUSTOM_ID = 'sticky_play'
MORE_BUTTON_CUSTOM_ID = 'sticky_more'
SCORES_BUTTON_CUSTOM_ID = 'sticky_scores'
STICKY_HEADING = "\U0001F47E **Now Playing**"

# Channel types the bot can be pointed at, and the permission bits that gate the
# admin commands. Shared by register_commands.py (which declares them to Discord)
# and interaction_lambda.py (which re-verifies them server-side).
TEXT_CHANNEL_TYPES = (0, 5)   # guild text, announcement
PERM_ADMINISTRATOR = 0x8
PERM_MANAGE_GUILD = 0x20

# Discord payload caps the rendered surfaces have to fit inside. Today's 18
# games clear both: /play tops out at 4 button rows plus the Random row, and the
# /setup games menu uses 18 of 25 options.
#
# FUTURE: when GAME_SPECS outgrows either cap, split across two messages (the
# interaction reply plus a follow-up) rather than truncating -- a silently
# dropped game looks identical to one an admin turned off. These live here as
# constants so that split has something to divide by.
MAX_ACTION_ROWS = 5          # top-level components in one message
MAX_BUTTONS_PER_ROW = 5
MAX_SELECT_OPTIONS = 25      # options in one string select
MAX_MESSAGE_LENGTH = 2000    # characters in one message's content


# (connect, read) seconds applied to every Discord call made through
# make_session. requests has no default timeout, and Discord occasionally stalls
# a connection rather than erroring on it, so an uncapped call waits for the
# caller's own ceiling to fire instead: one sticky run has taken 27s against a
# 30s Lambda timeout, and on an interaction the same stall spends a 3-second
# budget that can't be recovered. Read is generous enough for a 100-message page
# on a slow day and still well inside every caller's limit.
DISCORD_TIMEOUT = (3.05, 10)


class _TimeoutSession(requests.Session):
    """Session that applies DISCORD_TIMEOUT to calls that don't set their own.

    requests only supports a per-call timeout, and the alternative -- passing it
    at every get/post/patch/delete across four modules -- is exactly the kind of
    thing the next call site forgets, silently reverting to no cap at all.
    """

    def request(self, *args, **kwargs):
        kwargs.setdefault('timeout', DISCORD_TIMEOUT)
        return super().request(*args, **kwargs)


def make_session(token, pool_connections=4, pool_maxsize=32):
    s = _TimeoutSession()
    s.headers.update({
        'Authorization': f'Bot {token}',
        'Content-Type': 'application/json',
    })
    s.mount('https://', requests.adapters.HTTPAdapter(
        pool_connections=pool_connections, pool_maxsize=pool_maxsize,
    ))
    return s


_guild_id_cache = {}


def get_channel_guild_id(session, channel_id):
    """guild_id owning a channel, or None for a DM.

    REST channel-message payloads omit guild_id (it's gateway-only), so resolve
    it with one GET /channels/{id} and cache for the process lifetime.
    """
    if channel_id not in _guild_id_cache:
        r = session.get(f'{DISCORD_API_BASE}/channels/{channel_id}')
        r.raise_for_status()
        _guild_id_cache[channel_id] = r.json().get('guild_id')
    return _guild_id_cache[channel_id]


def safe_guild_id(session, channel_id):
    """get_channel_guild_id that returns None instead of raising.

    Streak decoration is optional on every surface, so guild resolution must
    never break a caller; None simply renders the streak-less view.
    """
    try:
        return get_channel_guild_id(session, channel_id)
    except Exception:
        return None


def gather_streaks(guild_id, ref_date, results, games, minimum_players=1,
                   include_players=True):
    """Display-ready streak numbers for one board render, or None when the
    store can't serve them (no guild, IAM grant not applied yet, outage) --
    callers render streak-less, so store problems never break a view.

    Takes the built `games` (not just their keys) because streak eligibility is
    scoring, not merely posting: game_parser.scoring_players() needs each game's
    metric to tell a scoring result from a poop. That is the same rule
    store.finalize_day() folds, so the two never disagree.

    Works identically on both sides of the daily finalize because
    store.display_streak() folds the "played on ref_date" flag in itself:
    a live view (aggregates through yesterday) and a just-finalized view
    (ref_date already folded) produce the same numbers, so every surface
    (daily post, Scores, Play, sticky) shares this one path.

    Bundle (plain ints/strings, JSON-safe):
      server        server-wide streak to show (points scored in ANY game that
                    day; displayed on the sticky, not the scoreboard)
      games         {game_key: streak to show}
      broken        {game_key: streak that ended on ref_date}
      players_30d   {game_key: rolling 30-day distinct-player count, as of the
                    last finalize (store.refresh_players_30d); 0 for a game
                    whose aggregate predates the field or has never been played}
      players_total {game_key: all-time distinct-player count}
      players       {game_key: {user_id: streak to show}} (players who SCORED
                    on ref_date only; empty when include_players=False). A
                    player who posted a poop is absent, so their score line
                    renders untagged -- their streak for that game is over.
      players_overall {user_id: overall streak to show} -- days running that
                    player scored in ANY game; drives the points summary, same
                    population/emptiness rule as `players`
    """
    if not guild_id:
        return None
    try:
        day = store.day_str(ref_date)
        game_keys = [g.key for g in games]
        # Poop scores earn 0 points and keep nothing alive; everything below
        # keys off who scored, never off who merely posted.
        scorers = scoring_players(results, games, minimum_players)
        aggs = store.query_aggs(store.guild_pk(guild_id))
        game_items = {store.game_key_from_sk(sk): item for sk, item in aggs.items()
                      if sk.startswith(store.GAME_AGG_PREFIX)}

        bundle = {'games': {}, 'broken': {}, 'players_30d': {},
                  'players_total': {}, 'players': {}, 'players_overall': {},
                  'server': store.display_streak(
                      aggs.get(store.SERVER_AGG_SK), day,
                      any(scorers.values()))}
        for key in game_keys:
            item = game_items.get(key)
            bundle['games'][key] = store.display_streak(item, day, bool(scorers.get(key)))
            ended = store.broken_streak_on(item, day)
            if ended:
                bundle['broken'][key] = ended
            # int(): DynamoDB hands numbers back as Decimal, and the bundle is
            # documented (and deferred-invoke serialized) as JSON-safe.
            bundle['players_30d'][key] = int((item or {}).get('players_30d') or 0)
            bundle['players_total'][key] = len((item or {}).get('players') or ())

        if include_players:
            pairs = sorted({(uid, key) for key in game_keys
                            for uid in scorers.get(key, ())})
            uids = sorted({uid for uid, _ in pairs})
            # Per-game and overall player aggregates ride in one batch; the
            # overall one is filed under game key None.
            keys = [{'PK': store.player_pk(guild_id, uid), 'SK': store.game_agg_sk(key)}
                    for uid, key in pairs]
            keys += [{'PK': store.player_pk(guild_id, uid), 'SK': store.SERVER_AGG_SK}
                     for uid in uids]
            fetched = {}
            for item in store.batch_get(keys):
                uid = item['PK'].split('#PLAYER#', 1)[1]
                sk = item['SK']
                game = None if sk == store.SERVER_AGG_SK else store.game_key_from_sk(sk)
                fetched[(uid, game)] = item
            for uid, key in pairs:
                bundle['players'].setdefault(key, {})[uid] = store.display_streak(
                    fetched.get((uid, key)), day, True)
            for uid in uids:
                bundle['players_overall'][uid] = store.display_streak(
                    fetched.get((uid, None)), day, True)
        return bundle
    except Exception as e:
        print(f'store: streak read failed, rendering without streaks -- '
              f'{type(e).__name__}: {e}')
        return None


def fetch_messages(session, channel_id, limit=100):
    """Up to `limit` messages from a channel, newest first.

    One loop covering every page including the first. The first page used to be
    fetched ahead of the loop, so an *empty* channel fell straight into the
    pagination step and died on messages[-1] with an IndexError. That is the
    state every freshly created channel is in, and for the sticky it deadlocks:
    the sticky is the thing that would have put the first message there.

    A short page means the channel is exhausted -- Discord returns fewer
    messages than asked only when there is no more history -- so stop instead
    of spending a round trip per run rediscovering the end of a small channel.
    """
    messages = []
    while len(messages) < limit:
        page_size = min(limit - len(messages), 100)
        url = f'{DISCORD_API_BASE}/channels/{channel_id}/messages?limit={page_size}'
        if messages:
            url += f'&before={messages[-1]["id"]}'
        r = session.get(url)
        r.raise_for_status()
        page = r.json()
        if not isinstance(page, list) or not page:
            break
        messages += page
        if len(page) < page_size:
            break
    return messages


def reference_date(now, tz, hours_after_midnight, days_back=0):
    """TZ-naive midnight-aligned scoreboard date.

    Walks back through the HOURS_AFTER_MIDNIGHT cutoff before applying
    days_back, so a call at 02:00 in TIMEZONE with days_back=0 still reports
    the previous calendar day (the active scoring window hasn't closed).
    """
    aware = now.astimezone(tz) if now.tzinfo else now.replace(tzinfo=tz)
    if aware.hour < hours_after_midnight:
        aware -= timedelta(days=1)
    aware -= timedelta(days=days_back)
    return aware.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)


def parse_results(messages, ref_date, tz, hours_after_midnight, time_window_hours,
                  *, avatar_hashes=None, game_overrides=None):
    puzzle_numbers = compute_puzzle_numbers(ref_date)
    games = build_games(puzzle_numbers, game_overrides)
    checker = make_timestamp_checker(ref_date, tz, hours_after_midnight, time_window_hours)
    results = defaultdict(dict)
    for msg in messages:
        for game_key, score, metadata, uid_override in match_message(
                msg, games, checker, avatar_hashes=avatar_hashes):
            user_id = (uid_override
                       or msg.get('interaction_metadata', {}).get('user', {}).get('id')
                       or msg['author']['id'])
            results[game_key][user_id] = score
            puzzle_numbers.update(metadata)
    return results, puzzle_numbers


def build_avatar_pool(session, messages, checker, guild_id=None):
    """{user_id: (avatar hash, ...)} for attributing multi-player Wordle grids.

    Built only when the window actually holds a multi-player image, since it
    costs a CDN round trip per candidate user (plus a member lookup when
    guild_id is known). That guard is an in-memory scan of messages the caller
    already fetched, so a channel the Wordle bot doesn't post in pays nothing
    for this call. Each user can carry more than one hash -- see
    _user_avatar_hashes for why -- and _match_avatar scores them by their
    closest one.

    guild_id is optional so a caller that can't resolve it still gets the
    global-avatar pool rather than nothing.
    """
    if not _has_multiplayer_wordle(messages, checker):
        return {}
    uid_to_avatar = _extract_user_avatars(messages)
    if not uid_to_avatar:
        return {}
    # Whole-guild server avatars in one read, before the per-user hashing.
    server_avatars = _guild_server_avatars(session, guild_id)

    from concurrent.futures import ThreadPoolExecutor, as_completed
    pool = {}
    with ThreadPoolExecutor(max_workers=16) as ex:
        futures = {
            ex.submit(_user_avatar_hashes, session, guild_id, uid, avatar,
                      server_avatars.get(uid)): uid
            for uid, avatar in uid_to_avatar.items()
        }
        for fut in as_completed(futures):
            uid = futures[fut]
            hashes = fut.result()
            if hashes:
                pool[uid] = hashes
    return pool


def is_scoreboard_message(msg):
    """True for posted scoreboards (v2 components flag set).

    Daily uses this for double-fire dedup. The sticky's posts don't set the
    v2 flag, so they don't get confused with prior scoreboards.
    """
    flags = msg.get('flags') or 0
    return bool(flags & FLAG_IS_COMPONENTS_V2)


def is_sticky_message(msg, bot_id=None):
    """True for one of the bot's own sticky posts.

    Matched by the sticky's intrinsic Play button (custom_id), with the
    "Now Playing" heading as a fallback for a sticky somehow posted without its
    buttons. This is deliberately precise: a looser "any non-scoreboard bot
    message" test also matches the daily scoreboard and any unrelated bot post
    (e.g. leftovers from older code versions).

    Every caller deletes what it matches -- sticky_lambda collapsing duplicates
    back to one, /setup sticky off clearing the channel -- so both go through
    this single definition rather than each carrying its own idea of what a
    sticky looks like.
    """
    author = msg.get('author') or {}
    if bot_id:
        if author.get('id') != str(bot_id):
            return False
    elif not author.get('bot'):
        return False
    for row in (msg.get('components') or []):
        for c in row.get('components', []):
            if c.get('custom_id') == PLAY_BUTTON_CUSTOM_ID:
                return True
    return (msg.get('content') or '').startswith(STICKY_HEADING)


def _extract_user_avatars(messages):
    """{user_id: global avatar id or None} for everyone seen in the window.

    Users with no global avatar are kept with a None value rather than dropped:
    they may still have a server-profile avatar, which is the picture the
    Wordle image actually renders. _user_avatar_hashes resolves that, and
    anyone who ends up with no usable picture falls out of the pool there.
    """
    out = {}
    for m in messages:
        candidates = [m.get('author')]
        iu = m.get('interaction_metadata', {})
        if iu:
            candidates.append(iu.get('user'))
        for src in candidates:
            if not src:
                continue
            uid = src.get('id')
            if not uid:
                continue
            # First sighting wins, but a later one carrying an avatar upgrades
            # an entry we first saw without one.
            if out.get(uid) is None:
                out[uid] = src.get('avatar')
    return out


def build_name_map(messages):
    """{user_id: display name} for everyone seen in the window.

    Feeds the scoreboard's podium_only reduction, which swaps a 21-character
    `<@id>` for a plain name once the board is over Discord's text budget.
    Built from messages the caller already fetched, so it adds no API call and
    no latency: a player only has a score because they posted the message that
    produced it, so everyone the reduction can reach is already in here.

    Prefers the guild nickname, then the global display name, then the
    username -- the order Discord itself renders a member in. Messages arrive
    newest-first and the first sighting wins, so a rename shows up immediately.
    """
    names = {}
    for m in messages:
        sources = [(m.get('author'), (m.get('member') or {}).get('nick'))]
        iu = m.get('interaction_metadata', {})
        if iu:
            sources.append((iu.get('user'), None))
        for src, nick in sources:
            if not src or not src.get('id'):
                continue
            name = nick or src.get('global_name') or src.get('username')
            if name:
                names.setdefault(src['id'], name)
    return names


def _has_multiplayer_wordle(messages, checker):
    for m in messages:
        if m.get('author', {}).get('id') != WORDLE_BOT_ID:
            continue
        if not checker(m['timestamp']):
            continue
        for att in (m.get('attachments') or []):
            if 'finished games' in att.get('description', ''):
                return True
    return False


# Keyed by the full avatar URL, which embeds Discord's own content hash for the
# picture. A member who changes their avatar therefore gets a new key and is
# re-hashed on sight, while the old entry simply goes unused -- so this never
# needs to expire and never serves a stale picture. Survives across calls
# within a warm process.
_avatar_hash_cache = {}   # avatar url -> hash


def _download_avatar_hash(session, url):
    if url in _avatar_hash_cache:
        return _avatar_hash_cache[url]
    from PIL import Image
    import io
    try:
        r = session.get(url, timeout=3)
        r.raise_for_status()
        img = Image.open(io.BytesIO(r.content)).convert('RGB')
        h = _avatar_ahash(img)
        _avatar_hash_cache[url] = h
        return h
    except Exception:
        return None


# One request per 1000 members, via the Server Members Intent. This replaced a
# per-user fan-out at GET /guilds/{id}/members/{id}, whose 5-requests-per-second
# bucket 429d most of a pool built flat-out -- and did it silently, since a
# dropped lookup just falls back to the global avatar and looks exactly like
# having no server avatar at all.
GUILD_MEMBER_PAGE = 1000
MAX_MEMBER_PAGES = 10    # 10k members; past that we degrade to global avatars

# Server avatars are re-read rather than pinned to a member, so a player who
# changes their picture is picked up on the next refresh at no extra cost. The
# window bounds how long they can go unattributed while still absorbing the
# ~900 sticky runs a day that all share one image.
_SERVER_AVATAR_TTL = 900   # seconds
_SERVER_AVATAR_RETRY = 60  # after a failed read, retry sooner than a good one
_server_avatar_cache = {}  # guild_id -> (expires_at, {uid: server avatar id})


def _fetch_server_avatars(session, guild_id):
    """{uid: server avatar id} for every member of the guild that has one.

    Members without one are simply absent, and so are all of them if the guild
    is bigger than this will page through -- both leave the caller on the
    global avatar, which is the pre-existing behaviour rather than a failure.
    """
    out = {}
    after = None
    for _ in range(MAX_MEMBER_PAGES):
        url = f'{DISCORD_API_BASE}/guilds/{guild_id}/members?limit={GUILD_MEMBER_PAGE}'
        if after:
            url += f'&after={after}'
        r = session.get(url)
        r.raise_for_status()
        page = r.json()
        if not page:
            break
        for m in page:
            if m.get('avatar') and m.get('user', {}).get('id'):
                out[m['user']['id']] = m['avatar']
        if len(page) < GUILD_MEMBER_PAGE:
            break
        after = page[-1]['user']['id']
    return out


def _guild_server_avatars(session, guild_id):
    """_fetch_server_avatars behind a short per-process TTL.

    A failed read keeps serving the previous answer (or none) and retries on a
    shorter clock, so losing the intent or a transient 5xx costs attribution
    for one window instead of hardening into "nobody has a server avatar".
    """
    if not guild_id:
        return {}
    cached = _server_avatar_cache.get(guild_id)
    if cached and cached[0] > time.monotonic():
        return cached[1]
    try:
        avatars, ttl = _fetch_server_avatars(session, guild_id), _SERVER_AVATAR_TTL
    except Exception as e:
        print(f'avatar pool: server-avatar read failed, using global avatars -- '
              f'{type(e).__name__}: {e}')
        avatars, ttl = (cached[1] if cached else {}), _SERVER_AVATAR_RETRY
    _server_avatar_cache[guild_id] = (time.monotonic() + ttl, avatars)
    return avatars


def _user_avatar_hashes(session, guild_id, uid, global_avatar, server_avatar):
    """Hashes of every picture the Wordle image might render this user with.

    Discord shows a member's *server* avatar everywhere inside that guild, and
    the Wordle bot's preview is no exception -- so for anyone who has set one,
    the global avatar we harvest from the channel is simply the wrong picture
    and their grid goes unattributed (observed at 37 bits against an 18-bit
    ceiling, where the server avatar landed at 3). Both are hashed rather than
    just the server one, because most members have never set a server avatar
    and older images predate whatever they have set since.
    """
    urls = []
    if guild_id and server_avatar:
        urls.append(f'https://cdn.discordapp.com/guilds/{guild_id}/users/{uid}'
                    f'/avatars/{server_avatar}.png?size=64')
    if global_avatar:
        urls.append(f'https://cdn.discordapp.com/avatars/{uid}/{global_avatar}.png?size=64')
    return tuple(h for h in (_download_avatar_hash(session, u) for u in urls)
                 if h is not None)
