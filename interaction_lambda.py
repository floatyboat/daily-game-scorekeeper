import base64
import json
import os
import random
import traceback
from datetime import datetime
from zoneinfo import ZoneInfo
from nacl.signing import VerifyKey
from nacl.exceptions import BadSignatureError

from game_parser import (
    build_games, compute_puzzle_numbers, format_scoreboard_components,
    make_timestamp_checker, game_sort_key, GAME_SPECS, spec_enabled, STREAK_MIN,
)
from scoreboard import (
    DISCORD_API_BASE, make_session, fetch_messages, reference_date, parse_results,
    build_avatar_pool, safe_guild_id, gather_streaks, is_sticky_message,
    PLAY_BUTTON_CUSTOM_ID, SCORES_BUTTON_CUSTOM_ID,
    TEXT_CHANNEL_TYPES, PERM_ADMINISTRATOR, PERM_MANAGE_GUILD, MAX_BUTTONS_PER_ROW,
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

    Returns (results, puzzle_numbers, today).
    """
    tz = ZoneInfo(cfg['timezone'])
    today = reference_date(datetime.now(tz), tz, cfg['hours_after_midnight'])
    messages = fetch_messages(_session, channel_id, limit=100)
    checker = make_timestamp_checker(today, tz, cfg['hours_after_midnight'],
                                     cfg['time_window_hours'])
    avatar_pool = build_avatar_pool(_session, messages, checker, cfg['wordle_bot_id'])
    results, puzzle_numbers = parse_results(
        messages, today, tz, cfg['hours_after_midnight'], cfg['time_window_hours'],
        wordle_bot_id=cfg['wordle_bot_id'], avatar_hashes=avatar_pool,
        game_overrides=cfg['game_overrides'],
    )
    return results, puzzle_numbers, today


def build_scoreboard_response(channel_id, guild_id=None, cfg=None):
    """Build today's scoreboard as an ephemeral Components V2 reply.

    Streaks ride along when the store is reachable: live views show a streak
    kept alive today as current + 1 (SPEC.md), so the board updates the moment
    someone plays.
    """
    cfg = cfg or guild_cfg(guild_id)
    results, puzzle_numbers, today = fetch_today_results(channel_id, cfg)

    streaks = gather_streaks(guild_id, today, results,
                             [g.key for g in build_games(puzzle_numbers, cfg['game_overrides'])])
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
    """
    today = None
    try:
        results, puzzle_numbers, today = fetch_today_results(channel_id, cfg)
    except Exception:
        # Counts are a nice-to-have; never let a fetch/parse hiccup block the
        # core action. Fall back to today's games with no counts or streaks.
        results, puzzle_numbers = {}, compute_puzzle_numbers(datetime.utcnow())

    games = build_games(puzzle_numbers, cfg['game_overrides'])

    streaks = None
    if today is not None:
        streaks = gather_streaks(guild_id, today, results,
                                 [g.key for g in games], include_players=False)

    if user_id is not None:
        games = [g for g in games if user_id not in results.get(g.key, {})]

    return games, results, streaks


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
    """
    cfg = cfg or guild_cfg(guild_id)
    games, results, streaks = unplayed_games(channel_id, cfg, user_id, guild_id)
    game_streaks = (streaks or {}).get('games', {})

    games.sort(key=lambda g: game_sort_key(g, results, streaks))

    buttons = []
    for g in games:
        count = len(results.get(g.key) or {})
        label = f"{g.emoji} {g.title} ({count})" if count else f"{g.emoji} {g.title}"
        streak = game_streaks.get(g.key, 0)
        if streak >= STREAK_MIN:
            label += f" \U0001F525{streak}"
        buttons.append({"type": 2, "style": 5, "label": label, "url": g.url})

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


# --- /setup and /games (admin configuration) -----------------------------------

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


def handle_games(body, guild_id):
    cfg = guild_cfg(guild_id, strict=True)
    return _ephemeral(
        'Select every game this server should track — unselected games are hidden '
        'from parsing, the scoreboard, and the Play list.',
        components=[games_select_row(cfg['game_overrides'])],
    )


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


def admin_dispatch(fn, body):
    """Shared gate for every config surface: guild-only, Manage Server (or
    Administrator) re-verified server-side, store failures turned into a
    readable ephemeral instead of Discord's opaque 'interaction failed'."""
    guild_id = body.get('guild_id')
    if not guild_id:
        return _ephemeral('Run this in a server — configuration is per-server.')
    if not is_admin(body):
        return _ephemeral('You need **Manage Server** to configure the scoreboard.')
    try:
        return fn(body, guild_id)
    except Exception as e:
        traceback.print_exc()
        return _ephemeral(f'Something went wrong ({type(e).__name__}) — try again shortly.')


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
            guild_id = interaction_guild_id(body)
            return _http(build_play_response(
                body['channel_id'], interaction_user_id(body), guild_id, guild_cfg(guild_id)))
        if command_name == 'setup':
            return _http(admin_dispatch(handle_setup, body))
        if command_name == 'games':
            return _http(admin_dispatch(handle_games, body))

    # MESSAGE_COMPONENT (type 3) — sticky buttons + setup selects
    if body.get('type') == 3:
        custom_id = body.get('data', {}).get('custom_id', '')
        if custom_id == PLAY_BUTTON_CUSTOM_ID:
            guild_id = interaction_guild_id(body)
            return _http(build_play_response(
                body['channel_id'], interaction_user_id(body), guild_id, guild_cfg(guild_id)))
        if custom_id == SCORES_BUTTON_CUSTOM_ID:
            guild_id = interaction_guild_id(body)
            return _http(build_scoreboard_response(
                body['channel_id'], guild_id, guild_cfg(guild_id)))
        if custom_id == GAMES_SELECT_ID or custom_id.startswith(CHANNEL_SELECT_PREFIX):
            return _http(admin_dispatch(handle_setup_component, body))

    return {'statusCode': 400, 'body': 'Unknown interaction type'}


if __name__ == '__main__':
    import sys, re
    # Fixtures use ${VAR} placeholders for installation-specific values
    # (e.g. channel_id) so the handler can stay env-free.
    fixture = sys.argv[1] if len(sys.argv) > 1 else 'test_events/Interaction/interaction_sticky_scores.json'
    with open(fixture) as f:
        raw = f.read()
    raw = re.sub(r'\$\{(\w+)\}', lambda m: os.environ[m.group(1)], raw)
    print(lambda_handler(json.loads(raw), None))
