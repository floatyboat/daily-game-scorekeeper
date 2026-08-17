import os
import random
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

    candidate_hashes maps user_id -> one hash or several, because a member can
    be rendered with either their server-profile avatar or their global one and
    the pool carries whichever it could find. Each user is scored by their
    closest picture, so `margin` below always compares two *different people*
    rather than two pictures of the same one.

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

    scored = []
    for uid, hashes in candidate_hashes.items():
        if isinstance(hashes, int):   # single-hash pool (older callers)
            hashes = (hashes,)
        if not hashes:
            continue
        scored.append((uid, min(_hamming(crop_hash, ah) for ah in hashes)))
    if not scored:
        return None
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


# The official Wordle Discord bot. One application, so it carries this same ID
# in every server it joins -- there is nothing per-server to discover, which is
# why it is a constant here rather than a setting each admin has to find and
# type in. It costs a server without that bot nothing: every path below keys on
# a message's author being this exact ID, and none is reached until the
# text-pattern loop has already failed to match, so the work starts when a grid
# actually shows up and not before.
WORDLE_BOT_ID = '1211781489931452447'


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
      disabled  the game's DEFAULT state: guilds start with it untracked. Admins
                flip any game either way per guild via /setup games
                (game_overrides in the guild config); this flag only decides
                what a guild gets before anyone touches the menu.
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
#
# Adding a game is one entry here and nothing else -- except that two rendered
# surfaces have Discord payload caps this list now feeds (scoreboard.py holds
# the constants): the /setup games menu is one option per spec and tops out
# at 25, and /play is one button per ENABLED game at 5 per row plus the Random
# row, so it tops out at 20. 18 specs today. Past either, the fix is to split
# the response across two messages -- see the FUTURE note in scoreboard.py.

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
        total=50, url='https://dialed.gg/?d=1', needs_timestamp=True,
        puzzle=lambda ref: f'{ref.strftime("%B")} {ref.day}',
        pattern=lambda ref, n: re.compile(r'dialed\.gg/\?\S*&s=(\d+(?:\.\d+)?)', re.IGNORECASE),
        parse=lambda m, c: (float(m.group(1)), {}),
    ),
    GameSpec(
        key='dialed_sound', emoji='🔊', title='Sound', metric='score',
        total=50, url='https://dialed.gg/sound?d=1', needs_timestamp=True,
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
    GameSpec(
        key='minutecryptic', emoji='🧩', title='Minute Cryptic', metric='guesses',
        total=0, url='https://www.minutecryptic.com',
        puzzle=lambda ref: (ref - datetime(2024, 6, 26)).days + 1,
        # Scored like golf on hints used, so fewer is better and 0 is a clean
        # solve -- the same shape as the other total=0 'guesses' games. The
        # leading emoji on the hint line varies with par (🏆 at or under, 🏋
        # over), so match the count and not the emoji.
        # The share text heads on the date, not the puzzle number, so the
        # pattern derives its own '7 August, 2026' from ref and ignores n --
        # n is the real puzzle number, used only for the '#773' display label.
        pattern=lambda ref, n: re.compile(
            r'Minute Cryptic\s*[-–—]\s*'
            + re.escape(f'{ref.day} {ref.strftime("%B")}, {ref.year}')
            + r'.*?(\d+)\s+hints?\b',
            re.IGNORECASE | re.DOTALL),
        parse=lambda m, c: (int(m.group(1)), {}),
    ),
]


def spec_enabled(spec, game_overrides=None):
    """Whether a game is tracked for a guild: the guild's explicit /setup games
    choice when present, the spec's coded default otherwise."""
    if game_overrides and spec.key in game_overrides:
        return bool(game_overrides[spec.key])
    return not spec.disabled


