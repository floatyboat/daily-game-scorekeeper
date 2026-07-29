import base64
import json
import os
import random
import re
import traceback
from datetime import datetime
from zoneinfo import ZoneInfo
from nacl.signing import VerifyKey
from nacl.exceptions import BadSignatureError

from game_parser import (
    build_games, compute_puzzle_numbers, format_scoreboard_components,
    make_timestamp_checker, game_sort_key, match_suggestion, GAME_SPECS,
    spec_enabled, STREAK_MIN,
)
from scoreboard import (
    DISCORD_API_BASE, make_session, fetch_messages, reference_date, parse_results,
    build_avatar_pool, safe_guild_id, gather_streaks, is_sticky_message,
    find_launch_url,
    PLAY_BUTTON_CUSTOM_ID, SCORES_BUTTON_CUSTOM_ID,
    TEXT_CHANNEL_TYPES, PERM_ADMINISTRATOR, PERM_MANAGE_GUILD, MAX_BUTTONS_PER_ROW,
    MAX_MESSAGE_LENGTH,
)
import store

# Global bot identity only -- per-server settings live in the guild's config
# item (store.CONFIG_FIELDS) and are managed by the /setup command below.
DISCORD_PUBLIC_KEY = os.getenv('DISCORD_PUBLIC_KEY', '')
DISCORD_BOT_TOKEN = os.getenv('DISCORD_BOT_TOKEN')
DISCORD_BOT_ID = os.getenv('DISCORD_BOT_ID') or 0

_session = make_session(DISCORD_BOT_TOKEN)

