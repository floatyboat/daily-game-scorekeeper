import json
import os
import sys
import time
import traceback
from datetime import datetime, timedelta
from urllib.parse import quote
from zoneinfo import ZoneInfo
from collections import defaultdict

from game_parser import (
    compute_puzzle_numbers, build_games, top_game_buttons,
    match_message, make_timestamp_checker, shown_streak,
    performance_tier, place_at_post, result_reactions, WORDLE_BOT_ID,
)
from scoreboard import (
    DISCORD_API_BASE, FLAG_SUPPRESS_EMBEDS, FLAG_SUPPRESS_NOTIFICATIONS,
    make_session, fetch_messages, reference_date, is_scoreboard_message,
    is_sticky_message, is_wordle_recap, build_avatar_pool, safe_guild_id,
    gather_streaks, guild_aggs, send_commentary,
    PLAY_BUTTON_CUSTOM_ID, SCORES_BUTTON_CUSTOM_ID,
    STICKY_HEADING,
)
import commentary
import store

# Global bot identity only -- per-server settings come from each guild's
# config item (see store.CONFIG_DEFAULTS), managed by /setup.
DISCORD_BOT_ID = os.getenv('DISCORD_BOT_ID') or 0
DISCORD_BOT_TOKEN = os.getenv('DISCORD_BOT_TOKEN')

_session = make_session(DISCORD_BOT_TOKEN)


def build_sticky_components(yesterday_url=None, game_buttons=()):
    """The sticky's rows: today's games, then the action row.

    game_buttons are the games the sticky advertises (sticky_row_games) -- the
    day's rotation, or the most-played of the roster where none narrows it --
    and they sit ABOVE the action row, directly under the heading, because they
    are what the sticky is for; Play and Scores are the chrome around them.
    Empty -- sticky_games is 0 (the default), or the guild has no games enabled
    -- just drops the row; Discord rejects an action row with no components.

    There is no More button: these games plus the Play list behind them are the
    whole roster, so a second list button could only re-list what is on screen.
    There is no How it works button either: the explainer reaches a newcomer on
    its own, as the follow-up under their first live view, so the sticky spends
    its width on the games instead of a door nobody needs twice. Neither custom
    ID is routed any more: the sticky reposts whenever its buttons change, so
    a client holds a stale row for a minute at most.
    """
    buttons = [
        {'type': 2, 'style': 1, 'label': 'Play', 'custom_id': PLAY_BUTTON_CUSTOM_ID},
        {'type': 2, 'style': 2, 'label': 'Scores', 'custom_id': SCORES_BUTTON_CUSTOM_ID},
    ]
    if yesterday_url:
        buttons.append({'type': 2, 'style': 5, 'label': 'Yesterday', 'url': yesterday_url})
    rows = []
    if game_buttons:
        rows.append({'type': 1, 'components': list(game_buttons)})
    rows.append({'type': 1, 'components': buttons})
    return rows


def send_sticky(channel_id, content, components):
    payload = {
        'content': content,
        'components': components,
        'flags': FLAG_SUPPRESS_NOTIFICATIONS,
        'allowed_mentions': {'parse': []},
    }
    url = f'{DISCORD_API_BASE}/channels/{channel_id}/messages'
    r = _session.post(url, json=payload)
    r.raise_for_status()
    return r.json()


def delete_message(channel_id, message_id):
    """Delete one message, reporting whether it is actually gone.

    A 404 counts as gone -- a concurrent run got there first -- while a
    failure (most likely missing MANAGE_MESSAGES on a message the bot didn't
    author) reports False so callers can keep treating the message as live.
    """
    url = f'{DISCORD_API_BASE}/channels/{channel_id}/messages/{message_id}'
    r = _session.delete(url)
    return r.ok or r.status_code == 404


