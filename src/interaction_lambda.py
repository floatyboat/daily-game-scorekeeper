import base64
import json
import os
import random
import re
import traceback
from datetime import datetime
from zoneinfo import ZoneInfo
from nacl.signing import VerifyKey

from game_parser import (
    build_games, compute_puzzle_numbers, format_scoreboard_components,
    make_timestamp_checker, game_sort_key, match_suggestion, GAME_SPECS,
    spec_enabled, game_link_button, sticky_row_games,
    SCORING_PLACEMENT, SCORING_PER_GAME, SCORING_OFF, tier_examples, ACED,
    PLACE_EMOJI, BELOW_PODIUM, POOP_MEDAL, NO_SHOW_SCORE,
)
from scoreboard import (
    DISCORD_API_BASE, make_session, fetch_messages, reference_date, parse_results,
    build_avatar_pool, build_name_map, safe_guild_id, gather_streaks,
    gather_player_stats, is_sticky_message,
    PLAY_BUTTON_CUSTOM_ID, SCORES_BUTTON_CUSTOM_ID,
    TEXT_CHANNEL_TYPES, PERM_ADMINISTRATOR, PERM_MANAGE_GUILD, MAX_BUTTONS_PER_ROW,
    MAX_ENABLED_GAMES,
    MAX_MESSAGE_LENGTH, FLAG_EPHEMERAL, FLAG_IS_COMPONENTS_V2,
)
import commentary
import store

# Global bot identity only -- per-server settings live in the guild's config
# item (store.CONFIG_FIELDS) and are managed by the /setup command below.
DISCORD_PUBLIC_KEY = os.getenv('DISCORD_PUBLIC_KEY', '')
DISCORD_BOT_TOKEN = os.getenv('DISCORD_BOT_TOKEN')
DISCORD_BOT_ID = os.getenv('DISCORD_BOT_ID') or 0

_session = make_session(DISCORD_BOT_TOKEN)

GAMES_SELECT_ID = 'setup_games'
COMMENTARY_SELECT_ID = 'setup_commentary'
CHANNEL_SELECT_PREFIX = 'setup_channel:'
# Every reply from a one-sided channel subcommand says so: `/setup channel`
# points both sides at one channel, and an admin who then runs `/setup input`
# should be able to tell that it moved one side and left the other alone.
OVERRIDE_NOTE = ('-# Overrides `/setup channel` on this side only — '
                 'the other channel stays as it is.')


def get_body(event):
    """Extract the raw body string, decoding base64 if needed."""
    body = event.get('body', '')
    if event.get('isBase64Encoded'):
        body = base64.b64decode(body).decode('utf-8')
    return body


def verify_signature(body, event):
    """Verify Discord Ed25519 request signature. Raises on failure."""
    headers = {k.lower(): v for k, v in event.get('headers', {}).items()}
    signature = headers.get('x-signature-ed25519', '')
    timestamp = headers.get('x-signature-timestamp', '')

    verify_key = VerifyKey(bytes.fromhex(DISCORD_PUBLIC_KEY))
    verify_key.verify(f'{timestamp}{body}'.encode(), bytes.fromhex(signature))


def guild_cfg(guild_id, strict=False):
    """Effective config for the interaction's guild; defaults when the guild
    never ran /setup (a legitimate state -- the summary then says so).

    Live views (Play, Scores, the sticky buttons) also fall back to defaults
    when the store itself is unreachable: they must render either way, they
    just use stock settings. The admin surfaces pass strict=True instead --
    showing an admin a plausible-looking default config when the read failed
    reads as "my settings vanished", so let admin_dispatch turn it into a
    try-again message.
    """
    try:
        if guild_id:
            cfg = store.get_config(guild_id)
            if cfg:
                return cfg
    except Exception as e:
        if strict:
            raise
        print(f'config read failed, using defaults -- {type(e).__name__}: {e}')
    return store.default_config(guild_id)


def fetch_today_results(channel_id, cfg):
    """Fetch one page of channel history and parse today's game results.

    Shared by the Scores and Play buttons so both reflect the same live view of
    the channel they were clicked in. Single page (limit=100) keeps the call
    under Discord's 3-second interaction-response budget; the daily summary
    lambda is the source of truth for the full archive, this is a live preview.

    Returns (results, puzzle_numbers, today, rotation, names) -- rotation is
    store.current_rotation's key list for today, or None when the day is
    unrestricted, and names is the display-name map the scoreboard falls back
    to when it has to trade mentions for room inside Discord's text budget.
    """
    tz = ZoneInfo(cfg['timezone'])
    today = reference_date(datetime.now(tz), tz, cfg['hours_after_midnight'])
    rotation = store.current_rotation(cfg, store.day_str(today))
    messages = fetch_messages(_session, channel_id, limit=100)
    checker = make_timestamp_checker(today, tz, cfg['hours_after_midnight'],
                                     cfg['time_window_hours'])
    avatar_pool = build_avatar_pool(_session, messages, checker, cfg['guild_id'])
    results, puzzle_numbers = parse_results(
        messages, today, tz, cfg['hours_after_midnight'], cfg['time_window_hours'],
        avatar_hashes=avatar_pool, game_overrides=cfg['game_overrides'],
    )
    return results, puzzle_numbers, today, rotation, build_name_map(messages)


def _ephemeral(content, components=None):
    """CHANNEL_MESSAGE_WITH_SOURCE, visible only to the invoker."""
    data = {'flags': FLAG_EPHEMERAL, 'content': content}
    if components is not None:
        data['components'] = components
    return {'type': 4, 'data': data}


def empty_board_hint(channel_id, cfg):
    """What an empty live board should say instead of its default nudge, or
    None to keep it.

    A live view parses the channel it was run in, so `/scoreboard` typed
    anywhere else comes back empty however busy the day has been. That is the
    one thing the board itself cannot know, and the only useful thing to say
    about it, so it displaces the nudge rather than joining it.
    """
    home = cfg.get('input_channel_id')
    if home and str(channel_id) != str(home):
        return f'This reads the channel you ran it in. Scores go in <#{home}>.'
    return None


def build_scoreboard_response(channel_id, guild_id=None, cfg=None):
    """Build today's scoreboard as an ephemeral Components V2 reply.

    Streaks ride along when the store is reachable: live views show a streak
    kept alive today as current + 1 (SPEC.md), so the board updates the moment
    someone plays.
    """
    cfg = cfg or guild_cfg(guild_id)
    results, puzzle_numbers, today, rotation, names = fetch_today_results(channel_id, cfg)

    streaks = gather_streaks(guild_id, today, results,
                             build_games(puzzle_numbers, cfg['game_overrides']),
                             cfg['minimum_players'])
    components = format_scoreboard_components(
        results, today, puzzle_numbers,
        title="Today's Scores", minimum_players=cfg['minimum_players'], streaks=streaks,
        game_overrides=cfg['game_overrides'], rotation=rotation,
        rotation_off=cfg['rotation_off_mode'], names=names,
        scoring=cfg['scoring'], live=True,
        empty_hint=empty_board_hint(channel_id, cfg),
    )

    # V2 messages can't have a content field, so the builder's output goes
    # directly into components.
    return {
        "type": 4,
        "data": {
            "flags": FLAG_EPHEMERAL | FLAG_IS_COMPONENTS_V2,
            "components": components,
        },
    }


STATS_NO_GUILD = ("\U0001F4CA Stats are per-server — run this in a server "
                  "where the scoreboard is set up.")
STATS_UNAVAILABLE = "\U0001F4CA Couldn't read your stats just now — try again shortly."
STATS_EMPTY = ("\U0001F4CA No stats yet — post a result in the scoreboard channel "
               "and your first streak starts today.")


def _stats_lines(bundle):
    """The per-game body of /stats: one line per game with a live streak, then
    a single subtext line naming the rest.

    Ordered by the streak the player is actually being shown, so the view leads
    with what they're most at risk of losing. Games whose streak has lapsed are
    named but not numbered -- "0" reads as a score, and the honest content is
    just that the streak is over.
    """
    specs = {spec.key: spec for spec in GAME_SPECS}
    # A game dropped from GAME_SPECS keeps its stored history but has no title
    # to render it under, so it falls out here rather than showing a raw key.
    entries = [(specs[key], st) for key, st in bundle['games'].items() if key in specs]
    entries.sort(key=lambda e: (-e[1]['current'], -e[1]['best'], e[0].title.lower()))

    lines, lapsed = [], []
    for spec, st in entries:
        if not st['current']:
            lapsed.append(spec.title)
            continue
        # best == current is the same number twice; it earns its place only
        # once the player has been further than they are now.
        best = f" · best {st['best']}" if st['best'] > st['current'] else ''
        lines.append(f"{spec.emoji} {spec.title} — \U0001F525{st['current']}{best}")
    if lapsed:
        lines.append(f"-# No active streak: {', '.join(lapsed)}")
    return lines