def next_rotation(enabled_keys, count, mode, min_players, prev_rotation, results):
    """Draw the rotation for a new day: the game keys that will score on it.

    prev_rotation is yesterday's list only when it actually governed the day
    just scored, else None -- None or mode='random' means a fresh sample of
    `count` games. Swap mode treats membership as earned by participation
    (distinct posters in `results`, the same len(day_games[key]) count the
    archive stores -- poop results keep a game in), one threshold both ways:
    members that drew at least min_players stay, and off-rotation games that
    drew them join. `count` is a hard cap -- more qualifiers than slots keeps
    the most played, and the sort is stable, so an exact tie favors the
    sitting member over the newcomer. Remaining slots are filled at random
    from the enabled remainder -- never a key that just fell out, unless
    nothing else is left to keep the board from shrinking.
    """
    target = min(count, len(enabled_keys))
    if target <= 0:
        return []
    if mode != 'swap' or not prev_rotation:
        return random.sample(list(enabled_keys), target)
    enabled = set(enabled_keys)
    prev = [k for k in prev_rotation if k in enabled]
    prev_set = set(prev)

    def played(k):
        return len(results.get(k) or {})

    keep = [k for k in prev if played(k) >= min_players]
    promoted = [k for k in enabled_keys
                if k not in prev_set and played(k) >= min_players]
    dropped = [k for k in prev if played(k) < min_players]
    rotation = keep + promoted
    if len(rotation) > target:
        rotation.sort(key=lambda k: -played(k))
        rotation = rotation[:target]
    taken = set(rotation)
    pool = [k for k in enabled_keys if k not in taken and k not in set(dropped)]
    random.shuffle(pool)
    while len(rotation) < target and pool:
        rotation.append(pool.pop())
    random.shuffle(dropped)
    while len(rotation) < target and dropped:
        rotation.append(dropped.pop())
    return rotation


def _alnum(text):
    """Letters and digits only, lowercased -- 'Pop Culture Colors' and
    'popcultureColors' are the same answer to "which game is this?"."""
    return re.sub(r'[^a-z0-9]', '', (text or '').lower())


def _site(url):
    """A game's page identity: host (minus scheme and www.) plus its path, with
    the query string dropped -- `dialed.gg/sound` and `nytimes.com/games/wordle`.
    Compared whole, never as a prefix: dialed.gg hosts three separate tracked
    games and nytimes.com/games four, so a host-level match would read a fourth
    game on either as already supported."""
    m = re.search(r'(?:https?://)?(?:www\.)?([^\s?#<>]+)', url or '', re.IGNORECASE)
    return m.group(1).rstrip('/').lower() if m else ''


def match_suggestion(name='', url='', text=''):
    """The GameSpec a /suggest submission is plainly already about, or None.

    Deliberately narrow: an exact name/key hit, or the game's own page among the
    links in the submission. match_message() cannot answer this -- its patterns
    are pinned to one day's puzzle number and a suggestion gets pasted whenever
    -- and a looser scan (spec titles as substrings) would read the games called
    "Color" and "Sound" into any text that mentions them, discarding a real
    suggestion to save the devs a duplicate. Anything less than obvious is
    forwarded instead and read by a human, which is the cheaper mistake.
    """
    key = _alnum(name)
    sites = {s for s in
             [_site(url)] + [_site(u) for u in re.findall(r'https?://\S+', text or '')]
             if s}
    for spec in GAME_SPECS:
        if key and key in (_alnum(spec.title), _alnum(spec.key)):
            return spec
        if _site(spec.url) in sites:
            return spec
    return None


def build_games(puzzle_numbers, game_overrides=None):
    """Resolve GAME_SPECS into concrete Game descriptors for one reference date.

    game_overrides is the guild's per-game enable map (config.game_overrides);
    games resolving disabled are dropped here, so they are skipped by both the
    parser and the scoreboard. GAME_SPECS order (parse priority) is preserved;
    the scoreboard re-sorts for display.
    """
    ref = puzzle_numbers['reference_date']
    games = []
    for spec in GAME_SPECS:
        if not spec_enabled(spec, game_overrides):
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


