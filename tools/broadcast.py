"""Say something to the servers the bot serves, as the bot.

Local-only tooling (never deployed). The bot has no other outbound voice: the
scheduled lambdas post scoreboards and stickies, /suggest forwards suggestions
to the dev channel, and interaction replies only ever answer the person who
clicked. Run from the repository root:

    dotenv run -- python3 tools/broadcast.py --game fermi          # preview
    dotenv run -- python3 tools/broadcast.py --game fermi --send
    dotenv run -- python3 tools/broadcast.py -m "back in an hour" --send
    dotenv run -- python3 tools/broadcast.py --file note.md --guild 818... --send

--game closes the loop on /suggest. When a suggested game ships, it thanks the
people who asked for it, in the servers they asked from, and reaches no other
server. interaction_lambda keeps every suggestion it forwards (store.SUGGESTIONS),
and game_parser.match_suggestion -- the same test /suggest itself uses to spot a
game that is already tracked -- decides which of them this game answers. Each is
stamped once its server has been thanked, so running this again for the same
game only reaches people not yet thanked (--again resends). A game nobody
suggested has nobody to thank; announce it with -m instead.

-m and --file send free text to every configured server, or just the ones named
with --guild. Recipients come from store.all_configs(), the same fan-out the
scheduled lambdas use.

PREVIEW IS THE DEFAULT. Nothing is sent without --send: a message from here
lands in front of a whole channel this tool has never looked at, and a ping
can't be taken back.
"""
import argparse
import os
import sys
from pathlib import Path
from typing import NamedTuple

from dotenv import load_dotenv

load_dotenv()

# The lambda modules live in src/ and ship flat in the deploy zip; put that
# directory on the path so this tool runs against the same code as production.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))

from game_parser import (                                            # noqa: E402
    GAME_SPECS, game_link_button, match_suggestion, spec_enabled,
)
from scoreboard import (                                             # noqa: E402
    DISCORD_API_BASE, make_session, FLAG_SUPPRESS_EMBEDS,
    FLAG_SUPPRESS_NOTIFICATIONS,
)
import store                                                         # noqa: E402

_session = make_session(os.getenv('DISCORD_BOT_TOKEN'))

# Which configured channel each --channel choice posts in, checked against
# store.CHANNEL_FIELDS so a renamed field breaks here loudly rather than
# silently broadcasting nowhere.
CHANNELS = {'output': 'output_channel_id', 'input': 'input_channel_id'}
assert set(CHANNELS.values()) <= set(store.CHANNEL_FIELDS)


class Send(NamedTuple):
    """One message to one server. `ping` is who it may notify, and `answers`
    the kept suggestions to stamp as thanked once it has gone out."""
    guild_id: str
    channel_id: str
    content: str
    components: list = None
    ping: tuple = ()
    answers: tuple = ()


def _people(user_ids):
    """'<@a>', '<@a> and <@b>', '<@a>, <@b> and <@c>'."""
    tags = [f'<@{u}>' for u in user_ids]
    return tags[0] if len(tags) == 1 else f"{', '.join(tags[:-1])} and {tags[-1]}"


def thank_you_message(spec, user_ids, enabled):
    """The note to a server a game was suggested from, naming who asked.

    Worded off that server's own setting rather than the spec's default: an
    admin may already have found the game and switched it on, and being told to
    turn on a game that is already on reads as nobody having looked.
    """
    thanks = f'Thank you {_people(user_ids)} for suggesting {spec.emoji} **{spec.title}**!'
    if enabled:
        return (f"{thanks} It's already switched on here, so post your results "
                f"and they'll land on the board.")
    return (f'{thanks} You can now find it in the game menu, and an admin can turn '
            f'it on with `/setup games`.')


def answered_suggestions(spec, only_guilds, again):
    """{guild_id: [kept suggestions]} for every /suggest this game answers, and
    (guild_id, reason) for the ones left out -- printed rather than dropped,
    since someone silently not thanked looks the same as someone who never
    asked."""
    by_guild, skipped = {}, []
    for item in store.suggestions():
        answer = match_suggestion(item.get('name'), item.get('url'), item.get('text'))
        if not answer or answer.key != spec.key:
            continue
        gid = item['guild_id']
        if only_guilds and gid not in only_guilds:
            continue
        if item.get('thanked_at') and not again:
            skipped.append((gid, f"<@{item['user_id']}> already thanked "
                                 f"{item['thanked_at'][:10]} (--again to resend)"))
            continue
        by_guild.setdefault(gid, []).append(item)
    return by_guild, skipped


def game_sends(spec, configs, channel_field, only_guilds, again):
    """One thank-you per server the game was suggested from."""
    by_guild, skipped = answered_suggestions(spec, only_guilds, again)
    sends = []
    for gid, items in sorted(by_guild.items()):
        cfg = configs.get(gid)
        channel_id = (cfg or {}).get(channel_field)
        if not channel_id:
            skipped.append((gid, f'no {channel_field}' if cfg
                           else 'server is no longer set up'))
            continue
        # Oldest first, each person once however many times they asked.
        users = tuple(dict.fromkeys(it['user_id'] for it in items))
        enabled = spec_enabled(spec, cfg['game_overrides'])
        sends.append(Send(gid, str(channel_id),
                          thank_you_message(spec, users, enabled),
                          [{'type': 1, 'components': [game_link_button(spec)]}],
                          ping=users, answers=tuple(items)))
    return sends, skipped


