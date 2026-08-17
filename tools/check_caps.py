"""Assert the scoreboard renders inside Discord's Components-V2 caps.

Local-only tooling (never deployed). The daily post is a single API call that
Discord rejects outright if the board breaks either cap, and the daily lambda
does not advance last_posted_day on failure -- so an over-budget board posts
nothing and retries-and-fails every hour until the day rolls over. This is the
check that keeps the 19th GameSpec, or a busy Saturday, from finding that out
in production. Run from the repository root:

    python3 tools/check_caps.py            # assert; exit 1 on any breach
    python3 tools/check_caps.py --report   # also print the grid and rungs used

Needs no credentials, no network and no table: it drives the same
format_scoreboard_components the lambdas call, over synthetic results dense
enough to force every rung of the reduction ladder.
"""
import argparse
import contextlib
import dataclasses
import io
import sys
from datetime import datetime
from pathlib import Path

# The lambda modules live in src/ and ship flat in the deploy zip; put that
# directory on the path so this tool runs against the same code as production.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))

import game_parser as gp

REF = datetime(2026, 8, 17)

# Long enough that a plain name is a realistic saving over a 21-character
# mention, not a best case that flatters the mention_limit rungs.
NAME = 'PlayerName'

# Score lines the board is guaranteed to fit, whatever they divide into: the
# frontier is close to flat, because past the last rung a line is down to a
# name, a score and a newline and 4000 characters only holds so many of them.
# Measured at 216 lines (18 games x 12 players) and 230 (10 x 23); this is the
# conservative floor across the shapes in between. For scale, this server runs
# around 35 score lines a day. Raising it needs a new rung in _REDUCTIONS or a
# second message -- not a bigger number here.
SUPPORTED_LINES = 200


def spec_pool(n_games):
    """At least n_games specs: the real ones, then clones with fresh keys.

    Cloning rather than inventing keeps every rendered line honest -- real
    titles, real URLs, real emoji, real metrics -- so the character counts this
    asserts against are the ones a real board would produce.
    """
    specs = list(gp.GAME_SPECS)
    pool = list(specs)
    round_n = 0
    while len(pool) < n_games:
        pool += [dataclasses.replace(s, key=f'{s.key}_x{round_n}', total_key=None)
                 for s in specs]
        round_n += 1
    return pool[:n_games]


def fake_score(metric, i):
    """A score of the shape each metric's formatter expects."""
    return {'connections': (i % 4, 4), 'maptap': (100 - i, 90 - i),
            'travle': (0, 3, 0, -2), 'time': 90 + i,
            'timed_win': (0, 1, 0, 90 + i),
            'score': 50 + i}.get(metric, 3)


def score_lines(n_games, n_players):
    return n_games * n_players


def build(n_games, n_players, *, broken=False, off_rotation=0, with_names=True):
    """One board, plus the reduction rungs its render needed."""
    original = gp.GAME_SPECS
    gp.GAME_SPECS = spec_pool(n_games)
    try:
        pn = gp.compute_puzzle_numbers(REF)
        overrides = {s.key: True for s in gp.GAME_SPECS}
        games = gp.build_games(pn, overrides)
        uids = [str(100000000000000000 + i) for i in range(n_players)]
        results = {g.key: {u: fake_score(g.metric, i) for i, u in enumerate(uids)}
                   for g in games}
        keys = [g.key for g in games]
        streaks = None
        if broken:
            streaks = {'games': {}, 'players': {}, 'broken': {k: 7 for k in keys[:3]}}
        names = {u: NAME for u in uids} if with_names else None
        # Captured rather than printed: the ladder's own log lines are how this
        # tool reports which rungs a shape needed.
        log = io.StringIO()
        with contextlib.redirect_stdout(log):
            board = gp.format_scoreboard_components(
                results, REF, pn, minimum_players=1, streaks=streaks,
                game_overrides=overrides,
                rotation=(keys[:n_games - off_rotation] if off_rotation else None),
                rotation_off='shown', names=names)
        rungs = [line.split('retrying with ')[1]
                 for line in log.getvalue().splitlines() if 'retrying with ' in line]
        exhausted = 'STILL over budget' in log.getvalue()
        return board, rungs, exhausted
    finally:
        gp.GAME_SPECS = original


