"""The /setup panel: every per-server setting, one section and one form at a time.

`/setup` answers with a panel listing the server's whole configuration, a
section per group of settings, each with an Edit button that opens that
section's form filled in with what is stored. Submitting the form saves it and
redraws the panel in place. A server with no scores channel yet skips the panel
and is handed the Basics form directly, since nothing else it could set would
post anywhere until that is filled in.

Why a panel of forms rather than one form: Discord caps a form (a modal) at
five fields, won't open one form from another, and there are thirty-odd
settings. Each form is built straight off store.CONFIG_FIELDS -- a field's
`section` puts it on a form, and its type, bounds and choices pick the input --
and read back through the same declarations, so a setting cannot exist on one
side only. check_limits() holds the forms and the panel inside Discord's caps.

Nothing here does I/O: it builds payloads and turns a submission into config
updates. interaction_lambda does the rest (the channel checks, the write, the
sticky cleanup).
"""
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

import commentary
import store
from game_parser import GAME_SPECS, NO_SHOW_SCORE, POOP_MEDAL, SCORING_PLACEMENT, spec_enabled
from scoreboard import MAX_ENABLED_GAMES, MAX_SELECT_OPTIONS, TEXT_CHANNEL_TYPES, reference_date

# Every custom_id /setup puts on a control starts with this, the menus older
# deploys drew included, so a stale one still reaches the handler.
CUSTOM_ID_PREFIX = 'setup_'
EDIT_PREFIX = 'setup_edit:'   # + section key: a panel's Edit button
FORM_PREFIX = 'setup_form:'   # + section key: the form that button opens
TOGGLES = 'toggles'           # custom_id of the checkbox group holding a form's on/off fields

FIELDS = {f.name: f for f in store.CONFIG_FIELDS}

# Discord's caps on what this module builds, held by check_limits().
MAX_FORM_FIELDS = 5      # top-level components in a modal
MAX_TITLE = 45           # modal title
MAX_LABEL = 45           # a form field's label
MAX_HINT = 100           # its description, and an option's label or description
MAX_GROUP_OPTIONS = 10   # options in a checkbox or radio group
MAX_COMPONENTS = 40      # components in a message, nested ones included
MAX_TEXT = 4000          # characters across a message's text displays


class Invalid(ValueError):
    """A submitted value that can't be saved; the message tells the admin why."""


MISSING = object()   # a field the submission didn't carry: leave it as it is


def _sentence(text):
    """`text` with its first letter capitalized and the rest untouched."""
    return text[:1].upper() + text[1:]


# --- Pick lists -----------------------------------------------------------------

@dataclass(frozen=True)
class Catalog:
    """A setting that is a set of picks from a list declared in code -- the
    games, the commentary kinds -- stored as overrides of each entry's coded
    default.

        entries  () -> the entries, in menu order; each has a `key`
        option   entry -> its menu option, minus the tick
        enabled  (entry, overrides) -> whether it is on for the server
        cap      the most picks a server may have, or None
    """
    entries: object
    option: object
    enabled: object
    cap: int = None

    def overrides(self, picked, stored):
        """Picks -> the map to store. An entry the picks switched is recorded
        as chosen; every other entry keeps what was stored, so a choice the
        server made once survives a later save of the same menu. An entry
        never chosen stays absent and keeps following its coded default, which
        is how an entry added later reaches every server with its own."""
        chosen = dict(stored)
        for e in self.entries():
            if (e.key in picked) != self.enabled(e, stored):
                chosen[e.key] = e.key in picked
        return chosen


CATALOGS = {
    # /play is one button per enabled game, five to a row under the Random
    # row, so the cap is what fits there (scoreboard.MAX_ENABLED_GAMES).
    'game_overrides': Catalog(
        entries=lambda: sorted(GAME_SPECS, key=lambda s: s.title.lower()),
        option=lambda s: {'label': s.title, 'value': s.key, 'emoji': {'name': s.emoji}},
        enabled=spec_enabled,
        cap=MAX_ENABLED_GAMES),
    'commentary_overrides': Catalog(
        entries=lambda: commentary.TRIGGERS,
        option=lambda t: {'label': t.label, 'value': t.key, 'description': t.describe[:MAX_HINT]},
        enabled=commentary.trigger_enabled),
}


# --- What the panel says under each heading -------------------------------------

