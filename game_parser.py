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
MAPTAP_CHALLENGE_LINK = 'https://maptap.gg/adventures?gametype=challenge'
GLOBLE_LINK = 'https://globle.org'
FLAGLE_LINK = 'https://flagle.org'
WORLDLE_LINK = 'https://worldlegame.io'
QUIZL_LINK = 'https://quizl.io'
CHRONOPHOTO_LINK = 'https://www.chronophoto.app/daily.html'

# Accent color constants (Discord integer colors)
HEADER_COLOR = 16766720       # gold
OTHER_GAMES_COLOR = 10395294  # gray
GAME_COLORS = {
    'connections': 10181046,  # purple
    'bandle': 15277667,       # pink
    'sports': 5763719,        # green
    'pips': 10181046,         # purple
    'maptap': 15105570,       # orange
    'maptap_challenge': 15105570,  # orange
    'chronophoto': 11027200,  # brown
    'globle': 3447003,        # blue
    'worldle': 1752220,       # cyan
    'flagle': 15105570,       # orange
    'quizl': 9807270,         # blue-gray
}

# Start date constants
CONNECTIONS_START_DATE = datetime(2023, 6, 12)
BANDLE_START_DATE = datetime(2022, 8, 18)
PIPS_START_DATE = datetime(2025, 8, 18)
SPORTS_CONNECTIONS_START_DATE = datetime(2024, 9, 24)
MAPTAP_START_DATE = datetime(2024, 6, 22)
QUIZL_START_DATE = datetime(2022, 3, 16)

# Default totals
DEFAULT_BANDLE_TOTAL = 6
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
        'maptap_challenge_date': f'{reference_date.strftime("%b")} {reference_date.day}',
        'globle_number': f'{reference_date.strftime("%B")} {reference_date.day}',
        'worldle_number': f'{reference_date.strftime("%B")} {reference_date.day}',
        'flagle_number': f'{reference_date.strftime("%B")} {reference_date.day}',
        'chronophoto_number': f'{reference_date.month}/{reference_date.day}/{reference_date.year}',
        'bandle_total': DEFAULT_BANDLE_TOTAL,
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
            'key': 'maptap_challenge',
            'pattern': re.compile(rf'MapTap Challenge Round.*{pn["maptap_challenge_date"]}', re.IGNORECASE),
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
        elif key == 'maptap_challenge':
            score_match = re.search(r'Score: (\d+)', content, re.IGNORECASE)
            if score_match:
                weighted_score = int(score_match.group(1))
                lines = content.split('\n')
                raw_score = weighted_score
                for line in lines:
                    if 'score' in line.lower() or 'maptap' in line.lower():
                        continue
                    nums = re.findall(r'\d+', line)
                    if len(nums) >= 3:
                        raw_score = sum(int(n) for n in nums)
                        break
                return (key, (weighted_score, raw_score), metadata)
        elif key == 'maptap':
            score_match = re.search(r'Final Score: (\d+)', content, re.IGNORECASE)
            if score_match:
                weighted_score = int(score_match.group(1))
                # Parse individual round scores from the emoji line
                # The scores line has multiple numbers interspersed with emojis
                lines = content.split('\n')
                raw_score = weighted_score
                for line in lines:
                    if 'final score' in line.lower() or 'maptap' in line.lower():
                        continue
                    nums = re.findall(r'\d+', line)
                    if len(nums) >= 3:
                        raw_score = sum(int(n) for n in nums)
                        break
                return (key, (weighted_score, raw_score), metadata)
        elif key in ('globle', 'worldle', 'flagle'):
            return (key, int(match.group(1)), metadata)
        elif key == 'quizl':
            score = len(re.findall(r'🟩', content))
            return (key, score, metadata)

    return None


def build_games_list(puzzle_numbers):
    """Build the list of game descriptors used by scoreboard and medal computation."""
    pn = puzzle_numbers
    bandle_total = pn.get('bandle_total', DEFAULT_BANDLE_TOTAL)
    quizl_total = pn.get('quizl_total', DEFAULT_QUIZL_TOTAL)

    return [
        ('bandle', '🎵', 'Bandle', 'guesses', bandle_total, pn['bandle_puzzle_number'], BANDLE_LINK),
        ('chronophoto', '📷', 'Chronophoto', 'score', 0, pn['chronophoto_number'], CHRONOPHOTO_LINK),
        ('connections', '🔗', 'Connections', 'connections', 4, pn['connections_puzzle_number'], CONNECTIONS_LINK),
        ('flagle', '🏁', 'Flagle', 'guesses', 0, f'{pn["flagle_number"]}', FLAGLE_LINK),
        ('globle', '🌍', 'Globle', 'guesses', 0, f'{pn["globle_number"]}', GLOBLE_LINK),
        ('maptap', '🎯', 'MapTap', 'maptap', 0, pn['maptap_number'], MAPTAP_LINK),
        ('maptap_challenge', '⚡', 'MapTap Challenge', 'maptap', 0, pn['maptap_number'], MAPTAP_CHALLENGE_LINK),
        ('pips', '🎲', 'Pips', 'time', 0, pn['pips_puzzle_number'], PIPS_LINK),
        ('quizl', '⁉️', 'Quizl', 'score', quizl_total, pn['quizl_puzzle_number'], QUIZL_LINK),
        ('sports', '🏈', 'Sports Connections', 'connections', 4, pn['sports_puzzle_number'], SPORTS_CONNECTIONS_LINK),
        ('worldle', '🗺️', 'Worldle', 'guesses', 0, f'{pn["worldle_number"]}', WORLDLE_LINK),
    ]


