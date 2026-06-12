import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from dateutil import parser as dateutil_parser
from collections import defaultdict, Counter
from dataclasses import dataclass

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
WORDLE_LINK = 'https://www.nytimes.com/games/wordle'
TRAVLE_LINK = 'https://travle.earth'
DIALED_COLOR_LINK = 'https://dialed.gg/?d=1'
DIALED_SOUND_LINK = 'https://dialed.gg/sound?d=1'
DIALED_COLOR2_LINK = 'https://dialed.gg/color2?d=1'

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
    'wordle': 5763719,        # green
    'travle': 3066993,        # forest green
    'dialed_color': 16738155,  # coral
    'dialed_sound': 9442302,   # violet
    'dialed_color2': 16711935,  # magenta
}

# Start date constants
CONNECTIONS_START_DATE = datetime(2023, 6, 12)
BANDLE_START_DATE = datetime(2022, 8, 18)
PIPS_START_DATE = datetime(2025, 8, 18)
SPORTS_CONNECTIONS_START_DATE = datetime(2024, 9, 24)
MAPTAP_START_DATE = datetime(2024, 6, 22)
QUIZL_START_DATE = datetime(2022, 3, 16)
WORDLE_START_DATE = datetime(2021, 6, 19)
TRAVLE_START_DATE = datetime(2022, 12, 15)

# Default totals
DEFAULT_BANDLE_TOTAL = 6
DEFAULT_QUIZL_TOTAL = 5
DEFAULT_WORDLE_TOTAL = 6

# Games turned fully off: skipped everywhere (parsing + scoreboard + Play button).
DISABLED_GAMES = {'globle', 'flagle', 'worldle', 'maptap_challenge'}


@dataclass
class Game:
    """One tracked game: display metadata plus the regex used to parse its scores.

    Single source of truth consumed by both the scoreboard (emoji/title/metric/
    total/puzzle/url) and the message parser (pattern/needs_timestamp/search_pattern).
    """
    key: str
    emoji: str
    title: str
    metric: str
    total: int
    puzzle: object            # int or pre-formatted str; display only
    url: str
    pattern: re.Pattern
    needs_timestamp: bool = False
    search_pattern: re.Pattern = None   # chronophoto: cheap pre-check before the full pattern


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
        'dialed_number': f'{reference_date.strftime("%B")} {reference_date.day}',
        'chronophoto_number': f'{reference_date.month}/{reference_date.day}/{reference_date.year}',
        'wordle_puzzle_number': int((reference_date - WORDLE_START_DATE).days),
        'travle_puzzle_number': int((reference_date - TRAVLE_START_DATE).days + 1),
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


# Wordle bot preview image: cell colors
_WORDLE_GREEN = (83, 141, 78)
_WORDLE_YELLOW = (181, 159, 59)
_WORDLE_GRAY = (58, 58, 60)
_WORDLE_EMPTY = (18, 18, 19)


def _color_distance(c1, c2):
    return sum((a - b) ** 2 for a, b in zip(c1, c2)) ** 0.5


def _classify_cell(px):
    distances = {
        'green': _color_distance(px, _WORDLE_GREEN),
        'yellow': _color_distance(px, _WORDLE_YELLOW),
        'gray': _color_distance(px, _WORDLE_GRAY),
        'empty': _color_distance(px, _WORDLE_EMPTY),
    }
    closest = min(distances, key=distances.get)
    return closest if distances[closest] < 40 else 'unknown'


def _is_tile_color(px, tol=5):
    """True for GRAY/GREEN/YELLOW pixels. Excludes EMPTY (aliases background)."""
    return min(
        _color_distance(px, _WORDLE_GREEN),
        _color_distance(px, _WORDLE_YELLOW),
        _color_distance(px, _WORDLE_GRAY),
    ) < tol