def _channel(channel_id):
    return f'<#{channel_id}>' if channel_id else '*not set*'


def _amount(field, n):
    """A number the way its field counts: '09:00', '3 hours', 'About 200 a day'."""
    if field.unit == 'clock':
        return f'{n:02d}:00'
    if field.unit == 'hundred':
        return f'About {n * 100} a day'
    if field.unit:
        return f"{n} {field.unit}{'' if n == 1 else 's'}"
    return str(n)


def _chosen(name, cfg):
    """The label of a choice field's current value."""
    return next(label for value, label, _ in FIELDS[name].choices if value == cfg[name])


def _switches(section):
    return [f for f in store.section_fields(section) if f.coerce is bool]


def _game_list(specs):
    return ', '.join(f'{s.emoji} {s.title}'
                     for s in sorted(specs, key=lambda s: s.title.lower()))


def _off_in_basics(consequence):
    return f'Off (switched on under Basics), so {consequence}.'


def _summarize_basics(cfg):
    scores, board = cfg['input_channel_id'], cfg['output_channel_id']
    where = (f'Scores and the board: <#{scores}>' if scores and scores == board
             else f'Scores: {_channel(scores)} · board: {_channel(board)}')
    on = [f.label for f in _switches('basics') if cfg[f.name]]
    off = [f.label for f in _switches('basics') if not cfg[f.name]]
    lines = [where,
             f"Timezone `{cfg['timezone']}` · scoring: **{_chosen('scoring', cfg)}**",
             f"On: {', '.join(on)}" if on else 'Every feature is off.']
    if on and off:
        lines.append(f"-# Off: {', '.join(off)}")
    return lines


def _summarize_games(cfg):
    enabled = [s for s in GAME_SPECS if spec_enabled(s, cfg['game_overrides'])]
    off = [s for s in GAME_SPECS if s not in enabled]
    lines = [f'Tracking {len(enabled)}: {_game_list(enabled)}' if enabled
             else '⚠️ Tracking no games, so the board stays empty until some are ticked.']
    if off:
        lines.append(f'-# Off: {_game_list(off)}')
    return lines


def _summarize_schedule(cfg):
    window = _amount(FIELDS['time_window_hours'], cfg['time_window_hours'])
    return [f"Day starts {cfg['hours_after_midnight']:02d}:00 · board posts "
            f"{store.post_hour(cfg):02d}:00 · results count for {window}"]


def _shame_phrase(cfg):
    """The board's no-show lines. Named for the setting, phrased for the board,
    where the line itself reads DNF. Says plainly when the setting is moot: the
    lines only exist on the placement scale, the only one that ranks a player
    who never turned up."""
    if not cfg['daily_shame']:
        return 'no DNF lines'
    if cfg['scoring'] != SCORING_PLACEMENT:
        return f"DNF lines on (unused while scoring is {_chosen('scoring', cfg).lower()})"
    return f'skipped games shown as {POOP_MEDAL} {NO_SHOW_SCORE}'


def _summarize_board(cfg):
    n = cfg['minimum_players']
    need = 'any game with a player is on it' if n <= 1 else f'a game needs {n} players'
    return [f"{_sentence(_shame_phrase(cfg))} · {need}",
            f"Off-rotation games {cfg['rotation_off_mode']} · {cfg['pin_keep_days']} days "
            f"of boards pinned · about {cfg['hundreds_of_messages'] * 100} messages a day"]


def _summarize_rotation(cfg):
    if not cfg['rotation_enabled']:
        return [_off_in_basics('every tracked game scores, every day')]
    shape = (f"{_amount(FIELDS['rotation_count'], cfg['rotation_count'])} a day, "
             f"{_chosen('rotation_mode', cfg).lower()} draw")
    # The thresholds are swap rules, so a random draw isn't told two numbers
    # that are never consulted.
    if cfg['rotation_mode'] == 'swap':
        shape += (f" (a seat is kept at {cfg['rotation_keep_players']}+ players, "
                  f"won at {cfg['rotation_promote_players']}+)")
    lines = [shape + (' · announced' if cfg['rotation_announce'] else ' · drawn silently')]
    tz = ZoneInfo(cfg['timezone'])
    today = store.day_str(reference_date(datetime.now(tz), tz, cfg['hours_after_midnight']))
    by_key = {s.key: s for s in GAME_SPECS}
    todays = [by_key[k] for k in store.current_rotation(cfg, today) or () if k in by_key]
    if todays:
        lines.append(f"-# Today's games: {_game_list(todays)}")
    return lines