def suppress_embeds(channel_id, message):
    """Strip URL previews on a matched game-score message.

    Per-guild, via the `suppress_embeds` config field -- run_guild owns that
    gate and this stays the mechanism. No-op when the message has no embeds or
    already has the flag set. Requires MANAGE_MESSAGES for messages the bot
    didn't author; failures are swallowed so a missing perm or since-deleted
    message doesn't kill the run.
    """
    if not message.get('embeds'):
        return False
    flags = message.get('flags') or 0
    if flags & FLAG_SUPPRESS_EMBEDS:
        return False
    url = f'{DISCORD_API_BASE}/channels/{channel_id}/messages/{message["id"]}'
    r = _session.patch(url, json={'flags': flags | FLAG_SUPPRESS_EMBEDS})
    return r.ok


# Reactions only go on results this fresh. Longer than PROBE_MAX_AGE on
# purpose: a reaction that failed is retried by the full pass the probe lets
# through once it expires, while the result is still new. It also bounds what
# switching reactions on can touch, so the day's backlog is never swept.
REACTION_WINDOW = timedelta(minutes=20)
# Discord rate limits reactions tightly per channel, and the session sleeps
# out a 429 inside the pass, so one pass sends at most this many; the rest wait.
REACTIONS_PER_PASS = 20


def react_to_results(channel_id, posts, games, now, react_keys=None):
    """React to fresh results with how they went and where they placed.

    posts is every (msg, game_key, user_id, score) the pass matched, newest
    first as fetched, and games the day's games with this parse's totals. They
    are walked oldest first, so a result's place counts only what was already
    in when it was posted, and only a player's first result in a game counts
    -- the one the board keeps -- so a repost gets nothing. The Wordle app's own
    messages count toward places but get no reaction: several players share
    one, and the app keeps editing it. react_keys, when given, limits the
    reactions (never the places) to those games: the rotation, for a guild
    that only wants the games that score today reacted to.

    Stateless, like suppress_embeds: an emoji the bot already has on a message
    is skipped, so the passes that see a result again add nothing. Returns a
    note for the pass summary.
    """
    by_key = {g.key: g for g in games}
    seen = defaultdict(dict)    # game_key -> {uid: score}, first results so far
    added = sent = 0
    for msg, game_key, uid, score in reversed(posts):
        game = by_key.get(game_key)
        earlier = seen[game_key]
        if game is None or uid in earlier:
            continue
        place = place_at_post(game.metric, score, list(earlier.values()))
        earlier[uid] = score
        if (msg['author']['id'] == WORDLE_BOT_ID
                or (react_keys is not None and game_key not in react_keys)
                or now - datetime.fromisoformat(msg['timestamp']) > REACTION_WINDOW):
            continue
        mine = {(r.get('emoji') or {}).get('name')
                for r in msg.get('reactions') or () if r.get('me')}
        # The message id seeds the flourish draw: stable per result, so the
        # passes that see it again re-draw the same emoji and add nothing.
        for emoji in result_reactions(performance_tier(game, score), place,
                                      seed=msg['id']):
            if emoji in mine:
                continue
            if sent == REACTIONS_PER_PASS:
                return f'reactions: {added} added, capped'
            sent += 1
            r = _session.put(f'{DISCORD_API_BASE}/channels/{channel_id}/messages/'
                             f'{msg["id"]}/reactions/{quote(emoji)}/@me')
            if r.status_code == 403:
                return f'reactions: {added} added, missing Add Reactions'
            if not r.ok:
                return f'reactions: {added} added, stopped on {r.status_code}'
            added += 1
    return f'reactions: {added} added'


def find_stickies(messages):
    """Every bot sticky in the channel, newest first (normally exactly one).

    Returning *all* matches rather than just the newest is what lets
    update_sticky collapse back to a single sticky. A scheduled run that
    double-fires can briefly post two stickies; a single-match scan would then
    delete only the newer one on each later run and orphan the older "No scores
    yet today" post indefinitely. /play replies are ephemeral and never appear
    in fetch_messages.
    """
    return [m for m in messages if is_sticky_message(m, DISCORD_BOT_ID)]


def find_latest_scoreboard_id(messages):
    for msg in messages:
        if is_scoreboard_message(msg):
            return msg['id']
    return None


def _button_identity(rows):
    """Every button in row order, reduced to what we actually render.

    Discord echoes components back with extra server-set fields (component ids
    and the like), so compare this projection rather than the raw dicts.
    """
    return [(c.get('custom_id'), c.get('label'), c.get('url'))
            for row in (rows or []) for c in row.get('components', [])]


