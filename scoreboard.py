"""Shared orchestration above game_parser: session, fetch, parse, dedup."""
import requests
from datetime import timedelta
from collections import defaultdict

import store
from game_parser import (
    compute_puzzle_numbers, build_games,
    make_timestamp_checker, match_message, _avatar_ahash,
)

DISCORD_API_BASE = 'https://discord.com/api/v10'

FLAG_SUPPRESS_EMBEDS         = 1 << 2    # 4
FLAG_EPHEMERAL               = 1 << 6    # 64
FLAG_SUPPRESS_NOTIFICATIONS  = 1 << 12   # 4096
FLAG_IS_COMPONENTS_V2        = 1 << 15   # 32768

# Custom IDs for the sticky's interactive buttons. Defined here, in the module
# both lambdas already import, so the producer (sticky_lambda) and the consumer
# (interaction_lambda) share one source of truth instead of duplicating the
# literal strings and silently drifting apart.
PLAY_BUTTON_CUSTOM_ID = 'sticky_play'
SCORES_BUTTON_CUSTOM_ID = 'sticky_scores'


def make_session(token, pool_connections=4, pool_maxsize=32):
    s = requests.Session()
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


def gather_streaks(guild_id, ref_date, results, game_keys, include_players=True):
    """Display-ready streak numbers for one board render, or None when the
    store can't serve them (no guild, IAM grant not applied yet, outage) --
    callers render streak-less, so store problems never break a view.

    Works identically on both sides of the daily finalize because
    store.display_streak() folds the "played on ref_date" flag in itself:
    a live view (aggregates through yesterday) and a just-finalized view
    (ref_date already folded) produce the same numbers, so every surface
    (daily post, Scores, Play, sticky) shares this one path.

    Bundle (plain ints/strings, JSON-safe):
      games         {game_key: streak to show}
      broken        {game_key: streak that ended on ref_date}
      players_total {game_key: all-time distinct-player count}
      players       {game_key: {user_id: streak to show}} (players with
                    results on ref_date only; empty when include_players=False)
    """
    if not guild_id:
        return None
    try:
        day = store.day_str(ref_date)
        aggs = store.query_aggs(store.guild_pk(guild_id))
        game_items = {store.game_key_from_sk(sk): item for sk, item in aggs.items()
                      if sk.startswith(store.GAME_AGG_PREFIX)}

        bundle = {'games': {}, 'broken': {}, 'players_total': {}, 'players': {}}
        for key in game_keys:
            item = game_items.get(key)
            bundle['games'][key] = store.display_streak(item, day, bool(results.get(key)))
            ended = store.broken_streak_on(item, day)
            if ended:
                bundle['broken'][key] = ended
            bundle['players_total'][key] = len((item or {}).get('players') or ())

        if include_players:
            pairs = sorted({(uid, key) for key in game_keys
                            for uid in (results.get(key) or {})})
            fetched = {}
            for item in store.batch_get(
                    [{'PK': store.player_pk(guild_id, uid), 'SK': store.game_agg_sk(key)}
                     for uid, key in pairs]):
                uid = item['PK'].split('#PLAYER#', 1)[1]
                fetched[(uid, store.game_key_from_sk(item['SK']))] = item
            for uid, key in pairs:
                bundle['players'].setdefault(key, {})[uid] = store.display_streak(
                    fetched.get((uid, key)), day, True)
        return bundle
    except Exception as e:
        print(f'store: streak read failed, rendering without streaks -- '
              f'{type(e).__name__}: {e}')
        return None


def fetch_messages(session, channel_id, limit=100):
    per_page = min(limit, 100)
    url = f'{DISCORD_API_BASE}/channels/{channel_id}/messages?limit={per_page}'
    r = session.get(url)
    r.raise_for_status()
    messages = r.json()
    if not isinstance(messages, list):
        return []
    while len(messages) < limit:
        last_id = messages[-1]['id']
        remaining = min(limit - len(messages), 100)
        page_url = f'{DISCORD_API_BASE}/channels/{channel_id}/messages?limit={remaining}&before={last_id}'
        r = session.get(page_url)
        r.raise_for_status()
        page = r.json()
        if not isinstance(page, list) or not page:
            break
        messages += page
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
                  *, wordle_bot_id=None, avatar_hashes=None):
    puzzle_numbers = compute_puzzle_numbers(ref_date)
    games = build_games(puzzle_numbers)
    checker = make_timestamp_checker(ref_date, tz, hours_after_midnight, time_window_hours)
    results = defaultdict(dict)
    for msg in messages:
        for game_key, score, metadata, uid_override in match_message(
                msg, games, checker,
                wordle_bot_id=wordle_bot_id, avatar_hashes=avatar_hashes):
            user_id = (uid_override
                       or msg.get('interaction_metadata', {}).get('user', {}).get('id')
                       or msg['author']['id'])
            results[game_key][user_id] = score
            puzzle_numbers.update(metadata)
    return results, puzzle_numbers


def build_avatar_pool(session, messages, checker, wordle_bot_id):
    if not wordle_bot_id:
        return {}
    if not _has_multiplayer_wordle(messages, checker, wordle_bot_id):
        return {}
    uid_to_avatar = _extract_user_avatars(messages)
    if not uid_to_avatar:
        return {}
    from concurrent.futures import ThreadPoolExecutor, as_completed
    pool = {}
    with ThreadPoolExecutor(max_workers=16) as ex:
        futures = {
            ex.submit(_download_avatar_hash, session, uid, avatar): uid
            for uid, avatar in uid_to_avatar.items()
        }
        for fut in as_completed(futures):
            uid = futures[fut]
            h = fut.result()
            if h is not None:
                pool[uid] = h
    return pool


def is_scoreboard_message(msg):
    """True for posted scoreboards (v2 components flag set).

    Daily uses this for double-fire dedup. The sticky's posts don't set the
    v2 flag, so they don't get confused with prior scoreboards.
    """
    flags = msg.get('flags') or 0
    return bool(flags & FLAG_IS_COMPONENTS_V2)


def _extract_user_avatars(messages):
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
            avatar = src.get('avatar')
            if uid and avatar and uid not in out:
                out[uid] = avatar
    return out


def _has_multiplayer_wordle(messages, checker, wordle_bot_id):
    for m in messages:
        if m.get('author', {}).get('id') != wordle_bot_id:
            continue
        if not checker(m['timestamp']):
            continue
        for att in (m.get('attachments') or []):
            if 'finished games' in att.get('description', ''):
                return True
    return False


_avatar_hash_cache = {}  # (uid, avatar_id) -> hash; survives across calls within a warm process


def _download_avatar_hash(session, uid, discord_avatar):
    key = (uid, discord_avatar)
    if key in _avatar_hash_cache:
        return _avatar_hash_cache[key]
    from PIL import Image
    import io
    try:
        url = f'https://cdn.discordapp.com/avatars/{uid}/{discord_avatar}.png?size=64'
        r = session.get(url, timeout=3)
        r.raise_for_status()
        img = Image.open(io.BytesIO(r.content)).convert('RGB')
        h = _avatar_ahash(img)
        _avatar_hash_cache[key] = h
        return h
    except Exception:
        return None