def _summarize_sticky(cfg):
    if cfg['sticky_enabled']:
        n = cfg['sticky_games']
        state = (f"On, with {n} game button{'' if n == 1 else 's'}" if n
                 else 'On, with no game buttons')
    else:
        state = _off_in_basics('none of the below happens')
    reactions = 'off'
    if cfg['reactions_enabled']:
        reactions = 'on, rotation games only' if cfg['reactions_rotation_only'] else 'on'
    return [state,
            f"Link previews {'stripped' if cfg['suppress_embeds'] else 'kept'} · "
            f"Wordle recap {'deleted' if cfg['delete_wordle_recap'] else 'kept'} · "
            f"reactions {reactions}"]


def _summarize_commentary(cfg):
    if not cfg['commentary_enabled']:
        return [_off_in_basics('nothing posts between the boards')]
    on = commentary.enabled_triggers(cfg)
    # Only the tuning of the kinds actually posting: with a kind switched off
    # -- the nudge ships that way -- its hours would describe nothing.
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
    lines = [f"Posting {', '.join(t.label for t in on)}" if on
             else '⚠️ Every kind is off, so nothing posts.']
    if tuning:
        lines.append(f"-# {_sentence(', '.join(tuning))}")
    return lines


# --- The panel ------------------------------------------------------------------

@dataclass(frozen=True)
class Section:
    """One part of the panel, and the form that edits it.

        key           ConfigField.section of the fields its form edits
        title         the panel's heading, and the form's title
        emoji         marks the heading
        summary       cfg -> the lines the panel shows under the heading
        toggles       the label over the form's on/off fields, if it has any
        toggles_hint  the line under that label
        form_title    the form's title, where it differs from `title`
    """
    key: str
    title: str
    emoji: str
    summary: object
    toggles: str = 'Options'
    toggles_hint: str = None
    form_title: str = None


BASICS = 'basics'

# Panel order. Basics comes first because it is what makes the bot post; the
# rest tune what Basics has switched on.
SECTIONS = (
    # A form can't carry buttons or links, so the hint is the form's only way
    # to point at the other sections: a new server meets this form before it
    # has ever seen the panel.
    Section(BASICS, 'Basics', '\U0001F4CD', _summarize_basics, toggles='Features',
            toggles_hint="Each one's settings are on the panel that opens after you submit.",
            form_title='Set up the scoreboard'),
    Section('games', 'Games', '\U0001F3AE', _summarize_games),
    Section('schedule', 'Schedule', '\U0001F550', _summarize_schedule),
    Section('board', 'Board', '\U0001F3C6', _summarize_board),
    Section('rotation', 'Rotation', '\U0001F504', _summarize_rotation),
    Section('sticky', 'Sticky', '\U0001F4CC', _summarize_sticky),
    Section('commentary', 'Commentary', '\U0001F4AC', _summarize_commentary),
)
SECTIONS_BY_KEY = {s.key: s for s in SECTIONS}


def go_live_hint(cfg):
    """What a server still has to set before anything posts, or None once
    both channels are."""
    if not cfg['input_channel_id']:
        return '⚠️ Nothing posts yet: pick a scores channel under **Basics**.'
    if not cfg['output_channel_id']:
        return '⚠️ The board has nowhere to post: pick a board channel under **Basics**.'
    return None


def panel(cfg, status=()):
    """The panel's Components V2 body: a heading over `status` (what a save
    just did), then every section with its summary and Edit button -- the
    Basics one highlighted while the server isn't live."""
    hint = go_live_hint(cfg)
    head = ['### ⚙️ Scoreboard setup', *status] + ([hint] if hint else [])
    children = [{'type': 10, 'content': '\n'.join(head)},     # text display
                {'type': 14, 'divider': True, 'spacing': 1}]  # separator
    for s in SECTIONS:
        children.append({
            'type': 9,   # section: text with a button beside it
            'components': [{'type': 10, 'content': '\n'.join(
                [f'**{s.emoji} {s.title}**', *s.summary(cfg)])}],
            'accessory': {'type': 2, 'style': 1 if hint and s.key == BASICS else 2,
                          'label': 'Edit', 'custom_id': EDIT_PREFIX + s.key},
        })
    return [{'type': 17, 'components': children}]   # container