GAMES_SELECT_ID = 'setup_games'
CHANNEL_SELECT_PREFIX = 'setup_channel:'
# The two channel settings, as their config field and the phrase that explains
# them -- kept together so a third channel is one entry, not two edits.
CHANNEL_FIELDS = {'input': 'input_channel_id', 'output': 'output_channel_id'}
CHANNEL_BLURBS = {
    'input': 'where scores are read and the sticky lives',
    'output': 'where the daily scoreboard posts',
}


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

    Returns (results, puzzle_numbers, today, messages) -- the raw page comes
    back too so the Play list can find a game app's launch button in it without
    paying for a second fetch.
    """
    tz = ZoneInfo(cfg['timezone'])
    today = reference_date(datetime.now(tz), tz, cfg['hours_after_midnight'])
    messages = fetch_messages(_session, channel_id, limit=100)
    checker = make_timestamp_checker(today, tz, cfg['hours_after_midnight'],
                                     cfg['time_window_hours'])
    avatar_pool = build_avatar_pool(_session, messages, checker, cfg['wordle_bot_id'],
                                    cfg['guild_id'])
    results, puzzle_numbers = parse_results(
        messages, today, tz, cfg['hours_after_midnight'], cfg['time_window_hours'],
        wordle_bot_id=cfg['wordle_bot_id'], avatar_hashes=avatar_pool,
        game_overrides=cfg['game_overrides'],
    )
    return results, puzzle_numbers, today, messages


def build_scoreboard_response(channel_id, guild_id=None, cfg=None):
    """Build today's scoreboard as an ephemeral Components V2 reply.

    Streaks ride along when the store is reachable: live views show a streak
    kept alive today as current + 1 (SPEC.md), so the board updates the moment
    someone plays.
    """
    cfg = cfg or guild_cfg(guild_id)
    results, puzzle_numbers, today, _ = fetch_today_results(channel_id, cfg)

    streaks = gather_streaks(guild_id, today, results,
                             build_games(puzzle_numbers, cfg['game_overrides']),
                             cfg['minimum_players'])
    components = format_scoreboard_components(
        results, today, puzzle_numbers,
        title="Today's Scores", minimum_players=cfg['minimum_players'], streaks=streaks,
        game_overrides=cfg['game_overrides'],
    )

    # 64 (EPHEMERAL) | 1<<15 (IS_COMPONENTS_V2). V2 messages can't have a
    # content field, so the builder's output goes directly into components.
    return {
        "type": 4,
        "data": {
            "flags": 32832,
            "components": components,
        },
    }


def interaction_user_id(body):
    """ID of the user who triggered an interaction.

    Discord nests the acting user under `member.user` for guild interactions and
    promotes it to a top-level `user` in DMs, so check both. Returns None when
    neither is present (e.g. a bare test fixture), which callers treat as
    "unknown user" and fall back to listing every game.
    """
    member = body.get('member') or {}
    return (member.get('user') or body.get('user') or {}).get('id')


def interaction_guild_id(body):
    """guild_id of the interaction, for config and streak lookups.

    Guild interactions carry it directly; local test fixtures (and DMs) don't,
    so fall back to resolving the channel via the API (cached per process).
    None -- a channel with no guild -- just renders with default settings.
    """
    return body.get('guild_id') or safe_guild_id(_session, body.get('channel_id'))


def unplayed_games(channel_id, cfg, user_id=None, guild_id=None):
    """Today's tracked games the presser hasn't logged yet, plus the live
    results and streak bundle backing them.

    Shared by the Play and Random buttons so both work off the same live view
    of the channel. When user_id is known, games that user has already logged
    today are dropped, making the result personal to whoever pressed; with no
    user_id (an unidentifiable presser) every game is returned. results and
    the gather_streaks() bundle (or None) cover ALL games, so counts and
    streak numbers reflect the whole server, not just the presser's remainder.
    The fetched page rides along for the caller's own use of the same messages.
    """
    today = None
    messages = []
    try:
        results, puzzle_numbers, today, messages = fetch_today_results(channel_id, cfg)
    except Exception:
        # Counts are a nice-to-have; never let a fetch/parse hiccup block the
        # core action. Fall back to today's games with no counts or streaks.
        results, puzzle_numbers = {}, compute_puzzle_numbers(datetime.utcnow())

    games = build_games(puzzle_numbers, cfg['game_overrides'])

    streaks = None
    if today is not None:
        streaks = gather_streaks(guild_id, today, results, games,
                                 cfg['minimum_players'], include_players=False)

    if user_id is not None:
        games = [g for g in games if user_id not in results.get(g.key, {})]

    return games, results, streaks, messages


ALL_PLAYED_MESSAGE = "\U0001F389 You've played every tracked game today!"


def build_play_response(channel_id, user_id=None, guild_id=None, cfg=None):
    """Build an ephemeral message with link buttons for tracked games.

    When user_id is known, only games that user hasn't logged today are shown,
    so the Play list is personal to whoever pressed the button. Buttons follow
    the app-wide game ordering (game_sort_key, same as scoreboard sections):
    today's live count, then active server streak, then all-time distinct
    players, then title. Labels get a "(count)" suffix once someone has played
    today and a fire-streak suffix while the game's server streak is alive.
    With no user_id (an unidentifiable presser) every game is listed; with no
    reachable store the order falls back to live count then title.

    A game whose official Discord app this guild has configured points at that
    app's own launch button in the channel instead of its website, so it opens
    inside Discord rather than in a browser.
    """
    cfg = cfg or guild_cfg(guild_id)
    games, results, streaks, messages = unplayed_games(channel_id, cfg, user_id, guild_id)
    game_streaks = (streaks or {}).get('games', {})
    apps = store.installed_app_ids(cfg)
    guild = cfg['guild_id'] or guild_id

    games.sort(key=lambda g: game_sort_key(g, results, streaks))

    def play_url(g):
        """Where this game's button sends the presser: the Discord app's live
        launch button when the guild has that app and it has posted one, the
        game's website otherwise (no app, or nothing to jump to yet)."""
        if g.discord_app in apps:
            return find_launch_url(messages, g.discord_app, guild, channel_id) or g.url
        return g.url

    buttons, urls = [], {}
    for g in games:
        count = len(results.get(g.key) or {})
        label = f"{g.emoji} {g.title} ({count})" if count else f"{g.emoji} {g.title}"
        streak = game_streaks.get(g.key, 0)
        if streak >= STREAK_MIN:
            label += f" \U0001F525{streak}"
        urls[g.key] = play_url(g)
        buttons.append({"type": 2, "style": 5, "label": label, "url": urls[g.key]})

    action_rows = []
    for i in range(0, len(buttons), MAX_BUTTONS_PER_ROW):
        action_rows.append({"type": 1, "components": buttons[i:i + MAX_BUTTONS_PER_ROW]})

    # A "surprise me" shortcut: one random unplayed game as its own grey link
    # button on its own row above the list. Resolved here, at click time, so the
    # link points straight at a game this user hasn't logged — one tap, no
    # follow-up. It reuses the same resolved URL as the game's own button, so a
    # random Wordle lands on the Discord app just like the deliberate pick does.
    if games:
        pick = random.choice(games)
        action_rows.insert(0, {"type": 1, "components": [
            {"type": 2, "style": 5, "label": "\U0001F52E Random", "url": urls[pick.key]},
        ]})

    # Filtering can empty the list once a user has logged everything today.
    content = "Pick a game to play!" if action_rows else ALL_PLAYED_MESSAGE

    return {
        "type": 4,
        "data": {
            "flags": 64,
            "content": content,
            "components": action_rows,
        },
    }