def format_stats(bundle, ref_date):
    """/stats as one markdown message, or the empty state for a new player."""
    overall = bundle['overall']
    lines = _stats_lines(bundle)
    if not overall['plays'] and not lines:
        return STATS_EMPTY

    plays = overall['plays']
    played = f"{plays} play" + ('' if plays == 1 else 's')
    head = [f"### \U0001F4CA Your Stats — {ref_date.strftime('%B %d, %Y')}"]
    if overall['current']:
        best = (f" · best {overall['best']}"
                if overall['best'] > overall['current'] else '')
        head.append(f"\U0001F525 **{overall['current']}-day streak**{best} · {played}")
    else:
        # No live overall streak still has a story: what they've done, and the
        # furthest they've taken it.
        head.append(f"{played} · best streak {overall['best']}")
    if lines:
        head.append('')
        head.append('**Streaks by game**')
    return "\n".join(head + lines)


def build_stats_response(channel_id, user_id=None, guild_id=None, cfg=None):
    """The invoker's own streaks, as an ephemeral reply.

    Reads the guild's input channel rather than wherever /stats was typed: that
    is where results are posted, so it is the only channel that can tell
    whether today already counts. Falls back to the invoking channel for a
    guild that has never pointed the bot anywhere.
    """
    if not guild_id or not user_id:
        return _ephemeral(STATS_NO_GUILD)
    cfg = cfg or guild_cfg(guild_id)

    source = cfg['input_channel_id'] or channel_id
    try:
        results, puzzle_numbers, today, _, _ = fetch_today_results(source, cfg)
    except Exception as e:
        # Today's parse only decides whether today counts yet; stored history is
        # the substance, so a channel hiccup costs a day, not the whole reply.
        print(f'stats: live parse failed, showing stored history -- '
              f'{type(e).__name__}: {e}')
        tz = ZoneInfo(cfg['timezone'])
        today = reference_date(datetime.now(tz), tz, cfg['hours_after_midnight'])
        results, puzzle_numbers = {}, compute_puzzle_numbers(today)

    bundle = gather_player_stats(
        guild_id, user_id, today, results,
        build_games(puzzle_numbers, cfg['game_overrides']), cfg['minimum_players'])
    if bundle is None:
        return _ephemeral(STATS_UNAVAILABLE)
    return _ephemeral(format_stats(bundle, today))


# --- /help and the one-time welcome ----------------------------------------------
# One explainer, two doors: `/help`, and an automatic ephemeral follow-up under a
# player's first live view. Built off the live config every time, so it never
# describes a setting the server doesn't have -- the scoring blurb in particular
# says what a result is worth HERE.

HELP_HEADING = "### ❓ How the scoreboard works"


def _scoring_blurb(mode, board, rotation, shame=True):
    crown = 'takes the \U0001F451 on the morning board' if board else 'takes the \U0001F451'
    if mode == SCORING_PER_GAME:
        return ("each game pays **1 point plus one for every player you beat**; the "
                f"most points across the day {crown}.")
    if mode == SCORING_OFF:
        return ("no points here: every score is ranked, best first, and the streaks "
                "are the game.")
    # "today's games" is the rotation's own term for the games that score, so a
    # server running without one says plain "any game"; the scale is the same
    # either way (first place is worth the day's turnout).
    where = "any of today's games" if rotation else 'any game'
    # The other half of a scale built on the day's turnout: skipping a game
    # puts you under everyone who played it. The board draws that as a poop, so
    # it is explained here -- but only where there is a board to see it on.
    skipped = (f" Sit one out and you place last in it ({POOP_MEDAL} "
               f"{NO_SHOW_SCORE})." if board and shame else '')
    return (f"first place in {where} is worth **the number of players "
            "who showed up today**, one fewer for each place below; the most points "
            f"across the day {crown}.{skipped}")


def _listing(places):
    """'a', 'a and b', 'a, b and c'."""
    return places[0] if len(places) == 1 else ', '.join(places[:-1]) + ' and ' + places[-1]


def build_help_text(cfg):
    """The explainer: how to play, what a result is worth, when the day turns,
    where streaks live, and the commands. Every part reads this server's
    settings, so it never sends anyone to a board, a sticky, a post or a
    reaction the server has switched off. One message, under Discord's 2000."""
    channel = (f"<#{cfg['input_channel_id']}>" if cfg['input_channel_id']
               else 'the scoreboard channel')
    board, sticky = cfg['daily_enabled'], cfg['sticky_enabled']
    if cfg['rotation_enabled']:
        n = cfg['rotation_count']
        places = ['`/play`']
        if cfg['rotation_announce']:
            places.append("the daily Today's games post")
        if sticky and cfg['sticky_games']:
            places.append("the sticky's game buttons")
        rotation = (f"**{n} game{'' if n == 1 else 's'}** score each day (today's "
                    f"rotation), listed in {_listing(places)}. Every other tracked game "
                    "still counts toward streaks.")
    else:
        rotation = "every tracked game scores, every day."
    start = cfg['hours_after_midnight']
    day = (f"\U0001F550 **The day**: starts at {start:02d}:00 ({cfg['timezone']}) and "
           f"results count for {cfg['time_window_hours']} hours"
           + (f"; the board for it posts at {store.post_hour(cfg):02d}:00." if board else '.'))
    fire = [where for where, on in (('the board', board), ('the sticky', sticky)) if on]
    streaks = ("\U0001F525 **Streaks**: a scoring result a day keeps yours alive. "
               + (f"The fire on {_listing(fire)} is the server's; `/stats` shows yours."
                  if fire else "`/stats` shows yours."))
    lines = [
        HELP_HEADING,
        f"\U0001F3AE **Play**: pick a game from {'the sticky or ' if sticky else ''}`/play`, "
        f"then paste the share text it gives you into {channel}. That's it: the bot "
        "reads it from there.",
        f"\U0001F3C6 **Points**: {_scoring_blurb(cfg['scoring'], board, cfg['rotation_enabled'], cfg['daily_shame'])}",
        f"\U0001F504 **Today's games**: {rotation}",
        day,
        streaks,
    ]
    # Reactions go on as the sticky pass counts each result, so no sticky, none.
    if cfg['reactions_enabled'] and sticky:
        which = (" in today's games" if cfg['reactions_rotation_only']
                 and cfg['rotation_enabled'] else '')
        tiers = dict(tier_examples())
        listed = ', '.join(f'{emoji} {tier}' for tier, emoji in tiers.items())
        lines.append(f"{BELOW_PODIUM} **Reactions**: each result{which} gets where it "
                     f"placed when it was posted ({' '.join(PLACE_EMOJI)}, {BELOW_PODIUM} "
                     f"otherwise), and how it went on top of that ({listed}), plus a "
                     f"flourish or two on the best of them. Every face but the "
                     f"{tiers[ACED]} is picked at random, so no two days read the same.")
    lines.append("-# `/play` today's games · `/stats` your streaks · `/suggest` propose a "
                 "game · `/help` this again")
    return '\n'.join(lines)


def build_help_response(cfg):
    return _ephemeral(build_help_text(cfg))


def _mark_welcomed(guild_id, user_id, where):
    """Best-effort: a failed stamp costs a repeat explainer, never a reply."""
    if not (guild_id and user_id):
        return
    try:
        store.mark_welcomed(guild_id, user_id)
    except Exception as e:
        print(f'{where}: welcome mark skipped -- {type(e).__name__}: {e}')


def handle_help(body):
    """`/help`. Answered inline -- nothing here reads a channel -- and the player
    is marked welcomed, so the automatic first-click copy never follows something
    they have already read."""
    guild_id = interaction_guild_id(body)
    _mark_welcomed(guild_id, interaction_user_id(body), 'help')
    return build_help_response(guild_cfg(guild_id))


def welcome_if_new(guild_id, user_id, application_id, token):
    """The explainer as a second ephemeral message under a player's FIRST live
    view (Play, Scores, /stats), sent from phase two of the deferred reply on
    the same interaction token. PROFILE.welcomed_at records that it went out;
    the check is one GetItem per click. True when the follow-up was sent.

    Never fails the view it rides under: any store or Discord problem just
    means the welcome waits for the next click.
    """
    if not (guild_id and user_id and application_id and token):
        return False
    try:
        if store.get_profile(guild_id, user_id).get('welcomed_at'):
            return False
        r = _session.post(
            f'{DISCORD_API_BASE}/webhooks/{application_id}/{token}',
            json={'content': build_help_text(guild_cfg(guild_id)),
                  'flags': FLAG_EPHEMERAL, 'allowed_mentions': {'parse': []}})
        if not r.ok:
            print(f'welcome: follow-up failed {r.status_code} {r.text[:200]}')
            return False
        store.mark_welcomed(guild_id, user_id)
        return True
    except Exception as e:
        print(f'welcome: skipped -- {type(e).__name__}: {e}')
        return False


