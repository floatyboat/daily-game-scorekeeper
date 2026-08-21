# Daily Scoreboard Bot — Spec

The bot reads a Discord channel for daily puzzle results, posts a scoreboard for
yesterday's games, maintains a live sticky in the input channel, and serves slash
commands and buttons. It persists each day's parsed results to DynamoDB and derives
streaks and per-game stats from them. One deployment serves any number of servers;
all per-server configuration lives in the table.

## Architecture

| Lambda | Module | Trigger | Role |
|---|---|---|---|
| `daily-game-score` | `src/lambda_function.py` | EventBridge rule `time`, `cron(0 * * * ? *)` | Two stages per tick, draw first: draws the rotation at each guild's day start, posts and pins yesterday's scoreboard at its post hour, and announces "Today's games" on either — so a later post hour gets it twice; the only writer of day and aggregate items |
| `daily-game-sticky` | `src/sticky_lambda.py` | EventBridge rule `daily-game-sticky`, `cron(* * * * ? *)` | Maintains the one sticky ("Now Playing") at the bottom of the input channel |
| `daily-game-play` | `src/interaction_lambda.py` | Discord Function URL | `/play`, `/setup`, `/suggest`, sticky Play/More/Scores buttons; live ephemeral views |

Shared modules: `game_parser.py` (game specs, parsing, scoring, render), `scoreboard.py`
(Discord fetch/format helpers), `store.py` (all DynamoDB I/O and the config schema).

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
aggregates. `tools/register_commands.py` registers the slash commands.

## Database: DynamoDB

One table `daily-game-tracker`, generic string keys `PK`/`SK`, provisioned 5 RCU / 5 WCU,
**no GSIs**. Auth is the Lambda IAM role; boto3 ships in the runtime, so the store adds no
deploy dependency. Six item types cover every access pattern:

