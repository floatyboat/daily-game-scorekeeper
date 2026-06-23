import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from dateutil import parser as dateutil_parser
from collections import defaultdict, Counter
from dataclasses import dataclass

# Accent color constants (Discord integer colors) for the scoreboard containers.
HEADER_COLOR = 16766720       # gold
OTHER_GAMES_COLOR = 10395294  # gray

# Wordle's guess limit. Shared by the Wordle game spec and the standalone
# bot-image grid parser (_parse_single_grid), so it stays a module constant.
DEFAULT_WORDLE_TOTAL = 6


@dataclass
class Game:
    """One tracked game resolved for a specific date.

    Produced by build_games() from a GameSpec: display metadata plus the
    compiled regex and the score extractor. Consumed by both the scoreboard
    (emoji/title/metric/total/puzzle/url) and the parser (pattern/parse/
    needs_timestamp/search_pattern).
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
    search_pattern: re.Pattern = None   # optional cheap pre-check before the full pattern
    parse: object = None                # callable(match, content) -> (score, metadata)


def compute_puzzle_numbers(reference_date):
    """Build the render context threaded through build_games / format_*.

    Each game computes its own puzzle number from reference_date, so this only
    carries the date plus default totals for games whose total can be overridden
    by a parsed message (bandle). match_message returns those overrides in its
    metadata dict, which callers merge back via puzzle_numbers.update(metadata).
    """
    pn = {'reference_date': reference_date}
    for spec in GAME_SPECS:
        if spec.total_key:
            pn[spec.total_key] = spec.total
    return pn


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


@dataclass
class GameSpec:
    """A game's complete definition -- THE single place to add or change a game.

    build_games() resolves each spec into a Game for a given reference date:
      puzzle(reference_date)          -> displayed puzzle number (int) or label (str)
      pattern(reference_date, puzzle) -> compiled regex matched against a message
      parse(match, content)           -> (score, metadata) once the pattern matches;
                                         return (None, {}) to decline and let other
                                         games try the same message
    Optional:
      search    cheap pre-filter regex builder (same signature as pattern); if set,
                it must match before the full pattern is attempted
      total_key puzzle_numbers slot whose value overrides `total` (bandle's total
                comes off the message); parse must emit it in its metadata
      disabled  drop the game from parsing AND the scoreboard entirely
    """
    key: str
    emoji: str
    title: str
    metric: str
    total: int
    url: str
    puzzle: object              # callable(reference_date) -> int | str
    pattern: object             # callable(reference_date, puzzle) -> re.Pattern
    parse: object               # callable(match, content) -> (score, metadata)
    needs_timestamp: bool = False
    search: object = None       # callable(reference_date, puzzle) -> re.Pattern
    total_key: str = None       # puzzle_numbers key that overrides `total`
    disabled: bool = False


# --- Per-game score extractors -------------------------------------------------
# Trivial extractions are written inline in the specs below; these cover games
# whose scoring needs more than one expression. Each returns (score, metadata),
# or (None, {}) to decline the message after a pattern match (so another game may
# still claim it).

def _parse_bandle(m, content):
    score_str = m.group(1)
    total = int(m.group(2))
    score = total + 1 if score_str == 'x' else int(score_str)
    return score, {'bandle_total': total}


def _parse_pips(m, content):
    pips_match = re.search(r'(\d+):(\d+)', content, re.IGNORECASE)
    if not pips_match:
        return None, {}
    minutes = int(pips_match.group(1))
    seconds = int(pips_match.group(2))
    return minutes * 60 + seconds, {}


def _parse_maptap_challenge(m, content):
    score_match = re.search(r'Score: (\d+)', content, re.IGNORECASE)
    if not score_match:
        return None, {}
    weighted_score = int(score_match.group(1))
    raw_score = weighted_score
    for line in content.split('\n'):
        if 'score' in line.lower() or 'maptap' in line.lower():
            continue
        nums = re.findall(r'\d+', line)
        if len(nums) >= 3:
            raw_score = sum(int(n) for n in nums)
            break
    return (weighted_score, raw_score), {}


def _parse_maptap(m, content):
    score_match = re.search(r'Final Score: (\d+)', content, re.IGNORECASE)
    if not score_match:
        return None, {}
    weighted_score = int(score_match.group(1))
    # Parse individual round scores from the emoji line: it has multiple numbers
    # interspersed with emojis; their sum is the unweighted raw score.
    raw_score = weighted_score
    for line in content.split('\n'):
        if 'final score' in line.lower() or 'maptap' in line.lower():
            continue
        nums = re.findall(r'\d+', line)
        if len(nums) >= 3:
            raw_score = sum(int(n) for n in nums)
            break
    return (weighted_score, raw_score), {}


def _parse_quizl(m, content):
    return len(re.findall('\U0001F7E9', content)), {}   # count green squares


def _parse_wordle(m, content):
    score_str = m.group(1)
    score = DEFAULT_WORDLE_TOTAL + 1 if score_str.upper() == 'X' else int(score_str)
    return score, {}


def _parse_travle(m, content):
    plus_str = m.group(1)
    away_str = m.group(2)
    hints = int(m.group(3)) if m.group(3) else 0
    squares = m.group(4) or ''
    checkmarks = squares.count('✅')  # path countries guessed in-order (check mark)
    # Escalating hint penalty (+1/+2/+3 per successive hint, since hint 2 reveals
    # all outlines and hint 3 adds initials) folded into the +N/away count, so
    # hint-assisted results rank below clean ones on the same currency as wrong
    # guesses. Triangular: 0/1/3/6 for 0-3 hints.
    penalty = hints * (hints + 1) // 2
    # Encode as (tier, effective_n, hints, -checkmarks): 0=solved(+N), 1=failed
    # but got at least one correct country (check or green), 2=complete wiff (no
    # greens). hints is a tiebreak (fewer ranks higher at equal effective_n);
    # raw +N = effective_n - penalty. Negate checkmarks so ascending tuple order
    # ranks more checks higher (in-order tiebreaker).
    if plus_str is not None:
        return (0, int(plus_str) + penalty, hints, -checkmarks), {}
    tier = 1 if (checkmarks or '\U0001F7E9' in squares) else 2
    return (tier, int(away_str) + penalty, hints, -checkmarks), {}


# --- The single source of truth ------------------------------------------------
# List order is PARSE PRIORITY and is load-bearing: maptap_challenge must precede
# maptap, whose '(.*)MapTap(.*)' pattern would otherwise swallow challenge
# messages (match_message returns on the first hit). The scoreboard re-sorts by
# player count then title at render time, so order does not affect display.

GAME_SPECS = [
    GameSpec(
        key='connections', emoji='🔗', title='Connections', metric='connections',
        total=4, url='https://www.nytimes.com/games/connections',
        puzzle=lambda ref: (ref - datetime(2023, 6, 12)).days + 1,
        pattern=lambda ref, n: re.compile(rf'Connections.*?Puzzle #{n}', re.IGNORECASE | re.DOTALL),
        parse=lambda m, c: (get_connections_results(c), {}),
    ),
    GameSpec(
        key='bandle', emoji='🎵', title='Bandle', metric='guesses',
        total=6, total_key='bandle_total', url='https://bandle.app/daily',
        puzzle=lambda ref: (ref - datetime(2022, 8, 18)).days + 1,
        pattern=lambda ref, n: re.compile(rf'Bandle #{n} (\d+|x)/(\d+)', re.IGNORECASE),
        parse=_parse_bandle,
    ),
    GameSpec(
        key='sports', emoji='🏈', title='Sports Connections', metric='connections',
        total=4, url='https://www.nytimes.com/athletic/connections-sports-edition',
        puzzle=lambda ref: (ref - datetime(2024, 9, 24)).days + 1,
        pattern=lambda ref, n: re.compile(rf'Connections: Sports Edition.*? #{n}', re.IGNORECASE | re.DOTALL),
        parse=lambda m, c: (get_connections_results(c), {}),
    ),
    GameSpec(
        key='pips', emoji='🎲', title='Pips', metric='time',
        total=0, url='https://www.nytimes.com/games/pips',
        puzzle=lambda ref: (ref - datetime(2025, 8, 18)).days + 1,
        pattern=lambda ref, n: re.compile(rf'Pips #{n} Hard', re.IGNORECASE),
        parse=_parse_pips,
    ),
    GameSpec(
        key='maptap_challenge', emoji='⚡', title='MapTap Challenge', metric='maptap',
        total=0, url='https://maptap.gg/adventures?gametype=challenge', disabled=True,
        puzzle=lambda ref: (ref - datetime(2024, 6, 22)).days + 1,
        pattern=lambda ref, n: re.compile(rf'MapTap Challenge Round.*{ref.strftime("%b")} {ref.day}', re.IGNORECASE),
        parse=_parse_maptap_challenge,
    ),
    GameSpec(
        key='maptap', emoji='🎯', title='MapTap', metric='maptap',
        total=0, url='https://maptap.gg',
        puzzle=lambda ref: (ref - datetime(2024, 6, 22)).days + 1,
        pattern=lambda ref, n: re.compile(rf'(.*)MapTap(.*){ref.strftime("%B")} {ref.day}', re.IGNORECASE),
        parse=_parse_maptap,
    ),
    GameSpec(
        key='chronophoto', emoji='📷', title='Chronophoto', metric='score',
        total=0, url='https://www.chronophoto.app/daily.html',
        puzzle=lambda ref: f'{ref.month}/{ref.day}/{ref.year}',
        pattern=lambda ref, n: re.compile(rf"I got a score of (\d+) on today's Chronophoto: {re.escape(n)}", re.IGNORECASE),
        search=lambda ref, n: re.compile(re.escape(n), re.IGNORECASE),
        parse=lambda m, c: (int(m.group(1)), {}),
    ),
    GameSpec(
        key='globle', emoji='🌍', title='Globle', metric='guesses',
        total=0, url='https://globle.org', needs_timestamp=True, disabled=True,
        puzzle=lambda ref: f'{ref.strftime("%B")} {ref.day}',
        pattern=lambda ref, n: re.compile(r"I guessed today['’]s Globle in (\d+) tr", re.IGNORECASE),
        parse=lambda m, c: (int(m.group(1)), {}),
    ),
    GameSpec(
        key='worldle', emoji='🗺️', title='Worldle', metric='guesses',
        total=0, url='https://worldlegame.io', needs_timestamp=True, disabled=True,
        puzzle=lambda ref: f'{ref.strftime("%B")} {ref.day}',
        pattern=lambda ref, n: re.compile(r"I guessed today['’]s Worldle in (\d+) tr", re.IGNORECASE),
        parse=lambda m, c: (int(m.group(1)), {}),
    ),
    GameSpec(
        key='flagle', emoji='🏁', title='Flagle', metric='guesses',
        total=0, url='https://flagle.org', needs_timestamp=True, disabled=True,
        puzzle=lambda ref: f'{ref.strftime("%B")} {ref.day}',
        pattern=lambda ref, n: re.compile(r"I guessed today['’]s Flag in (\d+) tr", re.IGNORECASE),
        parse=lambda m, c: (int(m.group(1)), {}),
    ),
    GameSpec(
        key='quizl', emoji='⁉️', title='Quizl', metric='score',
        total=5, url='https://quizl.io',
        puzzle=lambda ref: (ref - datetime(2022, 3, 16)).days + 1,
        pattern=lambda ref, n: re.compile(rf'Quizl#{n}', re.IGNORECASE),
        parse=_parse_quizl,
    ),
    GameSpec(
        key='wordle', emoji='📗', title='Wordle', metric='guesses',
        total=DEFAULT_WORDLE_TOTAL, url='https://www.nytimes.com/games/wordle',
        puzzle=lambda ref: (ref - datetime(2021, 6, 19)).days,
        pattern=lambda ref, n: re.compile(rf'Wordle\s+{n:,}\s+([1-6X])/6', re.IGNORECASE),
        parse=_parse_wordle,
    ),
    GameSpec(
        key='travle', emoji='✈️', title='Travle', metric='travle',
        total=0, url='https://travle.earth',
        puzzle=lambda ref: (ref - datetime(2022, 12, 15)).days + 1,
        pattern=lambda ref, n: re.compile(rf'#travle\s+#{n}\s+(?:\+(\d+)|\((\d+)\s+away\))(?:[^\n]*?\((\d+)\s+hints?\))?[^\n]*(?:\n([^\n]*))?', re.IGNORECASE),
        parse=_parse_travle,
    ),
    GameSpec(
        key='dialed_color', emoji='🎨', title='Color', metric='score',
        total=50, url='https://dialed.gg/?d=1', needs_timestamp=True, disabled=True,
        puzzle=lambda ref: f'{ref.strftime("%B")} {ref.day}',
        pattern=lambda ref, n: re.compile(r'dialed\.gg/\?\S*&s=(\d+(?:\.\d+)?)', re.IGNORECASE),
        parse=lambda m, c: (float(m.group(1)), {}),
    ),
    GameSpec(
        key='dialed_sound', emoji='🔊', title='Sound', metric='score',
        total=50, url='https://dialed.gg/sound?d=1', needs_timestamp=True, disabled=True,
        puzzle=lambda ref: f'{ref.strftime("%B")} {ref.day}',
        pattern=lambda ref, n: re.compile(r'dialed\.gg/sound\?\S*&s=(\d+(?:\.\d+)?)', re.IGNORECASE),
        parse=lambda m, c: (float(m.group(1)), {}),
    ),
    GameSpec(
        key='dialed_color2', emoji='🎭', title='Pop Culture Colors', metric='score',
        total=50, url='https://dialed.gg/color2?d=1', needs_timestamp=True,
        puzzle=lambda ref: f'{ref.strftime("%B")} {ref.day}',
        pattern=lambda ref, n: re.compile(r'dialed\.gg/color2\?\S*&s=(\d+(?:\.\d+)?)', re.IGNORECASE),
        parse=lambda m, c: (float(m.group(1)), {}),
    ),
    GameSpec(
        key='enclose', emoji='🐴', title='Enclose', metric='score',
        total=100, url='https://enclose.horse',
        puzzle=lambda ref: (ref - datetime(2025, 12, 30)).days + 1,
        pattern=lambda ref, n: re.compile(rf'enclose\.horse Day {n}\b.*?(\d+)%', re.IGNORECASE | re.DOTALL),
        parse=lambda m, c: (int(m.group(1)), {}),
    ),
]


def build_games(puzzle_numbers):
    """Resolve GAME_SPECS into concrete Game descriptors for one reference date.

    Games flagged disabled are dropped here, so they are skipped by both the
    parser and the scoreboard. GAME_SPECS order (parse priority) is preserved;
    the scoreboard re-sorts for display.
    """
    ref = puzzle_numbers['reference_date']
    games = []
    for spec in GAME_SPECS:
        if spec.disabled:
            continue
        puzzle = spec.puzzle(ref)
        total = puzzle_numbers.get(spec.total_key, spec.total) if spec.total_key else spec.total
        games.append(Game(
            key=spec.key, emoji=spec.emoji, title=spec.title, metric=spec.metric,
            total=total, puzzle=puzzle, url=spec.url,
            pattern=spec.pattern(ref, puzzle),
            needs_timestamp=spec.needs_timestamp,
            search_pattern=spec.search(ref, puzzle) if spec.search else None,
            parse=spec.parse,
        ))
    return games


def match_message(msg, games, timestamp_checker, wordle_bot_id=None, avatar_hashes=None):
    """Run a single message through all games, including Wordle bot image parsing.

    Returns a list of (game_key, score, metadata, user_id_override) tuples.
    user_id_override is None for everything except multi-player Wordle bot images,
    where each entry is attributed to the user matched by avatar. Returns [] if no
    match. The first game whose pattern matches and yields a non-None score wins
    (GAME_SPECS order is parse priority).
    """
    content = msg['content']
    timestamp = msg['timestamp']

    for game in games:
        # Optional cheap pre-filter before the full pattern (chronophoto).
        if game.search_pattern is not None and not game.search_pattern.search(content):
            continue
        match = game.pattern.search(content)
        if not match:
            continue
        if game.needs_timestamp and not timestamp_checker(timestamp):
            continue
        score, metadata = game.parse(match, content)
        if score is None:
            # Pattern matched but no usable score (e.g. pips with no time); let
            # other games try this message.
            continue
        return [(game.key, score, metadata, None)]

    # Wordle bot image parsing -- a separate input path (bot attachments, not text).
    if (wordle_bot_id
            and msg['author']['id'] == wordle_bot_id
            and msg.get('attachments')
            and timestamp_checker(timestamp)):
        for attachment in msg['attachments']:
            pairs = parse_wordle_attachment(attachment, avatar_hashes)
            if pairs:
                return [('wordle', score, {}, uid) for uid, score in pairs]

    return []


def compute_points(results, games, minimum_players=1):
    """Compute total points per user across all games.

    Takes the already-built games list (from build_games) rather than rebuilding
    it, so a single render shares one build instead of resolving GAME_SPECS
    (regex compilation, puzzle-number math) twice.

    Scoring: in a game with N players, 1st place gets N points and each place
    below earns one fewer, so last place gets 1 (and the sole player in a
    1-player game gets 1). Poop scores (failed games) earn 0 points. Ties share
    the higher rank's points, matching the standard ranking used by the
    scoreboard display.

    Returns {user_id: int}.
    """
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
            tier, eff_n, hints, neg_cm = current_score
            k = -neg_cm
            raw_n = eff_n - hints * (hints + 1) // 2  # undo hint penalty for display
            parts = []
            if tier == 0 or k:
                parts.append(f"{k}✅")
            if hints:
                parts.append(f"{hints} hint" + ("s" if hints != 1 else ""))
            extra = f" ({', '.join(parts)})" if parts else ""
            if tier == 0:
                score_str = f"+{raw_n}{extra}"
            elif tier == 1:
                score_str = f"{raw_n} away{extra}"
            else:  # tier == 2: complete wiff
                medal = '💩 '
                score_str = f"{raw_n} away{extra}"
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


def _puzzle_label(puzzle, reference_date):
    """Display label shown after a game's title, e.g. '#1234'.

    Games with a real integer puzzle number show it directly. Date-keyed games
    (puzzle is a non-int, used only for pattern matching) have no puzzle number,
    so we show a date-derived one: the month followed by the zero-padded day, so
    1/20 -> #120, 11/4 -> #1104, 1/3 -> #103.
    """
    if type(puzzle) == int:
        return f'#{puzzle}'
    return f'#{reference_date.month}{reference_date.day:02d}'


def format_scoreboard(results, reference_date, puzzle_numbers, title="Daily Game Scoreboard", minimum_players=1):
    """Format the scoreboard message. Parameterized version of format_message()."""
    games = build_games(puzzle_numbers)
    message = f"🧮 **{title}**"
    if not results:
        message += "\n\nNo results found!"
    else:
        message += f" - {reference_date.strftime('%B %d, %Y')}\n\n"
        points = compute_points(results, games, minimum_players)
        points_section = format_points_summary(points)
        if points_section:
            message += points_section
        games.sort(key=lambda g: (-len(results.get(g.key, {})), g.title.lower()))
        for game in games:
            if game.key not in results or not results[game.key] or len(results[game.key]) < minimum_players:
                continue

            title_link = f"[{game.title}]({game.url})"
            message += f'**{title_link} {game.emoji} {_puzzle_label(game.puzzle, reference_date)}**\n'
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
    points = compute_points(results, games, minimum_players)
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
        puzzle_label = _puzzle_label(game.puzzle, reference_date)
        score_text = f"**[{game.title}]({game.url}) {game.emoji} {puzzle_label}**\n"
        score_text += _format_game_players(results[game.key], game.metric, game.total).rstrip('\n')
        scores_children.append({"type": 10, "content": score_text})

    if scores_children:
        components.append({"type": 17, "accent_color": OTHER_GAMES_COLOR, "components": scores_children})

    return components

