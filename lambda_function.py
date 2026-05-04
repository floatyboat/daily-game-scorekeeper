import json
import os
from datetime import datetime
from zoneinfo import ZoneInfo

from game_parser import format_scoreboard_components, make_timestamp_checker
from scoreboard import (
    DISCORD_API_BASE, make_session, fetch_messages, reference_date,
    parse_results, build_avatar_pool, is_scoreboard_message,
)

DISCORD_BOT_TOKEN = os.getenv('DISCORD_BOT_TOKEN')
INPUT_CHANNEL_ID = os.getenv('INPUT_CHANNEL_ID')
OUTPUT_CHANNEL_ID = os.getenv('OUTPUT_CHANNEL_ID')
TEST_CHANNEL_ID = os.getenv('TEST_CHANNEL_ID')
WORDLE_BOT_ID = os.getenv('WORDLE_BOT_ID')
HUNDREDS_OF_MESSAGES = int(os.getenv('HUNDREDS_OF_MESSAGES') or 1)
MINIMUM_PLAYERS = int(os.getenv('MINIMUM_PLAYERS') or 1)

TIMEZONE = ZoneInfo(os.getenv('TIMEZONE') or 'UTC')
TIME_WINDOW_HOURS = int(os.getenv('TIME_WINDOW_HOURS') or 24)
HOURS_AFTER_MIDNIGHT = int(os.getenv('HOURS_AFTER_MIDNIGHT') or 0)

_session = make_session(DISCORD_BOT_TOKEN)


def send_message(channel_id, message=None, components=None):
    url = f'{DISCORD_API_BASE}/channels/{channel_id}/messages'

    if components is not None:
        payload = {
            'components': components,
            'flags': 32768,
            'allowed_mentions': {'parse': ['users']},
        }
    else:
        payload = {'content': message, 'allowed_mentions': {'parse': ['users']}, 'flags': 4}

    response = _session.post(url, json=payload)
    response.raise_for_status()

    return response.json()

def pin_message(channel_id, message_id):
    url = f'{DISCORD_API_BASE}/channels/{channel_id}/messages/pins/{message_id}'
    _session.put(url)


def lambda_handler(event, context):
    import time
    t0 = time.time()

    yesterday = reference_date(datetime.now(), TIMEZONE, HOURS_AFTER_MIDNIGHT, days_back=1)

    messages = fetch_messages(_session, INPUT_CHANNEL_ID, limit=HUNDREDS_OF_MESSAGES * 100)
    print(f'[t+{time.time()-t0:.2f}s] fetched {len(messages)} messages')

    if not messages:
        return {
            'statusCode': 400,
            'body': json.dumps('No messages found')
        }
    elif is_scoreboard_message(messages[0]):
        return {
            'statusCode': 200,
            'body': json.dumps('Function triggered twice. No message sent.')
        }

    checker = make_timestamp_checker(yesterday, TIMEZONE, HOURS_AFTER_MIDNIGHT, TIME_WINDOW_HOURS)
    avatar_pool = build_avatar_pool(_session, messages, checker, WORDLE_BOT_ID)
    print(f'[t+{time.time()-t0:.2f}s] avatar pool has {len(avatar_pool)} users')

    results, puzzle_numbers = parse_results(
        messages, yesterday, TIMEZONE, HOURS_AFTER_MIDNIGHT, TIME_WINDOW_HOURS,
        wordle_bot_id=WORDLE_BOT_ID, avatar_hashes=avatar_pool,
    )
    print(f'[t+{time.time()-t0:.2f}s] parsed {sum(len(v) for v in results.values())} game results')

    components = format_scoreboard_components(results, yesterday, puzzle_numbers, minimum_players=MINIMUM_PLAYERS)

    channel = TEST_CHANNEL_ID if 'test' in event else OUTPUT_CHANNEL_ID
    response = send_message(channel, components=components)
    print(f'[t+{time.time()-t0:.2f}s] posted scoreboard')

    if 'test' in event:
        msg = 'TEST: Scoreboard posted'
    else:
        msg = 'Scoreboard posted'
        pin_message(channel, response['id'])

    return {
        'statusCode': 200,
        'body': json.dumps(msg)
    }

if __name__ == '__main__':
    print(lambda_handler('', ''))