def _detect_grids(img):
    """Detect Wordle grid positions in a preview image.

    Two-phase scan to keep Python hot loops small:
      1. Coarse pass (every 4th row) to find rows that contain any tile-colored
         pixels — produces a small candidate list.
      2. Full scan of each candidate row to extract 5-cell player groups; stop
         as soon as a row yields at least one valid group since cells are much
         taller than the sample stride.

    Returns a list of dicts: {grid_x, grid_y, cell_size, pitch_x, pitch_y,
    avatar_cx, avatar_cy, avatar_r}. Empty list if nothing detected.
    """
    w, h = img.size
    pixels = img.load()

    # Inline tile check — 3 squared distances under a tolerance. Hoisting the
    # color constants into locals avoids repeated global lookups in the hot loop.
    gR, gG, gB = _WORDLE_GREEN
    yR, yG, yB = _WORDLE_YELLOW
    aR, aG, aB = _WORDLE_GRAY
    tol2 = 25  # tol=5 squared

    def is_tile(px):
        r, g, b = px
        dr = r - gR; dg = g - gG; db = b - gB
        if dr * dr + dg * dg + db * db < tol2:
            return True
        dr = r - yR; dg = g - yG; db = b - yB
        if dr * dr + dg * dg + db * db < tol2:
            return True
        dr = r - aR; dg = g - aG; db = b - aB
        return dr * dr + dg * dg + db * db < tol2

    def cell_runs_on_row(y):
        runs = []
        in_run = False
        start = 0
        for x in range(w):
            if is_tile(pixels[x, y]):
                if not in_run:
                    start = x
                    in_run = True
            elif in_run:
                if x - start >= 3:
                    runs.append((start, x - 1))
                in_run = False
        if in_run and w - start >= 3:
            runs.append((start, w - 1))
        return runs

    def group_by_player(runs):
        if not runs:
            return []
        bands = []
        cur = [runs[0]]
        for r in runs[1:]:
            # Cells in same grid have gaps < 4px (1px stride gap). Player separators are wider.
            if r[0] - cur[-1][1] < 8:
                cur.append(r)
            else:
                bands.append(cur)
                cur = [r]
        bands.append(cur)
        valid = []
        for band in bands:
            if len(band) != 5:
                continue
            widths = [r[1] - r[0] + 1 for r in band]
            if max(widths) - min(widths) <= 2:
                valid.append(band)
        return valid

    # Phase 1: coarse pass — find rows with tile pixels. Cells are ≥15px tall,
    # so sampling every 4 rows cannot miss a grid row entirely.
    candidate_ys = []
    for y in range(0, h, 4):
        for x in range(0, w, 8):  # also stride horizontally — cells are ≥15px wide
            if is_tile(pixels[x, y]):
                candidate_ys.append(y)
                break

    # Phase 2: full scan of each candidate row; keep best
    best_y = None
    best_bands = []
    for y in candidate_ys:
        runs = cell_runs_on_row(y)
        if not runs:
            continue
        bands = group_by_player(runs)
        if len(bands) > len(best_bands):
            best_bands = bands
            best_y = y

    if not best_bands:
        return []

    grids = []
    for band in best_bands:
        grid_x = band[0][0]
        cell_size = band[0][1] - band[0][0] + 1
        pitch_x = band[1][0] - band[0][0]
        cx = (band[0][0] + band[0][1]) // 2

        # Walk column cx top-to-bottom, find tile runs (each = one row of cells)
        tile_rows = []
        in_run = False
        rs = 0
        for y in range(h):
            if is_tile(pixels[cx, y]):
                if not in_run:
                    rs = y
                    in_run = True
            elif in_run:
                if abs((y - rs) - cell_size) <= 2:
                    tile_rows.append((rs, y - 1))
                in_run = False
        if in_run and abs((h - rs) - cell_size) <= 2:
            tile_rows.append((rs, h - 1))

        if not tile_rows:
            continue

        grid_y = tile_rows[0][0]
        pitch_y = tile_rows[1][0] - tile_rows[0][0] if len(tile_rows) > 1 else pitch_x

        # Multi-player layouts stack the avatar circle directly above each grid.
        # Avatar diameter ≈ grid width; sits with a small gap above grid_y.
        grid_width = pitch_x * 5 - (pitch_x - cell_size)
        avatar_r = grid_width // 2
        avatar_cx = grid_x + grid_width // 2
        avatar_cy = max(0, grid_y - avatar_r - 18)

        grids.append({
            'grid_x': grid_x,
            'grid_y': grid_y,
            'cell_size': cell_size,
            'pitch_x': pitch_x,
            'pitch_y': pitch_y,
            'avatar_cx': avatar_cx,
            'avatar_cy': avatar_cy,
            'avatar_r': avatar_r,
        })

    return grids