def interaction_user_id(body):
    """ID of the user who triggered an interaction.

    Discord nests the acting user under `member.user` for guild interactions and
    promotes it to a top-level `user` in DMs, so check both. Returns None when
    neither is present (e.g. a bare test fixture), which callers treat as
    "unknown user" and fall back to listing every game.
    """
    member = body.get('member') or {}
    return (member.get('user') or body.get('user') or {}).get('id')


def _wants_all(body):
    """True when the caller asked for the whole roster in one list -- the games
    on the sticky included -- rather than the everything-else list Play gives.
    That is `/play all:true`, the only way in now that the sticky's More button
    is gone.
    """
    data = body.get('data') or {}
    return any(o.get('name') == 'all' and o.get('value')
               for o in data.get('options') or [])


def interaction_guild_id(body):
    """guild_id of the interaction, for config and streak lookups.

    Guild interactions carry it directly; local test fixtures (and DMs) don't,
    so fall back to resolving the channel via the API (cached per process).
    None -- a channel with no guild -- just renders with default settings.
    """
    return body.get('guild_id') or safe_guild_id(_session, body.get('channel_id'))


# Discord interaction types, by the `type` on every interaction body.
INTERACTION_KINDS = {2: 'command', 3: 'component', 4: 'autocomplete', 5: 'modal'}
# The option types that nest further options: SUB_COMMAND and SUB_COMMAND_GROUP.
_NESTING_OPTIONS = (1, 2)


def describe_interaction(body):
    """One flat record of what an interaction is and who sent it.

    `name` is the thing the user reached for -- a command with its subcommand
    path (`setup rotation`), or a component's custom_id (`sticky_play`) -- and
    `args` its leaf options as `k=v` pairs, or a select's chosen values. Modal
    fields are left out: a /suggest paste is long, and it already lands in the
    dev channel whole.
    """
    data = body.get('data') or {}
    kind = INTERACTION_KINDS.get(body.get('type'), f"type{body.get('type')}")
    name = data.get('name') or data.get('custom_id') or ''
    args = ''
    if kind == 'command':
        options = data.get('options') or []
        while options and options[0].get('type') in _NESTING_OPTIONS:
            name += f" {options[0].get('name')}"
            options = options[0].get('options') or []
        args = ' '.join(f"{o.get('name')}={o.get('value')}" for o in options)
    elif kind == 'component' and data.get('values'):
        args = ','.join(map(str, data['values']))
    user = (body.get('member') or {}).get('user') or body.get('user') or {}
    return {
        'event': 'interaction', 'kind': kind, 'name': name, 'args': args[:200],
        'guild': body.get('guild_id'), 'channel': body.get('channel_id'),
        'user': user.get('id'), 'username': user.get('username'),
    }


def log_interaction(body):
    """Print the per-click record as one JSON line -- the bot's only usage
    telemetry.

    JSON rather than prose because the point is querying, not reading:
    CloudWatch Logs Insights discovers the keys of a JSON log line as fields,
    so `filter event = "interaction" | stats count() by name, username` answers
    who uses what with no parse step. Printed once per interaction, on the
    invocation that ACKs it -- phase two of a deferred reply is the same click,
    not a second one, and the keep-warm ping never reaches this.
    """
    print(json.dumps(describe_interaction(body)))


def unplayed_games(channel_id, cfg, user_id=None, guild_id=None):
    """Today's tracked games the presser hasn't logged yet, plus the live
    results and streak bundle backing them, plus the keys the sticky is
    showing as buttons right now.

    Shared by the Play and Random buttons so both work off the same live view
    of the channel. When user_id is known, games that user has already logged
    today are dropped, making the result personal to whoever pressed; with no
    user_id (an unidentifiable presser) every game is returned. results and
    the gather_streaks() bundle (or None) cover ALL games, so counts and
    streak numbers reflect the whole server, not just the presser's remainder.
    games always spans the full enabled list (the /play all:true view); the
    returned rotation (key list or None) is how the caller narrows it.

    on_sticky is ranked over every game today offers, deliberately BEFORE the
    presser's filter: it has to name the same set sticky_lambda rendered for
    the channel, which knows nothing about who is looking.
    """
    today = None
    rotation = None
    try:
        results, puzzle_numbers, today, rotation, _ = fetch_today_results(channel_id, cfg)
    except Exception:
        # Counts are a nice-to-have; never let a fetch/parse hiccup block the
        # core action. Fall back to today's games with no counts or streaks --
        # and no rotation filter, the same fail-open the rest of this takes.
        results, puzzle_numbers = {}, compute_puzzle_numbers(datetime.utcnow())

    games = build_games(puzzle_numbers, cfg['game_overrides'])

    streaks = None
    if today is not None:
        streaks = gather_streaks(guild_id, today, results, games,
                                 cfg['minimum_players'])

    rot = None if rotation is None else set(rotation)
    playable = games if rot is None else [g for g in games if g.key in rot]
    on_sticky = {g.key for g in sticky_row_games(playable, results, streaks,
                                                 cfg['sticky_games'])}

    if user_id is not None:
        games = [g for g in games if user_id not in results.get(g.key, {})]

    return games, results, streaks, rotation, on_sticky


ALL_PLAYED_MESSAGE = "\U0001F389 You've played every tracked game today!"
STICKY_GAMES_MESSAGE = ("\U0001F389 You've played everything but today's games! "
                        "They're the buttons on the sticky — "
                        "or `/play all:true` for one list")


def build_play_response(channel_id, user_id=None, guild_id=None, cfg=None, show_all=False):
    """Build an ephemeral message with link buttons for tracked games.

    When user_id is known, only games that user hasn't logged today are shown,
    so the Play list is personal to whoever pressed the button. Play is the
    COMPLEMENT of the sticky's game row -- every enabled game except the ones
    already sitting there as buttons -- so the two surfaces divide the roster
    instead of repeating it, and a guild with the row off (sticky_games 0, the
    default) gets the whole list exactly as before. show_all
    (`/play all:true`) overrides that and lists everything, today's scoring
    games sorted to the top. Buttons
    follow the app-wide game ordering (game_sort_key, same as scoreboard
    sections): today's live count, then the server streak the labels actually
    show, then 30-day distinct players, then all-time distinct players, then
    title. Labels
    (game_link_button) carry a fire-streak suffix
    while the game's server streak is alive; today's count orders the list but
    is not shown. With no user_id (an unidentifiable presser) every game is
    listed; with no reachable store the order falls back to live count then
    title.
    """
    cfg = cfg or guild_cfg(guild_id)
    games, results, streaks, rotation, on_sticky = unplayed_games(
        channel_id, cfg, user_id, guild_id)
    game_streaks = (streaks or {}).get('games', {})

    rot = set(rotation) if rotation is not None else None
    restricted = bool(on_sticky) and not show_all
    sticky_left = False
    if restricted:
        # The complement: those games are already buttons on the sticky, so a
        # Play list carrying them would be the sticky with extra taps.
        sticky_left = any(g.key in on_sticky for g in games)
        games = [g for g in games if g.key not in on_sticky]

    # Scored games always sort above off-rotation ones -- the split only bites
    # on all:true, the one list that mixes both; the app-wide play-count
    # ordering applies within each block.
    games.sort(key=lambda g: (rot is not None and g.key not in rot,)
               + game_sort_key(g, results, streaks))

    buttons = [game_link_button(g, game_streaks.get(g.key, 0)) for g in games]

    # /setup games holds a server to MAX_ENABLED_GAMES, which is exactly what
    # fits under the Random row. The one way past it is a default-on GameSpec
    # shipping into a server already at the cap, so drop the tail rather than
    # send a sixth row for Discord to reject -- and say so in the content, since
    # a game that vanished silently is indistinguishable from one an admin
    # turned off. The list is in play-count order: what goes is what the server
    # plays least.
    hidden = max(0, len(buttons) - MAX_ENABLED_GAMES)
    buttons = buttons[:MAX_ENABLED_GAMES]

    action_rows = []
    for i in range(0, len(buttons), MAX_BUTTONS_PER_ROW):
        action_rows.append({"type": 1, "components": buttons[i:i + MAX_BUTTONS_PER_ROW]})

    # A "surprise me" shortcut: one random unplayed game as its own grey link
    # button on its own row above the list. Resolved here, at click time, so the
    # link points straight at a game this user hasn't logged — one tap, no
    # follow-up.
    if games:
        pick = random.choice(games)
        action_rows.insert(0, {"type": 1, "components": [
            {"type": 2, "style": 5, "label": "\U0001F52E Random", "url": pick.url},
        ]})

    # Filtering can empty the list once a user has logged everything today --
    # everything off the sticky, when its row is up; point at the row's games
    # only while some of them are actually left to play.
    if action_rows:
        content = "Pick a game to play!"
        if hidden:
            content += (f" (+{hidden} more this list can't fit — "
                        f"turn some off with `/setup games`.)")
    elif restricted and sticky_left:
        content = STICKY_GAMES_MESSAGE
    else:
        content = ALL_PLAYED_MESSAGE

    return {
        "type": 4,
        "data": {
            "flags": FLAG_EPHEMERAL,
            "content": content,
            "components": action_rows,
        },
    }


