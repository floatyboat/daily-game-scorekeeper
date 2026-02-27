import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from dateutil import parser as dateutil_parser
from collections import defaultdict

# Game link constants
CONNECTIONS_LINK = 'https://www.nytimes.com/games/connections'
BANDLE_LINK = 'https://bandle.app/daily'
PIPS_LINK = 'https://www.nytimes.com/games/pips'
SPORTS_CONNECTIONS_LINK = 'https://www.nytimes.com/athletic/connections-sports-edition'
MAPTAP_LINK = 'https://maptap.gg'
GLOBLE_LINK = 'https://globle.org'
FLAGLE_LINK = 'https://flagle.org'
WORLDLE_LINK = 'https://worldlegame.io'
WHEREDLE_LINK = 'https://wheredle.xyz'
QUIZL_LINK = 'https://quizl.io'
CHRONOPHOTO_LINK = 'https://www.chronophoto.app/daily.html'

# Start date constants
CONNECTIONS_START_DATE = datetime(2023, 6, 12)
BANDLE_START_DATE = datetime(2022, 8, 18)
PIPS_START_DATE = datetime(2025, 8, 18)
SPORTS_CONNECTIONS_START_DATE = datetime(2024, 9, 24)
MAPTAP_START_DATE = datetime(2024, 6, 22)
QUIZL_START_DATE = datetime(2022, 3, 16)

# Default totals
DEFAULT_BANDLE_TOTAL = 6
DEFAULT_WHEREDLE_TOTAL = 7
DEFAULT_QUIZL_TOTAL = 5


def compute_puzzle_numbers(reference_date):
    """Compute all puzzle numbers and date strings for a given reference date."""
    return {
        'connections_puzzle_number': int((reference_date - CONNECTIONS_START_DATE).days + 1),
        'bandle_puzzle_number': int((reference_date - BANDLE_START_DATE).days + 1),
        'sports_puzzle_number': int((reference_date - SPORTS_CONNECTIONS_START_DATE).days + 1),
        'pips_puzzle_number': int((reference_date - PIPS_START_DATE).days + 1),
        'maptap_number': int((reference_date - MAPTAP_START_DATE).days + 1),
        'quizl_puzzle_number': int((reference_date - QUIZL_START_DATE).days + 1),
        'maptap_date': f'{reference_date.strftime("%B")} {reference_date.day}',
        'globle_number': f'{reference_date.strftime("%B")} {reference_date.day}',
        'worldle_number': f'{reference_date.strftime("%B")} {reference_date.day}',
        'flagle_number': f'{reference_date.strftime("%B")} {reference_date.day}',
        'wheredle_number': f'{reference_date.strftime("%B")} {reference_date.day}',
        'chronophoto_number': f'{reference_date.month}/{reference_date.day}/{reference_date.year}',
        'bandle_total': DEFAULT_BANDLE_TOTAL,
        'wheredle_total': DEFAULT_WHEREDLE_TOTAL,
        'quizl_total': DEFAULT_QUIZL_TOTAL,
    }


def make_timestamp_checker(reference_date, tz, hours_after_midnight, time_window_hours):
    """Return a callable (iso_timestamp) -> bool for checking if a timestamp falls in the window."""
    window_start = reference_date.replace(
        hour=hours_after_midnight, minute=0, second=0, microsecond=0
    )
    # Make it timezone-aware
    window_start = window_start.replace(tzinfo=tz)
    window_end = window_start + timedelta(hours=time_window_hours)

    def check(iso_timestamp):
        try:
            timestamp = dateutil_parser.isoparse(iso_timestamp)
        except (ValueError, TypeError) as e:
            raise ValueError(f"Invalid ISO8601 timestamp: {iso_timestamp}") from e
        timestamp_in_ref_tz = timestamp.astimezone(tz)
        return window_start <= timestamp_in_ref_tz < window_end

    return check


def get_connections_results(content):
    """Parse connections-style emoji grids and return (mistakes, solved_groups)."""
    squares = re.findall(r'[🟨🟩🟦🟪🟡🟢🔵🟣]', content)
    if len(squares) % 4 == 0:
        rows = [squares[i:i+4] for i in range(0, len(squares), 4)]
        solved_groups = sum(1 for row in rows if len(set(row)) == 1)
        mistakes = len(rows) - solved_groups

        is_vertical = False
        if len(rows) == 4 and solved_groups == 0:
            vert = set()
            for col in range(4):
                column = [rows[row][col] for row in range(4)]
                if len(set(column)) == 1:
                    vert.add(column[0])
                else:
                    break
            is_vertical = len(vert) == 4

        if is_vertical:
            return (-1, 0)
        else:
            return (mistakes, solved_groups)
    return (69, 0)