def _parse_single_grid(img, grid_x, grid_y, cell_size, pitch_x, pitch_y):
    """Read a single 5x6 Wordle grid.

    Returns:
        int 1..6: solved in that many guesses
        7: X/6 (6 rows filled, last row not all green)
        -1: in progress (some rows filled, not solved, fewer than 6)
        None: empty (no rows filled)
    """
    filled_rows = 0
    last_row_all_green = False
    for row in range(6):
        cy = grid_y + row * pitch_y + cell_size // 2
        row_colors = [
            _classify_cell(img.getpixel((grid_x + col * pitch_x + cell_size // 2, cy)))
            for col in range(5)
        ]
        if all(c == 'empty' for c in row_colors):
            break
        filled_rows += 1
        last_row_all_green = all(c == 'green' for c in row_colors)

    if filled_rows == 0:
        return None
    if last_row_all_green:
        return filled_rows
    if filled_rows == 6:
        return DEFAULT_WORDLE_TOTAL + 1  # X/6
    return -1  # in progress


def _avatar_ahash(img_crop):
    """8x8 grayscale average hash over the inscribed square of the crop.

    Taking an inscribed square discards the corners, which are the part most
    affected by the Wordle bot's circular avatar mask — the rendered crop has
    black corners while the reference CDN avatar has image pixels in the
    corners, and matching those directly blows up the hamming distance.
    """
    w, h = img_crop.size
    side = min(w, h)
    inscribed = int(side * 0.707)  # sqrt(2)/2 — inscribed square of the circle
    left = (w - inscribed) // 2
    upper = (h - inscribed) // 2
    inner = img_crop.crop((left, upper, left + inscribed, upper + inscribed))
    small = inner.convert('L').resize((8, 8))
    pixels = list(small.getdata())
    avg = sum(pixels) / 64
    bits = 0
    for i, p in enumerate(pixels):
        if p > avg:
            bits |= 1 << i
    return bits


def _hamming(a, b):
    return bin(a ^ b).count('1')


def _match_avatar(img, grid, candidate_hashes, max_distance=18, margin=4):
    """Crop the avatar at grid position, compare against candidate hashes.

    Returns matched user_id or None. Requires the best match to beat the
    second-best by at least `margin` bits to guard against default-avatar
    look-alikes.
    """
    if not candidate_hashes:
        return None
    cx, cy, r = grid['avatar_cx'], grid['avatar_cy'], grid['avatar_r']
    w, h = img.size
    left = max(0, cx - r)
    upper = max(0, cy - r)
    right = min(w, cx + r)
    lower = min(h, cy + r)
    if right - left < 8 or lower - upper < 8:
        return None
    crop = img.crop((left, upper, right, lower))
    crop_hash = _avatar_ahash(crop)

    scored = [(uid, _hamming(crop_hash, h)) for uid, h in candidate_hashes.items()]
    scored.sort(key=lambda x: x[1])
    best_uid, best_d = scored[0]
    if best_d > max_distance:
        return None
    if len(scored) > 1 and scored[1][1] - best_d < margin:
        return None
    return best_uid


def parse_wordle_image(image_bytes, candidate_hashes=None):
    """Parse a Wordle bot preview image.

    Returns a list of (user_id_or_None, score) pairs for every finished grid we
    could attribute. For single-player images, yields [(None, score)] and the
    caller assigns the user from message metadata. For multi-player images,
    yields one entry per grid that we matched to a candidate user. Unfinished
    grids and unmatchable grids are dropped.
    """
    from PIL import Image
    import io

    img = Image.open(io.BytesIO(image_bytes)).convert('RGB')
    grids = _detect_grids(img)
    if not grids:
        return []

    results = []
    if len(grids) == 1:
        g = grids[0]
        score = _parse_single_grid(img, g['grid_x'], g['grid_y'], g['cell_size'], g['pitch_x'], g['pitch_y'])
        if score is not None and score != -1:
            results.append((None, score))
        return results

    for g in grids:
        score = _parse_single_grid(img, g['grid_x'], g['grid_y'], g['cell_size'], g['pitch_x'], g['pitch_y'])
        if score is None or score == -1:
            continue
        uid = _match_avatar(img, g, candidate_hashes)
        if uid is None:
            continue
        results.append((uid, score))
    return results


_wordle_fetch_session = None


def _get_wordle_fetch_session():
    """Lazy module-level Session so CDN connections are reused across calls."""
    global _wordle_fetch_session
    if _wordle_fetch_session is None:
        import requests
        _wordle_fetch_session = requests.Session()
        _wordle_fetch_session.mount(
            'https://',
            requests.adapters.HTTPAdapter(pool_connections=2, pool_maxsize=8),
        )
    return _wordle_fetch_session


def parse_wordle_attachment(attachment, candidate_hashes=None):
    """Download and parse a Wordle bot image attachment.

    Returns list of (user_id_or_None, score) pairs; empty list on skip/failure.
    Skips recap images (streak summaries) — those use "solved" in their
    description rather than "finished"/"unfinished".
    """
    if not attachment.get('content_type', '').startswith('image/'):
        return []
    desc = attachment.get('description', '')
    if 'finished' not in desc:
        return []
    try:
        img_response = _get_wordle_fetch_session().get(attachment['url'], timeout=4)
        img_response.raise_for_status()
        return parse_wordle_image(img_response.content, candidate_hashes)
    except Exception:
        return []


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


def build_games(puzzle_numbers):
    """Build the single ordered list of Game descriptors.

    Order is parse priority and is load-bearing: maptap_challenge must precede
    maptap, whose '(.*)MapTap(.*)' pattern would otherwise swallow challenge
    messages (match_message returns on the first hit). The scoreboard re-sorts by
    player count then title at render time, so this order does not affect display.
    Games in DISABLED_GAMES are dropped here, so they are skipped by both the
    parser and the scoreboard.
    """
    pn = puzzle_numbers
    bandle_total = pn.get('bandle_total', DEFAULT_BANDLE_TOTAL)
    quizl_total = pn.get('quizl_total', DEFAULT_QUIZL_TOTAL)

    games = [
        Game('connections', '🔗', 'Connections', 'connections', 4, pn['connections_puzzle_number'], CONNECTIONS_LINK,
             re.compile(rf'Connections.*?Puzzle #{pn["connections_puzzle_number"]}', re.IGNORECASE | re.DOTALL)),
        Game('bandle', '🎵', 'Bandle', 'guesses', bandle_total, pn['bandle_puzzle_number'], BANDLE_LINK,
             re.compile(rf'Bandle #{pn["bandle_puzzle_number"]} (\d+|x)/(\d+)', re.IGNORECASE)),
        Game('sports', '🏈', 'Sports Connections', 'connections', 4, pn['sports_puzzle_number'], SPORTS_CONNECTIONS_LINK,
             re.compile(rf'Connections: Sports Edition.*? #{pn["sports_puzzle_number"]}', re.IGNORECASE | re.DOTALL)),
        Game('pips', '🎲', 'Pips', 'time', 0, pn['pips_puzzle_number'], PIPS_LINK,
             re.compile(rf'Pips #{pn["pips_puzzle_number"]} Hard', re.IGNORECASE)),
        Game('maptap_challenge', '⚡', 'MapTap Challenge', 'maptap', 0, pn['maptap_number'], MAPTAP_CHALLENGE_LINK,
             re.compile(rf'MapTap Challenge Round.*{pn["maptap_challenge_date"]}', re.IGNORECASE)),
        Game('maptap', '🎯', 'MapTap', 'maptap', 0, pn['maptap_number'], MAPTAP_LINK,
             re.compile(rf'(.*)MapTap(.*){pn["maptap_date"]}', re.IGNORECASE)),
        Game('chronophoto', '📷', 'Chronophoto', 'score', 0, pn['chronophoto_number'], CHRONOPHOTO_LINK,
             re.compile(rf"I got a score of (\d+) on today's Chronophoto: {re.escape(pn['chronophoto_number'])}", re.IGNORECASE),
             search_pattern=re.compile(re.escape(pn["chronophoto_number"]), re.IGNORECASE)),
        Game('globle', '🌍', 'Globle', 'guesses', 0, f'{pn["globle_number"]}', GLOBLE_LINK,
             re.compile(r"I guessed today['\u2019]s Globle in (\d+) tr", re.IGNORECASE), needs_timestamp=True),
        Game('worldle', '🗺️', 'Worldle', 'guesses', 0, f'{pn["worldle_number"]}', WORLDLE_LINK,
             re.compile(r"I guessed today['\u2019]s Worldle in (\d+) tr", re.IGNORECASE), needs_timestamp=True),
        Game('flagle', '🏁', 'Flagle', 'guesses', 0, f'{pn["flagle_number"]}', FLAGLE_LINK,
             re.compile(r"I guessed today['\u2019]s Flag in (\d+) tr", re.IGNORECASE), needs_timestamp=True),
        Game('quizl', '⁉️', 'Quizl', 'score', quizl_total, pn['quizl_puzzle_number'], QUIZL_LINK,
             re.compile(rf'Quizl#{pn["quizl_puzzle_number"]}', re.IGNORECASE)),
        Game('wordle', '📗', 'Wordle', 'guesses', DEFAULT_WORDLE_TOTAL, pn['wordle_puzzle_number'], WORDLE_LINK,
             re.compile(rf'Wordle\s+{pn["wordle_puzzle_number"]:,}\s+([1-6X])/6', re.IGNORECASE)),
        Game('travle', '✈️', 'Travle', 'travle', 0, pn['travle_puzzle_number'], TRAVLE_LINK,
             re.compile(rf'#travle\s+#{pn["travle_puzzle_number"]}\s+(?:\+(\d+)|\((\d+)\s+away\))[^\n]*(?:\n([^\n]*))?', re.IGNORECASE)),
        Game('dialed_color', '🎨', 'Color', 'score', 50, f'{pn["dialed_number"]}', DIALED_COLOR_LINK,
             re.compile(r'dialed\.gg/\?\S*&s=(\d+(?:\.\d+)?)', re.IGNORECASE), needs_timestamp=True),
        Game('dialed_sound', '🔊', 'Sound', 'score', 50, f'{pn["dialed_number"]}', DIALED_SOUND_LINK,
             re.compile(r'dialed\.gg/sound\?\S*&s=(\d+(?:\.\d+)?)', re.IGNORECASE), needs_timestamp=True),
        Game('dialed_color2', '🎭', 'Pop Culture Colors', 'score', 50, f'{pn["dialed_number"]}', DIALED_COLOR2_LINK,
             re.compile(r'dialed\.gg/color2\?\S*&s=(\d+(?:\.\d+)?)', re.IGNORECASE), needs_timestamp=True),
    ]
    return [g for g in games if g.key not in DISABLED_GAMES]


def match_message(msg, games, timestamp_checker, wordle_bot_id=None, avatar_hashes=None):
    """Run a single message through all games, including Wordle bot image parsing.

    Returns a list of (game_key, score, metadata, user_id_override) tuples.
    user_id_override is None for everything except multi-player Wordle bot images,
    where each entry is attributed to the user matched by avatar.
    Returns [] if no match.
    """
    content = msg['content']
    timestamp = msg['timestamp']

    for game in games:
        key = game.key

        # For chronophoto, use search_pattern for initial check
        if key == 'chronophoto':
            if not game.search_pattern.search(content):
                continue
            match = game.pattern.search(content)
            if match:
                if game.needs_timestamp and not timestamp_checker(timestamp):
                    continue
                return [(key, int(match.group(1)), {}, None)]
            continue

        match = game.pattern.search(content)
        if not match:
            continue

        if game.needs_timestamp and not timestamp_checker(timestamp):
            continue

        metadata = {}

        if key == 'connections':
            return [(key, get_connections_results(content), metadata, None)]
        elif key == 'bandle':
            score_str = match.group(1)
            total = int(match.group(2))
            metadata['bandle_total'] = total
            score = total + 1 if score_str == 'x' else int(score_str)
            return [(key, score, metadata, None)]
        elif key == 'sports':
            return [(key, get_connections_results(content), metadata, None)]
        elif key == 'pips':
            pips_match = re.search(r'(\d+):(\d+)', content, re.IGNORECASE)
            if pips_match:
                minutes = int(pips_match.group(1))
                seconds = int(pips_match.group(2))
                return [(key, minutes * 60 + seconds, metadata, None)]
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
                return [(key, (weighted_score, raw_score), metadata, None)]
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
                return [(key, (weighted_score, raw_score), metadata, None)]
        elif key in ('globle', 'worldle', 'flagle'):
            return [(key, int(match.group(1)), metadata, None)]
        elif key == 'quizl':
            score = len(re.findall(r'🟩', content))
            return [(key, score, metadata, None)]
        elif key == 'wordle':
            score_str = match.group(1)
            score = DEFAULT_WORDLE_TOTAL + 1 if score_str.upper() == 'X' else int(score_str)
            return [(key, score, metadata, None)]
        elif key == 'travle':
            plus_str = match.group(1)
            away_str = match.group(2)
            squares = match.group(3) or ''
            checkmarks = squares.count('✅')  # path countries guessed in-order
            # Encode as (tier, n, -checkmarks): 0=solved(+N), 1=failed but got
            # at least one correct country (✅ or 🟩), 2=complete wiff (no greens).
            # Negate checkmarks so natural ascending tuple order ranks more ✅
            # higher within the same +N (tiebreaker for in-order distinction).
            if plus_str is not None:
                return [(key, (0, int(plus_str), -checkmarks), metadata, None)]
            tier = 1 if (checkmarks or '🟩' in squares) else 2
            return [(key, (tier, int(away_str), -checkmarks), metadata, None)]
        elif key in ('dialed_color', 'dialed_sound', 'dialed_color2'):
            # Score (e.g. 45.32) comes from the share URL's &s= param, also
            # shown as "<score>/50" in the message text.
            return [(key, float(match.group(1)), metadata, None)]

    # Wordle bot image parsing
    if (wordle_bot_id
            and msg['author']['id'] == wordle_bot_id
            and msg.get('attachments')
            and timestamp_checker(timestamp)):
        for attachment in msg['attachments']:
            pairs = parse_wordle_attachment(attachment, avatar_hashes)
            if pairs:
                return [('wordle', score, {}, uid) for uid, score in pairs]

    return []


def compute_points(results, puzzle_numbers, minimum_players=1):
    """Compute total points per user across all games.

    Scoring: in a game with N players, 1st place gets N points and each place
    below earns one fewer, so last place gets 1 (and the sole player in a
    1-player game gets 1). Poop scores (failed games) earn 0 points. Ties share
    the higher rank's points, matching the standard ranking used by the
    scoreboard display.

    Returns {user_id: int}.
    """
    games = build_games(puzzle_numbers)
    points = defaultdict(int)

    for game in games:
        game_key, metric, total = game.key, game.metric, game.total
        if game_key not in results or not results[game_key] or len(results[game_key]) < minimum_players:
            continue

        # Sort players using the same keys as format_scoreboard
        if metric == 'connections':
            players = sorted(results[game_key].items(), key=lambda x: (x[1][0], -x[1][1]))
        elif metric == 'score':
            players = sorted(results[game_key].items(), key=lambda x: (-x[1]))
        elif metric == 'maptap':
            players = sorted(results[game_key].items(), key=lambda x: (-x[1][0], -x[1][1]))
        else:
            players = sorted(results[game_key].items(), key=lambda x: x[1])

        n = len(players)

        # Walk with tie-aware ranking
        rank = 0
        prev_score = None
        i = 0
        while i < len(players):
            current_score = players[i][1]
            if current_score != prev_score:
                rank = i + 1

            # Check for poop override (no points)
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
                if current_score[0] == 0:
                    is_poop = True
            elif metric == 'travle':
                if current_score[0] == 2:
                    is_poop = True

            # Collect tied players
            j = i + 1
            while j < len(players) and players[j][1] == current_score:
                j += 1

            if not is_poop:
                player_points = n - rank + 1
                for k in range(i, j):
                    points[players[k][0]] += player_points

            prev_score = current_score
            i = j

    return dict(points)


def format_points_summary(points):
    """Format the points summary section.

    Returns empty string if no points earned.
    """
    users_with_points = {uid: p for uid, p in points.items() if p > 0}

    if not users_with_points:
        return ''

    sorted_users = sorted(users_with_points.items(), key=lambda x: -x[1])

    medals = ['👑', '🥈', '🥉']
    message = ''

    rank = 0
    prev_val = None
    i = 0
    while i < len(sorted_users):
        current_val = sorted_users[i][1]
        if current_val != prev_val:
            rank = i + 1

        j = i + 1
        while j < len(sorted_users) and sorted_users[j][1] == current_val:
            j += 1

        medal = f"{medals[rank - 1]} " if rank <= len(medals) else ""
        unit = 'pt' if current_val == 1 else 'pts'
        for k in range(i, j):
            uid = sorted_users[k][0]
            message += f'{medal}<@{uid}>: {current_val} {unit}\n'

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
        # Rank by the default (weighted) score; the unweighted raw score is only
        # a tiebreaker, and is shown only where a weighted score is tied.
        sorted_players = sorted(game_scores.items(), key=lambda x: (-x[1][0], -x[1][1]))
        weighted_counts = Counter(v[0] for v in game_scores.values())
        rank = 0
        prev_val = None
        i = 0
        while i < len(sorted_players):
            weighted = sorted_players[i][1][0]
            unweighted = sorted_players[i][1][1]
            score_tuple = (weighted, unweighted)
            if score_tuple != prev_val:
                rank = i + 1
            tied = [f'<@{sorted_players[i][0]}>']
            j = i + 1
            while j < len(sorted_players) and (sorted_players[j][1][0], sorted_players[j][1][1]) == score_tuple:
                tied.append(f'<@{sorted_players[j][0]}>')
                j += 1
            medal = f"{medals[rank - 1]} " if rank <= len(medals) else ""
            if weighted == 0:
                medal = '💩 '
            players_str = " ".join(reversed(tied))
            if weighted_counts[weighted] > 1:
                lines += f'{medal}{players_str}: {weighted} ({unweighted} unweighted)\n'
            else:
                lines += f'{medal}{players_str}: {weighted}\n'
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
        elif metric == 'travle':
            tier, n, neg_cm = current_score
            k = -neg_cm
            if tier == 0:
                score_str = f"+{n} ({k}✅)"
            elif tier == 1:
                score_str = f"{n} away" + (f" ({k}✅)" if k else "")
            else:  # tier == 2: complete wiff
                medal = '💩 '
                score_str = f"{n} away"
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
    games = build_games(puzzle_numbers)
    message = f"🧮 **{title}**"
    if not results:
        message += "\n\nNo results found!"
    else:
        message += f" - {reference_date.strftime('%B %d, %Y')}\n\n"
        points = compute_points(results, puzzle_numbers, minimum_players)
        points_section = format_points_summary(points)
        if points_section:
            message += points_section
        games.sort(key=lambda g: (-len(results.get(g.key, {})), g.title.lower()))
        for game in games:
            if game.key not in results or not results[game.key] or len(results[game.key]) < minimum_players:
                continue

            title_link = f"[{game.title}]({game.url})"
            message += f'**{title_link} {game.emoji} {f"#{game.puzzle}" if type(game.puzzle) == int else f"#67"}**\n'
            message += _format_game_players(results[game.key], game.metric, game.total)
            message += "\n"
    return message


MEDAL_COLOR = 15844367  # dark gold

def format_scoreboard_components(results, reference_date, puzzle_numbers, title="Daily Game Scoreboard", minimum_players=1):
    """Format the scoreboard as Discord Components V2 (list of top-level components).

    Returns a list[dict] suitable for the 'components' field in a Discord message.
    """
    games = build_games(puzzle_numbers)
    components = []

    # --- Header container ---
    header_text = f"### 🧮 {title} - {reference_date.strftime('%B %d, %Y')}"

    if not results:
        return [{"type": 17, "accent_color": HEADER_COLOR, "components": [
            {"type": 10, "content": header_text},
            {"type": 10, "content": "No results found!"},
        ]}]

    # --- Points container (gold accent) ---
    points = compute_points(results, puzzle_numbers, minimum_players)
    points_section = format_points_summary(points)
    if points_section:
        components.append({"type": 17, "accent_color": HEADER_COLOR, "components": [
            {"type": 10, "content": header_text},
            {"type": 10, "content": points_section.rstrip('\n')},
        ]})
    else:
        components.append({"type": 17, "accent_color": OTHER_GAMES_COLOR, "components": [
            {"type": 10, "content": header_text},
        ]})

    # Sort games by player count descending, then title alphabetically
    games.sort(key=lambda g: (-len(results.get(g.key, {})), g.title.lower()))

    qualified = [g for g in games if g.key in results and results[g.key] and len(results[g.key]) >= minimum_players]

    # --- Scores container ---
    scores_children = []
    for g_idx, game in enumerate(qualified):
        if g_idx > 0:
            scores_children.append({"type": 14, "spacing": 1})  # Separator
        puzzle_label = f"#{game.puzzle}" if type(game.puzzle) == int else "#67"
        score_text = f"**[{game.title}]({game.url}) {game.emoji} {puzzle_label}**\n"
        score_text += _format_game_players(results[game.key], game.metric, game.total).rstrip('\n')
        scores_children.append({"type": 10, "content": score_text})

    if scores_children:
        components.append({"type": 17, "accent_color": OTHER_GAMES_COLOR, "components": scores_children})

    return components