# --- Deferred replies for the live views ---------------------------------------
# /play and the sticky's three buttons all read a page of channel history and the
# streak store before they can answer. Warm that is ~300ms, with room to spare
# inside Discord's 3-second ACK deadline -- but cold the same work has measured
# 3.7s end to end, and Discord renders anything past 3s as "This interaction
# failed". Two defences:
#
# - ACK first and do the work in a second, asynchronous invocation of this same
#   function, which answers by editing the placeholder. That takes the deadline
#   off the work entirely: the ACK is a bare type-5 with no I/O behind it, and
#   the follow-up has the interaction token's full 15 minutes.
# - A keep-warm EventBridge ping (rule daily-game-play, every 5 minutes) holds
#   one environment warm. At a couple dozen clicks a day, that turns cold ACKs
#   from roughly every other click into a rarity; the deferral above stays as
#   the backstop for the colds that remain (a deploy, a concurrent overlap).

ACTION_PLAY, ACTION_SCORES, ACTION_STATS = 'play', 'scores', 'stats'

# Envelope key for the self-invoke payload. Only ever read off a direct
# invocation -- anything arriving through the public Function URL carries a
# requestContext, so this cannot be driven from outside.
DEFERRED_KEY = 'deferred_work'

_lambda_client = None


def _lambda():
    """Lazy Lambda client for the self-invoke.

    Timeouts are tight on purpose: this call sits inside the 3-second ACK
    budget, so it has to either succeed quickly or fail early enough to leave
    time for the inline fallback.
    """
    global _lambda_client
    if _lambda_client is None:
        import boto3
        from botocore.config import Config
        _lambda_client = boto3.client('lambda', region_name=store.AWS_REGION, config=Config(
            connect_timeout=1, read_timeout=1, retries={'max_attempts': 1, 'mode': 'standard'}))
    return _lambda_client


def _invoke_self(work):
    """Queue phase two. True when Lambda accepted it (202)."""
    function_name = os.getenv('AWS_LAMBDA_FUNCTION_NAME')
    if not function_name:
        return False    # not on Lambda (local run): answer inline
    try:
        resp = _lambda().invoke(FunctionName=function_name, InvocationType='Event',
                                Payload=json.dumps({DEFERRED_KEY: work}).encode())
        return resp.get('StatusCode') == 202
    except Exception as e:
        print(f'defer: self-invoke failed, answering inline -- {type(e).__name__}: {e}')
        return False


# Build the self-invoke client during INIT, which runs at boosted CPU on every
# fresh environment -- including the ones the keep-warm ping creates -- so a
# cold click's ACK doesn't pay client construction inside the 3-second window.
# Local runs (no function name) skip it; a failure falls back to the lazy path.
if os.getenv('AWS_LAMBDA_FUNCTION_NAME'):
    try:
        _lambda()
    except Exception as _e:
        print(f'init: lambda client prebuild failed, will retry lazily -- '
              f'{type(_e).__name__}: {_e}')


def build_live_response(action, channel_id, user_id=None, guild_id=None, cfg=None,
                        show_all=False):
    """The reply for one live view, as a complete interaction response.

    Single entry point for both phases: phase two PATCHes its ['data'] over the
    placeholder, and the inline fallback returns it whole.
    """
    cfg = cfg or guild_cfg(guild_id)
    if action == ACTION_SCORES:
        return build_scoreboard_response(channel_id, guild_id, cfg)
    if action == ACTION_STATS:
        return build_stats_response(channel_id, user_id, guild_id, cfg)
    return build_play_response(channel_id, user_id, guild_id, cfg, show_all)


def defer(action, body):
    """ACK now; hand the work to a second invocation.

    Falls back to answering inline whenever the self-invoke can't be made -- no
    lambda:InvokeFunction on the role, a throttle, or a local run. That is
    precisely the old behaviour, so the surface keeps working; it is just back to
    racing the 3-second clock, which is where it started.
    """
    work = {
        'action': action,
        'channel_id': body['channel_id'],
        'user_id': interaction_user_id(body),
        'show_all': _wants_all(body),
        # A missing guild_id costs a Discord round trip to resolve, so leave that
        # to phase two, which has no deadline worth protecting.
        'guild_id': body.get('guild_id'),
        'application_id': body.get('application_id'),
        'token': body.get('token'),
    }
    if work['application_id'] and work['token'] and _invoke_self(work):
        # DEFERRED_CHANNEL_MESSAGE_WITH_SOURCE. EPHEMERAL is the only flag
        # Discord accepts on a defer, so the Scores board sets IS_COMPONENTS_V2
        # on the follow-up edit -- which is where Discord wants it anyway.
        return {'type': 5, 'data': {'flags': FLAG_EPHEMERAL}}
    guild_id = interaction_guild_id(body)
    return build_live_response(action, body['channel_id'], interaction_user_id(body),
                               guild_id, guild_cfg(guild_id), work['show_all'])


def run_deferred(work):
    """Phase two: build the real reply and edit it over the placeholder."""
    channel_id = work['channel_id']
    guild_id = work.get('guild_id') or safe_guild_id(_session, channel_id)
    try:
        data = build_live_response(work['action'], channel_id,
                                   work.get('user_id'), guild_id,
                                   show_all=work.get('show_all'))['data']
    except Exception as e:
        traceback.print_exc()
        # The placeholder would otherwise sit on "thinking" until it expires, so
        # always leave something readable behind.
        data = {'content': f'Something went wrong ({type(e).__name__}) — try again shortly.'}
    r = _session.patch(
        f"{DISCORD_API_BASE}/webhooks/{work['application_id']}/{work['token']}"
        f"/messages/@original", json=data)
    if not r.ok:
        print(f'defer: follow-up edit failed {r.status_code} {r.text[:200]}')
    # The one-time explainer rides under the view, after it -- a newcomer's
    # first click gets what they asked for first and the rules second.
    welcomed = r.ok and welcome_if_new(guild_id, work.get('user_id'),
                                       work['application_id'], work['token'])
    return {'statusCode': 200,
            'body': json.dumps({'deferred': work['action'], 'edit': r.status_code,
                                'welcomed': welcomed})}


# --- /setup (admin configuration) ----------------------------------------------

def _update(content, components=None):
    """UPDATE_MESSAGE: rewrite the ephemeral message a component lives on --
    how a select menu turns into its own confirmation."""
    return {'type': 7, 'data': {'content': content, 'components': components or []}}


def is_admin(body):
    """member.permissions re-checked here; command registration also gates on
    Manage Server, but registration-side gating is a UI default admins can
    re-map, so the handler stays the authority."""
    try:
        perms = int((body.get('member') or {}).get('permissions') or 0)
    except (TypeError, ValueError):
        perms = 0
    return bool(perms & (PERM_ADMINISTRATOR | PERM_MANAGE_GUILD))


def _sub_options(body):
    """(subcommand_name, {option: value}) for a slash command with subcommands."""
    sub = (body.get('data', {}).get('options') or [{}])[0]
    return sub.get('name'), {o['name']: o.get('value') for o in sub.get('options') or []}