def compute_medals(results, puzzle_numbers, minimum_players=1):
    """Compute medal counts per user across all games.

    Returns {user_id: {'gold': int, 'silver': int, 'bronze': int}}.
    """
    games = build_games_list(puzzle_numbers)
    medal_counts = defaultdict(lambda: {'gold': 0, 'silver': 0, 'bronze': 0})
    medal_keys = ['gold', 'silver', 'bronze']

    for game_key, _, _, metric, total, _, _ in games:
        if game_key not in results or not results[game_key] or len(results[game_key]) < minimum_players:
            continue

        # Sort players using the same keys as format_scoreboard
        if metric == 'connections':
            players = sorted(results[game_key].items(), key=lambda x: (x[1][0], -x[1][1]))
        elif metric == 'score':
            players = sorted(results[game_key].items(), key=lambda x: (-x[1]))
        elif metric == 'maptap':
            players = sorted(results[game_key].items(), key=lambda x: (-x[1][1], -x[1][0]))
        else:
            players = sorted(results[game_key].items(), key=lambda x: x[1])

        # Walk with tie-aware ranking
        rank = 0
        prev_score = None
        i = 0
        while i < len(players):
            current_score = players[i][1]
            if current_score != prev_score:
                rank = i + 1

            # Check for poop override (no medal)
            is_poop = False
            if metric == 'connections':
                mistakes, solved = current_score
                if mistakes == total and solved == 0:
                    is_poop = True
            elif metric == 'guesses' and total > 0:
                if current_score > total:
                    is_poop = True
            elif metric == 'score':
                if current_score == 0:
                    is_poop = True
            elif metric == 'maptap':
                if current_score[1] == 0:
                    is_poop = True

            # Collect tied players
            j = i + 1
            while j < len(players) and players[j][1] == current_score:
                j += 1

            # Award medals if rank <= 3 and not poop
            if rank <= 3 and not is_poop:
                medal_key = medal_keys[rank - 1]
                for k in range(i, j):
                    medal_counts[players[k][0]][medal_key] += 1

            prev_score = current_score
            i = j

    return dict(medal_counts)


def format_medal_summary(medal_counts):
    """Format the medal count summary section.

    Returns empty string if no medals earned.
    """
    users_with_medals = {
        uid: counts for uid, counts in medal_counts.items()
        if counts['gold'] + counts['silver'] + counts['bronze'] > 0
    }

    if not users_with_medals:
        return ''

    sorted_users = sorted(
        users_with_medals.items(),
        key=lambda x: (-x[1]['gold'], -x[1]['silver'], -x[1]['bronze'])
    )

    medals = ['👑', '🥈', '🥉']
    message = ''

    rank = 0
    prev_val = None
    i = 0
    while i < len(sorted_users):
        g, s, b = sorted_users[i][1]['gold'], sorted_users[i][1]['silver'], sorted_users[i][1]['bronze']
        current_val = (g, s, b)
        if current_val != prev_val:
            rank = i + 1

        j = i + 1
        while j < len(sorted_users):
            gj, sj, bj = sorted_users[j][1]['gold'], sorted_users[j][1]['silver'], sorted_users[j][1]['bronze']
            if (gj, sj, bj) != current_val:
                break
            j += 1

        medal = f"{medals[rank - 1]} " if rank <= len(medals) else ""
        for k in range(i, j):
            uid = sorted_users[k][0]
            message += f'{medal}<@{uid}>: 👑x{g} 🥈x{s} 🥉x{b}\n'

        prev_val = current_val
        i = j

    return message + '\n'