def _sticky_is_current(sticky, content, components):
    """True when the live sticky already renders exactly what we'd post now.

    Content plus every button, so a game row today's draw has redrawn, a stale
    Yesterday link, a reshuffled or restreaked game row, a row an admin has
    just resized or switched off, and a sticky posted before any of these
    buttons existed all force a repost.
    """
    if sticky.get('content', '') != content:
        return False
    return _button_identity(sticky.get('components')) == _button_identity(components)


def build_sticky_content(results, server_streak=0):
    """The heading, carrying the server-wide streak inline, over the day's
    counts -- how much the server has played, under what to play it on.

    The streak rides on the heading rather than the counts line because it is a
    property of the server, not of today: it survives a day the counts reset,
    and reading `Now Playing · \U0001F525 17` as one line is the nudge.
    """
    flair = f' · \U0001F525{server_streak}' if shown_streak(server_streak) else ''

    # Distinct games that have at least one score, then every play logged
    # against them (each player x game result counts once).
    game_count = sum(1 for scores in results.values() if scores)
    play_count = sum(len(scores) for scores in results.values())
    if play_count == 0:
        counts = 'No scores yet today'
    else:
        g = 'game' if game_count == 1 else 'games'
        p = 'play' if play_count == 1 else 'plays'
        counts = f'{game_count} {g} · {play_count} {p} today'
    return f'{STICKY_HEADING}{flair}\n{counts}'


def update_sticky(channel_id, channel_messages, results, server_streak=0,
                  link_yesterday=True, game_buttons=()):
    """Maintain exactly one sticky at the bottom of channel_id.

    No-op only when a single sticky is already the most recent message AND both
    its content and its buttons match what we'd render now — content comparison
    catches the day-transition case where the sticky is still at the bottom but
    shows yesterday's stats, and button comparison catches a Yesterday link gone
    stale behind a freshly posted scoreboard, or a shortcut row the day's plays
    have since reordered (or an admin has resized via /setup sticky).

    link_yesterday=False (the guild has the daily scoreboard disabled, or the
    board covering the day before this one has not posted yet) drops the
    Yesterday button even when an old board is still in the channel — the
    freshest link would only ever point at a stale day.

    Otherwise delete *every* existing sticky before posting a fresh one. The
    morning scoreboard de-positions the sticky and staleness forces a repost; a
    double-fire of that run leaves two stickies, and deleting only the newest
    (the old behavior) orphaned the older "No scores yet today" post forever.
    Deleting all matches, plus the post-write sweep below, collapses any such
    duplicates back to one.
    """
    stickies = find_stickies(channel_messages)
    content = build_sticky_content(results, server_streak)

    yesterday_url = None
    if link_yesterday:
        scoreboard_id = find_latest_scoreboard_id(channel_messages)
        if scoreboard_id:
            # Discord's client routes by channel_id/message_id; the guild slot
            # accepts @me even for guild messages.
            yesterday_url = f'https://discord.com/channels/@me/{channel_id}/{scoreboard_id}'
    components = build_sticky_components(yesterday_url, game_buttons)

    if (len(stickies) == 1 and channel_messages
            and channel_messages[0]['id'] == stickies[0]['id']
            and _sticky_is_current(stickies[0], content, components)):
        return 'unchanged'

    for old in stickies:
        delete_message(channel_id, old['id'])

    send_sticky(channel_id, content, components)

    # Close the double-fire window: a concurrent run can post a second sticky in
    # parallel with ours. Re-read the tail and drop everything but the newest so
    # the channel converges to one — both runs agree on "keep newest", and
    # delete_message swallows the 404 when the other already removed it.
    extra = find_stickies(fetch_messages(_session, channel_id, limit=10))
    for dup in extra[1:]:
        delete_message(channel_id, dup['id'])

    if not stickies:
        return 'created'
    return 'collapsed' if len(stickies) > 1 else 'reposted'


