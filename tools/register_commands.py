"""Register the bot's slash commands with Discord (bulk overwrite, safe to re-run).

    dotenv run -- python3 tools/register_commands.py

The interaction lambda's deploy workflow runs this after every deploy of that function;
run it by hand only for an instance of your own. /setup takes no options: every
setting is edited on the panel it opens, whose forms setup_panel builds from
store.CONFIG_FIELDS, so nothing here changes when a setting does. Registration
refuses to run while any of those forms is over Discord's caps (see
setup_panel.check_limits). /setup defaults to Manage Server via
default_member_permissions; the interaction handler re-verifies the permission bit
regardless of how a server re-maps the command.
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
from scoreboard import PERM_MANAGE_GUILD
import setup_panel

APPLICATION_ID = os.getenv('DISCORD_APPLICATION_ID') or os.getenv('DISCORD_BOT_ID')
BOT_TOKEN = os.getenv('DISCORD_BOT_TOKEN')
API_BASE = 'https://discord.com/api/v10'

CHAT_INPUT = 1                          # command type (not an option type)
OPT_BOOLEAN = 5                         # option type
MANAGE_GUILD = str(PERM_MANAGE_GUILD)   # default_member_permissions is a decimal string


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
        # Personal and read-only: no permission gate, no options, and the reply
        # is ephemeral, so a server full of people running it stays quiet.
        'name': 'stats',
        'description': 'Your own streaks — overall and game by game',
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
        # The explainer (interaction_lambda.build_help_text): ephemeral, built
        # off the server's live config, the same text the one-time welcome
        # follow-up sends.
        'name': 'help',
        'description': 'How the scoreboard works — playing, points, the daily rotation',
        'type': CHAT_INPUT,
    },
    {
        # No options: the reply is the settings panel (setup_panel), or for a
        # server that hasn't picked a scores channel yet, the form that does.
        'name': 'setup',
        'description': 'Configure the daily game scoreboard for this server',
        'type': CHAT_INPUT,
        'default_member_permissions': MANAGE_GUILD,
        'dm_permission': False,
    },
]


def check_setup_forms():
    """Refuse to register while a /setup form or the panel is over Discord's
    caps. Discord rejects such a payload whole, so the form would fail to open
    for every server, and only when an admin tried it."""
    problems = setup_panel.check_limits()
    if problems:
        raise SystemExit('/setup is over Discord\'s caps:\n  ' + '\n  '.join(problems))


check_setup_forms()


def register():
    url = f'{API_BASE}/applications/{APPLICATION_ID}/commands'
    headers = {
        'Authorization': f'Bot {BOT_TOKEN}',
        'Content-Type': 'application/json',
    }

    response = requests.put(url, headers=headers, json=COMMANDS)
    print(f'Status: {response.status_code}')
    if not response.ok:
        # A non-zero exit, so the deploy workflow's registration step fails
        # where it can be seen instead of passing with the error in its log.
        raise SystemExit(response.text[:2000])
    for cmd in response.json():
        print(f"  /{cmd['name']} (id {cmd['id']})")


if __name__ == '__main__':
    register()