def resolve_channel(channel_id, guild_id):
    """(channel, None) when the bot can see channel_id and it belongs to this
    guild, else (None, user-facing error). The visibility check is the real
    gate: a channel the bot can't read can't be parsed or posted to, so refuse
    it now with instructions instead of silently going dark later.
    """
    try:
        r = _session.get(f'{DISCORD_API_BASE}/channels/{channel_id}')
    except Exception:
        return None, "Couldn't reach Discord to check that channel — try again."
    if not r.ok:
        return None, (f"I can't access that channel (`{channel_id}`). If the ID is right, "
                      "give me **View Channel** and **Read Message History** there and retry. "
                      "You can always pass the raw ID via the `channel_id` option.")
    ch = r.json()
    if str(ch.get('guild_id')) != str(guild_id):
        return None, 'That channel belongs to a different server.'
    if ch.get('type') not in TEXT_CHANNEL_TYPES:
        return None, 'Pick a text or announcement channel.'
    return ch, None


def channel_select_row(sub):
    return {'type': 1, 'components': [{
        'type': 8,   # channel select
        'custom_id': f'{CHANNEL_SELECT_PREFIX}{sub.name}',
        'channel_types': list(TEXT_CHANNEL_TYPES),
        'placeholder': f'Select the {sub.label}',
    }]}


def channel_picker_response(sub):
    lines = [f'Pick the **{sub.label}** — {sub.blurb}.',
             f'-# Channel not listed? Run `/setup {sub.name} channel_id:<id>` '
             'with the raw ID instead.']
    if not sub.combined:
        lines.append(OVERRIDE_NOTE)
    return _ephemeral('\n'.join(lines), components=[channel_select_row(sub)])


def go_live_hint(cfg):
    """The `-#` line naming what a server still has to point the bot at, or
    None when both channels are set.

    A server with neither set is being onboarded, so it gets pointed at the
    one-channel default rather than told to run two commands.
    """
    if not any(cfg[f] for f in store.CHANNEL_FIELDS):
        return '-# Nothing posts yet — run `/setup channel` to go live.'
    missing = [c.name for c in store.CHANNEL_SUBS
               if not c.combined and not cfg[c.fields[0]]]
    return f'-# Still needed to go live: `/setup {missing[0]}`.' if missing else None


def set_channel(guild_id, sub, channel_id, cfg=None):
    """Validate and store a channel choice. Returns (user-facing text, error).

    `sub` is the store.ChannelSub that was invoked, which is also what decides
    how many fields the choice writes: `/setup channel` sets both sides at once.
    """
    ch, err = resolve_channel(channel_id, guild_id)
    if err:
        return None, err
    cfg = cfg or guild_cfg(guild_id, strict=True)
    updates = {f: str(channel_id) for f in sub.fields}
    store.update_config(guild_id, updates)
    lines = [f'✅ Set: <#{channel_id}> is {sub.blurb}.']
    if not sub.combined:
        lines.append(OVERRIDE_NOTE)
    # Apply the write to the config we already hold rather than re-reading it:
    # get_item is eventually consistent, so a read this soon after the write can
    # still miss it and tell the admin to set the channel they just set.
    hint = go_live_hint({**cfg, **updates})
    if hint:
        lines.append(hint)
    return '\n'.join(lines), None


def games_select_row(game_overrides):
    # One option per GameSpec, 22 of scoreboard.MAX_SELECT_OPTIONS today; see
    # the split-across-two-messages note on that constant for when it runs out.
    options = [{
        'label': spec.title,
        'value': spec.key,
        'emoji': {'name': spec.emoji},
        'default': spec_enabled(spec, game_overrides),
    } for spec in sorted(GAME_SPECS, key=lambda s: s.title.lower())]
    # Discord's picker stops taking ticks at max_values, which is where the cap
    # is felt. It widens past the cap only for a menu that already shows more
    # ticked -- a default-on GameSpec shipped into a server at the cap -- rather
    # than pre-ticking more games than it allows; the submit handler still
    # refuses to save that many.
    ticked = sum(1 for o in options if o['default'])
    return {'type': 1, 'components': [{
        'type': 3,   # string select
        'custom_id': GAMES_SELECT_ID,
        'options': options,
        'min_values': 0,
        'max_values': min(len(options), max(MAX_ENABLED_GAMES, ticked)),
        'placeholder': f'Choose up to {MAX_ENABLED_GAMES} games to track',
    }]}


def _game_list(specs):
    return ', '.join(f'{s.emoji} {s.title}'
                     for s in sorted(specs, key=lambda s: s.title.lower()))


def apply_games_selection(guild_id, selected_keys):
    """Store the admin's menu choice as overrides-only: games matching their
    coded default are left unset, so a future game arrives with its default
    instead of a frozen snapshot of this menu."""
    selected = set(selected_keys)
    overrides = {spec.key: spec.key in selected for spec in GAME_SPECS
                 if (spec.key in selected) != (not spec.disabled)}
    store.update_config(guild_id, {'game_overrides': overrides})
    enabled = [s for s in GAME_SPECS if s.key in selected]
    disabled = [s for s in GAME_SPECS if s.key not in selected]
    if not enabled:
        return '⚠️ No games tracked — the scoreboard will be empty until some are re-enabled.'
    text = f'✅ Tracking {len(enabled)} games: {_game_list(enabled)}'
    if disabled:
        text += f'\n-# Off: {_game_list(disabled)}'
    return text


def commentary_select_row(overrides):
    """One option per registered message kind (commentary.TRIGGERS), ticked to
    the guild's effective state -- the same shape as the games menu, so a
    Trigger added in code shows up here with no re-registration."""
    options = [{
        'label': t.label,
        'value': t.key,
        'description': t.describe[:100],
        'default': commentary.trigger_enabled(t, overrides),
    } for t in commentary.TRIGGERS]
    return {'type': 1, 'components': [{
        'type': 3,   # string select
        'custom_id': COMMENTARY_SELECT_ID,
        'options': options,
        'min_values': 0,
        'max_values': len(options),
        'placeholder': 'Choose which messages to post',
    }]}


def apply_commentary_selection(guild_id, selected_keys):
    """Overrides-only, like apply_games_selection: kinds matching their coded
    default are left unset, so a future kind arrives with its default."""
    selected = set(selected_keys)
    overrides = {t.key: t.key in selected for t in commentary.TRIGGERS
                 if (t.key in selected) != t.default}
    store.update_config(guild_id, {'commentary_overrides': overrides})
    on = [t.label for t in commentary.TRIGGERS if t.key in selected]
    off = [t.label for t in commentary.TRIGGERS if t.key not in selected]
    text = (f"✅ Commentary will post: {', '.join(on)}." if on
            else '⚠️ Every message kind is off — commentary will post nothing.')
    if off:
        text += f"\n-# Off: {', '.join(off)}"
    return text


def commentary_phrase(cfg):
    """The commentary settings in prose, for the summary and the toggle reply."""
    if not cfg['commentary_enabled']:
        return ('\U0001F4AC Commentary: **off** — nothing posts between boards '
                '(`/setup commentary enabled:True` starts it).')
    on = commentary.enabled_triggers(cfg)
    # Only the tuning of the kinds actually posting: with a kind switched off --
    # the nudge ships that way -- its hours would describe nothing.
    keys = {t.key for t in on}
    tuning = []
    if 'midday' in keys:
        tuning.append(f"midday standings at {cfg['commentary_midday_hour']:02d}:00")
    if 'last_call' in keys:
        tuning.append(f"last call {cfg['commentary_last_call_hours']}h before the close"
                      if cfg['commentary_last_call_hours'] else 'no last call (0 hours)')
    if 'nudge' in keys:
        tuning.append(f"nudges {cfg['commentary_nudge_after_hours']}h after a player's "
                      'last result')
    return (f"\U0001F4AC Commentary: **on**{' — ' + ', '.join(tuning) if tuning else ''}; "
            f"posting {', '.join(t.label for t in on) if on else 'nothing (every kind is off)'}.")


def reactions_phrase(cfg):
    """The reactions setting in a few words, for the summary."""
    if not cfg['reactions_enabled']:
        return 'off'
    return 'on, rotation games only' if cfg['reactions_rotation_only'] else 'on'


def delete_stickies(channel_id):
    """Best-effort removal of the bot's sticky when an admin turns it off --
    otherwise the last sticky would sit there dead until someone deletes it.
    Uses scoreboard.is_sticky_message, the same definition sticky_lambda posts
    and collapses against, so this delete path can't match anything wider."""
    removed = 0
    for m in fetch_messages(_session, channel_id, limit=50):
        if is_sticky_message(m, DISCORD_BOT_ID):
            _session.delete(f'{DISCORD_API_BASE}/channels/{channel_id}/messages/{m["id"]}')
            removed += 1
    return removed


