"""Register the bot's slash commands with Discord (bulk overwrite, safe to re-run).

    dotenv run -- python3 register_commands.py

Re-run whenever a command or option changes. /setup and /games default to
Manage Server via default_member_permissions; the interaction handler
re-verifies the permission bit regardless of how a server re-maps the command.
"""

import os
import requests
from dotenv import load_dotenv

load_dotenv()

APPLICATION_ID = os.getenv('DISCORD_APPLICATION_ID') or os.getenv('DISCORD_BOT_ID')
BOT_TOKEN = os.getenv('DISCORD_BOT_TOKEN')
API_BASE = 'https://discord.com/api/v10'

SUB_COMMAND, STRING, INTEGER, BOOLEAN, USER, CHANNEL = 1, 3, 4, 5, 6, 7
TEXT_CHANNEL_TYPES = [0, 5]   # guild text, announcement
MANAGE_GUILD = '32'


def channel_sub(name, blurb):
    """A channel-setting subcommand: native picker option, raw-ID escape hatch,
    or no args at all (the handler then replies with a select menu)."""
    return {'type': SUB_COMMAND, 'name': name, 'description': blurb, 'options': [
        {'type': CHANNEL, 'name': 'channel', 'description': 'Pick the channel',
         'channel_types': TEXT_CHANNEL_TYPES, 'required': False},
        {'type': STRING, 'name': 'channel_id',
         'description': "Channel ID, for channels the picker can't show", 'required': False},
    ]}


COMMANDS = [
    {
        'name': 'play',
        'description': 'Play daily games',
        'type': 1,
    },
    {
        'name': 'setup',
        'description': 'Configure the daily game scoreboard for this server',
        'type': 1,
        'default_member_permissions': MANAGE_GUILD,
        'dm_permission': False,
        'options': [
            {'type': SUB_COMMAND, 'name': 'show',
             'description': 'Show the current configuration'},
            channel_sub('input', 'Set where scores are read and the sticky lives'),
            channel_sub('output', 'Set where the daily scoreboard posts'),
            {'type': SUB_COMMAND, 'name': 'daily',
             'description': 'Turn the daily scoreboard post on or off', 'options': [
                {'type': BOOLEAN, 'name': 'enabled',
                 'description': 'Post the daily scoreboard?', 'required': True}]},
            {'type': SUB_COMMAND, 'name': 'sticky',
             'description': 'Turn the Now Playing sticky on or off', 'options': [
                {'type': BOOLEAN, 'name': 'enabled',
                 'description': 'Keep a sticky pinned to the bottom of the input channel?',
                 'required': True}]},
            {'type': SUB_COMMAND, 'name': 'time',
             'description': 'Timezone and daily schedule', 'options': [
                {'type': STRING, 'name': 'timezone',
                 'description': 'IANA name, e.g. America/New_York', 'required': False},
                {'type': INTEGER, 'name': 'day_start_hour',
                 'description': 'Hour the scoring day starts (default 0)',
                 'min_value': 0, 'max_value': 23, 'required': False},
                {'type': INTEGER, 'name': 'post_hour',
                 'description': 'Local hour the scoreboard posts (default: day start hour)',
                 'min_value': 0, 'max_value': 23, 'required': False},
                {'type': INTEGER, 'name': 'window_hours',
                 'description': 'Hours submissions stay open each day (default 24)',
                 'min_value': 1, 'max_value': 24, 'required': False}]},
            {'type': SUB_COMMAND, 'name': 'limits',
             'description': 'Display minimum, message volume, and the Wordle bot', 'options': [
                {'type': INTEGER, 'name': 'minimum_players',
                 'description': 'Hide games with fewer players than this (default 1)',
                 'min_value': 1, 'required': False},
                {'type': INTEGER, 'name': 'message_volume',
                 'description': 'Hundreds of messages/day in the input channel (default 1)',
                 'min_value': 1, 'max_value': 8, 'required': False},
                {'type': USER, 'name': 'wordle_bot',
                 'description': 'The official Wordle bot (enables image results)',
                 'required': False}]},
        ],
    },
    {
        'name': 'games',
        'description': 'Choose which games are tracked in this server',
        'type': 1,
        'default_member_permissions': MANAGE_GUILD,
        'dm_permission': False,
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