def text_sends(content, configs, channel_field, only_guilds):
    """The same free text to every configured server, or the --guild ones."""
    sends, skipped = [], []
    for gid, cfg in sorted(configs.items()):
        if only_guilds and gid not in only_guilds:
            continue
        channel_id = cfg.get(channel_field)
        if not channel_id:
            skipped.append((gid, f'no {channel_field}'))
            continue
        sends.append(Send(gid, str(channel_id), content))
    return sends, skipped


def guild_name(guild_id):
    """The server's name, for a preview a human can check before sending."""
    try:
        r = _session.get(f'{DISCORD_API_BASE}/guilds/{guild_id}')
        return r.json().get('name', '?') if r.ok else f'HTTP {r.status_code}'
    except Exception as e:                       # noqa: BLE001 - preview only
        return type(e).__name__


def member_name(guild_id, user_id):
    """Who a ping would reach, by the name that server shows them -- and whether
    they are still there to see it."""
    try:
        r = _session.get(f'{DISCORD_API_BASE}/guilds/{guild_id}/members/{user_id}')
        if r.status_code == 404:
            return 'NO LONGER IN THIS SERVER'
        if not r.ok:
            return f'HTTP {r.status_code}'
        member = r.json()
        user = member.get('user') or {}
        return member.get('nick') or user.get('global_name') or user.get('username', '?')
    except Exception as e:                       # noqa: BLE001 - preview only
        return type(e).__name__


def post(send, silent):
    """One message, in the bot's own voice.

    Embeds suppressed, and nobody notified but the users the message thanks:
    they should hear that their suggestion landed, and nothing else sent from
    here has any business pinging anybody.
    """
    flags = FLAG_SUPPRESS_EMBEDS | (FLAG_SUPPRESS_NOTIFICATIONS if silent else 0)
    payload = {'content': send.content, 'flags': flags,
               'allowed_mentions': {'parse': [], 'users': list(send.ping)}}
    if send.components:
        payload['components'] = send.components
    r = _session.post(f'{DISCORD_API_BASE}/channels/{send.channel_id}/messages',
                      json=payload)
    r.raise_for_status()
    return r.json()


def main():
    ap = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        epilog='Preview is the default; pass --send to actually post.')
    what = ap.add_mutually_exclusive_group(required=True)
    what.add_argument('--game', metavar='KEY',
                      help='thank whoever /suggest-ed this game, in the server '
                           'they asked from')
    what.add_argument('-m', '--message', help='free text, to every server (or --guild)')
    what.add_argument('--file', type=Path, help='free text, read from a file')
    ap.add_argument('--guild', action='append', default=[], metavar='ID',
                    help='only this guild; repeatable')
    ap.add_argument('--channel', choices=sorted(CHANNELS), default='output',
                    help='which configured channel to post in (default output)')
    ap.add_argument('--again', action='store_true',
                    help='with --game, also resend to suggesters already thanked')
    ap.add_argument('--silent', action='store_true',
                    help='suppress the push notification')
    ap.add_argument('--send', action='store_true',
                    help='actually post; without it this only previews')
    args = ap.parse_args()

    channel_field = CHANNELS[args.channel]
    only = set(args.guild)
    configs = {str(c['guild_id']): c for c in store.all_configs()}

    if args.game:
        spec = next((s for s in GAME_SPECS if s.key == args.game), None)
        if not spec:
            ap.error(f'no such game {args.game!r}; have '
                     f'{", ".join(s.key for s in GAME_SPECS)}')
        sends, skipped = game_sends(spec, configs, channel_field, only, args.again)
        if not sends and not skipped:
            print(f'Nobody has /suggest-ed {spec.title}, so there is no one to '
                  f'thank. Announce it with -m instead.')
            return 1
    else:
        content = (args.file.read_text(encoding='utf-8') if args.file
                   else args.message).strip()
        if not content:
            ap.error('empty message')
        sends, skipped = text_sends(content, configs, channel_field, only)

    for send in sends:
        print(f'--- {guild_name(send.guild_id)} ({send.guild_id}) '
              f'-> {args.channel} channel #{send.channel_id}')
        for uid in send.ping:
            print(f'    pings {member_name(send.guild_id, uid)} ({uid})')
        for line in send.content.splitlines():
            print(f'    | {line}')
        for row in send.components or ():
            for button in row['components']:
                print(f"    | [{button['label']}] -> {button['url']}")
    for gid, why in skipped:
        print(f'--- {gid} SKIPPED: {why}')

    if not args.send:
        print('\npreview only -- nothing sent. Re-run with --send to post.')
        return 0
    if not sends:
        print('\nno recipients; nothing to send.')
        return 1

    print()
    failed = 0
    for send in sends:
        try:
            msg = post(send, args.silent)
        except Exception as e:                   # noqa: BLE001 - report, continue
            failed += 1
            detail = getattr(getattr(e, 'response', None), 'text', '') or ''
            print(f'  FAILED {send.guild_id}: {type(e).__name__}: {e} {detail[:200]}')
            continue
        print(f'  sent {send.guild_id} -> https://discord.com/channels/'
              f'{send.guild_id}/{send.channel_id}/{msg["id"]}')
        for item in send.answers:
            try:
                store.mark_thanked(item['SK'])
            except Exception as e:               # noqa: BLE001 - report, continue
                print(f"  WARNING: sent, but <@{item['user_id']}> wasn't stamped as "
                      f"thanked ({type(e).__name__}: {e}) -- a re-run would thank "
                      f"them again")
    print(f'\n{len(sends) - failed} sent, {failed} failed.')
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