def _format_game_players(game_scores, metric, total):
    """Format ranked player lines for a single game.

    Returns a markdown string with medal emojis, player mentions, and scores.
    """
    medals = ['👑', '🥈', '🥉']
    lines = ''

    if metric == 'maptap':
        sorted_players = sorted(game_scores.items(), key=lambda x: (-x[1][1], -x[1][0]))
        rank = 0
        prev_val = None
        i = 0
        while i < len(sorted_players):
            weighted = sorted_players[i][1][0]
            unweighted = sorted_players[i][1][1]
            score_tuple = (unweighted, weighted)
            if score_tuple != prev_val:
                rank = i + 1
            tied = [f'<@{sorted_players[i][0]}>']
            j = i + 1
            while j < len(sorted_players) and (sorted_players[j][1][1], sorted_players[j][1][0]) == score_tuple:
                tied.append(f'<@{sorted_players[j][0]}>')
                j += 1
            medal = f"{medals[rank - 1]} " if rank <= len(medals) else ""
            if unweighted == 0:
                medal = '💩 '
            players_str = " ".join(reversed(tied))
            lines += f'{medal}{players_str}: {unweighted} ({weighted} weighted)\n'
            prev_val = score_tuple
            i = j
        return lines

    if metric == 'connections':
        players = sorted(game_scores.items(), key=lambda x: (x[1][0], -x[1][1]))
    elif metric == 'score':
        players = sorted(game_scores.items(), key=lambda x: (-x[1]))
    else:
        players = sorted(game_scores.items(), key=lambda x: x[1])

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
                    medal = '💩 '
            else:
                score_str = f"{mistakes}/{total} mistakes"
        elif metric == 'score':
            if current_score == 0:
                medal = '💩 '
            score_str = f"{str(current_score)}"
            if total > 0:
                score_str = f"{score_str}/{total}"
        else:  # guesses
            if total == 0:
                score_str = f"{str(current_score)} {metric}"
            else:
                if current_score > total:
                    medal = '💩 '
                    current_score = 'X'
                score_str = f"{str(current_score)}/{total} {metric}"

        players_str = " ".join(reversed(tied_players))
        lines += f'{medal}'
        lines += f"{players_str}: {score_str}\n"

        prev_score = current_score
        i = j

    return lines


def format_scoreboard(results, reference_date, puzzle_numbers, title="Daily Game Scoreboard", minimum_players=1):
    """Format the scoreboard message. Parameterized version of format_message()."""
    games = build_games_list(puzzle_numbers)
    message = f"🧮 **{title}**"
    if not results:
        message += "\n\nNo results found!"
    else:
        message += f" - {reference_date.strftime('%B %d, %Y')}\n\n"
        medal_counts = compute_medals(results, puzzle_numbers, minimum_players)
        medal_section = format_medal_summary(medal_counts)
        if medal_section:
            message += medal_section
        games.sort(key=lambda x: len(results.get(x[0], {})), reverse=True)
        for game_key, game_emoji, game_title, metric, total, puzzle, link in games:
            if game_key not in results or not results[game_key] or len(results[game_key]) < minimum_players:
                continue

            game_title = f"[{game_title}]({link})"
            message += f'**{game_title} {game_emoji} {f"#{puzzle}" if type(puzzle) == int else f"#67"}**\n'
            message += _format_game_players(results[game_key], metric, total)
            message += "\n"
    return message


MEDAL_COLOR = 15844367  # dark gold

def format_scoreboard_components(results, reference_date, puzzle_numbers, title="Daily Game Scoreboard", minimum_players=1):
    """Format the scoreboard as Discord Components V2 (list of top-level components).

    Returns a list[dict] suitable for the 'components' field in a Discord message.
    """
    games = build_games_list(puzzle_numbers)
    components = []

    # --- Header container ---
    header_text = f"### 🧮 {title} - {reference_date.strftime('%B %d, %Y')}"

    if not results:
        return [{"type": 17, "accent_color": HEADER_COLOR, "components": [
            {"type": 10, "content": header_text},
            {"type": 10, "content": "No results found!"},
        ]}]

    # --- Medal container (gold accent) ---
    medal_counts = compute_medals(results, puzzle_numbers, minimum_players)
    medal_section = format_medal_summary(medal_counts)
    if medal_section:
        components.append({"type": 17, "accent_color": HEADER_COLOR, "components": [
            {"type": 10, "content": header_text},
            {"type": 10, "content": medal_section.rstrip('\n')},
        ]})
    else:
        components.append({"type": 17, "accent_color": OTHER_GAMES_COLOR, "components": [
            {"type": 10, "content": header_text},
        ]})

    # Sort games by player count descending
    games.sort(key=lambda x: len(results.get(x[0], {})), reverse=True)

    qualified = [g for g in games if g[0] in results and results[g[0]] and len(results[g[0]]) >= minimum_players]

    # --- Scores container ---
    scores_children = []
    for g_idx, (game_key, game_emoji, game_title, metric, total, puzzle, link) in enumerate(qualified):
        if g_idx > 0:
            scores_children.append({"type": 14, "spacing": 1})  # Separator
        puzzle_label = f"#{puzzle}" if type(puzzle) == int else "#67"
        score_text = f"**[{game_title}]({link}) {game_emoji} {puzzle_label}**\n"
        score_text += _format_game_players(results[game_key], metric, total).rstrip('\n')
        scores_children.append({"type": 10, "content": score_text})

    if scores_children:
        components.append({"type": 17, "accent_color": OTHER_GAMES_COLOR, "components": scores_children})

    return components