# --- Deferred replies for the live views ---------------------------------------
# /play and the sticky's two buttons all read a page of channel history and the
# streak store before they can answer. Warm that is ~300ms, with room to spare
# inside Discord's 3-second ACK deadline -- but these surfaces are used a couple
# of dozen times a day, so the container is rarely still warm and roughly half of
# all clicks pay a cold start. Cold, the same work has measured 3.7s end to end,
# and Discord renders anything past 3s as "This interaction failed".
#
# So ACK first and do the work in a second, asynchronous invocation of this same
# function, which answers by editing the placeholder. That takes the deadline off
# the work entirely: the ACK is a bare type-5 with no I/O behind it, and the
# follow-up has the interaction token's full 15 minutes.

ACTION_PLAY, ACTION_SCORES = 'play', 'scores'

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


def build_live_response(action, channel_id, user_id=None, guild_id=None, cfg=None):
    """The reply for one live view, as a complete interaction response.

    Single entry point for both phases: phase two PATCHes its ['data'] over the
    placeholder, and the inline fallback returns it whole.
    """
    cfg = cfg or guild_cfg(guild_id)
    if action == ACTION_SCORES:
        return build_scoreboard_response(channel_id, guild_id, cfg)
    return build_play_response(channel_id, user_id, guild_id, cfg)


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
        return {'type': 5, 'data': {'flags': 64}}
    guild_id = interaction_guild_id(body)
    return build_live_response(action, body['channel_id'], interaction_user_id(body),
                               guild_id, guild_cfg(guild_id))


def run_deferred(work):
    """Phase two: build the real reply and edit it over the placeholder."""
    channel_id = work['channel_id']
    guild_id = work.get('guild_id') or safe_guild_id(_session, channel_id)
    try:
        data = build_live_response(work['action'], channel_id,
                                   work.get('user_id'), guild_id)['data']
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
    return {'statusCode': 200,
            'body': json.dumps({'deferred': work['action'], 'edit': r.status_code})}


# --- /setup (admin configuration) ----------------------------------------------

def _ephemeral(content, components=None):
    """CHANNEL_MESSAGE_WITH_SOURCE, visible only to the invoker."""
    data = {'flags': 64, 'content': content}
    if components is not None:
        data['components'] = components
    return {'type': 4, 'data': data}


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