```
PK                          SK                 Contents
GUILDS                      GUILD#<guild_id>   per-server config: input_channel_id,
                                               output_channel_id, timezone,
                                               hours_after_midnight, post_hour,
                                               time_window_hours, minimum_players,
                                               hundreds_of_messages,
                                               daily_enabled, sticky_enabled,
                                               sticky_games, suppress_embeds,
                                               rotation_enabled, rotation_count,
                                               rotation_mode, rotation_min_players,
                                               rotation_off_mode,
                                               game_overrides (map key->bool),
                                               last_finalized_day, last_posted_day,
                                               rotation_day + rotation_games (the
                                               drawn rotation and the day it governs)
                                               and rotation_prev_day +
                                               rotation_prev_games (the pair it
                                               displaced, still needed by the board)
GUILD#<guild_id>            DAY#<YYYY-MM-DD>   full parsed results for the day:
                                               {game: {user_id: {score, points}}}, puzzle
                                               numbers, and the governing rotation when
                                               one did. The durable archive + rebuild
                                               source.
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
GUILD#<gid>#PLAYER#<uid>    PROFILE            display-name snapshot, totals, dm_opt_in
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
| `sticky_games` | `sticky games` | `0` | Game shortcut buttons on the sticky's second row (0–3); 0 skips the ranking pass entirely |
| `suppress_embeds` | `embeds suppress` | `true` | Whether link previews are stripped off counted results |
| `rotation_enabled` | `rotation enabled` | `true` | Score only a rotating subset of the enabled games each day |
| `rotation_count` | `rotation games` | `3` | Games in the daily rotation (1–10) |
| `rotation_mode` | `rotation mode` | `swap` | `swap` replaces under-played members, `random` re-draws daily |
| `rotation_min_players` | `rotation min_players` | `3` | Membership threshold: games under it rotate out, outsiders reaching it rotate in |
| `rotation_off_mode` | `rotation off_rotation` | `shown` | Board display of off-rotation plays: `shown` below the scored games, or `hidden` |
| `game_overrides` | `games` | `{}` | Explicit per-guild flips of each game's default state |
| `last_finalized_day` | — | — | Written at finalize; records how far aggregates are folded |
| `last_posted_day` | — | — | Written after a real post; the post gate |
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
- Two Discord payload caps bound how far `GAME_SPECS` can grow before these surfaces need
  splitting across two messages (constants in `scoreboard.py`, noted at the `GAME_SPECS`
  declaration): the `/setup games` menu is one option per spec, capped at 25; `/play` is one
  button per *enabled* game at 5 per row plus the Random row, capped at 20.

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
  earn-in uses. An ungoverned day keeps the per-game scale (`compute_points` with no
  `first_place_points`), unchanged in every respect.
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
  same count the archive stores), one threshold both ways: members that drew at least
  `rotation_min_players` stay, off-rotation games that drew them **join**.
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
  on one tick and sees it once, as does one with the board off. Each trigger fires once a
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
- **Consumers.** Bare `/play` and the sticky's Play button list rotation games only
  (`/play all:true` and the sticky's More button list every enabled game, scored games
  sorted above off-rotation ones; an exhausted rotation points at them). More is the
  one surface that exists *because* of the rotation: the sticky carries it only while a
  rotation governs the day, since unrestricted it would just repeat Play. The sticky's shortcut row is
  rotation-only, while its content counts every play, on or off rotation. The Scores
  button renders exactly like the board, `rotation_off_mode` included. All of them see
  the new set from day start — including the pre-post-hour window that used to read
  unrestricted, and the morning window before the announcement itself goes out — and
  `daily_enabled` off stops only the board: the rotation still draws and announces.

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
- One display threshold governs every streak surface (game and Play suffixes, sticky flair,
  personal markers, break callouts): the `MINIMUM_STREAK` env var, default 3. Shorter
  streaks still accrue and still sort.

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
  server streak desc → distinct players in the last 30 days desc (`players_30d` off the game
  aggregate, via the streak bundle) → all-time distinct players desc → title. The 30-day
  tier keeps the tail current: all-time sets only grow, so without it a game the server has
  drifted away from outranks a newer one forever. Used everywhere games are
  listed — Play buttons, the sticky's shortcut row, and scoreboard sections — so the app
  presents one consistent order. One helper (`game_link_button`) renders every game link
  button: emoji, title, and a streak suffix — `🔗 Connections 🔥14`. Today's live count
  orders the list but is not in the label.
- **Scoreboard and Scores button** (`format_scoreboard_components`, one shared path) take an
  optional streaks argument: a `🔥N` suffix on each game's title line, a `💔 <Game> streak
  ended at N` callout on the day it breaks — at the foot of the scores section, under the
  games that were played, since a broken streak is a result for that game too (on a
  no-results day, where there is no scores section, it falls back to the header
  container) — and personal streak markers at or above the
  display minimum — the player's **overall** streak in the points summary
  (`👑 @alice: 12 pts 🔥14`) and their per-game streak on each score line
  (`👑 @alice (x9): 3/6 guesses`). The header carries no server-streak line; the server
  streak's display surface is the sticky.
- **Fire emoji vs `(xN)`**: streaks that land at most once per board *per subject* render as
  `🔥N` — a game's title line, the sticky's server streak, and each player's overall streak
  in the points summary (`_overall_streak_tag()`). Per-game player streaks repeat on every
  score line of every game and stay a plain `(xN)` (`_streak_tag()`).
- **Sticky**: the content line ends with the server-wide streak (points scored in any game,
  live-adjusted) as a bare `🔥N`. One row of buttons always — Play · Scores · Yesterday ·
  [More], with the grey More (the `/play all:true` view) trailing the everyday buttons and
  present only while a rotation is narrowing Play —
  and an optional second: a shortcut row of the first `sticky_games` games in
  `game_sort_key` order, the head of the Play list one tap earlier. Yesterday appears only
  once the board covering the day before the tracked one has posted (`last_posted_day`),
  so a guild whose post hour is later than its day start loses the button for that morning
  window rather than pointing it at a day-older board. `sticky_games` is 0 by
  default, and at 0 the ranking pass is skipped rather than run and thrown away. The sticky
  is identified by its own Play button, so extra rows never confuse the match; it reposts
  when its content *or* any button changes, which covers the shortcut row reshuffling as
  the day's plays land and an admin resizing or removing it.

## Commands

- **`/setup`** — admin-only via `default_member_permissions` = Manage Server; the handler
  re-verifies `member.permissions`, since servers can re-map the default. Subcommands, in
  the order `register_commands.py` lists them (which is the order Discord displays):
  `show` · `channel` (both sides at once) · `time` · `limits` · `games` · `daily on|off` ·
  `sticky on|off` (off also deletes the existing sticky; carries the optional `games`
  shortcut-row size, a `ConfigField` in the `sticky` group that `toggle_sub` appends the
  same way `field_sub` builds a whole subcommand) · `rotation on|off` (carries the four
  `rotation`-group fields the same way; the mode fields register fixed choice menus off
  `ConfigField.choices`) · `embeds suppress:on|off` ·
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
- **`/suggest`** — open to everyone, no permission gate: a modal (Discord's only multi-line
  input) taking a game name, an optional link, and a pasted result, posted to the
  `DEV_CHANNEL_ID` channel as a candidate `GAME_SPECS` entry. The paste goes in a code
  fence, the link in angle brackets, and `allowed_mentions: {parse: []}` on the post, so
  nothing a stranger typed can ping or unfurl in the dev's server.
  `game_parser.match_suggestion()` short-circuits games already in `GAME_SPECS` — exact
  name or key, or a spec's own host and path among the submitted links — and answers
  whether the game is tracked or merely off in this server. Modal submits (interaction
  type 5) answer inline rather than deferring.

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
- With `daily_enabled` off the board stops and the sticky drops its Yesterday link; the
  rotation stage keeps running, so today's games are still drawn and announced.
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