def _shame_phrase(cfg):
    """The board's no-show lines in a few words, for the summary and the
    /setup daily reply. Named for the setting (`shame`), phrased for the board,
    where the line itself reads DNF. Says plainly when the setting is moot: the
    lines only exist on the placement scale, which is the only one that ranks a
    player who never turned up."""
    if not cfg['daily_shame']:
        return 'no DNF lines'
    if cfg['scoring'] != SCORING_PLACEMENT:
        return f"DNF lines on (unused while scoring is {cfg['scoring']})"
    return f'skipped games shown as {POOP_MEDAL} {NO_SHOW_SCORE}'


def sticky_row_phrase(count):
    """The sticky's game row in prose, for the summary and the toggle reply."""
    if not count:
        return 'no game buttons'
    return f"{count} game button{'' if count == 1 else 's'}"


def config_summary(cfg):
    def ch(v):
        return f'<#{v}>' if v else '*not set*'

    def onoff(v):
        return 'on' if v else 'off'

    post_hour = store.post_hour(cfg)
    lines = ['### ⚙️ Scoreboard setup']
    lines += [f'{c.label.capitalize()} ({c.blurb}): {ch(cfg[c.fields[0]])}'
              for c in store.CHANNEL_SUBS if not c.combined]
    lines += [
        f"Daily scoreboard: **{onoff(cfg['daily_enabled'])}** "
        f"({_shame_phrase(cfg)}) · "
        f"Sticky: **{onoff(cfg['sticky_enabled'])}** "
        f"({sticky_row_phrase(cfg['sticky_games'])}) · "
        f"Link previews: **{'stripped' if cfg['suppress_embeds'] else 'kept'}** · "
        f"Wordle recap: **{'deleted' if cfg['delete_wordle_recap'] else 'kept'}** · "
        f"Reactions: **{reactions_phrase(cfg)}**",
        f"Rotation: **{onoff(cfg['rotation_enabled'])}** — "
        f"{cfg['rotation_count']} games/day, {cfg['rotation_mode']} mode, "
        f"stay \u2265{cfg['rotation_keep_players']} / "
        f"join \u2265{cfg['rotation_promote_players']} players, "
        f"off-rotation {cfg['rotation_off_mode']}, "
        f"announcement **{onoff(cfg['rotation_announce'])}** · "
        f"Scoring: **{cfg['scoring']}**",
        f"Timezone `{cfg['timezone']}` · day starts {cfg['hours_after_midnight']:02d}:00 · "
        f"posts {post_hour:02d}:00 · window {cfg['time_window_hours']}h",
        f"Minimum players {cfg['minimum_players']} · "
        f"volume ~{cfg['hundreds_of_messages'] * 100} msgs/day · "
        f"pins {cfg['pin_keep_days']} days",
        commentary_phrase(cfg),
    ]
    enabled = [s for s in GAME_SPECS if spec_enabled(s, cfg['game_overrides'])]
    disabled = [s for s in GAME_SPECS if not spec_enabled(s, cfg['game_overrides'])]
    lines.append(f'Tracking {len(enabled)} games: {_game_list(enabled)}'
                 if enabled else '⚠️ Tracking no games!')
    if disabled:
        lines.append(f'-# Off: {_game_list(disabled)}')
    tz = ZoneInfo(cfg['timezone'])
    today = store.day_str(reference_date(datetime.now(tz), tz,
                                         cfg['hours_after_midnight']))
    rotation = store.current_rotation(cfg, today)
    if rotation:
        by_key = {s.key: s for s in GAME_SPECS}
        todays = [by_key[k] for k in rotation if k in by_key]
        if todays:
            lines.append(f"-# Today's games: {_game_list(todays)}")
    hint = go_live_hint(cfg)
    if hint:
        lines.append(hint)
    return '\n'.join(lines)


def collect_updates(group, args):
    """Slash-command options for one /setup subcommand -> config updates.

    Driven entirely by store.CONFIG_FIELDS, the same table register_commands.py
    registers the options from, so an option name cannot exist on one side only
    -- the old hand-written mapping silently ignored anything that drifted.
    Absent options are left out, so a subcommand only writes what was passed.

    field.apply, not field.coerce: it also holds the value inside the field's
    declared bounds, which Discord's picker enforces only for the registration
    currently live.
    """
    updates = {}
    for field in store.setup_options(group):
        value = args.get(field.option_name)
        if value is not None:
            updates[field.name] = field.apply(value)
    return updates


def handle_setup(body, guild_id):
    sub, args = _sub_options(body)
    cfg = guild_cfg(guild_id, strict=True)

    chan_sub = store.channel_sub(sub)
    if chan_sub:
        cid = args.get('channel') or (str(args.get('channel_id') or '').strip() or None)
        if cid is None:
            return channel_picker_response(chan_sub)
        text, err = set_channel(guild_id, chan_sub, cid, cfg)
        return _ephemeral(err or text)

    if sub == 'daily':
        # `dnf` rides along on this subcommand (store.CONFIG_FIELDS declares it
        # group='daily'), the same way the sticky's own fields do. Options left
        # out keep their stored values.
        enabled = bool(args.get('enabled'))
        updates = {'daily_enabled': enabled, **collect_updates('daily', args)}
        store.update_config(guild_id, updates)
        if not enabled:
            return _ephemeral('⏸️ Daily scoreboard paused — no daily posts, '
                              'and the sticky drops its Yesterday link.')
        return _ephemeral('▶️ Daily scoreboard resumed — posts at its scheduled '
                          f'hour, {_shame_phrase({**cfg, **updates})}.')

    if sub == 'sticky':
        # `games` and `delete_wordle_recap` ride along on this subcommand
        # (store.CONFIG_FIELDS declares them group='sticky'), so one call can
        # switch the sticky on, size its game row, and set the recap cleanup.
        # Options left out keep their stored values.
        enabled = bool(args.get('enabled'))
        updates = {'sticky_enabled': enabled, **collect_updates('sticky', args)}
        store.update_config(guild_id, updates)
        merged = {**cfg, **updates}
        if enabled:
            recap = (", deleting the Wordle app's daily recap on sight"
                     if merged['delete_wordle_recap'] else '')
            return _ephemeral('▶️ Sticky enabled — it will appear in the input '
                              'channel within a minute, with '
                              f"{sticky_row_phrase(merged['sticky_games'])}{recap}. "
                              'Play lists whatever the row leaves out.')
        note = ''
        if cfg['input_channel_id']:
            try:
                removed = delete_stickies(cfg['input_channel_id'])
                if removed:
                    note = f' (removed {removed} existing)'
            except Exception:
                pass
        return _ephemeral(f'⏸️ Sticky disabled{note}.')

    if sub == 'rotation':
        # The shape fields ride along on this toggle (group='rotation'), the
        # same way sticky_games rides on /setup sticky: one call can switch
        # rotation on and size, re-mode, or re-threshold it in one write.
        # Options left out keep their stored values.
        enabled = bool(args.get('enabled'))
        updates = {'rotation_enabled': enabled, **collect_updates('rotation', args)}
        store.update_config(guild_id, updates)
        merged = {**cfg, **updates}
        if enabled:
            # The thresholds are swap-mode rules, so a random-mode server is
            # not told two numbers that will never be consulted.
            shape = f"{merged['rotation_count']} games a day, {merged['rotation_mode']} mode"
            if merged['rotation_mode'] == 'swap':
                shape += (f" (stay \u2265{merged['rotation_keep_players']} players, "
                          f"join \u2265{merged['rotation_promote_players']})")
            # The announcement is the only part a server sees as a message of
            # its own, so say which way it is set rather than promising a post
            # that rotation_announce has switched off.
            tail = ("The first draw is announced with the next daily board."
                    if merged['rotation_announce'] else
                    "Today's games are drawn silently — no announcement post.")
            return _ephemeral(
                f"\U0001F504 Rotation on — {shape}, off-rotation games "
                f"{merged['rotation_off_mode']}. {tail}")
        return _ephemeral('▶️ Rotation off — every enabled game scores daily.')

    if sub == 'embeds':
        suppress = bool(args.get('suppress'))
        store.update_config(guild_id, {'suppress_embeds': suppress})
        # Suppression happens on the sticky pass, as each result is counted, so
        # a server with the sticky off has nothing scanning for links to strip.
        note = '' if cfg['sticky_enabled'] else ('\n-# The sticky is off, so nothing '
                                                 'scans for results to strip.')
        if suppress:
            return _ephemeral('🔗 Link previews will be stripped from game results '
                              f'as they are counted.{note}')
        return _ephemeral('🔗 Link previews left alone — results already stripped '
                          f'stay that way.{note}')

    if sub == 'reactions':
        # rotation_only rides along (group='reactions'), the way the sticky's
        # options ride on /setup sticky; left out, it keeps its stored value.
        enabled = bool(args.get('enabled'))
        updates = {'reactions_enabled': enabled, **collect_updates('reactions', args)}
        store.update_config(guild_id, updates)
        merged = {**cfg, **updates}
        if not enabled:
            return _ephemeral('⏸️ Reactions off — results already reacted to keep theirs.')
        which = ("today's rotation games" if merged['reactions_rotation_only']
                 and merged['rotation_enabled'] else 'every game')
        # Reactions go on as the sticky pass counts each result, like link
        # stripping, so a server with the sticky off has nothing reacting.
        note = '' if merged['sticky_enabled'] else ('\n-# The sticky is off, so nothing '
                                                    'reacts to results.')
        return _ephemeral(
            f"{BELOW_PODIUM} Reactions on for {which}: every new result gets one, "
            f"where it placed ({' '.join(PLACE_EMOJI)}, {BELOW_PODIUM} otherwise), and how "
            f"it went on top of that, from an ace down to a rough one "
            f"({' '.join(e for _, e in tier_examples())} and friends: every face but the "
            f"{dict(tier_examples())[ACED]} is picked at random), with a flourish or two "
            "piled on a great one or an ace.\n-# Needs Add Reactions in the input "
            "channel. Most servers give it "
            f"to everyone; where yours doesn't, grant it to the bot's role.{note}")

    if sub == 'games':
        return _ephemeral(
            f'Select every game this server should track, up to {MAX_ENABLED_GAMES} — '
            'unselected games are hidden from parsing, the scoreboard, and the Play list.',
            components=[games_select_row(cfg['game_overrides'])],
        )

    if sub == 'time':
        updates = collect_updates('time', args)
        if not updates:
            return _ephemeral(config_summary(cfg))
        tz_name = updates.get('timezone')
        if tz_name:
            try:
                ZoneInfo(tz_name)
            except Exception:
                return _ephemeral(f'Unknown timezone `{tz_name}` — use an IANA name '
                                  'like `America/New_York` or `Europe/London`.')
        merged = {**cfg, **updates}
        post_hour = store.post_hour(merged)
        if post_hour < merged['hours_after_midnight']:
            return _ephemeral("`post_hour` can't be earlier than `day_start_hour` — "
                              "the scoring window must close before the board posts.")
        store.update_config(guild_id, updates)
        return _ephemeral(f"✅ Schedule updated: timezone `{merged['timezone']}`, "
                          f"day starts {merged['hours_after_midnight']:02d}:00, posts "
                          f"{post_hour:02d}:00, window {merged['time_window_hours']}h.")

    if sub == 'limits':
        updates = collect_updates('limits', args)
        if not updates:
            return _ephemeral(config_summary(cfg))
        store.update_config(guild_id, updates)
        merged = {**cfg, **updates}
        return _ephemeral(f"✅ Limits updated: minimum players {merged['minimum_players']}, "
                          f"volume ~{merged['hundreds_of_messages'] * 100} msgs/day, "
                          f"pinning {merged['pin_keep_days']} days of scoreboards.")

    if sub == 'scoring':
        updates = collect_updates('scoring', args)
        if not updates:
            return _ephemeral(config_summary(cfg))
        store.update_config(guild_id, updates)
        # The archive freezes each day's points on the scale that scored it, so
        # a change is forward-only: the boards already posted keep their numbers.
        blurb = dict((v, label) for label, v in store.SCORING_MODES)[updates['scoring']]
        return _ephemeral(f"✅ Scoring set to **{blurb}**. Applies from the next "
                          "board; days already scored keep their points.")

    if sub == 'commentary':
        # The two tuning fields ride along (group='commentary') as on the other
        # toggles; `enabled` is optional here, unlike theirs, because the bare
        # command is the door to the per-kind menu.
        updates = collect_updates('commentary', args)
        if args.get('enabled') is not None:
            updates['commentary_enabled'] = bool(args['enabled'])
        if not updates:
            return _ephemeral(commentary_phrase(cfg) + '\nPick which messages it posts:',
                              components=[commentary_select_row(cfg['commentary_overrides'])])
        store.update_config(guild_id, updates)
        return _ephemeral('✅ ' + commentary_phrase({**cfg, **updates}))

    # 'show' and anything unrecognized fall back to the summary.
    return _ephemeral(config_summary(cfg))