def build_game_regexes(puzzle_numbers):
    """Return a list of game descriptors with compiled regex patterns."""
    pn = puzzle_numbers
    return [
        {
            'key': 'connections',
            'pattern': re.compile(rf'Connections.*?Puzzle #{pn["connections_puzzle_number"]}', re.IGNORECASE | re.DOTALL),
            'needs_timestamp': False,
        },
        {
            'key': 'bandle',
            'pattern': re.compile(rf'Bandle #{pn["bandle_puzzle_number"]} (\d+|x)/(\d+)', re.IGNORECASE),
            'needs_timestamp': False,
        },
        {
            'key': 'sports',
            'pattern': re.compile(rf'Connections: Sports Edition.*?puzzle #{pn["sports_puzzle_number"]}', re.IGNORECASE | re.DOTALL),
            'needs_timestamp': False,
        },
        {
            'key': 'pips',
            'pattern': re.compile(rf'Pips #{pn["pips_puzzle_number"]} Hard', re.IGNORECASE),
            'needs_timestamp': False,
        },
        {
            'key': 'maptap',
            'pattern': re.compile(rf'(.*)MapTap(.*){pn["maptap_date"]}', re.IGNORECASE),
            'needs_timestamp': False,
        },
        {
            'key': 'chronophoto',
            'search_pattern': re.compile(re.escape(pn["chronophoto_number"]), re.IGNORECASE),
            'pattern': re.compile(rf"I got a score of (\d+) on today's Chronophoto: {re.escape(pn['chronophoto_number'])}", re.IGNORECASE),
            'needs_timestamp': False,
        },
        {
            'key': 'globle',
            'pattern': re.compile(r"I guessed today['\u2019]s Globle in (\d+) tr", re.IGNORECASE),
            'needs_timestamp': True,
        },
        {
            'key': 'worldle',
            'pattern': re.compile(r"I guessed today['\u2019]s Worldle in (\d+) tr", re.IGNORECASE),
            'needs_timestamp': True,
        },
        {
            'key': 'flagle',
            'pattern': re.compile(r"I guessed today['\u2019]s Flag in (\d+) tr", re.IGNORECASE),
            'needs_timestamp': True,
        },
        {
            'key': 'wheredle',
            'pattern': re.compile(r'#Wheredle', re.IGNORECASE),
            'needs_timestamp': True,
        },
        {
            'key': 'quizl',
            'pattern': re.compile(rf'Quizl#{pn["quizl_puzzle_number"]}', re.IGNORECASE),
            'needs_timestamp': False,
        },
    ]


def match_message(content, timestamp, game_regexes, timestamp_checker):
    """Run a single message through all game regexes.

    Returns (game_key, score, metadata) or None.
    metadata may contain {'bandle_total': N} etc.
    """
    for game in game_regexes:
        key = game['key']

        # For chronophoto, use search_pattern for initial check
        if key == 'chronophoto':
            if not game['search_pattern'].search(content):
                continue
            match = game['pattern'].search(content)
            if match:
                if game['needs_timestamp'] and not timestamp_checker(timestamp):
                    continue
                return (key, int(match.group(1)), {})
            continue

        match = game['pattern'].search(content)
        if not match:
            continue

        if game['needs_timestamp'] and not timestamp_checker(timestamp):
            continue

        metadata = {}

        if key == 'connections':
            return (key, get_connections_results(content), metadata)
        elif key == 'bandle':
            score_str = match.group(1)
            total = int(match.group(2))
            metadata['bandle_total'] = total
            score = total + 1 if score_str == 'x' else int(score_str)
            return (key, score, metadata)
        elif key == 'sports':
            return (key, get_connections_results(content), metadata)
        elif key == 'pips':
            pips_match = re.search(r'(\d+):(\d+)', content, re.IGNORECASE)
            if pips_match:
                minutes = int(pips_match.group(1))
                seconds = int(pips_match.group(2))
                return (key, minutes * 60 + seconds, metadata)
        elif key == 'maptap':
            score_match = re.search(r'Final Score: (\d+)', content, re.IGNORECASE)
            if score_match:
                return (key, int(score_match.group(1)), metadata)
        elif key in ('globle', 'worldle', 'flagle'):
            return (key, int(match.group(1)), metadata)
        elif key == 'wheredle':
            yellow_squares = re.findall(r'🟨', content)
            green_squares = re.findall(r'🟩', content)
            if len(green_squares) == 0:
                score = DEFAULT_WHEREDLE_TOTAL + 1
            else:
                score = len(yellow_squares) + len(green_squares)
            return (key, score, metadata)
        elif key == 'quizl':
            score = len(re.findall(r'🟩', content))
            return (key, score, metadata)

    return None