def section_for(custom_id):
    """The section an Edit button or a submitted form belongs to, or None for
    a control this deploy didn't draw."""
    for prefix in (EDIT_PREFIX, FORM_PREFIX):
        if custom_id.startswith(prefix) and custom_id[len(prefix):] in SECTIONS_BY_KEY:
            return custom_id[len(prefix):]
    return None


# --- The forms ------------------------------------------------------------------

def _shown(cfg):
    """`cfg` as the forms show it. post_hour is the one setting whose unset
    value means something ("when the day starts"), so its form shows that hour,
    and a form saved without touching it compares equal and leaves it unset."""
    return {**cfg, 'post_hour': store.post_hour(cfg)}


def _label(text, hint, component):
    """One form field: Discord's Label around a single input."""
    label = {'type': 18, 'label': text, 'component': component}
    if hint:
        label['description'] = hint
    return label


def _has_menu(field):
    """A whole number bounded tightly enough to offer every value in one menu."""
    return (field.coerce is int and field.minimum is not None and field.maximum is not None
            and field.maximum - field.minimum < MAX_SELECT_OPTIONS)


def _channel_input(field, cfg):
    scores = field.name == 'input_channel_id'
    current = cfg[field.name]
    if not scores and current == cfg['input_channel_id']:
        current = None   # blank is how this form says "the scores channel"
    select = {'type': 8, 'custom_id': field.name,   # channel select
              'channel_types': list(TEXT_CHANNEL_TYPES),
              'min_values': 1 if scores else 0, 'max_values': 1, 'required': scores}
    if current:
        select['default_values'] = [{'id': str(current), 'type': 'channel'}]
    return [_label(field.label, field.describe, select)]


def _catalog_inputs(field, cfg):
    """One multi-select per MAX_SELECT_OPTIONS entries. A lone menu holds a
    capped catalog to its cap by itself; menus split across a form can't see
    each other's ticks, so _read_catalog re-checks the cap on every submit."""
    catalog = CATALOGS[field.name]
    entries = list(catalog.entries())
    chunks = [entries[i:i + MAX_SELECT_OPTIONS]
              for i in range(0, len(entries), MAX_SELECT_OPTIONS)]
    inputs = []
    for i, chunk in enumerate(chunks):
        options = [{**catalog.option(e), 'default': catalog.enabled(e, cfg[field.name])}
                   for e in chunk]
        most = len(options)
        if catalog.cap and len(chunks) == 1:
            # The picker stops taking ticks at max_values, which is where the
            # cap is felt. It widens past the cap only for a menu that already
            # shows more ticked -- a default-on entry shipped into a server at
            # the cap -- rather than pre-ticking more than it allows.
            most = min(most, max(catalog.cap, sum(o['default'] for o in options)))
        select = {'type': 3, 'custom_id': f'{field.name}:{i}', 'options': options,   # string select
                  'min_values': 0, 'max_values': most, 'required': False}
        if catalog.cap:
            select['placeholder'] = f'Up to {catalog.cap}'
        text = field.label if len(chunks) == 1 else f'{field.label} ({i + 1} of {len(chunks)})'
        inputs.append(_label(text, field.describe, select))
    return inputs


def _choice_input(field, current):
    options = [{'value': value, 'label': label, 'description': hint, 'default': value == current}
               for value, label, hint in field.choices]
    return [_label(field.label, field.describe, {
        'type': 21, 'custom_id': field.name, 'options': options, 'required': True})]   # radio group


def _menu_input(field, current):
    options = [{'label': _amount(field, n), 'value': str(n), 'default': n == current}
               for n in range(field.minimum, field.maximum + 1)]
    return [_label(field.label, field.describe, {
        'type': 3, 'custom_id': field.name, 'options': options,   # string select
        'min_values': 1, 'max_values': 1, 'required': True})]


def _text_input(field, current):
    longest = len(str(field.maximum)) if field.maximum is not None else 4
    return [_label(field.label, field.describe, {
        'type': 4, 'custom_id': field.name, 'style': 1, 'required': True,   # text input
        'value': str(current), 'placeholder': str(field.default),
        'max_length': longest if field.coerce is int else 64})]


