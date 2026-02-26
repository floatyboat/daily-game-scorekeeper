import json
import os
import requests
from datetime import datetime, timedelta
from collections import defaultdict

from game_parser import (
    compute_puzzle_numbers, build_game_regexes, match_message,
    format_scoreboard, make_timestamp_checker
)

DISCORD_API_BASE = 'https://discord.com/api/v10'

DISCORD_BOT_ID = os.getenv('DISCORD_BOT_ID') or 0
DISCORD_BOT_TOKEN = os.getenv('DISCORD_BOT_TOKEN')
INPUT_CHANNEL_ID = os.getenv('INPUT_CHANNEL_ID')
OUTPUT_CHANNEL_ID = os.getenv('OUTPUT_CHANNEL_ID')
TEST_CHANNEL_ID = os.getenv('TEST_CHANNEL_ID')
HUNDREDS_OF_MESSAGES = int(os.getenv('HUNDREDS_OF_MESSAGES') or 1)
MINIMUM_PLAYERS = int(os.getenv('MINIMUM_PLAYERS') or 1)

UTC_OFFSET = int(os.getenv('UTC_OFFSET') or 0)
TIME_WINDOW_HOURS = int(os.getenv('TIME_WINDOW_HOURS') or 24)
HOURS_AFTER_MIDNIGHT = int(os.getenv('HOURS_AFTER_MIDNIGHT') or 0)

def get_messages(channel_id):
    headers = {
        'Authorization': f'Bot {DISCORD_BOT_TOKEN}',
        'Content-Type': 'application/json'
    }

    url = f'{DISCORD_API_BASE}/channels/{channel_id}/messages?limit=100'
    response = requests.get(url, headers=headers)
    messages = response.json()

    for x in range(HUNDREDS_OF_MESSAGES - 1):
        last_msg_id = messages[-1]['id']
        url_id = url + f'&before={last_msg_id}'
        response = requests.get(url_id, headers=headers)
        messages += response.json()
    return messages

def send_message(channel_id, message):
    headers = {
        'Authorization': f'Bot {DISCORD_BOT_TOKEN}',
        'Content-Type': 'application/json'
    }

    url = f'{DISCORD_API_BASE}/channels/{channel_id}/messages'
    payload = {'content': message, 'allowed_mentions': {'parse': ['users']}, 'flags': 4}

    response = requests.post(url, headers=headers, json=payload)

    return response.json()

def pin_message(channel_id, message_id):
    headers = {
        'Authorization': f'Bot {DISCORD_BOT_TOKEN}',
        'Content-Type': 'application/json'
    }

    url = f'{DISCORD_API_BASE}/channels/{channel_id}/messages/pins/{message_id}'
    requests.put(url, headers=headers)

def lambda_handler(event, context):
    yesterday = datetime.now() - timedelta(days=1)
    puzzle_numbers = compute_puzzle_numbers(yesterday)
    game_regexes = build_game_regexes(puzzle_numbers)
    checker = make_timestamp_checker(yesterday, UTC_OFFSET, HOURS_AFTER_MIDNIGHT, TIME_WINDOW_HOURS)

    messages = get_messages(INPUT_CHANNEL_ID)
    if not messages:
        return {
            'statusCode': 400,
            'body': json.dumps('No messages found')
        }
    elif messages[0]['author']['id'] == DISCORD_BOT_ID:
        return {
            'statusCode': 200,
            'body': json.dumps('Function triggered twice. No message sent.')
        }

    results = defaultdict(dict)
    for msg in messages:
        result = match_message(msg['content'], msg['timestamp'], game_regexes, checker)
        if result:
            game_key, score, metadata = result
            results[game_key][msg['author']['id']] = score
            puzzle_numbers.update(metadata)

    output = format_scoreboard(results, yesterday, puzzle_numbers, minimum_players=MINIMUM_PLAYERS)

    if 'test' in event:
        response = send_message(TEST_CHANNEL_ID, output)
        msg = f'TEST: Scoreboard posted'
    else:
        response = send_message(OUTPUT_CHANNEL_ID, output)
        msg = f'Scoreboard posted'
        pin_message(OUTPUT_CHANNEL_ID, response['id'])

    return {
        'statusCode': 200,
        'body': json.dumps(msg)
    }

if __name__ == '__main__':
    print(lambda_handler('', ''))