def run_commentary(cfg, now_local, day, messages, results, puzzle_numbers, times,
                   streaks, force):
    """One STICKY-cadence commentary pass over what run_guild has already
    parsed (commentary.py decides; this is the I/O around it). A post it makes
    is put at the head of `messages`, so update_sticky sees the channel as it
    now is and settles the sticky beneath the post in this same pass. Test
    runs (force) read the real state but record nothing. Returns a note."""
    gid = cfg['guild_id']
    state = store.get_commentary(gid, day)
    tick = commentary.make_tick(cfg, now_local, messages, results, puzzle_numbers, times,
                                streaks, guild_aggs(gid), None, state, DISCORD_BOT_ID)
    post = commentary.evaluate(tick, commentary.STICKY)
    # Record BEFORE posting: two passes share this state, and a post whose
    # record failed would be re-detected and said again the next time a human
    # posts, whereas a recorded post that failed to send is merely lost. A
    # record that raises here therefore also stops the post.
    if not force:
        ids, snapshot = commentary.to_record(tick, post, sent=post is not None)
        store.record_commentary(gid, day, ids, snapshot)
    if post:
        messages.insert(0, send_commentary(_session, cfg['input_channel_id'], post))
        return f'commentary: posted {post.kind}'
    return f"commentary: {commentary.blocked(tick) or 'quiet'}"


# One entry per guild whose last pass ended settled; run_guild's probe uses it
# to skip the full pass while nothing has moved. Process-lifetime state: the
# every-minute schedule keeps this container warm, so entries usually survive
# from one tick to the next and the common case collapses to one tiny fetch.
_probe_state = {}   # guild_id -> {'fingerprint', 'newest_id', 'expires'}
PROBE_MAX_AGE = 600

# Don't start another guild with less than this left on the clock; a typical
# pass is well under it, so the margin only ever trims the pathological runs.
DEADLINE_MARGIN_MS = 8000