def _inputs(field, cfg):
    """The Label(s) one field that isn't on/off takes up on its form."""
    if field.name in store.CHANNEL_FIELDS:
        return _channel_input(field, cfg)
    if field.name in CATALOGS:
        return _catalog_inputs(field, cfg)
    current = _shown(cfg)[field.name]
    if field.choices:
        return _choice_input(field, current)
    if _has_menu(field):
        return _menu_input(field, current)
    return _text_input(field, current)


def _toggles(section, fields, cfg):
    """A form's on/off fields, together as one checkbox group."""
    options = [{'value': f.name, 'label': f.label, 'description': f.describe,
                'default': bool(cfg[f.name])} for f in fields]
    return _label(section.toggles, section.toggles_hint, {
        'type': 22, 'custom_id': TOGGLES, 'options': options,   # checkbox group
        'min_values': 0, 'max_values': len(options), 'required': False})


def form_fields(key, cfg):
    """The section's form, field by field in declaration order. The on/off
    fields share one checkbox group, placed where the first of them is
    declared."""
    section = SECTIONS_BY_KEY[key]
    fields = store.section_fields(key)
    switches = [f for f in fields if f.coerce is bool]
    components = []
    for f in fields:
        if f.coerce is not bool:
            components += _inputs(f, cfg)
        elif f is switches[0]:
            components.append(_toggles(section, switches, cfg))
    return components


def form(key, cfg):
    """MODAL (interaction response type 9): the section's form, filled in
    with what the server has now."""
    section = SECTIONS_BY_KEY[key]
    return {'type': 9, 'data': {'custom_id': FORM_PREFIX + key,
                                'title': section.form_title or section.title,
                                'components': form_fields(key, cfg)}}


# --- Reading a submitted form ---------------------------------------------------

def _read_catalog(field, values, cfg):
    catalog = CATALOGS[field.name]
    menus = [v for k, v in values.items() if k.startswith(f'{field.name}:')]
    if not menus:
        return MISSING
    picked = set().union(*menus)
    if catalog.cap and len(picked) > catalog.cap:
        raise Invalid(f'**{field.label}** takes up to {catalog.cap}, and that was '
                      f'{len(picked)}: untick {len(picked) - catalog.cap} and save again.')
    return catalog.overrides(picked, cfg[field.name])


def _quoted(typed):
    """What an admin typed, as inline code that a backtick in it can't close."""
    return '`{}`'.format(typed.replace('`', "'"))


def _timezone(name):
    try:
        ZoneInfo(name)
    except Exception:
        raise Invalid(f"{_quoted(name)} isn't a timezone: use an IANA name like "
                      '`America/New_York` or `Europe/London`.') from None
    return name


def _read(field, values, cfg):
    """One field's submitted value as it would be stored, or MISSING when the
    submission doesn't carry it. A channel left blank reads as None."""
    if field.coerce is bool:
        ticked = values.get(TOGGLES)
        return MISSING if ticked is None else field.name in ticked
    if field.name in CATALOGS:
        return _read_catalog(field, values, cfg)
    raw = values.get(field.name)
    if raw is None:
        return MISSING
    if field.name in store.CHANNEL_FIELDS:
        return str(raw[0]) if raw else None
    if field.choices:
        return field.apply(raw)
    if _has_menu(field):
        return field.apply(int(raw[0])) if raw else MISSING
    if field.coerce is int:
        try:
            return field.apply(int(raw))
        except ValueError:
            raise Invalid(f'**{field.label}** takes a whole number, not '
                          f'{_quoted(raw)}.') from None
    if field.name == 'timezone':
        return _timezone(raw)
    return raw


def read_form(key, values, cfg):
    """A submitted form -> (updates, problems).

    `values` is the submission by custom_id (interaction_lambda.modal_values).
    `updates` holds only what changed: a field submitted as it was shown is
    left alone, so a setting the server never chose keeps following its
    default. `problems` are the reasons a value wasn't taken, worded for the
    admin; the rest of the form saves regardless.
    """
    read, problems = {}, []
    for field in store.section_fields(key):
        try:
            value = _read(field, values, cfg)
        except Invalid as e:
            problems.append(str(e))
            continue
        if value is not MISSING:
            read[field.name] = value
    if 'output_channel_id' in read and read['output_channel_id'] is None:
        read['output_channel_id'] = read.get('input_channel_id') or cfg['input_channel_id']

    shown = _shown(cfg)
    updates = {k: v for k, v in read.items() if v is not None and v != shown[k]}
    merged = {**cfg, **updates}
    if (updates.keys() & {'hours_after_midnight', 'post_hour'}
            and store.post_hour(merged) < merged['hours_after_midnight']):
        problems.append("**Board posts at** can't be earlier than **Day starts at**: the day "
                        'has to close before its board posts. The schedule was left as it was.')
        updates.pop('hours_after_midnight', None)
        updates.pop('post_hour', None)
    return updates, problems


