"""Register the bot's slash commands with Discord (bulk overwrite, safe to re-run).

    dotenv run -- python3 tools/register_commands.py

Re-run whenever a command or option changes. The `time` and `limits` options are
generated from store.CONFIG_FIELDS, the same table interaction_lambda reads them
back through, so no option name is written out twice. /setup defaults to Manage
Server via default_member_permissions; the interaction handler re-verifies the
permission bit regardless of how a server re-maps the command.
"""

import os
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

# The lambda modules live in src/ and ship flat in the deploy zip; put that
# directory on the path so this tool runs against the same code as production.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))

# Local imports after load_dotenv(): store reads TABLE_NAME at import time.
from scoreboard import PERM_MANAGE_GUILD, TEXT_CHANNEL_TYPES
import store
from store import (
    OPT_SUB_COMMAND, OPT_STRING, OPT_BOOLEAN, OPT_CHANNEL, CHANNEL_SUBS,
    setup_options,
)

APPLICATION_ID = os.getenv('DISCORD_APPLICATION_ID') or os.getenv('DISCORD_BOT_ID')
BOT_TOKEN = os.getenv('DISCORD_BOT_TOKEN')
API_BASE = 'https://discord.com/api/v10'

CHAT_INPUT = 1                          # command type (not an option type)
MANAGE_GUILD = str(PERM_MANAGE_GUILD)   # default_member_permissions is a decimal string


def field_option(field):
    """One slash-command option, straight off its ConfigField declaration."""
    option = {'type': field.opt_type, 'name': field.option_name,
              'description': field.describe, 'required': False}
    if field.minimum is not None:
        option['min_value'] = field.minimum
    if field.maximum is not None:
        option['max_value'] = field.maximum
    if field.choices:
        option['choices'] = [{'name': n, 'value': v} for n, v in field.choices]
    return option


def field_sub(group, description):
    """A /setup subcommand exposing every config field declared in that group."""
    return {'type': OPT_SUB_COMMAND, 'name': group, 'description': description,
            'options': [field_option(f) for f in setup_options(group)]}


def channel_sub(name):
    """A channel-setting subcommand, straight off its store.ChannelSub: native
    picker option, raw-ID escape hatch, or no args at all (the handler then
    replies with a select menu)."""
    sub = store.channel_sub(name)
    return {'type': OPT_SUB_COMMAND, 'name': sub.name, 'description': sub.describe,
            'options': [
                {'type': OPT_CHANNEL, 'name': 'channel', 'description': 'Pick the channel',
                 'channel_types': list(TEXT_CHANNEL_TYPES), 'required': False},
                {'type': OPT_STRING, 'name': 'channel_id',
                 'description': "Channel ID, for channels the picker can't show",
                 'required': False},
            ]}


def toggle_sub(name, description, prompt, option='enabled', group=None):
    """An on/off subcommand with a single required boolean.

    `group` appends every config field declared in that group as an optional
    option, the same way field_sub builds a whole subcommand from one — for
    settings that only shape a feature that is already on (see sticky_games).
    """
    options = [{'type': OPT_BOOLEAN, 'name': option, 'description': prompt,
                'required': True}]
    if group:
        options += [field_option(f) for f in setup_options(group)]
    return {'type': OPT_SUB_COMMAND, 'name': name, 'description': description,
            'options': options}


COMMANDS = [
    {
        'name': 'play',
        'description': 'Play daily games',
        'type': CHAT_INPUT,
        'options': [
            {'type': OPT_BOOLEAN, 'name': 'all',
             'description': "List every tracked game, not just today's rotation",
             'required': False},
        ],
    },
    {
        # No permission gate and no options: anyone can suggest, and the reply is
        # a modal (interaction_lambda.suggest_modal) because the payload is a
        # multi-line paste that command options can't carry.
        'name': 'suggest',
        'description': 'Suggest a daily game to add — paste one of your results',
        'type': CHAT_INPUT,
    },
    {
        'name': 'setup',
        'description': 'Configure the daily game scoreboard for this server',
        'type': CHAT_INPUT,
        'default_member_permissions': MANAGE_GUILD,
        'dm_permission': False,
        # Display order, and nothing else: Discord renders subcommands in the
        # order this array carries them, and dispatch is by name. Roughly: what
        # is this set to, how do I set it up, tune it, switch parts off -- with
        # the one-sided channel overrides last, since a server that needs them
        # already knows they exist.
        'options': [
            {'type': OPT_SUB_COMMAND, 'name': 'show',
             'description': 'Show the current configuration'},
            channel_sub('channel'),
            field_sub('time', 'Timezone and daily schedule'),
            {'type': OPT_SUB_COMMAND, 'name': 'games',
                         'description': 'Choose which games are tracked in this server'},
            field_sub('limits', 'Display minimum, message volume, and the Wordle bot'),
            toggle_sub('daily', 'Turn the daily scoreboard post on or off',
                       'Post the daily scoreboard?'),
            toggle_sub('sticky', 'Turn the Now Playing sticky on or off',
                       'Keep a sticky pinned to the bottom of the input channel?',
                       group='sticky'),
            toggle_sub('rotation', 'Score a rotating subset of games each day',
                       'Rotate which games are scored each day?', group='rotation'),
            toggle_sub('embeds', 'Strip link previews off posted game results',
                       'Suppress link previews on game results?', option='suppress'),
            channel_sub('input'),
            channel_sub('output'),
        ],
    },
]


def check_channel_coverage():
    """Every declared channel subcommand has to appear in COMMANDS.

    The subcommands above are spelled out one per line so the display order is
    editable, which costs the guarantee that spreading CHANNEL_SUBS gave for
    free: a newly declared channel would otherwise be handled by
    interaction_lambda and never registered, so nobody could reach it. Buy the
    guarantee back here instead of hoping the two lists stay in step.
    """
    registered = {o['name'] for c in COMMANDS if c['name'] == 'setup'
                  for o in c['options']}
    missing = [c.name for c in CHANNEL_SUBS if c.name not in registered]
    if missing:
        raise SystemExit(f'store.CHANNEL_SUBS declares {missing}, which /setup does '
                         'not register -- add it to COMMANDS in display order.')


check_channel_coverage()


def register():
    url = f'{API_BASE}/applications/{APPLICATION_ID}/commands'
    headers = {
        'Authorization': f'Bot {BOT_TOKEN}',
        'Content-Type': 'application/json',
    }

    response = requests.put(url, headers=headers, json=COMMANDS)
    print(f'Status: {response.status_code}')
    if response.ok:
        for cmd in response.json():
            print(f"  /{cmd['name']} (id {cmd['id']})")
    else:
        print(response.json())


if __name__ == '__main__':
    register()