def handle_setup_component(body, guild_id):
    custom_id = body['data']['custom_id']
    values = body['data'].get('values') or []

    if custom_id == GAMES_SELECT_ID:
        if len(values) > MAX_ENABLED_GAMES:
            # The picker enforces max_values itself, so this is a stale client
            # or a menu widened to show a server already over the cap. Nothing
            # is written; the menu comes back with their picks still ticked, so
            # the fix is unticking rather than starting over.
            over = len(values) - MAX_ENABLED_GAMES
            picks = {spec.key: spec.key in values for spec in GAME_SPECS}
            return _update(f'⚠️ A server can track up to {MAX_ENABLED_GAMES} games, '
                           f'and that was {len(values)} — untick {over} and save again.',
                           components=[games_select_row(picks)])
        return _update(apply_games_selection(guild_id, values))

    if custom_id == COMMENTARY_SELECT_ID:
        return _update(apply_commentary_selection(guild_id, values))

    if custom_id.startswith(CHANNEL_SELECT_PREFIX):
        sub = store.channel_sub(custom_id[len(CHANNEL_SELECT_PREFIX):])
        if not sub or not values:
            return _update('Nothing selected — run the command again.')
        text, err = set_channel(guild_id, sub, values[0])
        if err:
            # Keep the picker so the admin can retry after fixing permissions.
            return _update(err, components=[channel_select_row(sub)])
        return _update(text)

    return _update('Unknown control — re-run `/setup`.')


# --- /suggest (a game we don't track yet -> the dev channel) -------------------
# The one env-configured *destination* in the app, and deliberately so: a
# suggestion is for whoever maintains GAME_SPECS, not for the server that raised
# it, so it sits with the global identity vars rather than in per-guild config.
# Unset just means the bot says it has nowhere to send them.
DEV_CHANNEL_ID = os.getenv('DEV_CHANNEL_ID')

SUGGEST_MODAL_ID = 'suggest_modal'
SUGGEST_NAME, SUGGEST_URL, SUGGEST_SCORE = 'name', 'url', 'score'
# Discord allows 4000, but the paste has to fit one dev-channel message
# alongside its attribution, and a daily game's share block is a few lines.
SUGGEST_MAX_SCORE = 1000


def _text_input(custom_id, label, placeholder, style=1, required=True, max_length=None):
    """One modal row: a text input, alone, which is all Discord allows."""
    field = {'type': 4, 'custom_id': custom_id, 'label': label, 'style': style,
             'required': required, 'placeholder': placeholder}
    if max_length is not None:
        field['max_length'] = max_length
    return {'type': 1, 'components': [field]}


def suggest_modal():
    """The /suggest form (response type 9, MODAL).

    A modal rather than command options because what's being collected is a
    pasted share block: slash-command options are single-line, and the line
    breaks are most of what makes a result readable -- and writable as a
    GameSpec pattern later.
    """
    return {'type': 9, 'data': {
        'custom_id': SUGGEST_MODAL_ID,
        'title': 'Suggest a game',
        'components': [
            _text_input(SUGGEST_NAME, 'Game name', 'Framed', max_length=45),
            _text_input(SUGGEST_URL, 'Where do you play it?', 'https://framed.wtf',
                        required=False, max_length=200),
            _text_input(SUGGEST_SCORE, 'Paste a result, exactly as it shares',
                        'Framed #1234 \U0001F7E5\U0001F7E5\U0001F7E9⬛⬛⬛',
                        style=2, max_length=SUGGEST_MAX_SCORE),
        ],
    }}


def modal_values(body):
    """{custom_id: submitted value} for a modal submission."""
    return {c['custom_id']: (c.get('value') or '').strip()
            for row in (body.get('data') or {}).get('components') or []
            for c in row.get('components') or []}


def _fenced(text):
    """A paste, quoted so Discord renders it verbatim -- a share result is emoji
    art and '#'-prefixed lines, both of which markdown would rewrite. Backticks
    are swapped out because they would close the fence early."""
    return '```\n' + text.replace('`', "'") + '\n```'


def _inline(text):
    """One line of user text, rendered as the literal characters it is.

    The paste has a fence to keep it honest; this is for the two fields that sit
    in prose -- the suggested name and the server it came from, the second of
    which a hostile server names itself. Unescaped, either could forge bold text
    or a `[label](url)` masked link in the dev channel, and an embedded newline
    could start a line of its own. Mentions are already inert (allowed_mentions),
    so `<` and `>` are left alone rather than rendering as literal backslashes.
    """
    return re.sub(r'([*_~`|\\\[\]])', r'\\\1', ' '.join(str(text).split()))