def check(label, board, rungs, exhausted, failures, report):
    n, c = gp.count_components(board), gp.displayable_text(board)
    ok = n <= gp.MAX_TOTAL_COMPONENTS and c <= gp.MAX_DISPLAYABLE_TEXT
    if not ok or exhausted:
        failures.append(f'{label}: {n} components, {c} chars'
                        + (' (ladder exhausted)' if exhausted else ''))
    if report:
        print(f'  {label:<44} {n:>3} comp {c:>5} chars'
              f'{"  <- " + ", ".join(rungs) if rungs else ""}'
              f'{"  FAIL" if not ok else ""}')
    return ok


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--report', action='store_true',
                    help='print every case, not just the failures')
    args = ap.parse_args()

    failures = []
    n_specs = len(gp.GAME_SPECS)

    if args.report:
        print(f'caps: {gp.MAX_TOTAL_COMPONENTS} components, '
              f'{gp.MAX_DISPLAYABLE_TEXT} chars | GAME_SPECS: {n_specs}\n')
        print(f'supported envelope (<= {SUPPORTED_LINES} score lines)')

    # Shapes inside the envelope must fit, every one of them. The grid spans
    # today's spec count and well past it, at player counts from a quiet
    # Tuesday to a server an order of magnitude busier than this one.
    for n_games in (1, 5, 10, n_specs, n_specs + 1, 25, 30):
        for n_players in (1, 2, 3, 6, 10, 12):
            if score_lines(n_games, n_players) > SUPPORTED_LINES:
                continue
            board, rungs, out = build(n_games, n_players)
            check(f'{n_games} games x {n_players} players', board, rungs, out,
                  failures, args.report)

    if args.report:
        print('\nedge cases (everything that adds to the plain board)')

    edges = [
        ('streak breaks', dict(broken=True)),
        ('rotation split', dict(off_rotation=3)),
        ('rotation split + breaks', dict(broken=True, off_rotation=3)),
    ]
    for label, kw in edges:
        for n_games, n_players in ((n_specs, 6), (n_specs, 10), (30, 6)):
            board, rungs, out = build(n_games, n_players, **kw)
            check(f'{label} ({n_games}x{n_players})', board, rungs, out,
                  failures, args.report)

    # A board that already fits must be untouched by the ladder: those posts
    # have to render exactly as they did before the preflight existed. Full
    # style spends 3 components on the header container (itself plus the title
    # and points lines) and 2 per game (its text plus the separator above it),
    # so it holds this many games and no more.
    full_style_games = (gp.MAX_TOTAL_COMPONENTS - 3) // 2
    if args.report:
        print('\ninvariants')
    board, rungs, _ = build(min(n_specs, full_style_games), 3)
    if rungs:
        failures.append(f'a fitting board was reduced anyway: {rungs}')
    elif not any(c.get('type') == 14 for c in board[1].get('components', [])):
        failures.append('a fitting board lost its separators')
    elif args.report:
        print(f'  fitting board ({full_style_games} games) renders unreduced, '
              f'separators intact')

    # Past that the day is one where every tracked game got played, and the
    # only thing it costs is the dividers -- the first and least-lossy rung.
    # Every game and every player still renders, so this is the ladder working,
    # not the board breaking; it is asserted so a future spec pushes the board
    # no further down the ladder without this saying so.
    if n_specs > full_style_games:
        board, rungs, _ = build(n_specs, 3)
        if rungs != ['separators=False']:
            failures.append(f'all {n_specs} games x 3 players reduced past '
                            f'separators: {rungs}')
        elif args.report:
            print(f'  all {n_specs} games x 3 players costs separators only')

    # The empty board still renders (and still carries its break callouts).
    empty = gp.format_scoreboard_components({}, REF, gp.compute_puzzle_numbers(REF))
    if not empty or gp.count_components(empty) > gp.MAX_TOTAL_COMPONENTS:
        failures.append('empty board does not render')
    elif args.report:
        print('  empty board renders')

    # Past the envelope the text is irreducible -- ~200 score lines is more
    # than one Discord message holds however it is formatted. Three things are
    # asserted there. The component cap is ALWAYS satisfiable, because merging
    # makes the component count independent of how many games there are, so a
    # board must never come back over it however big the day was. The text cap
    # may not be, so the board must spend every text rung and say plainly that
    # it is still over. And it must come back complete either way: no crash,
    # and no silent truncation dressed up as a success.
    text_rungs = [f'{field}={value}' for field, value, cap in gp._REDUCTIONS
                  if cap == 'text']
    if args.report:
        print('\nbeyond the envelope (must degrade fully and say so)')
    for n_games, n_players in ((n_specs, 30), (40, 20), (60, 20)):
        board, rungs, exhausted = build(n_games, n_players)
        missing = [r for r in text_rungs if r not in rungs]
        if gp.count_components(board) > gp.MAX_TOTAL_COMPONENTS:
            failures.append(f'{n_games}x{n_players}: {gp.count_components(board)} '
                            f'components -- the component cap is always reducible')
        elif not exhausted:
            failures.append(f'{n_games}x{n_players}: over budget without saying so')
        elif missing:
            failures.append(f'{n_games}x{n_players}: gave up with rungs left: {missing}')
        elif not (len(board) >= 2 and gp.displayable_text(board) > 0):
            failures.append(f'{n_games}x{n_players}: board came back empty')
        elif args.report:
            print(f'  {n_games} games x {n_players} players: '
                  f'{gp.count_components(board)} comp (under cap), every text rung '
                  f'spent, logged, board intact ({gp.displayable_text(board)} chars)')

    if args.report:
        print()
    if failures:
        print(f'FAIL: {len(failures)} case(s)')
        for f in failures:
            print(f'  {f}')
        return 1
    print(f'OK: every board up to {SUPPORTED_LINES} score lines fits '
          f'{gp.MAX_TOTAL_COMPONENTS} components / {gp.MAX_DISPLAYABLE_TEXT} chars')
    return 0


if __name__ == '__main__':
    sys.exit(main())
