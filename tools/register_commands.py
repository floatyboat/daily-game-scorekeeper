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
from store import (
    OPT_SUB_COMMAND, OPT_STRING, OPT_BOOLEAN, OPT_CHANNEL, setup_options,
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
    return option


def field_sub(group, description):
    """A /setup subcommand exposing every config field declared in that group."""
    return {'type': OPT_SUB_COMMAND, 'name': group, 'description': description,
            'options': [field_option(f) for f in setup_options(group)]}


def channel_sub(name, blurb):
    """A channel-setting subcommand: native picker option, raw-ID escape hatch,
    or no args at all (the handler then replies with a select menu)."""
    return {'type': OPT_SUB_COMMAND, 'name': name, 'description': blurb, 'options': [
        {'type': OPT_CHANNEL, 'name': 'channel', 'description': 'Pick the channel',
         'channel_types': list(TEXT_CHANNEL_TYPES), 'required': False},
        {'type': OPT_STRING, 'name': 'channel_id',
         'description': "Channel ID, for channels the picker can't show", 'required': False},
    ]}


def toggle_sub(name, description, prompt):
    """An on/off subcommand with a single required boolean."""
    return {'type': OPT_SUB_COMMAND, 'name': name, 'description': description, 'options': [
        {'type': OPT_BOOLEAN, 'name': 'enabled', 'description': prompt, 'required': True}]}


COMMANDS = [
    {
        'name': 'play',
        'description': 'Play daily games',
        'type': CHAT_INPUT,
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
        'options': [
            {'type': OPT_SUB_COMMAND, 'name': 'show',
             'description': 'Show the current configuration'},
            channel_sub('input', 'Set where scores are read and the sticky lives'),
            channel_sub('output', 'Set where the daily scoreboard posts'),
            toggle_sub('daily', 'Turn the daily scoreboard post on or off',
                       'Post the daily scoreboard?'),
            toggle_sub('sticky', 'Turn the Now Playing sticky on or off',
                       'Keep a sticky pinned to the bottom of the input channel?'),
            {'type': OPT_SUB_COMMAND, 'name': 'games',
             'description': 'Choose which games are tracked in this server'},
            field_sub('time', 'Timezone and daily schedule'),
            field_sub('limits', 'Display minimum, message volume, and the Wordle bot'),
        ],
    },
]


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