# The settings that only act on the sticky's pass over each new result.
_STICKY_PASS = ('suppress_embeds', 'delete_wordle_recap', 'reactions_enabled')


def status_lines(cfg, updates, problems):
    """What the panel says over its sections after a save: what was refused,
    then what saved, with anything about it the summaries can't show."""
    lines = [f'⚠️ {p}' for p in problems]
    if not updates:
        return lines or ['Nothing changed.']
    after = {**cfg, **updates}
    notes = []
    if 'scoring' in updates:
        notes.append('The new scale applies from the next board; days already scored '
                     'keep their points.')
    if updates.get('rotation_enabled'):
        notes.append('The first draw is announced with the next daily board.'
                     if after['rotation_announce'] else
                     "Today's games are drawn silently, with no announcement post.")
    if updates.get('daily_enabled') is False:
        notes.append('No daily posts while the board is off, and the sticky drops its '
                     'Yesterday link.')
    if updates.get('reactions_enabled'):
        notes.append('Reactions need **Add Reactions** in the scores channel. Most servers '
                     "give it to everyone; where yours doesn't, grant it to the bot's role.")
    if (not after['sticky_enabled'] and updates.keys() & {'sticky_enabled', *_STICKY_PASS}
            and any(after[k] for k in _STICKY_PASS)):
        notes.append('Link stripping, the recap cleanup and reactions all happen on the '
                     "sticky's pass, so they wait until the sticky is back on.")
    saved = '✅ Saved everything else.' if problems else '✅ Saved.'
    return lines + [saved] + [f'-# {n}' for n in notes]


# --- Holding it all inside Discord's caps ---------------------------------------

def _walk(components):
    for c in components:
        yield c
        yield from _walk(c.get('components', ()))
        if 'component' in c:
            yield c['component']
        if 'accessory' in c:
            yield c['accessory']


def check_limits(cfg=None):
    """Every form and the panel against Discord's caps, built from `cfg`
    (default: a fresh server's). Discord refuses a payload over them whole --
    an admin clicking Edit gets "interaction failed" -- so register_commands
    runs this before registering anything. Returns the problems found."""
    cfg = cfg or store.default_config()
    problems = [f'{f.name}: section {f.section!r} has no Section' for f in store.CONFIG_FIELDS
                if f.section and f.section not in SECTIONS_BY_KEY]
    for s in SECTIONS:
        if len(s.form_title or s.title) > MAX_TITLE:
            problems.append(f'{s.key}: form title over {MAX_TITLE} chars')
        fields = form_fields(s.key, cfg)
        if not 1 <= len(fields) <= MAX_FORM_FIELDS:
            problems.append(f'{s.key}: {len(fields)} form fields, Discord takes '
                            f'1-{MAX_FORM_FIELDS}')
        for label in fields:
            name, inner = label['label'], label['component']
            if len(name) > MAX_LABEL or len(label.get('description', '')) > MAX_HINT:
                problems.append(f'{s.key}: {name!r} has a label or hint over the cap')
            options = inner.get('options', ())
            if inner['type'] in (21, 22) and len(options) > MAX_GROUP_OPTIONS:
                problems.append(f'{s.key}: {name!r} has {len(options)} options, Discord '
                                f'takes {MAX_GROUP_OPTIONS}')
            if any(len(o['label']) > MAX_HINT or len(o.get('description', '')) > MAX_HINT
                   for o in options):
                problems.append(f'{s.key}: {name!r} has an option over {MAX_HINT} chars')
    # With a long stand-in for the status lines a save puts over the sections.
    shown = list(_walk(panel(cfg, ['x' * 300])))
    if len(shown) > MAX_COMPONENTS:
        problems.append(f'panel: {len(shown)} components, Discord takes {MAX_COMPONENTS}')
    if sum(len(c.get('content', '')) for c in shown) > MAX_TEXT:
        problems.append(f'panel: text over {MAX_TEXT} chars')
    return problems