def format_scoreboard(results, reference_date, puzzle_numbers, title="Daily Game Scoreboard", minimum_players=1):
    """Format the scoreboard message. Parameterized version of format_message()."""
    pn = puzzle_numbers
    bandle_total = pn.get('bandle_total', DEFAULT_BANDLE_TOTAL)
    wheredle_total = pn.get('wheredle_total', DEFAULT_WHEREDLE_TOTAL)
    quizl_total = pn.get('quizl_total', DEFAULT_QUIZL_TOTAL)

    games = [
        ('bandle', '🎵', 'Bandle', 'guesses', bandle_total, pn['bandle_puzzle_number'], BANDLE_LINK),
        ('chronophoto', '📷', 'Chronophoto', 'score', 0, pn['chronophoto_number'], CHRONOPHOTO_LINK),
        ('connections', '🔗', 'Connections', 'connections', 4, pn['connections_puzzle_number'], CONNECTIONS_LINK),
        ('flagle', '🏁', 'Flagle', 'guesses', 0, f'{pn["flagle_number"]}', FLAGLE_LINK),
        ('globle', '🌍', 'Globle', 'guesses', 0, f'{pn["globle_number"]}', GLOBLE_LINK),
        ('maptap', '🎯', 'MapTap', 'score', 0, pn['maptap_number'], MAPTAP_LINK),
        ('pips', '🎲', 'Pips', 'time', 0, pn['pips_puzzle_number'], PIPS_LINK),
        ('quizl', '⁉️', 'Quizl', 'score', quizl_total, pn['quizl_puzzle_number'], QUIZL_LINK),
        ('sports', '🏈', 'Sports Connections', 'connections', 4, pn['sports_puzzle_number'], SPORTS_CONNECTIONS_LINK),
        ('wheredle', '🛣️', 'Wheredle', 'guesses', wheredle_total, f'{pn["wheredle_number"]}', WHEREDLE_LINK),
        ('worldle', '🗺️', 'Worldle', 'guesses', 0, f'{pn["worldle_number"]}', WORLDLE_LINK),
    ]
    medals = ['👑', '🥈', '🥉']
    message = f"🧮 **{title}**"
    no_players_reached = False
    one_player_reached = False
    if not results:
        message += "\n\nNo results found!"
    else:
        message += f" - {reference_date.strftime('%B %d, %Y')}\n\n"
        games.sort(key=lambda x: len(results.get(x[0], {})), reverse=True)
        for game_key, game_emoji, game_title, metric, total, puzzle, link in games:
            game_title = f"[{game_title}]({link})"

            if game_key not in results or not results[game_key] or len(results[game_key]) < minimum_players:
                if not no_players_reached:
                    if one_player_reached:
                        message += '\n'
                    message += f'-# Other games:\t'
                no_players_reached = True
                message += f'{game_emoji} {game_title}\t'
                continue

            one_player_reached = True

            if metric == 'connections':
                players = sorted(results[game_key].items(), key=lambda x: (x[1][0], -x[1][1]))
            elif metric == 'score':
                players = sorted(results[game_key].items(), key=lambda x: (-x[1]))
            else:
                players = sorted(results[game_key].items(), key=lambda x: x[1])

            message += f'**{game_title} {game_emoji} {f"#{puzzle}" if type(puzzle) == int else f"#67"}**\n'
            rank = 0
            prev_score = None
            i = 0

            while i < len(players):
                current_score = players[i][1]

                if current_score != prev_score:
                    rank = i + 1

                tied_players = [f'<@{players[i][0]}>']
                j = i + 1
                while j < len(players) and players[j][1] == current_score:
                    tied_players.append(f'<@{players[j][0]}>')
                    j += 1

                medal = f"{medals[rank - 1]} " if rank <= len(medals) else f""

                if metric == 'time':
                    minutes = current_score // 60
                    seconds = current_score % 60
                    score_str = f"{minutes}:{seconds:02d}"
                elif metric == 'connections':
                    mistakes, solved = current_score
                    if mistakes == -1:
                        score_str = "VERT 🧗"
                    elif mistakes == total:
                        score_str = f"{mistakes}/{total} mistakes ({solved} solved)"
                        if solved == 0:
                            medal = '💩'
                    else:
                        score_str = f"{mistakes}/{total} mistakes"
                elif metric == 'score':
                    score_str = f"{str(current_score)}"
                    if total > 0:
                        score_str = f"{score_str}/{total}"
                else:  # guesses
                    if total == 0:
                        score_str = f"{str(current_score)} {metric}"
                    else:
                        if current_score > total:
                            medal = '💩'
                            current_score = 'X'
                        score_str = f"{str(current_score)}/{total} {metric}"

                players_str = " ".join(reversed(tied_players))
                message += f'{medal}'
                message += f"{players_str}: {score_str}\n"

                prev_score = current_score
                i = j

            if len(players) >= minimum_players:
                message += "\n"
    return message
