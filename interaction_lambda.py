import base64
import json
import os
from datetime import datetime
from nacl.signing import VerifyKey
from nacl.exceptions import BadSignatureError

from game_parser import compute_puzzle_numbers, build_games_list

DISCORD_PUBLIC_KEY = os.getenv('DISCORD_PUBLIC_KEY', '')


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


def build_play_response():
    """Build an ephemeral message with link buttons for all tracked games."""
    today = datetime.utcnow()
    puzzle_numbers = compute_puzzle_numbers(today)
    games = build_games_list(puzzle_numbers)

    buttons = [
        {"type": 2, "style": 5, "label": f"{emoji} {name}", "url": url}
        for _, emoji, name, _, _, _, url in games
    ]

    action_rows = []
    for i in range(0, len(buttons), 5):
        action_rows.append({"type": 1, "components": buttons[i:i + 5]})

    return {
        "type": 4,
        "data": {
            "flags": 64,
            "content": "Pick a game to play!",
            "components": action_rows,
        },
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
        return {
            'statusCode': 200,
            'headers': {'Content-Type': 'application/json'},
            'body': json.dumps({'type': 1}),
        }

    # APPLICATION_COMMAND (type 2)
    if body.get('type') == 2:
        command_name = body.get('data', {}).get('name', '')
        if command_name == 'play':
            return {
                'statusCode': 200,
                'headers': {'Content-Type': 'application/json'},
                'body': json.dumps(build_play_response()),
            }

    return {'statusCode': 400, 'body': 'Unknown interaction type'}