def run_guild(cfg, force=False):
    """One guild's sticky pass: parse today's plays and settle the sticky.

    Runs around the clock. The day it tracks is whichever one reference_date
    says is open, so the sticky rolls over at the guild's DAY START -- back to
    "No scores yet today", then counting the new day live -- rather than
    waiting for the board hours later at post hour. A guild whose post hour is
    later therefore has a window each morning where the day has rolled but
    yesterday's board has not posted yet; the only thing in the sticky that
    depends on the board is the Yesterday link, and it gates itself below.
    """
    channel_id = cfg['input_channel_id']
    tz = ZoneInfo(cfg['timezone'])
    now_local = datetime.now(tz)

    today = reference_date(now_local, tz, cfg['hours_after_midnight'])

    # Probe short-circuit: when the last full pass left the sticky settled and
    # neither the date nor the config has moved, a single-message fetch proving
    # "the newest message is still the settled sticky" also proves the parse
    # could not have changed -- no new plays, same window, same games -- so the
    # 200-message fetch, the regex pass, and the streak read are all skipped.
    # Bounded by PROBE_MAX_AGE so what a head probe can't see (an edit or
    # deletion of an older message, a changed avatar) still heals within
    # minutes rather than waiting on the next new message.
    gid = cfg['guild_id']
    today_day = store.day_str(today)
    fingerprint = f"{today_day} {json.dumps(cfg, sort_keys=True, default=str)}"
    state = None if force else _probe_state.get(gid)
    if state and state['fingerprint'] == fingerprint \
            and state['expires'] > time.monotonic():
        probe = fetch_messages(_session, channel_id, limit=1)
        if probe and probe[0]['id'] == state['newest_id']:
            return 'unchanged (probe)'
    _probe_state.pop(gid, None)

    rotation = store.current_rotation(cfg, today_day)
    puzzle_numbers = compute_puzzle_numbers(today)
    games = build_games(puzzle_numbers, cfg['game_overrides'])
    checker = make_timestamp_checker(today, tz, cfg['hours_after_midnight'],
                                     cfg['time_window_hours'])

    messages = fetch_messages(_session, channel_id, limit=200)

    # Opt-in cleanup: the Wordle app's daily recap restates yesterday's
    # results and streak, which this bot's own board and flair already cover,
    # and pings the players it names -- so a guild can have this pass delete
    # it on sight (needs MANAGE_MESSAGES, like suppress_embeds). Deleted
    # recaps leave the working list too: the recap typically lands right on
    # top of a settled sticky, and once it is gone the sticky really is the
    # newest message again, so update_sticky can settle without a repost. A
    # failed delete stays in the list and the sticky reposts below it,
    # exactly as with any other message.
    recaps_deleted = 0
    if cfg['delete_wordle_recap']:
        kept = []
        for msg in messages:
            if is_wordle_recap(msg) and delete_message(channel_id, msg['id']):
                recaps_deleted += 1
            else:
                kept.append(msg)
        messages = kept

    avatar_pool = build_avatar_pool(_session, messages, checker, cfg['guild_id'])

    results = defaultdict(dict)
    times = {}      # uid -> ISO timestamp of their latest counted result
    posts = []      # (msg, game_key, uid, score) per entry, for the reactions
    suppressed = 0
    for msg in messages:
        entries = match_message(msg, games, checker, avatar_hashes=avatar_pool)
        if not entries:
            continue
        if cfg['suppress_embeds'] and suppress_embeds(channel_id, msg):
            suppressed += 1
        for game_key, score, metadata, uid_override in entries:
            user_id = uid_override or msg.get('interaction_metadata', {}).get('user', {}).get('id') or msg['author']['id']
            results[game_key][user_id] = score
            puzzle_numbers.update(metadata)
            times[user_id] = max(times.get(user_id, ''), msg['timestamp'])
            posts.append((msg, game_key, user_id, score))

    # Server-wide streak flair, bare fire+number at the end of the content
    # line -- kept alive today (live +1) or still extendable from yesterday.
    # Fail-open: no store, no flair.
    streaks = gather_streaks(cfg['guild_id'], today, results, games,
                             cfg['minimum_players'])
    server_streak = (streaks or {}).get('server', 0)

    # What the sticky advertises: the games that actually score today, in the
    # app-wide order, with the same labels Play uses.
    rot = set(rotation) if rotation is not None else None
    playable = games if rot is None else [g for g in games if g.key in rot]

    # The game row: today's games as buttons, playable without opening
    # anything. sticky_games is 0 by default, and then the ordering pass never
    # runs -- a guild that doesn't want the row pays nothing to rank games for
    # it. Whatever lands here is exactly what the Play list leaves out
    # (sticky_row_games is the shared definition), so the sticky and Play
    # partition the roster between them instead of repeating it.
    game_buttons = top_game_buttons(playable, results, streaks, cfg['sticky_games'])

    # Commentary at the sticky cadence: the kinds that answer a result as it
    # lands (a first result, a lead change, a clean sweep) post from here,
    # within a minute of the message that caused them. Its own try/except: a
    # commentary failure must never cost the channel its sticky.
    notes = []
    if cfg['commentary_enabled'] and commentary.enabled_triggers(cfg, commentary.STICKY):
        try:
            notes.append(run_commentary(cfg, now_local, today_day, messages, results,
                                        puzzle_numbers, times, streaks, force))
        except Exception as e:
            traceback.print_exc()
            notes.append(f'commentary FAILED {type(e).__name__}: {e}')

    # Yesterday links the newest board in the channel, which only covers the
    # day before this one once today's board has posted -- between day start
    # and post hour it is still the board for the day before THAT. Drop the
    # button rather than mislabel it; the next pass picks it up, since
    # last_posted_day rides in the probe fingerprint. force (test runs) skips
    # the check like every other timing gate.
    posted_yesterday = (cfg['last_posted_day'] or '') >= store.prev_day_str(today_day)
    link_yesterday = cfg['daily_enabled'] and (force or posted_yesterday)
    action = update_sticky(channel_id, messages, results, server_streak,
                           link_yesterday=link_yesterday,
                           game_buttons=game_buttons)
    if action == 'unchanged' and not force:
        # 'unchanged' guarantees messages[0] is the single, settled sticky.
        _probe_state[gid] = {'fingerprint': fingerprint,
                             'newest_id': messages[0]['id'],
                             'expires': time.monotonic() + PROBE_MAX_AGE}

    # Reactions once the sticky has settled: they move no message, so they
    # can't unsettle it, and a slow or rate-limited one can't hold it up. Own
    # try/except, like the commentary's. The games are rebuilt off this
    # parse, so bandle and minute cryptic carry the totals their shares gave.
    if cfg['reactions_enabled']:
        react_keys = rot if cfg['reactions_rotation_only'] else None
        try:
            notes.append(react_to_results(
                channel_id, posts, build_games(puzzle_numbers, cfg['game_overrides']),
                now_local, react_keys))
        except Exception as e:
            traceback.print_exc()
            notes.append(f'reactions FAILED {type(e).__name__}: {e}')
    if cfg['suppress_embeds']:
        notes.append(f'embeds suppressed: {suppressed}')
    if cfg['delete_wordle_recap']:
        notes.append(f'recaps deleted: {recaps_deleted}')
    note = f' ({", ".join(notes)})' if notes else ''
    return f'{action}{note}'