def channel_select_row(kind):
    return {'type': 1, 'components': [{
        'type': 8,   # channel select
        'custom_id': f'{CHANNEL_SELECT_PREFIX}{kind}',
        'channel_types': list(TEXT_CHANNEL_TYPES),
        'placeholder': f'Select the {kind} channel',
    }]}


def channel_picker_response(kind):
    return _ephemeral(
        f'Pick the **{kind}** channel — {CHANNEL_BLURBS[kind]}.\n'
        f'-# Channel not listed? Run `/setup {kind} channel_id:<id>` with the raw ID instead.',
        components=[channel_select_row(kind)],
    )


def set_channel(guild_id, kind, channel_id, cfg=None):
    """Validate and store a channel choice. Returns (user-facing text, error)."""
    ch, err = resolve_channel(channel_id, guild_id)
    if err:
        return None, err
    field = CHANNEL_FIELDS[kind]
    cfg = cfg or guild_cfg(guild_id, strict=True)
    store.update_config(guild_id, {field: str(channel_id)})
    text = f'✅ {kind.capitalize()} channel set to <#{channel_id}> — {CHANNEL_BLURBS[kind]}.'
    # Apply the write to the config we already hold rather than re-reading it:
    # get_item is eventually consistent, so a read this soon after the write can
    # still miss it and tell the admin to set the channel they just set.
    cfg = {**cfg, field: str(channel_id)}
    missing = [k for k, f in CHANNEL_FIELDS.items() if not cfg[f]]
    if missing:
        text += f'\n-# Still needed to go live: `/setup {missing[0]}`.'
    return text, None