def suggestion_message(name, url, score, body):
    """The dev-channel post for one suggestion: what it is, who sent it, and the
    raw paste a new GameSpec pattern would have to match."""
    user_id = interaction_user_id(body)
    who = f'<@{user_id}>' if user_id else 'an unknown user'
    where = (body.get('guild') or {}).get('name') or body.get('guild_id') or 'a DM'
    lines = [f'### \U0001F579️ Game suggestion: {_inline(name)}']
    if url:
        # Angle brackets suppress the embed -- this is a link a stranger typed --
        # so the link itself must not be able to carry a closing bracket and put
        # an embed (or anything else) back on the line.
        lines.append('<{}>'.format(re.sub(r'[<>\s]', '', url)))
    lines.append(f'-# from {who} in {_inline(where)}')
    lines.append(_fenced(score))
    return '\n'.join(lines)[:MAX_MESSAGE_LENGTH]


def already_tracked(spec, guild_id):
    """Reply for a suggestion naming a game GAME_SPECS already covers, including
    the case worth acting on: supported, but switched off in this server."""
    if spec_enabled(spec, guild_cfg(guild_id)['game_overrides']):
        return _ephemeral(f'{spec.emoji} **{spec.title}** is already tracked here — '
                          'post your result in the scores channel and it lands on '
                          "today's board.")
    # A DM has no server to have turned it off, so it is seeing the coded default.
    where = 'in this server' if guild_id else 'by default'
    return _ephemeral(f'{spec.emoji} **{spec.title}** is already supported but turned '
                      f'off {where} — an admin can switch it back on with `/setup games`.')


def keep_suggestion(body, name, url, score):
    """Keep a forwarded suggestion where tools/broadcast.py can find it once the
    game ships, to thank this person in this server. Server suggestions only --
    a DM has no server to be thanked in -- and never at the cost of the reply:
    the log line and the dev-channel post still carry it if the store is down."""
    guild_id, user_id = body.get('guild_id'), interaction_user_id(body)
    if not (guild_id and user_id):
        return
    try:
        store.record_suggestion(guild_id, user_id, name, url, score)
    except Exception as e:
        print(f'suggest: keep failed -- {type(e).__name__}: {e}')


def handle_suggest(body):
    """Modal submit: forward one game suggestion to the dev channel.

    Answered inline rather than deferred like the live views: the only work is a
    single POST, and the container is warm by definition -- opening the modal was
    an invocation of this same function seconds earlier.
    """
    values = modal_values(body)
    name = values.get(SUGGEST_NAME, '')
    url = values.get(SUGGEST_URL, '')
    score = values.get(SUGGEST_SCORE, '')
    if not name or not score:
        return _ephemeral('I need the game name and a pasted result — '
                          'run `/suggest` again.')

    spec = match_suggestion(name, url, score)
    if spec:
        return already_tracked(spec, body.get('guild_id'))

    # Logged and kept before it is sent, so a suggestion outlives a failed post.
    print(f'suggest: {name!r} url={url!r} guild={body.get("guild_id")} '
          f'user={interaction_user_id(body)}')
    keep_suggestion(body, name, url, score)
    if not DEV_CHANNEL_ID:
        return _ephemeral("Thanks! Suggestions aren't set up on this bot right now, "
                          'so there was nowhere to pass it along.')

    r = _session.post(f'{DISCORD_API_BASE}/channels/{DEV_CHANNEL_ID}/messages',
                      json={'content': suggestion_message(name, url, score, body),
                            # Nothing a stranger typed gets to ping the dev server.
                            'allowed_mentions': {'parse': []}})
    if not r.ok:
        print(f'suggest: post failed {r.status_code} {r.text[:200]}')
        return _ephemeral("I couldn't pass that along just now — try again shortly.")
    return _ephemeral(f'✅ Sent **{_inline(name)}** to the devs — thanks! Games show '
                      'up in `/setup games` once one is added.')


def guarded(fn, *args):
    """Run an interaction handler, turning a crash into something readable.
    Discord's own failure mode is an opaque 'interaction failed', which on a
    modal also throws away everything the user typed."""
    try:
        return fn(*args)
    except Exception as e:
        traceback.print_exc()
        return _ephemeral(f'Something went wrong ({type(e).__name__}) — try again shortly.')


def admin_dispatch(fn, body):
    """Shared gate for every config surface: guild-only, Manage Server (or
    Administrator) re-verified server-side, store failures turned into a
    readable ephemeral instead of Discord's opaque 'interaction failed'."""
    guild_id = body.get('guild_id')
    if not guild_id:
        return _ephemeral('Run this in a server — configuration is per-server.')
    if not is_admin(body):
        return _ephemeral('You need **Manage Server** to configure the scoreboard.')
    return guarded(fn, body, guild_id)


def _http(payload):
    return {
        'statusCode': 200,
        'headers': {'Content-Type': 'application/json'},
        'body': json.dumps(payload),
    }


def lambda_handler(event, context):
    # Direct invocations (AWS console/CLI) don't come through the Function URL
    # and already require IAM auth, so skip signature verification
    is_direct = 'requestContext' not in event

    if is_direct:
        # Keep-warm ping (EventBridge rule daily-game-play, every 5 minutes).
        # Answered before any interaction handling: the value of the ping is the
        # INIT that already ran, importing everything and prebuilding the
        # self-invoke client, so real clicks land on a warm environment.
        if event.get('source') == 'aws.events':
            try:
                _lambda()   # normally a no-op: prebuilt during INIT
            except Exception:
                pass
            return {'statusCode': 200, 'body': 'warm'}
        body = event
        # Phase two of a deferred reply, queued by the ACK invocation. Read only
        # here, on the direct (IAM-authenticated) path -- a Function URL request
        # always carries a requestContext, so this is unreachable from outside.
        work = event.get(DEFERRED_KEY)
        if work:
            return run_deferred(work)
    else:
        raw_body = get_body(event)
        try:
            verify_signature(raw_body, event)
        except Exception:
            return {'statusCode': 401, 'body': 'Invalid request signature'}
        body = json.loads(raw_body)

    # PING (type 1) — Discord endpoint validation
    if body.get('type') == 1:
        return _http({'type': 1})

    log_interaction(body)

    # APPLICATION_COMMAND (type 2)
    if body.get('type') == 2:
        command_name = body.get('data', {}).get('name', '')
        if command_name == 'play':
            return _http(defer(ACTION_PLAY, body))
        if command_name == 'stats':
            return _http(defer(ACTION_STATS, body))
        if command_name == 'help':
            return _http(guarded(handle_help, body))
        if command_name == 'setup':
            return _http(admin_dispatch(handle_setup, body))
        if command_name == 'suggest':
            # Opening a modal is the whole response -- the paste comes back as a
            # separate MODAL_SUBMIT interaction below.
            return _http(suggest_modal())

    # MESSAGE_COMPONENT (type 3) — sticky buttons + setup selects
    if body.get('type') == 3:
        custom_id = body.get('data', {}).get('custom_id', '')
        if custom_id == PLAY_BUTTON_CUSTOM_ID:
            return _http(defer(ACTION_PLAY, body))
        if custom_id == SCORES_BUTTON_CUSTOM_ID:
            return _http(defer(ACTION_SCORES, body))
        if (custom_id in (GAMES_SELECT_ID, COMMENTARY_SELECT_ID)
                or custom_id.startswith(CHANNEL_SELECT_PREFIX)):
            return _http(admin_dispatch(handle_setup_component, body))

    # MODAL_SUBMIT (type 5) — the /suggest form coming back filled in
    if body.get('type') == 5:
        if body.get('data', {}).get('custom_id') == SUGGEST_MODAL_ID:
            return _http(guarded(handle_suggest, body))

    return {'statusCode': 400, 'body': 'Unknown interaction type'}


if __name__ == '__main__':
    import sys
    from pathlib import Path
    # Fixtures use ${VAR} placeholders for installation-specific values
    # (e.g. channel_id) so the handler can stay env-free. Resolved from this
    # file so the fixture is found no matter the working directory.
    default_fixture = (Path(__file__).resolve().parent.parent
                       / 'tests' / 'events' / 'interaction'
                       / 'interaction_sticky_scores.json')
    fixture = sys.argv[1] if len(sys.argv) > 1 else str(default_fixture)
    with open(fixture) as f:
        raw = f.read()
    raw = re.sub(r'\$\{(\w+)\}', lambda m: os.environ[m.group(1)], raw)
    print(lambda_handler(json.loads(raw), None))
