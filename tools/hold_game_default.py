"""Hold servers that chose their own games where they stand before a game's default flips.

Local-only tooling (never deployed). The Games form stores only the games a
server has switched, so flipping a shipped game's `disabled` flag moves every
server that never switched that game. That is the point for servers still on
the defaults, but it also reaches servers that picked their own games, which
would find one added to or dropped from a list they chose. Run from the
repository root, BEFORE the flip, against the code that still carries the old
default:

    dotenv run -- python3 tools/hold_game_default.py seldom           # preview
    dotenv run -- python3 tools/hold_game_default.py seldom --write

Every server with at least one Games choice, and none for this game, gets the
game's current default written in as an explicit choice. That is what it
already tracks, so no board changes until the flip deploys, and from then on
the flip reaches only servers on the defaults. A later Games save keeps the
held value, since a save records only the games it switches. Run after the
flip, it would write the new default instead: harmless, but it holds nobody.
Re-running is safe -- a server already holding a choice is skipped, and each
write is conditioned on that.

PREVIEW IS THE DEFAULT. Nothing is written without --write.
"""
import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# The lambda modules live in src/ and ship flat in the deploy zip; put that
# directory on the path so this tool runs against the same code as production.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))

from game_parser import GAME_SPECS, spec_enabled                     # noqa: E402
import store                                                         # noqa: E402


def main():
    ap = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        epilog='Preview is the default; pass --write to store the choices.')
    ap.add_argument('game', help="key of the game whose default is about to flip")
    ap.add_argument('--write', action='store_true',
                    help='store the choices; without it this only previews')
    args = ap.parse_args()

    spec = next((s for s in GAME_SPECS if s.key == args.game), None)
    if not spec:
        ap.error(f'no such game {args.game!r}; have '
                 f'{", ".join(s.key for s in GAME_SPECS)}')
    default = spec_enabled(spec)
    state = 'on' if default else 'off'
    print(f'{spec.emoji} {spec.title} is {state} by default in this code.')

    holds = []
    for cfg in sorted(store.all_configs(), key=lambda c: c['guild_id']):
        gid, overrides = cfg['guild_id'], cfg['game_overrides']
        gone = ' (bot not in server)' if cfg['missing_since'] else ''
        if not overrides:
            print(f'  {gid}{gone}: on the defaults, so the flip reaches it')
        elif spec.key in overrides:
            print(f"  {gid}{gone}: already chose {'on' if overrides[spec.key] else 'off'}")
        else:
            print(f'  {gid}{gone}: chose its own games -> hold {state}')
            holds.append(gid)

    if not args.write:
        print(f'\npreview only -- nothing written. Re-run with --write to hold '
              f'{len(holds)}.')
        return 0
    print()
    for gid in holds:
        wrote = store.hold_game_choice(gid, spec.key, default)
        print(f"  {gid}: {'held ' + state if wrote else 'skipped, it chose meanwhile'}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