def match_message(msg, games, timestamp_checker, avatar_hashes=None):
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
    if (msg['author']['id'] == WORDLE_BOT_ID
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

    Scoring: you earn 1 point plus 1 for every player you beat (i.e. ranked
    strictly below you). Last place gets 1, and the sole player in a 1-player
    game gets 1. With no ties this is identical to N-rank+1 (1st gets N, each
    place below earns one fewer); ties differ -- tied players only get credit
    for those they actually beat, not each other. Poop scores (failed games)
    earn 0 points.

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

        # Walk the sorted players, grouping ties
        i = 0
        while i < len(players):
            current_score = players[i][1]

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
                # 1 point + 1 for each player strictly below (players[j:]); tied
                # players (players[i:j]) don't count as beaten.
                player_points = 1 + (n - j)
                for k in range(i, j):
                    points[players[k][0]] += player_points

            i = j

    return dict(points)


def points_per_game(results, games, minimum_players=1):
    """{game_key: {user_id: points}} for every game in `games`.

    compute_points scores each game independently, so scoring them one at a
    time sums to exactly the totals the posted points summary shows. One helper
    so the archive, the streak fold and the live views all agree on who scored.
    """
    return {g.key: compute_points(results, [g], minimum_players) for g in games}


def scoring_players(results, games, minimum_players=1):
    """{game_key: {user_id, ...}} -- who a day's streaks count as having played.

    A result by itself is not a play for streak purposes: a poop score earns 0
    points, and 0 points keeps nothing alive -- not that player's streak for the
    game, and not the game's own streak when nobody scored. Games below
    minimum_players score nobody, so they don't extend streaks either -- the
    same games the board leaves off.

    The single definition of streak eligibility, shared by the finalize fold
    (via the points it already stores) and every live view.
    """
    return {key: {uid for uid, pts in scores.items() if pts > 0}
            for key, scores in points_per_game(results, games, minimum_players).items()}


def _streak_tag(player_streaks, uid):
    """' (xN)' for a player at or above the display minimum, else ''.

    Per-game player streaks render as this plain multiplier -- they repeat on
    every score line of every game, so an emoji there drowns the board. The
    fire emoji is reserved for streaks that appear at most once per player per
    board (a game's title line, the sticky's server streak, and the overall
    streak in the points summary -- see _overall_streak_tag).
    """
    n = (player_streaks or {}).get(uid, 0)
    return f' (x{n})' if n >= STREAK_MIN else ''


def _overall_streak_tag(player_streaks, uid):
    """' \U0001F525N' for a player at or above the display minimum, else ''.

    The overall (any-game) streak is the headline number for a player and lands
    once, at the end of their points-summary line, so it earns the fire emoji
    that per-game streaks don't.
    """
    n = (player_streaks or {}).get(uid, 0)
    return f' \U0001F525{n}' if n >= STREAK_MIN else ''


def format_points_summary(points, player_streaks=None):
    """Format the points summary section.

    player_streaks ({user_id: overall streak}) tags players at or above the
    display minimum with their days-running-in-any-game count, rendered as a
    trailing fire emoji.

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
            message += (f'{medal}<@{uid}>: {current_val} {unit}'
                        f'{_overall_streak_tag(player_streaks, uid)}\n')

        prev_val = current_val
        i = j

    return message + '\n'


def _format_game_players(game_scores, metric, total, player_streaks=None):
    """Format ranked player lines for a single game.

    Returns a markdown string with medal emojis, player mentions, and scores.
    player_streaks ({user_id: streak}) appends an "(xN)" marker to players whose
    streak for this game has reached the display minimum.
    """
    def mention(uid):
        return f'<@{uid}>{_streak_tag(player_streaks, uid)}'

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
            tied = [mention(sorted_players[i][0])]
            j = i + 1
            while j < len(sorted_players) and (sorted_players[j][1][0], sorted_players[j][1][1]) == score_tuple:
                tied.append(mention(sorted_players[j][0]))
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

        tied_players = [mention(players[i][0])]
        j = i + 1
        while j < len(players) and players[j][1] == current_score:
            tied_players.append(mention(players[j][0]))
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
                score_str = f"{mistakes}/{total} ({solved} solved)"
                if solved == 0:
                    medal = '💩 '
            else:
                score_str = f"{mistakes}/{total}"
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
                parts.append(f"{k}✓")
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
                score_str = f"{str(current_score)}"
            else:
                if current_score > total:
                    medal = '💩 '
                    current_score = 'X'
                score_str = f"{str(current_score)}/{total}"

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
        games.sort(key=lambda g: game_sort_key(g, results, None))
        for game in games:
            if game.key not in results or not results[game.key] or len(results[game.key]) < minimum_players:
                continue

            title_link = f"[{game.title}]({game.url})"
            message += f'**{title_link} {game.emoji} {_puzzle_label(game.puzzle, reference_date)}**\n'
            message += _format_game_players(results[game.key], game.metric, game.total)
            message += "\n"
    return message


MEDAL_COLOR = 15844367  # dark gold

# Minimum streak length to display, everywhere streaks appear: game title
# suffixes, Play labels, sticky flair, personal markers, and break callouts.
# Shorter streaks still accrue and drive sort order -- they just don't render.
STREAK_MIN = int(os.getenv('MINIMUM_STREAK') or 3)


def game_sort_key(game, results, streaks):
    """The app-wide game ordering, shared by the scoreboard sections, the Play
    list and the sticky's shortcut row: today's players desc -> active streak
    desc -> distinct players in the last 30 days desc -> all-time distinct
    players desc -> title.

    The 30-day tier is what keeps the tail current: all-time player sets only
    grow, so a game the server has drifted away from outranks a newer one
    forever on that number alone. results supplies today's live counts; streaks
    is a gather_streaks() bundle or None (which degrades to count -> title).
    """
    bundle = streaks or {}
    return (-len(results.get(game.key) or {}),
            -bundle.get('games', {}).get(game.key, 0),
            -bundle.get('players_30d', {}).get(game.key, 0),
            -bundle.get('players_total', {}).get(game.key, 0),
            game.title.lower())


def game_link_button(game, streak=0):
    """One game as a Discord link button, shared by every surface that lists
    games: the Play/Random list and the sticky's shortcut row.

    Label is emoji + title, plus a fire suffix while the game's server streak
    is alive. Today's player count sorts games (game_sort_key) but is
    deliberately not in the label -- the counts read as a scoreboard the
    scoreboard already renders, and they churned the label on every play.
    """
    label = f'{game.emoji} {game.title}'
    if streak >= STREAK_MIN:
        label += f' \U0001F525{streak}'
    return {'type': 2, 'style': 5, 'label': label, 'url': game.url}


def top_game_buttons(games, results, streaks, limit):
    """Link buttons for the first `limit` games in the app-wide order."""
    game_streaks = (streaks or {}).get('games', {})
    ordered = sorted(games, key=lambda g: game_sort_key(g, results, streaks))
    return [game_link_button(g, game_streaks.get(g.key, 0)) for g in ordered[:limit]]


def _streak_break_lines(streaks, games_by_key):
    """One line per game streak that ended on the displayed day.

    Rendered at the foot of the scores section, under the games that were
    actually played: a broken streak is a result for that game too, and it
    reads as one when it sits with them rather than in the header.
    """
    if not streaks:
        return []
    lines = []
    for key, ended in sorted(streaks['broken'].items(), key=lambda kv: (-kv[1], kv[0])):
        game = games_by_key.get(key)
        if game and ended >= STREAK_MIN:
            lines.append(f"\U0001F494 {game.title} streak ended at {ended}")
    return lines


def format_scoreboard_components(results, reference_date, puzzle_numbers, title="Daily Game Scoreboard", minimum_players=1, streaks=None, game_overrides=None, rotation=None, rotation_off='shown'):
    """Format the scoreboard as Discord Components V2 (list of top-level components).

    streaks is an optional gather_streaks() bundle; it adds "streak ended"
    callouts at the foot of the scores section, per-game fire suffixes on title
    lines, a personal fire suffix for each player's overall streak in the points
    summary, and personal "(xN)" markers on the per-game score lines.
    None renders exactly the streak-less board. game_overrides is the
    guild's per-game enable map -- without it a guild-enabled game whose spec
    defaults to disabled would silently drop out of the render.

    rotation is the day's rotation (key list) or None for an unrestricted
    board. Scored games keep the points summary and the scores section to
    themselves; off-rotation games that were played render below them as a
    separate zero-point section -- unless rotation_off is 'hidden', which
    drops that section and nothing else. This board is the only surface the
    setting touches.

    Returns a list[dict] suitable for the 'components' field in a Discord message.
    """
    games = build_games(puzzle_numbers, game_overrides)
    rot = set(rotation) if rotation is not None else None
    components = []

    # --- Header container ---
    header_text = f"### 🧮 {title} - {reference_date.strftime('%B %d, %Y')}"
    header_children = [{"type": 10, "content": header_text}]
    break_lines = _streak_break_lines(streaks, {g.key: g for g in games})
    break_child = ([{"type": 10, "content": "\n".join(break_lines)}]
                   if break_lines else [])

    if not results:
        # Break callouts still render: a no-results day is exactly when every
        # alive streak snaps. There is no scores section to sit at the foot of,
        # so they go at the foot of the only container there is.
        return [{"type": 17, "accent_color": HEADER_COLOR, "components": header_children + [
            {"type": 10, "content": "No results found!"},
        ] + break_child}]

    # --- Points container (gold accent) ---
    # The one rotation-restricted compute_points call site: off-rotation games
    # earn no points on the board, whatever the archive froze for them.
    scored_games = games if rot is None else [g for g in games if g.key in rot]
    points = compute_points(results, scored_games, minimum_players)
    points_section = format_points_summary(points, (streaks or {}).get('players_overall'))
    if points_section:
        header_children.append({"type": 10, "content": points_section.rstrip('\n')})
        components.append({"type": 17, "accent_color": HEADER_COLOR, "components": header_children})
    else:
        components.append({"type": 17, "accent_color": OTHER_GAMES_COLOR, "components": header_children})

    # Canonical app-wide ordering, same as the Play list
    games.sort(key=lambda g: game_sort_key(g, results, streaks))

    qualified = [g for g in games if g.key in results and results[g.key]
                 and len(results[g.key]) >= minimum_players
                 and (rot is None or g.key in rot)]

    game_streaks = streaks['games'] if streaks else {}
    player_streaks = streaks['players'] if streaks else {}

    def game_sections(game_list):
        children = []
        for g_idx, game in enumerate(game_list):
            if g_idx > 0:
                children.append({"type": 14, "spacing": 1})  # Separator
            puzzle_label = _puzzle_label(game.puzzle, reference_date)
            score_text = f"**[{game.title}]({game.url}) {game.emoji} {puzzle_label}**"
            streak = game_streaks.get(game.key, 0)
            if streak >= STREAK_MIN:
                score_text += f" \U0001F525{streak}"
            score_text += "\n" + _format_game_players(
                results[game.key], game.metric, game.total,
                player_streaks.get(game.key)).rstrip('\n')
            children.append({"type": 10, "content": score_text})
        return children

    # --- Scores container ---
    scores_children = game_sections(qualified)

    if break_child:
        if scores_children:
            scores_children.append({"type": 14, "spacing": 1})  # Separator
        scores_children += break_child

    if scores_children:
        components.append({"type": 17, "accent_color": OTHER_GAMES_COLOR, "components": scores_children})

    # --- Off-rotation container ---
    # Games outside the rotation that were played: rendered with scores but no
    # points, always below the scored games -- or not at all under 'hidden'.
    if rot is not None and rotation_off != 'hidden':
        exhibition = [g for g in games if g.key not in rot and results.get(g.key)
                      and len(results[g.key]) >= minimum_players]
        if exhibition:
            components.append({"type": 17, "accent_color": OTHER_GAMES_COLOR, "components": [
                {"type": 10, "content": "**Off rotation** — played for fun, no points today"},
                {"type": 14, "spacing": 1},
            ] + game_sections(exhibition)})

    return components

