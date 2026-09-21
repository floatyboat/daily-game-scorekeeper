# Daily Scoreboard Bot — Spec

The bot reads a Discord channel for daily puzzle results, posts a scoreboard for
yesterday's games, maintains a live sticky in the input channel, and serves slash
commands and buttons. It persists each day's parsed results to DynamoDB and derives
streaks and per-game stats from them. One deployment serves any number of servers;
all per-server configuration lives in the table.

## Architecture

| Lambda | Module | Trigger | Role |
|---|---|---|---|
| `daily-game-score` | `src/lambda_function.py` | EventBridge rule `time`, `cron(0 * * * ? *)` | Three stages per tick, draw first: draws the rotation at each guild's day start, posts and pins yesterday's scoreboard at its post hour, and announces "Today's games" on either — so a later post hour gets it twice, and `rotation_announce` off gets it never; then, for guilds with it on, the hour's commentary (see Commentary); the only writer of day and aggregate items, and one of the two writers of the commentary-state item |
| `daily-game-sticky` | `src/sticky_lambda.py` | EventBridge rule `daily-game-sticky`, `cron(* * * * ? *)` | Maintains the one sticky ("Now Playing") at the bottom of the input channel, reacts to each fresh result with how it went and where it placed (see Reactions), and posts the commentary kinds that react to a result as it lands (see Commentary) |
| `daily-game-play` | `src/interaction_lambda.py` | Discord Function URL | `/play`, `/stats`, `/help`, `/setup`, `/suggest`, sticky Play/Scores/How-it-works buttons; live ephemeral views, plus the one-time welcome under a player's first one |

Shared modules: `game_parser.py` (game specs, parsing, scoring, render), `scoreboard.py`
(Discord fetch/format helpers), `store.py` (all DynamoDB I/O and the config schema), and
`commentary.py` (the registry of message kinds the commentary can post — pure; the daily
and sticky lambdas run it, one cadence each, and the interaction lambda reads it for the
`/setup commentary` menu).

Deployment is zip upload from three GitHub Actions workflows into `us-east-1`; there is
no IaC. Each workflow packs `src/` **flat** (`zip -j`) so the modules land at the archive
root, which is what `<module>.lambda_handler` requires.

Dependencies are bundled, never inherited. Every workflow installs `requests`, `Pillow`
and `python-dateutil` into the zip (plus `PyNaCl` for the interaction lambda) and asserts
they are present at the archive root before uploading. No lambda declares a layer;
`tools/infra_setup.py` reports any layer it finds as drift and removes it under `--prune`.

`tools/infra_setup.py` is the declaration of the stack — table, one IAM role per lambda,
the three functions, log retention, both schedules, the Function URL — and converges live
state to it. `tools/backfill.py` replays channel history into day items and recomputes
aggregates. `tools/register_commands.py` registers the slash commands; the interaction
lambda's workflow runs it after every deploy of that function, with the bot's identity
read off the function's own environment, so a new command or option goes live together
with the code that answers it.

## Database: DynamoDB

One table `daily-game-tracker`, generic string keys `PK`/`SK`, provisioned 5 RCU / 5 WCU,
**no GSIs**. Auth is the Lambda IAM role; boto3 ships in the runtime, so the store adds no
deploy dependency. These item types cover every access pattern:

```
PK                          SK                 Contents
GUILDS                      GUILD#<guild_id>   per-server config: input_channel_id,
                                               output_channel_id, timezone,
                                               hours_after_midnight, post_hour,
                                               time_window_hours, minimum_players,
                                               hundreds_of_messages,
                                               daily_enabled, sticky_enabled,
                                               sticky_games, delete_wordle_recap,
                                               suppress_embeds,
                                               rotation_enabled, rotation_count,
                                               rotation_mode, rotation_keep_players,
                                               rotation_promote_players,
                                               rotation_off_mode,
                                               game_overrides (map key->bool),
                                               last_finalized_day, last_posted_day,
                                               missing_since (set while the bot is
                                               not in the guild),
                                               rotation_day + rotation_games (the
                                               drawn rotation and the day it governs)
                                               and rotation_prev_day +
                                               rotation_prev_games (the pair it
                                               displaced, still needed by the board)
GUILD#<guild_id>            DAY#<YYYY-MM-DD>   full parsed results for the day:
                                               {game: {user_id: {score, points}}}, puzzle
                                               numbers, the governing rotation when one
                                               did, and the scoring mode the points were
                                               frozen on. The durable archive + rebuild
                                               source.
GUILD#<guild_id>            COMMENTARY#<day>   the commentary's state for the day:
                                               `announced` (string set of event ids that
                                               have gone out, ADDed to) and `standings`
                                               (JSON snapshot of the last pass, SET);
                                               shared by the daily and sticky lambdas
GUILD#<guild_id>            AGG#SERVER         overall server streak (points scored in ANY
                                               game that day): current_streak, best_streak,
                                               last_played_day
GUILD#<guild_id>            AGG#GAME#<key>     per-game server aggregate: current_streak,
                                               best_streak, last_played_day, total_plays
                                               (= days someone scored), players (string set,
                                               all-time, everyone who posted), players_30d
                                               (number, refreshed at finalize)
GUILD#<gid>#PLAYER#<uid>    AGG#SERVER         per-player overall streak (points scored in ANY
                                               game that day): current_streak, best_streak,
                                               last_played_day, total_plays
GUILD#<gid>#PLAYER#<uid>    AGG#GAME#<key>     per-player-per-game: current_streak,
                                               best_streak, last_played_day, total_plays,
                                               best/sum score fields where numeric
GUILD#<gid>#PLAYER#<uid>    PROFILE            welcomed_at (the explainer has been shown,
                                               see /help); dm_opt_in and a display-name
                                               snapshot are planned
SUGGESTIONS                 <gid>#<uid>#<name> one forwarded /suggest: guild_id, user_id,
                                               name, url, text (the paste), suggested_at
                                               (first asked), thanked_at (stamped by
                                               tools/broadcast.py once the game ships)
```

Access patterns → reads:

- **Play/Scores ordering**: one `Query PK=GUILD#<gid>, SK begins_with AGG#` returns every
  game's streaks, all-time player set, and 30-day count in a single call.
- **Rollups / rebuild**: a month is ≤31 `DAY#` items, one range Query; pivoted in memory
  for every player at once.
- **Multi-guild fan-out**: all configs share the `GUILDS` partition, so the scheduled
  lambdas load every guild with one small Query per tick.
- **Distinct players**: string set on the game aggregate; `ADD` is idempotent, and the
  all-time count is the set length. `players_30d` is computed once per day at finalize from
  the trailing 30 `DAY#` items and stored, so interactive reads stay one small Query.
- **Suggestion thank-yous**: every kept suggestion shares the `SUGGESTIONS` partition, so
  `tools/broadcast.py --game` reads them all with one small Query and matches each to the
  game with `match_suggestion()`.

A per-player overall streak is not derivable from that player's per-game items: a player
who alternates games has no per-game streak, so `PLAYER#<uid> / AGG#SERVER` is stored in
its own right.

## Configuration

Global config is env vars per lambda: `TABLE_NAME`, `DISCORD_BOT_TOKEN`, `DISCORD_BOT_ID`,
`MINIMUM_STREAK` on all three, plus `TEST_CHANNEL_ID` on the daily and sticky lambdas and
`DISCORD_PUBLIC_KEY` + `DEV_CHANNEL_ID` on the interaction lambda.