def games_select_row(game_overrides):
    # One option per GameSpec, 17 of scoreboard.MAX_SELECT_OPTIONS today; see
    # the split-across-two-messages note on that constant for when it runs out.
    options = [{
        'label': spec.title,
        'value': spec.key,
        'emoji': {'name': spec.emoji},
        'default': spec_enabled(spec, game_overrides),
    } for spec in sorted(GAME_SPECS, key=lambda s: s.title.lower())]
    return {'type': 1, 'components': [{
        'type': 3,   # string select
        'custom_id': GAMES_SELECT_ID,
        'options': options,
        'min_values': 0,
        'max_values': len(options),
        'placeholder': 'Choose the games to track',
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


def config_summary(cfg):
    def ch(v):
        return f'<#{v}>' if v else '*not set*'

    def onoff(v):
        return 'on' if v else 'off'

    post_hour = store.post_hour(cfg)
    lines = [
        '### ⚙️ Scoreboard setup',
        f"Input channel ({CHANNEL_BLURBS['input']}): {ch(cfg['input_channel_id'])}",
        f"Output channel ({CHANNEL_BLURBS['output']}): {ch(cfg['output_channel_id'])}",
        f"Daily scoreboard: **{onoff(cfg['daily_enabled'])}** · Sticky: **{onoff(cfg['sticky_enabled'])}**",
        f"Timezone `{cfg['timezone']}` · day starts {cfg['hours_after_midnight']:02d}:00 · "
        f"posts {post_hour:02d}:00 · window {cfg['time_window_hours']}h",
        f"Minimum players {cfg['minimum_players']} · volume ~{cfg['hundreds_of_messages'] * 100} msgs/day"
        + (f" · Wordle bot <@{cfg['wordle_bot_id']}>" if cfg['wordle_bot_id'] else ''),
    ]
    enabled = [s for s in GAME_SPECS if spec_enabled(s, cfg['game_overrides'])]
    disabled = [s for s in GAME_SPECS if not spec_enabled(s, cfg['game_overrides'])]
    lines.append(f'Tracking {len(enabled)} games: {_game_list(enabled)}'
                 if enabled else '⚠️ Tracking no games!')
    if disabled:
        lines.append(f'-# Off: {_game_list(disabled)}')
    if not cfg['input_channel_id'] or not cfg['output_channel_id']:
        lines.append('-# Set both channels (`/setup input`, `/setup output`) to go live.')
    return '\n'.join(lines)


def collect_updates(group, args):
    """Slash-command options for one /setup subcommand -> config updates.

    Driven entirely by store.CONFIG_FIELDS, the same table register_commands.py
    registers the options from, so an option name cannot exist on one side only
    -- the old hand-written mapping silently ignored anything that drifted.
    Absent options are left out, so a subcommand only writes what was passed.
    """
    updates = {}
    for field in store.setup_options(group):
        value = args.get(field.option_name)
        if value is not None:
            updates[field.name] = field.coerce(value) if field.coerce else value
    return updates


def handle_setup(body, guild_id):
    sub, args = _sub_options(body)
    cfg = guild_cfg(guild_id, strict=True)

    if sub in CHANNEL_FIELDS:
        cid = args.get('channel') or (str(args.get('channel_id') or '').strip() or None)
        if cid is None:
            return channel_picker_response(sub)
        text, err = set_channel(guild_id, sub, cid, cfg)
        return _ephemeral(err or text)

    if sub == 'daily':
        enabled = bool(args.get('enabled'))
        store.update_config(guild_id, {'daily_enabled': enabled})
        if enabled:
            return _ephemeral('▶️ Daily scoreboard resumed — posts at its scheduled hour.')
        return _ephemeral('⏸️ Daily scoreboard paused — no daily posts, '
                          'and the sticky drops its Yesterday link.')

    if sub == 'sticky':
        enabled = bool(args.get('enabled'))
        store.update_config(guild_id, {'sticky_enabled': enabled})
        if enabled:
            return _ephemeral('▶️ Sticky enabled — it will appear in the input '
                              'channel within a minute.')
        note = ''
        if cfg['input_channel_id']:
            try:
                removed = delete_stickies(cfg['input_channel_id'])
                if removed:
                    note = f' (removed {removed} existing)'
            except Exception:
                pass
        return _ephemeral(f'⏸️ Sticky disabled{note}.')

    if sub == 'games':
        return _ephemeral(
            'Select every game this server should track — unselected games are hidden '
            'from parsing, the scoreboard, and the Play list.',
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
                          f"volume ~{merged['hundreds_of_messages'] * 100} msgs/day"
                          + (f", Wordle bot <@{merged['wordle_bot_id']}>"
                             if merged['wordle_bot_id'] else ''))

    # 'show' and anything unrecognized fall back to the summary.
    return _ephemeral(config_summary(cfg))


def handle_setup_component(body, guild_id):
    custom_id = body['data']['custom_id']
    values = body['data'].get('values') or []

    if custom_id == GAMES_SELECT_ID:
        return _update(apply_games_selection(guild_id, values))

    if custom_id.startswith(CHANNEL_SELECT_PREFIX):
        kind = custom_id[len(CHANNEL_SELECT_PREFIX):]
        if kind not in CHANNEL_FIELDS or not values:
            return _update('Nothing selected — run the command again.')
        text, err = set_channel(guild_id, kind, values[0])
        if err:
            # Keep the picker so the admin can retry after fixing permissions.
            return _update(err, components=[channel_select_row(kind)])
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

    # Logged before it is sent, so a suggestion outlives a failed post.
    print(f'suggest: {name!r} url={url!r} guild={body.get("guild_id")} '
          f'user={interaction_user_id(body)}')
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
        except (BadSignatureError, ValueError, Exception):
            return {'statusCode': 401, 'body': 'Invalid request signature'}
        body = json.loads(raw_body)

    # PING (type 1) — Discord endpoint validation
    if body.get('type') == 1:
        return _http({'type': 1})

    # APPLICATION_COMMAND (type 2)
    if body.get('type') == 2:
        command_name = body.get('data', {}).get('name', '')
        if command_name == 'play':
            return _http(defer(ACTION_PLAY, body))
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
        if custom_id == GAMES_SELECT_ID or custom_id.startswith(CHANNEL_SELECT_PREFIX):
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
