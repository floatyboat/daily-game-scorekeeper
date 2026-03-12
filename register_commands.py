"""One-time script to register the /play slash command with Discord."""

import os
import requests
from dotenv import load_dotenv

load_dotenv()

APPLICATION_ID = os.getenv('DISCORD_APPLICATION_ID')
BOT_TOKEN = os.getenv('DISCORD_BOT_TOKEN')
API_BASE = 'https://discord.com/api/v10'


def register():
    url = f'{API_BASE}/applications/{APPLICATION_ID}/commands'
    headers = {
        'Authorization': f'Bot {BOT_TOKEN}',
        'Content-Type': 'application/json',
    }
    payload = {
        'name': 'play',
        'description': 'Play daily games',
        'type': 1,
    }

    response = requests.post(url, headers=headers, json=payload)
    print(f'Status: {response.status_code}')
    print(response.json())


if __name__ == '__main__':
    register()