**Per-server config lives only in the table; there is no env fallback.** Each setting is
declared once as a `ConfigField` in `store.CONFIG_FIELDS`, from which the default, the
stored-value coercion, the slash-command option `register_commands.py` registers, and the
update `handle_setup` writes back all derive, so an option name cannot drift between the
registrar and the handler.

| Field | `/setup` option | Default | Meaning |
|---|---|---|---|
| `input_channel_id` | `channel`, `input` | unset | Channel scores are read from; the sticky lives here |
| `output_channel_id` | `channel`, `output` | unset | Channel the daily scoreboard posts to |
| `timezone` | `time timezone` | `UTC` | IANA name |
| `hours_after_midnight` | `time day_start_hour` | `0` | Hour the scoring day starts |
| `post_hour` | `time post_hour` | day start hour | Guild-local hour the board posts, repeating "Today's games" under it |
| `time_window_hours` | `time window_hours` | `24` | Hours submissions stay open each day |
| `minimum_players` | `limits minimum_players` | `1` | Games with fewer players are hidden and score nobody |
| `hundreds_of_messages` | `limits message_volume` | `1` | Input-channel volume (1–8), sets the fetch depth |
| `daily_enabled` | `daily enabled` | `true` | Whether the daily board posts |
| `sticky_enabled` | `sticky enabled` | `true` | Whether the sticky is maintained |
| `sticky_games` | `sticky games` | `0` | Today's games as play buttons on the sticky (0–`MAX_BUTTONS_PER_ROW`, one row's worth); 0 skips the ranking pass entirely and hands Play the whole roster |
| `delete_wordle_recap` | `sticky delete_wordle_recap` | `false` | Whether the sticky pass deletes the Wordle app's daily recap of yesterday's results |
| `suppress_embeds` | `embeds suppress` | `true` | Whether link previews are stripped off counted results |
| `reactions_enabled` | `reactions enabled` | `false` | Whether the sticky pass reacts to each fresh result with how it went and where it placed — see Reactions |
| `reactions_rotation_only` | `reactions rotation_only` | `false` | While a rotation governs the day, react only to its games |
| `rotation_enabled` | `rotation enabled` | `true` | Score only a rotating subset of the enabled games each day |
| `rotation_count` | `rotation games` | `3` | Games in the daily rotation; the upper bound is `len(GAME_SPECS)` (currently 22), so adding a game widens the option — re-run `register_commands.py` for the picker to follow |
| `rotation_mode` | `rotation mode` | `swap` | `swap` replaces under-played members, `random` re-draws daily |
| `rotation_keep_players` | `rotation keep_players` | `5` | Swap threshold to hold a seat: a scored game under it rotates out |
| `rotation_promote_players` | `rotation promote_players` | `5` | Swap threshold to win a seat: an off-rotation game reaching it rotates in |
| `rotation_off_mode` | `rotation off_rotation` | `shown` | Board display of off-rotation plays: `shown` below the scored games, or `hidden` |
| `rotation_announce` | `rotation announce` | `true` | Whether "Today's games" is posted; `false` draws the rotation silently and changes nothing else |
| `scoring` | `scoring mode` | `placement` | The points scale: `placement` (1st is worth the day's turnout), `per_game` (1 + players beaten, game by game) or `off` (scores only, no points) — see Scoring modes |
| `commentary_enabled` | `commentary enabled` | `true` | Whether the hourly commentary posts between boards — see Commentary |
| `commentary_overrides` | `commentary` (bare, a menu) | `{}` | Explicit per-guild flips of each message kind's default, keyed by `Trigger.key` |
| `commentary_midday_hour` | `commentary midday_hour` | `13` | Guild-local hour the midday standings board posts |
| `commentary_last_call_hours` | `commentary last_call_hours` | `3` | Hours before the window closes that the streak last call posts; `0` turns it off |
| `commentary_nudge_after_hours` | `commentary nudge_after_hours` | `2` | Hours since a player's last result before the nudge names them |
| `game_overrides` | `games` | `{}` | Explicit per-guild flips of each game's default state |
| `last_finalized_day` | — | — | Written at finalize; records how far aggregates are folded |
| `last_posted_day` | — | — | Written after a real post; the post gate |
| `missing_since` | — | — | Set by the hourly membership reconcile while the bot is not in the guild; both scheduled lambdas skip a marked guild — see Membership |
| `rotation_day` | — | — | The day the stored rotation governs (set_rotation, real runs only) |
| `rotation_games` | — | — | The rotation drawn for that day, as game keys |
| `rotation_prev_day` | — | — | The day the displaced rotation governed — the board scores that day hours after the new draw lands |
| `rotation_prev_games` | — | — | The rotation drawn for *that* day, as game keys |

A guild with no stored item resolves to these defaults with both channels unset, which
posts nothing anywhere.

The channel subcommands are declared the same way, once each as a `ChannelSub` in
`store.CHANNEL_SUBS` (name, the config fields it writes, its prose blurb, its Discord
description), which `register_commands.py` registers from and `handle_setup` dispatches
off. `/setup channel` writes **both** fields — one channel for everything is the path a
fresh server is pointed at, and the only one `go_live_hint()` names while nothing is
set. `/setup input` / `/setup output` write one field each, to override one side of it
afterwards; every reply from them says so.

## Games and per-server enabling

- Each game is one `GameSpec` in `game_parser.GAME_SPECS`, carrying its key, title, emoji,
  metric, URL, puzzle-number function, match pattern, score parser, and `disabled` flag.
- `GameSpec.disabled` is the game's **default only** — every game can be flipped either way
  per guild. `config.game_overrides` stores just the explicit deviations, so a newly added
  game reaches every guild with its coded default rather than a frozen snapshot of an old
  menu submission.
- `GameSpec.breakpoints` is `(good, medium)`: where a result stops being good and where it
  stops being medium, for its reaction (`game_parser.performance_tier`, see Reactions). In
  the metric's own number, lower is better: guesses, connections mistakes, cryptic weighted
  hints, `time` and `timed_win` seconds, travle +N, Fermi the percentile its share reports
  (`top 66%`) rather than the multiple it ranks on, whose scale belongs to the day's puzzle
  and not to the player. `score` and `maptap` compare the result's percentage of `total`
  instead, higher is better, so every score game carries its real ceiling in `total`
  (Chronophoto 5000, Krillion 700, Size It Up 500, MapTap 1000), and a result at that
  ceiling is aced. No score game's board line
  prints its total — the line is the
  bare number, while guesses, connections and cryptic lines keep their "/N" — so the
  ceiling never reads as a fraction. A spec without breakpoints gets no good, medium or bad.
- The effective list is resolved by `spec_enabled(spec, overrides)` / `build_games(pn,
  overrides)` and used by **all** paths: daily parse, aggregate updates, sticky counts, Play
  list, Scores, scoreboard render.
- Historical day and aggregate data for a disabled game is retained, and neither displayed
  nor accrued while disabled.
- `/setup games` manages the set: one admin-only multi-select of all games, pre-selected to
  the guild's current effective state.
- Wordle results posted as **images** by the official Wordle Discord bot are parsed with no
  configuration at all. That bot is one application, so it carries the same ID in every
  server it joins: `game_parser.WORDLE_BOT_ID` is a **constant, not a setting** — there is
  nothing per-server to discover, and no reason a server would want it pointed elsewhere.
  Grids are attributed to players by avatar hash, and multi-player grids match against
  server avatars as well as global ones.
- **It is free where that bot isn't posting.** Both image paths key on the message's author
  being `WORDLE_BOT_ID`, and both are reached only after the text-pattern loop has already
  failed: `match_message` considers attachments only for that author, and
  `build_avatar_pool` returns `{}` from an in-memory scan (`_has_multiplayer_wordle`) unless
  the window actually holds a multi-player grid — before any member fetch or CDN round
  trip. A server without the Wordle bot does no image work whatsoever.
- Two Discord payload caps bound the game surfaces (constants in `scoreboard.py`, noted at
  the `GAME_SPECS` declaration). The `/setup games` menu is one option per spec, capped at
  25 — past that it needs splitting across two messages. `/play` is one button per
  *enabled* game at 5 per row under the Random row, so a server may enable at most
  `MAX_ENABLED_GAMES` (20): the menu's `max_values` enforces it in the picker, and the select
  handler re-checks it on submit, writing nothing and handing the menu back with the picks
  still ticked. A default-on `GameSpec` shipping into a server already at 20 is the one way
  past the cap; `build_play_response` drops the tail of that list and says how many it left
  off rather than sending a sixth row.

## Daily rotation

- On by default: each day only a drawn rotation of `rotation_count` games counts toward
  the board — its points summary and scores section. A day is governed
  only when `rotation_enabled` is on and one of the two stored slots names it exactly
  with a non-empty list; feature off, state absent, stale, or empty all mean
  **unrestricted** — every surface behaves as if the feature did not exist. The first
  run after a gap (deploy day included) therefore scores unrestricted and starts the
  rotation from that morning.
- **The rotation scale** (`game_parser.rotation_points_base`). A governed day pays out
  on the day's turnout instead of each game's, and on **placement alone**: first place
  in *any* rotation game is worth the number of distinct players who showed up in the
  rotation at all, and each place below earns one fewer. A 4-player day pays 4 for a
  win in its 2-player game exactly as in its 4-player one, so playing the quiet game
  costs nothing. **Ties take the best place in the group** and the next player skips
  the places it consumed — standard competition ranking (1,2,2,2,5). On a 4-pool day a
  winner with three players tied behind them scores 4, and all three score 3; add a
  fifth player behind them on a 5-pool day and it reads 5 / 4,4,4 / 1. This is the one
  place the two scales disagree beyond the top value: the per-game scale pays a tie
  what its LAST place would (credit only for players actually beaten), which is why
  those three would score 1 there. Poops still earn 0 and still hold their places.
  Games below `minimum_players` still score nobody and are out of the pool as well,
  which keeps the pool ≥ any scoring game's field, so no place can drop below 1 point.
  Poops are *in* the pool — a failed result is participation, the same rule swap-mode
  earn-in uses.
- **Scoring modes** (`scoring`, `/setup scoring mode`). The rotation picks *which* games
  score each day; this setting picks *how*, and `game_parser.points_per_game` is the one
  place the scale is applied — the archive, the board, the Scores button and the live
  standings all fold through it. `placement` (the default) is the rotation scale above,
  and on an unrestricted day it pools everyone who played *any* game, so first place in
  each game is worth the day's whole turnout even before the first draw lands. `per_game`
  is the pre-rotation scale on every game: 1 point plus one per player beaten, ties paying
  what the group's last place would. `off` scores nothing — no points summary, no crown,
  scores still listed and ranked. Off-rotation games always freeze on the per-game scale
  whatever the mode, since they earn no board points at all. Streaks are untouched by the
  mode: `scoring_players()` keys on `is_poop()`, not on points, so a server with points
  off still keeps every streak. The change is forward-only — each `DAY#` item freezes its
  points on the scale that scored it (and records which, as `scoring`), and
  `tools/backfill.py` always replays history per game, the only scale a rotation-less
  archive can honestly carry.
- **Two slots** (`rotation_day`/`rotation_games` and `rotation_prev_day`/`rotation_prev_games`).
  Day D's draw lands at D's **start**, while the board scoring D−1 posts later that
  morning at `post_hour`, so the draw shifts the pair it displaces into the previous
  slot and `store.current_rotation` matches either one. Without that the board would
  find only D and score D−1 unrestricted. The board is never more than one day behind
  the draw, so two slots are all it needs; anything older matches neither and reads
  unrestricted, the stale-state fallback above.
- **Lifecycle** (`store.current_rotation` / `game_parser.next_rotation`). The daily
  lambda draws day D's rotation on the first tick at or after D's start
  (`lambda_function.draw_rotation`), independently of the board: it parses D−1 for
  the participation counts when swap mode needs them, and needs no message data at all
  in `random` mode or on a fresh draw. When day start and post hour are the same hour
  — the default, since `post_hour` falls back to `hours_after_midnight` — both stages
  run on the same tick and share one parse; when the post hour is later, the draw still
  lands at day start and the announcement repeats under the board. `swap` mode treats membership
  as earned by participation (distinct posters on the scored day, poops included — the
  same count the archive stores), against two independent thresholds: members that
  drew at least `rotation_keep_players` **stay**, off-rotation games that drew at
  least `rotation_promote_players` **join**.
  `rotation_count` is a hard cap — more qualifiers than slots keeps the most played,
  with an exact tie favoring the sitting member (stable sort) — and the bot fills any
  remaining slots at random from the enabled remainder, never a key that just fell
  out, unless nothing else is left to keep the board from shrinking. (Earn-in is a
  swap-mode rule.) `random` mode re-draws the whole set. Yesterday's list seeds the
  swap only when it actually governed the scored day.
  Disabled games are never in the pool, and every consumer intersects the stored
  rotation with the built game list, so a mid-day `/setup games` disable drops a game
  everywhere at once.
- **Off-rotation results.** Every enabled game is parsed, archived, and finalized on
  every day, rotation or not — real points are frozen for off-rotation games, so
  per-game, per-player, and overall streaks stay alive off-rotation, and their play
  counts still drive earn-in. The rotation narrows the scoring to its own games: the
  points summary sums rotation games alone, on the rotation scale below. `rotation_off_mode` is purely a **daily-board
  display** switch — `shown` (default) renders the played off-rotation games as a
  separate zero-point section below the scored ones, `hidden` omits that section and
  changes nothing else (not the archive, not the sticky counts, not `/play all`). The
  `DAY#` item records the governing rotation, so future rollups can exclude
  off-rotation frozen points — the per-player `points_sum` aggregate still includes
  them, and the day record stays the rollup source of truth.
- **The announcement.** With the draw, the daily lambda posts "Today's games": a bare
  header over the new rotation as link-button rows — the buttons carry the emoji-title
  labels, the content repeats none of them. It goes to the output channel (input
  channel if that is unset, so a guild with the board off still gets it) on **two
  triggers**: any tick that draws the rotation, and any tick that posts the board — the
  post that actually notifies the channel. A guild whose post hour is later than its day
  start therefore sees it twice a day, at the draw and again under the board; one posting
  at day start (the default, `post_hour` falling back to `hours_after_midnight`) has both
  on one tick and sees it once, as does one with the board off. `rotation_announce`
  `false` silences **both** triggers and nothing else — the draw still lands, the board
  still scores the drawn set, and the sticky and `/play` still narrow to it; the guild
  simply gets no second message. Each trigger fires once a
  day — the draw is monotonic in the day, `last_posted_day` lets the board through once —
  so a board that never posts (empty input channel, or a marker healed from a manual post)
  costs that day only its second announcement, never the one at the draw.
  Deliberately a plain message, not Components V2 — `is_scoreboard_message` keys on
  that flag, so a V2 follow-up would hijack the sticky's Yesterday link and the
  posted-today dedup scan — and none of its buttons is the sticky Play button, so the
  sticky pass never matches it. Silent, embeds suppressed, never pinned, skipped
  entirely when unrestricted.
- **Ordering.** The draw persists **first** — before the board on a tick carrying both,
  so a post that fails cannot cost the day its rotation — and only then is anything
  announced: a crash in between costs one announcement (recoverable, since any later
  tick that posts the board announces the stored list), where the reverse would let the
  next hourly tick draw a *different* set into a day whose games have already been
  listed. A tick that runs both stages orders them `set_rotation` → post board → announce
  → pin → `set_last_posted`, so the announcement still sits directly under the board,
  ahead of Discord's "pinned a message" notice. A tick that only posts the board
  announces `stored_rotation` and writes nothing — with the streak flair and ordering
  recomputed, so that second post reflects the finalize the board just ran. `set_rotation` is the conditional-monotonic
  run-marker idiom carrying the lists, so double fires keep the first draw and cannot
  shift a good previous slot out from under the board; `process_guild`'s `draw_due`
  applies the same monotonic test before drawing, so a stored draw that already names
  today (or a later day, after a timezone or day-start edit moved the boundary) is left
  alone rather than redrawn hourly. Test runs post board + announcement to the test
  channel but never call `set_rotation`; like `last_posted_day`, rotation state
  advances only on a real run, so repeated test runs leave it untouched — over a day
  already drawn they announce that live set, and otherwise draw a throwaway one.
- **Consumers.** The rotation picks the games the sticky's row draws from; bare `/play`
  and the sticky's Play button are that row's **complement** — every enabled game *except*
  the ones already on screen as buttons — so the two surfaces partition the roster instead
  of repeating it. `/play all:true` is the one list that carries both, scored games sorted
  above the rest; an exhausted complement points at it. With the row off (`sticky_games` 0)
  there is nothing to subtract and Play lists everything, exactly as it did before
  rotations existed — the rotation narrows what the sticky shows, never what Play offers.
  The Scores
  button renders exactly like the board, `rotation_off_mode` included. All of them see
  the new set from day start — including the pre-post-hour window that used to read
  unrestricted, and the morning window before the announcement itself goes out — and
  `daily_enabled` off stops only the board: the rotation still draws and announces
  (`rotation_announce` off is the switch for the announcement alone, and stops nothing
  else).

## Commentary

The posts between boards: a nudge to whoever left today's games half-played, a midday
standings board, a last call for streaks about to break, and one-line callouts — lead
changes, beatable ties, a clean sweep, a first-ever result. On by default;
`/setup commentary enabled:False` stops it. The nudge is the one kind that ships **off**
(`Trigger.default`), until its own cadence is settled; every other kind is on.
Posts go to the **input** channel, where the
players are (the test channel on a test run).

- **A registry, not a pipeline.** `commentary.TRIGGERS` is a list of `Trigger` entries in
  priority order; each is one message kind with its own `detect(tick) -> events`,
  `render(events, tick)`, `sample(tick)` (for the preview), `cadence`, `notify` level,
  coded `default` and `once` flag. Adding a kind is one entry; the engine supplies dedup,
  the one-post rule, the gates, the notification policy, the `/setup commentary` menu (built from
  the registry, so a new kind needs no `register_commands` run) and persistence. The
  per-kind switch is `commentary_overrides`, an explicit-deviations map resolved by
  `trigger_enabled()` exactly like `game_overrides` by `spec_enabled()`.
- **Two cadences.** `HOURLY` kinds run on the daily lambda's tick
  (`lambda_function.commentary_tick`): the scheduled ones — last call, midday board,
  nudge — and the beatable-tie line, a nudge in spirit that waits for the hour and reads
  beneath the nudge when both fire. `STICKY` kinds run on the sticky's every-minute pass
  (`sticky_lambda.run_commentary`), which already holds the parse: the ones that answer a
  result the moment it lands — first result, lead change, clean sweep — so they post within a
  minute of the message that caused them, and the sticky reposts beneath them in the same
  pass (the post is put at the head of the working list before `update_sticky` runs).
  Each pass evaluates only its own cadence, and both persist to the same state item.
- **Three kinds.** `BODY` is the message itself (last call, nudge): at most one body per
  pass, the first in registry order wins and the rest re-detect next time. `BOARD` is a
  Components V2 board as the body (midday standings). `FLAVOR` lines ride beneath whatever
  body posts in the same pass — under a board as one more Text Display, unless that breaks
  the board's budget, in which case they wait — or post on their own. **One post per guild
  per pass**, whatever fired.
- **The tick.** `commentary.make_tick` assembles one `Tick` from what a pass has already
  fetched and parsed: today's results with each player's latest result time
  (`parse_results(times=...)`, or the sticky's own loop), the built games, today's
  rotation, the streak bundle, the guild's aggregate partition (`scoreboard.guild_aggs`,
  the cached Query the board already makes — its `players` sets are the roster), a
  memoized per-player aggregate loader that only the last call spends (the daily lambda
  supplies it; the sticky passes none), the day's state, and the two gate flags below.
  Derived views (`scored`, `scorers`, `pool`, `totals`) are cached properties, so a
  trigger never repeats a fold.
- **Gates** (`commentary.blocked`, every kind, both cadences): nothing before one hour
  after day start or after the window closes; nothing until the board covering yesterday
  has posted (`last_posted_day`, the same gate the sticky's Yesterday link uses — the
  morning board opens the conversation; a guild with the board off skips this one); and
  a quiet rule, per kind (`Trigger.waits`), over the bot's own messages since anyone else
  last spoke, the sticky aside (`unanswered_posts`: a board, an announcement, a commentary
  post). By default (`ANY`) a kind waits while there are any, so the bot never stacks two
  messages in a row. The nudge (`OWN`) waits only while one of them is itself a nudge, told
  apart by its openings (`NUDGE_HEADINGS`): a lead-change line, the midday board or the
  morning board never hold a nudge back, but two nudges never stack. The midday board and
  the last call (`NEVER`) wait on nothing, because a scheduled post is not the bot talking
  twice and a one-liner an hour earlier must not push it back for hours (a replay of a busy
  day showed exactly that). The last call needs it for a second reason: its server-streak
  branch fires only on a day nobody has played, and on exactly such a day nothing ever
  clears the morning board out of `unanswered`, so `ANY` held it back on every day it had
  something to say. Bodies then gate
  themselves: the last call fires on the first pass within
  `commentary_last_call_hours` of the close, the midday board on the first pass at or
  after `commentary_midday_hour` with somebody actually on the standings (exactly what
  `format_points_summary` would print, so the kind stays quiet on a day whose board would
  be empty: scoring `off`, or only poops so far), the nudge
  `commentary_nudge_after_hours` after a player's last result and never inside the final
  hour (the last call owns it).
- **What each kind says.** *Nudge* (the kind that ships off): everyone who has a result in
  a scored game, has scored games left, and last posted at least
  `commentary_nudge_after_hours` ago — one line each
  with the games still open and what first place there pays right now (`placement`: the
  day's pool, `per_game`: one more than the players already in it, `off`: no number), plus
  one row of link buttons for the union; pings. *Last call*: players whose server streak
  (alive through yesterday, nothing scored today) or per-game streak (same, that game
  unplayed today, off-rotation included — streaks survive off-rotation) dies at the close,
  filtered through the same `shown_streak()` floor (`MINIMUM_STREAK`) every other surface
  spends, so the ping never names a streak the board wouldn't print;
  longest first, at most 10 names; pings; notes the server's own streak when nobody has
  scored yet. *Midday*: **the standings so far and nothing else**
  (`format_scoreboard_components` with `MIDDAY_TITLE` and `standings_only`), so the post is
  the header container alone — heading, server streak, points summary — with no games
  section under it and `rotation_off_mode` therefore moot; `NOTIFY`. Points still come from
  the rotation alone, as on the morning board. One subtext line under it counts the results
  and games the Scores button would show this guild (the rotation, plus off-rotation games
  where `rotation_off_mode` is `shown`) and points at the button; no sticky means no
  button, so the line is dropped rather than pointed at nothing. *First
  result*: a player in today's results who is in no game's all-time `players` set —
  checked only once yesterday is finalized, since the sets fold at post hour. *Lead
  change*: a new **sole** leader who is clear of one win's worth of points (`one_win`: the
  pool under `placement`, the biggest game's field under `per_game`), at most once per
  clock hour — the event id is the hour, so a second change in the same hour dedups away.
  Replays of real days showed the lead flipping on every result through the first two
  hours at 2 to 6 points, and shared leads flipping back within minutes; this rule kept
  the two or three changes a day that were news. *Clean sweep*: one player sole first in
  most of the day's scored games — a strict majority of the slate (`tick.scored`, so the
  rotation when there is one) and never fewer than `SWEEP_MIN_GAMES` (3). The bar is a
  share of the day, not a count of results, because measuring it against what had been
  played meant announcing whoever was in front of the first two results of the morning;
  a five-game rotation now asks for three games, a three-game one for all three. Only
  contested (≥ 2 players) games count toward it, and the line says “a clean sweep in the
  making” only while that player holds every contested game — otherwise it just says how
  much of the day they have. How a single result went is no line at all: it is that
  result's own reaction (see Reactions). *Tie*: a scored
  `guesses`/`connections`/`cryptic` game whose best non-poop score is shared and beatable,
  with what first outright would pay. Every line has two or three phrasings, picked by a
  hash of the day and the event, so a retried pass repeats itself rather than rewording.
  House style: no em dashes in any line except the per-player nudge lines.
- **How loudly a post lands** (`Trigger.notify`, three levels defined in `scoreboard.py`
  beside the flag they map to, spent by `scoreboard.send_commentary`, shared by both
  passes). `SILENT` adds `FLAG_SUPPRESS_NOTIFICATIONS`, so nobody gets a push: the sticky
  one-liners (first result, lead change, clean sweep). `NOTIFY` drops that flag but names
  nobody, so it reaches whoever has the channel on All Messages and no one else: the
  midday board and the tie line, both once-a-day-ish and worth noticing. `PING` also puts
  the users the render names into `allowed_mentions`: the nudge and the last call. Discord
  marks the channel unread either way, so `NOTIFY` differs from `SILENT` only for members
  who opted into All Messages. Every other mention renders as text without notifying.
  A composed post takes the loudest level of the parts that are actually IN it
  (`commentary.loudest`) — a losing body waits for a later pass and must not raise the
  level of a message it contributed nothing to.
- **State** (`COMMENTARY#<day>`, two attributes): `announced`, a string set of every event
  id that has gone out — nothing is said twice, and a body that lost a pass to a
  higher-priority one is simply re-detected next time — and `standings`, the last pass's
  snapshot the lead-change trigger compares against. Two writers share it, so
  `store.record_commentary` **updates** rather than overwrites: `ADD` on the set, `SET` on
  the snapshot, and only when a pass has something to fold (`to_record`) — **before** the
  post goes out, so a record that fails stops the post rather than letting an unrecorded
  one be said again the next time a human posts. `once` kinds
  (last call, midday) have their key as their one id and are not even asked again after
  it: that is what keeps the last call from re-reading every player's partition on the
  hours after it fired. Never written on a test run.
- **The midday board is not a scoreboard.** `is_scoreboard_message` matches any Components
  V2 message, which would make a midday board in the input channel the sticky's Yesterday
  link and satisfy the posted-today check — so it excludes a board whose first Text Display
  starts with `board_heading(MIDDAY_TITLE)` (`is_midday_board`). The daily board, the Scores
  button and the midday board all open with `game_parser.board_heading`, so the title is the
  only thing that tells them apart and nothing else can drift.
- **Previewing.** `{'test': true, 'commentary': true}` on the daily lambda runs its stage
  alone for every guild, switched on or not, to the test channel — the real hour's
  evaluation against the real channel and state, writing nothing. `{'test': true,
  'commentary': 'samples'}` posts one example of every registered kind (both cadences)
  from fabricated events over the live parse (real players, real games), overrides and
  gates ignored, so a message is on screen before it is switched on. The sticky's cadence
  previews with `python3 src/sticky_lambda.py '{"test": true, "commentary_enabled": true}'`
  (any config field overrides straight from the event, as always). Fixtures:
  `tests/events/daily/commentary_test.json`, `commentary_samples.json`.
- **Replaying history.** `tools/replay_commentary.py` walks one archived day of the input
  channel minute by minute through both cadences — streak state rebuilt from the `DAY#`
  archive as it stood the evening before, the board landing at post hour, every post the
  bot makes fed back into the stream so the quiet gate sees it — and prints the timeline:
  results as they landed, what posted, what was held and why. Read-only. The way to see
  what a trigger change would have done before shipping it.

## Reactions

With `reactions_enabled` on (off by default), the sticky pass reacts to every fresh result
with one to four emoji, in this order: where it placed in its game the moment it was
posted, how it went (drawn at random from that tier's pool), then a flourish or two if it
was good enough to earn one.
`sticky_lambda.react_to_results` does the Discord side; the rules are pure, in `game_parser`.

- **Where it placed** (`place_at_post`): 1 plus every earlier first result in that game
  strictly better by `score_sort_key`, so a tie shares the better place, as on the board.
  🥇 🥈 🥉 (`PLACE_EMOJI`), 👍 below third; nothing for the first result in a game, and
  nothing on a poop. A set of its own rather than the board's `MEDALS`, so a reaction reads
  as the medals it sits beside while the board keeps its 👑 for the day's winner and each
  game's first place; only first place differs between the two.
- **How it went** (`performance_tier`). A poop is `is_poop`, so the reaction and the
  board's medal agree. Aced is the perfect result of the games that have one: a `guesses`
  game in 1, a connections grid with no mistakes (a VERT, which ranks above that, too), a
  cryptic with no hints, a `score` or `maptap` result at its ceiling (`total`), a Fermi in
  the world's top 1% (`FERMI_ACE`). Anything else is good, medium or bad against the
  game's `breakpoints` (see Games and per-server enabling). A Travle that missed the target is
  bad; a Gerrymandle won with the timer hidden has no time to measure, so no tier. Every
  tier carries an emoji, **drawn at random from its own pool** (`TIER_EMOJI`), seeded off
  the message id like the flourish below, so two good days in a row don't read the same: 💯 aced, then
  😎 good, 🙂 medium, 😬 bad and 💩 poop and their pool-mates. The ace is the one
  fixed point, a pool of one, because 100 is the only thing a perfect result should say.
  The rough tiers speak too: the bot noticing a bad day, not scolding it, so the bad and
  poop pools are wry rather than cutting. The first entry of each pool is its
  representative, which is what `/help` and `/setup` print (`tier_examples`), so those
  blurbs stay in step with the pools without reaching into their shape.
- **Never nothing** (`result_reactions`). With every tier speaking, the only result left
  bare is an untiered one that is first in its game (no place, no tier); it gets 👍 on
  its own. It says "counted", not "well played", and it keeps no reaction meaning the one
  thing it should: the bot didn't read the message.
- **A flourish on top** (`FLOURISH`, `FLOURISH_COUNT`): two more emoji on an ace, one on a
  good result, nothing below that, drawn at random from a bank of fourteen so two aces in a
  row don't read the same. The bank holds no game's emoji, no place, no tier and none of
  the app's own signs (🔥 streaks, 🏆 points, 💔 a broken streak), so a flourish can
  only mean "nice one"; the same rules govern every `TIER_EMOJI` pool, and every entry of
  both is a single code point needing no variation selector, so what Discord stores back is
  exactly what was sent. **Both draws are seeded on the message id**, which is what makes
  them safe: the pass is stateless and re-runs over the same result for as long as
  `REACTION_WINDOW` holds it open, skipping what it already added, so an unseeded draw
  would pile a fresh emoji on every minute. The tier draw takes `seed + tier` and the
  flourish the bare `seed`, keeping the two independent: a result whose tier moves between
  passes (a percentage game whose ceiling shifts) re-draws its tier without disturbing the
  flourishes already on it.
- **Which results.** Matches are walked oldest first. A player's first result in a game is
  the one that counts, as on the board, and a repost gets nothing. The Wordle app's own
  messages count toward places but get no reaction: several players share one, and the app
  keeps editing it. With `reactions_rotation_only` on and a rotation governing the day,
  only that rotation's games get reactions; places still count every result.
- **Stateless and bounded.** An emoji the bot already has on a message (`me`) is skipped,
  so the several full passes that see a result add nothing twice. Only results newer than
  `REACTION_WINDOW` (20 minutes, past the probe's ten, so a reaction that failed is retried
  by the full pass the probe lets through) are touched, and a pass sends at most
  `REACTIONS_PER_PASS` (20), so switching it on never sweeps the backlog. It runs after
  `update_sticky`, in its own try/except, so a reaction never delays or costs the sticky.
  A 403 ends that pass's reactions with a note: the bot needs **Add Reactions** there. The
  invite asks for it; older invites didn't, which only matters where a server has taken it
  from `@everyone`.

## Streak semantics

- A "day" is the existing `reference_date`, already timezone- and
  `hours_after_midnight`-aware. Streaks inherit the exact scoring window the scoreboard
  uses.
- **A play is a scoring result, not a posted one.** Poop scores earn 0 points
  (`compute_points`), and 0 points keeps nothing alive. Server streak per game =
  consecutive game-days with ≥1 result that *scored* for that game; a day everyone failed
  breaks it. Player streak = the same per user. Overall streak, server-wide and per player,
  = points scored in any game. `game_parser.scoring_players()` is the one definition, and
  the finalize fold uses the points it archives, so stored and displayed streaks cannot
  disagree. Games below `minimum_players` score nobody and so extend nothing — the same
  games the board omits. All-time player sets and 30-day counts still count everyone who
  posted: participation is a separate question from scoring.
- Streaks update **once per day at finalize** (the daily scoreboard run). Played on day D:
  `last_played_day == D-1` → increment, else reset to 1. Not played on D: archive into
  `best_streak` if higher, reset to 0.
- **Active** (for display and sort) = `last_played_day >= yesterday`; otherwise renders as
  0. Live views (sticky, Play, Scores) display `current_streak + 1` for games already
  played today per the live parse, so a 12-day streak reads "🔥 13" the moment someone
  keeps it alive.
- A game disabled for a while and re-enabled resumes from whatever `last_played_day`
  implies — normally a reset streak.
- One display threshold governs every *server* streak surface (the board's server line and
  game suffixes, Play suffixes, sticky flair, break callouts): the `MINIMUM_STREAK` env var,
  default 3. Shorter streaks still accrue but neither render nor sort: `game_sort_key`
  spends the same `shown_streak()` threshold the labels do, so a streak nothing on screen
  shows can't move a game in the order either. `/stats` is deliberately outside it — see
  Read paths and display.

## Write path (daily lambda, the only writer)

After parsing yesterday's results:

1. Write the `DAY#` item — plain overwrite, idempotent. Points are computed via
   `points_per_game` and **frozen into the item**, so historical rollups survive future
   scoring-rule changes; the governing rotation is archived alongside them (see Daily
   rotation). The rotation sets the scale its own games freeze on — what the item
   stores for them is what the board printed — while off-rotation games freeze on the
   per-game scale, the yardstick their zero-point board section never put them on.
   `tools/backfill.py` archives no rotation and so replays every day per-game.
2. Update `AGG#SERVER`, each `AGG#GAME#*`, and each player's `AGG#SERVER` and `AGG#GAME#*`
   via conditional writes guarded per item on `finalized_through` (the last day folded into
   that item). A double-fire cannot double-increment, and a run that crashes halfway resumes
   cleanly, because the retry updates exactly the items the first attempt didn't reach.
   `last_finalized_day` advances at the end as the run-level marker.
3. Refresh `players_30d` on each game aggregate from the trailing 30 `DAY#` items.
4. ≈50–100 writes per day per guild, paced under 5 WCU.

`tools/backfill.py` replays channel history day by day through the same parser, writing
`DAY#` items and then computing all aggregates from them, so streaks launch at their true
historical values. Its `--rebuild-only` mode recomputes every aggregate from the archived
days without touching Discord, which is also how a scoring-rule change is applied
retroactively.

## Read paths and display

- **`store.py`** owns all DynamoDB I/O and the config schema. IAM per lambda role:
  Query/GetItem/PutItem/UpdateItem/Scan on the table ARN.
- **Game ordering** (`game_sort_key`, one shared helper): today's live count desc → active
  *visible* server streak desc (`shown_streak()`, so below `MINIMUM_STREAK` it is 0 here
  exactly as it is absent from every label) → distinct players in the last 30 days desc
  (`players_30d` off the game aggregate, via the streak bundle) → all-time distinct players
  desc → title. The 30-day
  tier keeps the tail current: all-time sets only grow, so without it a game the server has
  drifted away from outranks a newer one forever. Used everywhere games are
  listed — Play buttons, the sticky's game row, and scoreboard
  sections — so the app presents one consistent order. One helper (`game_link_button`)
  renders every game link button: emoji, title, and a streak suffix — `🔗 Connections 🔥14`.
  Today's live count orders the list but is not in the label. `sticky_row_games` sits on
  the same ordering and is the single definition of *what the sticky is showing*: the
  sticky renders it and the Play list subtracts it, so the two can never drift into
  showing one game twice or dropping one between them.
- **The board shows the server's streaks; `/stats` shows yours.** That split is the whole
  display rule, and it is why a `🔥` on the board always means "this server" and never
  "you". A board carrying a number per player per game rendered around forty of them on an
  ordinary day, in three different senses of the same emoji; the numbers were never the
  problem, the ambiguity was.
- **Scoreboard and Scores button** (`format_scoreboard_components`, one shared path) take an
  optional streaks argument, and everything it renders is server-wide:
  a `🔥 **N-day server streak**` line under the heading (`_server_streak_line()`, carried on
  the heading's own Text Display so it costs no component), a `🔥N` suffix on each game's
  title line, and a `💔 <Game> streak ended at N` callout on the day it breaks. Score lines
  and the points summary carry no streak markers at all.
- **Where break callouts land**: with a rotation, at the foot of the **Other Games**
  container — a broken streak is a game nobody played, which is what that block is, and the
  container renders for callouts alone when no off-rotation game was played. Without a
  rotation (or under `rotation_off: hidden`, which suppresses that container) they fall back
  to the foot of the scores section. On a no-results day, and on a `standings_only` board
  (the midday standings), where there is no scores section either, they fall back to the
  header container.
- **A board with nothing on it** (`empty_board_lines()`) says which kind of empty it is. A
  live view (the Scores button, the midday standings, a `days_back: 0` preview) passes
  `live=True` and reads "no scores **yet**", names a few of the day's games, and closes on
  the server streak today would cost; the posted board's day is closed, so it says nobody
  played and points at today instead. The headline comes from a small pool keyed on the
  date, so a run of quiet days doesn't repeat itself, and both render **gray** rather than
  gold, which is the same rule the rest of the board follows: gold means the day was
  scored. `empty_hint` replaces the closing line — `interaction_lambda.empty_board_hint()`
  spends it on the one thing the board can't know, that a live view run outside the
  scoreboard channel is empty because it parses the channel it was run in.
- **`/stats`** (`gather_player_stats()` + `format_stats()`) is the personal counterpart: an
  ephemeral reply listing the invoker's overall streak, best, and lifetime plays, then one
  line per game with a live streak, ordered by that streak, with lapsed games named in a
  single subtext line. One Query on `GUILD#<gid>#PLAYER#<uid>` covers every game the player
  has ever played, so the cost is flat in the size of the server, and today's live parse
  folds in the same "played on ref_date" flag every other surface uses. **`MINIMUM_STREAK`
  does not gate it**: the board shows what is worth announcing to a server, `/stats` shows a
  player their real numbers, and a streak of one is a real number. Reading the guild's
  *input* channel rather than wherever `/stats` was typed is what makes today countable; a
  failed read costs the day, not the reply, since stored history is the substance.
- **Why `gather_streaks()` is server-only**: it used to fan out a `batch_get` across every
  player who scored, to feed markers no surface renders any more. That read is gone with
  them — one partition Query is now the whole cost of a board.
- **Sticky**: up to two rows — today's games (`sticky_games` of them, in `sticky_row_games`
  order, drawn from the rotation where one governs the day) sitting *above* the action row
  Play · Scores · Yesterday, because the games are what the sticky is for and the buttons
  are the chrome around them. Above both sits the content: the heading carrying the
  server-wide streak inline — `👾 Now Playing · 🔥17` — over the day's game and play counts.
  The streak rides on the heading rather than the counts because it is the server's, not
  the day's; it survives a rollover the counts reset through.
  `sticky_games` is capped at `MAX_BUTTONS_PER_ROW` — a sixth game wraps to a second row,
  which is the screenful the sticky is trying not to be — and the cap and the config bound
  are the same constant, declared in `store` next to `PIN_CAP`.
  There is no More: Play is already everything the row isn't, so a second list button would
  only ever re-list what is on screen. There is no How it works either: the explainer
  already finds a newcomer as the follow-up under their first live view, so the sticky
  spends its width on the games rather than a door nobody needs twice. Neither custom ID is
  routed any more — the sticky reposts whenever its buttons change, so a stale row lives a
  minute at most. Yesterday appears only
  once the board covering the day before the tracked one has posted (`last_posted_day`),
  so a guild whose post hour is later than its day start loses the button for that morning
  window rather than pointing it at a day-older board. `sticky_games` is 0 by
  default, and at 0 the ranking pass is skipped rather than run and thrown away. The sticky
  is identified by its own Play button, so extra rows never confuse the match; it reposts
  when its content *or* any button changes, which covers the counts moving, the game row
  reshuffling as the day's plays land, and an admin resizing or removing it.

## Commands

- **`/setup`** — admin-only via `default_member_permissions` = Manage Server; the handler
  re-verifies `member.permissions`, since servers can re-map the default. Subcommands, in
  the order `register_commands.py` lists them (which is the order Discord displays):
  `show` · `channel` (both sides at once) · `time` · `limits` · `games` · `daily on|off` ·
  `sticky on|off` (off also deletes the existing sticky; carries the optional `games`
  row size and `delete_wordle_recap`, `ConfigField`s in the `sticky` group that
  `toggle_sub` appends the same way `field_sub` builds a whole subcommand) ·
  `rotation on|off` (carries the four
  `rotation`-group fields the same way; the mode fields register fixed choice menus off
  `ConfigField.choices`) · `scoring mode` (a `field_sub` like `time`, one choice option)
  · `commentary [enabled] [midday_hour] [last_call_hours]` (the one toggle whose boolean is
  optional: with no options at all it answers with the per-kind menu, one option per
  `commentary.TRIGGERS` entry ticked to the effective state, written back as
  `commentary_overrides` exactly the way `/setup games` writes `game_overrides`) ·
  `embeds suppress:on|off` · `reactions on|off` (carries `rotation_only`, the one
  `reactions`-group field) ·
  `input`/`output` (override one side of `channel`, so they sit last). `limits` carries
  the display minimum and the message volume only — the Wordle bot is a code constant, not
  a per-server option. That array is
  display order and nothing else — dispatch is by name, so it is free to churn;
  `check_channel_coverage()` fails the registration if a declared `ChannelSub` was left
  out of it. Each channel subcommand takes a channel-type option, a `channel_id` string
  escape hatch for channels the picker can't show, or no arguments at all — the reply is
  then an ephemeral channel-select menu. Every path validates that the bot can see the
  channel and errors with instructions when it can't.
- **`/play`** — ephemeral list of today's games as link buttons, in `game_sort_key`
  order, with streak suffixes, plus a Random row. Under a rotation it lists the games
  that score today; the optional `all:true` lists every enabled game, scored games
  sorted above off-rotation ones (see Daily rotation).
- **`/stats`** — open to everyone, no permission gate: the invoker's own streaks, overall
  then per game, as an ephemeral reply. Deferred like the other live views, since it reads
  the channel to decide whether today counts yet. See Read paths and display.
- **`/suggest`** — open to everyone, no permission gate: a modal (Discord's only multi-line
  input) taking a game name, an optional link, and a pasted result, posted to the
  `DEV_CHANNEL_ID` channel as a candidate `GAME_SPECS` entry. The paste goes in a code
  fence, the link in angle brackets, and `allowed_mentions: {parse: []}` on the post, so
  nothing a stranger typed can ping or unfurl in the dev's server.
  `game_parser.match_suggestion()` short-circuits games already in `GAME_SPECS` — exact
  name or key, or a spec's own host and path among the submitted links — and answers
  whether the game is tracked or merely off in this server. Modal submits (interaction
  type 5) answer inline rather than deferring. A suggestion forwarded from a server is also
  kept (`store.record_suggestion`, keyed by server, user and name, so asking twice updates
  one item) before the post goes out — it outlives a failed post and the 30-day log, and
  `tools/broadcast.py --game` reads it back to thank the sender in that server once the
  game ships. Keeping it never fails the reply.
- **`/help`** — open to everyone: the explainer (`interaction_lambda.build_help_text`),
  an ephemeral reply built off the live config every time — how to play, what a result is
  worth under the server's `scoring`, the rotation, the day's clock, streaks, reactions
  where they're on, and the command list — so it never describes a setting the server
  doesn't have: the sticky, the board and the Today's games post are named only where
  they're switched on. The scoring line follows the same rule, saying "today's games"
  only where a rotation narrows them and plain "any game" otherwise. Answered inline;
  nothing in it reads a channel.
- **First interaction.** The first time a player opens any live view (Play, Scores,
  `/stats`), phase two of the deferred reply sends the same explainer as a second
  ephemeral follow-up on the interaction token, after the view they asked for, and stamps
  `PROFILE.welcomed_at` (`store.mark_welcomed`). `/help` stamps it too, so nobody is
  welcomed twice. The check is one GetItem per deferred click. The inline
  fallback path (no self-invoke) skips it, and any store or Discord failure costs a repeat
  welcome later, never the reply.
- **Usage log.** Every verified interaction except Discord's endpoint PING prints one
  JSON line before it is routed (`interaction_lambda.log_interaction`):
  `{"event": "interaction", "kind": "command" | "component" | "modal", "name": "setup
  rotation" | "sticky_play", "args": "enabled=True games=3", "guild", "channel", "user",
  "username"}`. It is the bot's only usage telemetry — nothing else records a click. One
  line per click, written by the invocation that ACKs it: the self-invoked second phase of
  a deferred reply is the same click, and the keep-warm ping returns before it. JSON
  rather than prose because Logs Insights discovers a JSON line's keys as fields, so on
  `/aws/lambda/daily-game-play` a query needs no parse step:

  ```
  filter event = "interaction"
  | stats count() as clicks by name, username
  | sort clicks desc
  ```

## Scheduling

- The daily rule is **hourly**, and carries two independently gated stages so one rule
  covers every guild's own clock. Each tick loads all configs and, per guild, posts the
  board when the local hour has reached its `post_hour` and `last_posted_day` is stale
  (with a scoreboard-already-in-output-channel check as belt and braces), and draws the
  rotation when the scoring day has rolled over past `rotation_day`, which lands it on the
  first tick at or after the guild's day start. Posting and finalizing are decoupled: test
  runs finalize, idempotently, but never post for real and never advance `last_posted_day`
  or the rotation.
- The sticky rule fires every minute, loops guilds the same way, and runs around the
  clock: the day it tracks is whichever one `reference_date` says is open, so it rolls
  over to "No scores yet today" at each guild's **day start** rather than waiting for the
  board. Between day start and a later post hour the newest board in the channel still
  covers the day before the one being tracked, so the Yesterday button is dropped until
  `last_posted_day` reaches that day.
- Link-preview suppression rides on that pass: each message the sticky counts also gets its
  embeds flagged away when `suppress_embeds` is on (the default). It therefore needs Manage
  Messages, and does nothing in a guild with `sticky_enabled` off — that guild is skipped
  before anything is scanned. Turning it off stops future stripping; it never restores an
  already-stripped preview.
- So does Wordle-recap cleanup: with `delete_wordle_recap` on (off by default), every
  fetched message matching `is_wordle_recap` — the Wordle app's once-a-day "here are
  yesterday's results" post, identified by its `summary_launch` button, never the
  live-game message scores are parsed from — is deleted (Manage Messages again) and
  dropped from the working list, so a recap that landed on a settled sticky doesn't force
  a repost. A failed delete leaves the message in the list and the sticky reposts below
  it as usual.
- Reactions ride on it too (`reactions_enabled`, off by default; see Reactions): after the
  sticky settles, in their own try/except, needing Add Reactions, and nothing at all in a
  guild with the sticky off.
- With `daily_enabled` off the board stops and the sticky drops its Yesterday link; the
  rotation stage keeps running, so today's games are still drawn and announced.
- The commentary's `HOURLY` kinds ride the same hourly rule, after the board and the
  draw, for guilds with `commentary_enabled` on: their own parse of *today's* input
  channel, their own try/except, their own summary part — a failed board never costs the
  hour its commentary, nor the reverse. Its `STICKY` kinds ride the sticky's every-minute
  pass, on the parse it already makes, again in their own try/except so a commentary
  failure never costs the channel its sticky. See Commentary.
- Onboarding is automatic: `/setup` writes the config item and the next tick picks the guild
  up, with no deploy or schedule change.
- **Test mode is event-driven.** `{'test': true}` on the daily posts every guild's board to
  the test channel (`test_channel_id` in the event overrides the `TEST_CHANNEL_ID` env;
  `guild_id` filters); on the sticky it runs the test channel under a default config, with
  any config field overridable straight from the event.
- **Test mode never writes.** It reads the real table and parses the real input channel, so
  the board it renders is exactly the live one, but no `DAY#` archive, aggregate, rotation or
  `last_posted_day` is touched. `write_day`'s `put_item` is unconditional, so a test parse
  made after the input channel scrolled past the fetch window would otherwise replace a
  complete archived day with a partial one.
- **`days_back` picks the scored day**, counting back from the guild's current day: `1` (the
  default, and the only value the schedule uses) is the closed day. `0` scores today so far —
  a preview, never persisted whether or not it is a test, because an open day must not be
  archived. Fixtures: `tests/events/daily/scoreboard_test.json` and `scoreboard_today.json`;
  either runs locally as `dotenv run -- python3 src/lambda_function.py <path>`.

## Membership

A server can remove the bot at any time, and Discord has no way to say so here: the
removal arrives as a `GUILD_DELETE` gateway event, and there is no gateway connection to
receive it on. Left undetected, the guild's config keeps being loaded by every tick, and
every tick spends a Discord call on a channel it can no longer read — hourly for a board,
once a minute for a sticky — each one landing in the logs as a `FAILED HTTPError: 403`.

- **Polled once an hour**, at the top of the daily lambda's run (`reconcile_membership`).
  One `GET /users/@me/guilds` lists every guild the bot is in, dormant ones included,
  which is the whole reason it beats reading per-guild errors: a `403 Missing Access` says
  "kicked", "channel deleted" and "permissions changed" in exactly the same words, and
  only the first of those should disarm a server.
- **Absent guilds are marked**, not deleted: `missing_since` is stamped on the config
  (first sighting only, so it stays a stable "gone since when?"), and both scheduled
  lambdas skip a marked guild from then on. The guild's days, streaks and settings are
  left untouched, so re-adding the bot resumes where it left off instead of starting over.
- **Returning guilds are unmarked** on the next hour, and the run they are found in picks
  them straight back up — the reconcile reports absent guilds to the caller, so a cleared
  mark does not cost a guild one more hour of being skipped.
- **Never acts on silence**: an empty guild list is a bad token or a bad response, not
  every server leaving at once, and marking on it would disarm the bot everywhere at the
  first Discord wobble. A raise is caught per-stage and leaves the stored marks standing.
- **Nothing purges itself.** Deleting a departed guild's data is a deliberate, manual act:
  its members' streaks are the kind of thing that should outlive an accidental kick.

## Capacity and cost

- Storage: a `DAY#` item is ≈1–3 KB per day per guild, ≈1 MB per year per guild, against
  25 GB of always-free storage.
- Capacity: 5/5 provisioned covers the daily write burst, the per-minute sticky reads, and
  interaction clicks, with burst credits absorbing spikes. Total provisioned capacity across
  all tables must stay ≤ 25/25 to remain in the free tier.

## Rollups (planned)

Weekly and monthly mode on the same hourly tick, firing when guild-local time reaches
Sunday evening or the 1st: Query the window's `DAY#` items and pivot per player — plays per
game, points totals from the frozen per-day points, current and best streaks from the
aggregates, participation leaders, most-improved — then post to the output channel. A
per-player DM version is gated on `PROFILE.dm_opt_in` via `/stats dm on|off`, and
`/stats [@user]` (ephemeral, on demand) is one partition Query.