def lambda_handler(event, context):
    """Frequent tick: settle the sticky for every guild with one enabled.

    The guild list comes from the table each invocation, so onboarding a
    server (/setup) needs no deploy or schedule change. Test events operate on
    the test channel with a default config so local runs never touch real user
    messages: {'test': true} plus optional 'channel_id' and any config-field
    overrides (e.g. 'daily_enabled': false to preview the linkless sticky).
    """
    event = event if isinstance(event, dict) else {}

    if 'test' in event:
        cfg = store.default_config()
        cfg.update({k: v for k, v in event.items() if k in store.CONFIG_DEFAULTS})
        cfg['input_channel_id'] = (event.get('channel_id')
                                   or cfg['input_channel_id']
                                   or os.getenv('TEST_CHANNEL_ID'))
        if not cfg['input_channel_id']:
            return {'statusCode': 400,
                    'body': json.dumps('test mode needs channel_id in the event '
                                       'or TEST_CHANNEL_ID in the env')}
        # A default config has no guild_id; resolve it from the test channel so
        # run_guild reads it off cfg exactly as it does for a stored config.
        cfg['guild_id'] = safe_guild_id(_session, cfg['input_channel_id'])
        result = run_guild(cfg, force=True)
        return {'statusCode': 200, 'body': json.dumps(f'Sticky (test): {result}')}

    configs = [cfg for cfg in store.all_configs()
               if cfg['sticky_enabled'] and cfg['input_channel_id']]
    # A different starting guild each minute: if a run ever runs out of time,
    # the deferral below lands on different guilds each tick instead of
    # deterministically starving the tail of the partition order.
    if len(configs) > 1:
        offset = int(time.time() // 60) % len(configs)
        configs = configs[offset:] + configs[:offset]

    summary = {}
    for i, cfg in enumerate(configs):
        gid = cfg['guild_id']
        if context is not None \
                and context.get_remaining_time_in_millis() < DEADLINE_MARGIN_MS:
            # Stop cleanly rather than letting Lambda kill the run mid-guild;
            # the start-offset rotation above spreads the deferral around.
            print(f'sticky: out of time, deferring {len(configs) - i} guild(s)')
            for later in configs[i:]:
                summary[later['guild_id']] = 'deferred: out of time'
            break
        try:
            summary[gid] = run_guild(cfg)
        except Exception as e:
            traceback.print_exc()
            summary[gid] = f'FAILED {type(e).__name__}: {e}'

    if not summary:
        summary = 'no guilds with a sticky to run'
    return {'statusCode': 200, 'body': json.dumps(summary)}


if __name__ == '__main__':
    # No argument runs the plain test event. Pass inline JSON (or a fixture
    # path) to override config fields for the run, e.g. to preview the
    # sticky-cadence commentary on the test channel:
    #   dotenv run -- python3 src/sticky_lambda.py '{"test": true, "commentary_enabled": true}'
    arg = sys.argv[1] if len(sys.argv) > 1 else None
    if arg:
        event = json.loads(arg if arg.lstrip().startswith('{') else open(arg).read())
    else:
        event = {'test': True}
    print(lambda_handler(event, None))
