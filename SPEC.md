# Streaks, Stats & Multi-Server Spec

Adds persistence (DynamoDB) for streak tracking, per-game player stats, stat rollups,
and multi-server support. Written 2026-07 against the current three-lambda architecture.

## Current architecture (context)

All three lambdas are stateless and recompute everything by re-parsing recent Discord
message history (last 100–800 messages). Nothing older than the fetch window exists
anywhere, which is why streaks and all-time counts need a store.

| Lambda | File | Trigger | Role |
|---|---|---|---|
| `daily-game-score` | `lambda_function.py` | EventBridge, daily | Posts + pins yesterday's scoreboard to the output channel |
| `daily-game-sticky` | `sticky_lambda.py` | EventBridge, frequent | Maintains the one sticky ("Now Playing") at the channel bottom |
| `daily-game-play` | `interaction_lambda.py` | Discord Function URL | `/play`, sticky Play/Scores buttons; live ephemeral views |

Global secrets are env vars per lambda; per-server config is table-only (Phase 3).
Deploys are zip uploads from three GitHub Actions workflows (us-east-1). No IaC.
`realtime_lambda` is dead and stays dead.

## Database: DynamoDB

- **Always on, no cold start.** HTTP API, not a connection: a cold Lambda adds ~100ms
  of boto3 client init, then single-digit-ms reads. Discord's 3-second interaction
  deadline is never at risk; there is no "database was asleep" failure mode.
- **Free forever at this scale.** Always-free tier: 25 GB storage + 25 provisioned
  RCU/WCU (legacy free tier applies to this AWS account). This project writes a few KB
  per guild per day. Provision the table at 5 RCU / 5 WCU. Worst case on-demand
  pricing would be pennies/month.
- **Zero new dependencies.** boto3 ships in the Lambda runtime — deploy zips are
  unchanged except for code. Auth is the Lambda IAM role; no secrets.

SQL fallback: if we ever want SQL instead, Turso (SQLite over HTTP) is the pick, and
this data model maps 1:1 onto tables. Nothing else in this spec would change.

## Data model

One table `daily-game-tracker`, generic string keys `PK`/`SK`, provisioned 5/5.
**No GSIs.** Six item types cover every access pattern:

```
PK                          SK                 Contents
GUILDS                      GUILD#<guild_id>   per-server config: input_channel_id,
                                               output_channel_id, timezone,
                                               hours_after_midnight, post_hour,
                                               time_window_hours, minimum_players,
                                               hundreds_of_messages, wordle_bot_id,
                                               daily_enabled, sticky_enabled,
                                               game_overrides (map key->bool),
                                               last_finalized_day, last_posted_day
GUILD#<guild_id>            DAY#<YYYY-MM-DD>   full parsed results for the day:
                                               {game: {user_id: {score, points}}}, puzzle
                                               numbers. The durable archive + rebuild source.
GUILD#<guild_id>            AGG#SERVER         overall server streak (≥1 result in ANY game
                                               that day): current_streak, best_streak,
                                               last_played_day
GUILD#<guild_id>            AGG#GAME#<key>     per-game server aggregate: current_streak,
                                               best_streak, last_played_day, total_plays
                                               (= days with ≥1 result), players (string set,
                                               all-time), players_30d (number, refreshed at
                                               finalize)
GUILD#<gid>#PLAYER#<uid>    AGG#GAME#<key>     per-player-per-game: current_streak,
                                               best_streak, last_played_day, total_plays,
                                               best/sum score fields where numeric
GUILD#<gid>#PLAYER#<uid>    PROFILE            display-name snapshot, totals, dm_opt_in
```

Access patterns → reads:

- **Play/Scores ordering**: one `Query PK=GUILD#<gid>, SK begins_with AGG#` returns
  every game's streaks, all-time player set, and 30-day count in a single ~5ms call.
- **Rollups / rebuild**: a month is ≤31 `DAY#` items, one range Query; pivot in memory
  for every player at once.
- **Multi-guild fan-out**: all configs share the `GUILDS` partition, so the scheduled
  lambdas load every guild with one small Query per tick. (A Scan was the original
  sketch, but a Scan reads the whole table — every DAY/AGG item — which a per-minute
  sticky tick would repeat forever; the shared partition costs ~0.5 RCU.) SQS fan-out
  is the escape hatch if the guild count ever outgrows sequential processing.
- **Distinct players**: string set on the game aggregate; `ADD` is idempotent; all-time
  count is the set length. `players_30d` is computed once per day at finalize from the
  trailing 30 `DAY#` items and stored, so interactive reads stay one small Query.

## Streak semantics

- A "day" is the existing `reference_date` — already timezone- and
  `HOURS_AFTER_MIDNIGHT`-aware. Streaks inherit the exact scoring window the
  scoreboard already uses. No new date logic.
- Server streak per game = consecutive game-days with ≥1 tracked result for that game.
  Player streak = same per user. Overall server streak = ≥1 result in any game.
- Streaks update **once per day at finalize** (the daily scoreboard run — the
  authoritative daily tick). Played on day D: `last_played_day == D-1` → increment,
  else reset to 1. Not played on D: archive into `best_streak` if higher, reset to 0.
- **Active** (for display/sort) = `last_played_day >= yesterday`; otherwise renders
  as 0. Live views (sticky, Play, Scores) display `current_streak + 1` for games
  already played today per the live parse, so a 12-day streak reads "🔥 13" the moment
  someone keeps it alive.
- A game disabled for a while and re-enabled resumes with whatever
  `last_played_day` implies — normally a broken (reset) streak. Accepted; no special
  casing.

## Per-server game disabling

- `GameSpec.disabled` is the game's **default only** — every game can be flipped
  either way per guild. `config.game_overrides` (map of game key -> bool) stores just
  the explicit deviations, so a newly added game reaches every guild with its coded
  default rather than a frozen snapshot of an old menu submission.
- Effective list resolved by `spec_enabled(spec, overrides)` / `build_games(pn,
  overrides)`, used by **all** paths: daily parse, aggregate updates, sticky counts,
  Play list, Scores, scoreboard render.
- Historical `DAY#`/aggregate data for a disabled game is retained, just not displayed
  or accrued while disabled.
- Managed by `/games`: one admin-only multi-select menu of all games, pre-selected to
  the guild's current effective state (17 games today; Discord's option cap is 25).

## Write path (daily lambda — the only writer)

After parsing yesterday's results:

1. Write the `DAY#` item — plain overwrite, idempotent. Points are computed via the
   existing `compute_points` and **frozen into the item**, so historical rollups
   survive future scoring-rule changes.
2. Update `AGG#SERVER`, each `AGG#GAME#*`, and each player `AGG#GAME#*` via
   conditional writes guarded per item on a `finalized_through` attribute (last day
   folded into that item) — a double-fire physically cannot double-increment, and a
   run that crashes halfway resumes cleanly because the retry updates exactly the
   items the first attempt didn't reach. `CONFIG.last_finalized_day` advances at the
   end as the run-level marker Phase 3's hourly scheduler reads. The existing
   newest-message-is-a-scoreboard check stays as belt and braces.
3. Refresh `players_30d` on each game aggregate from the trailing 30 `DAY#` items.
4. ~50–100 writes/day per guild, paced trivially under 5 WCU.

**Backfill (one-off script, run locally with dotenv):** `fetch_messages` already
paginates arbitrarily far back — replay channel history day by day through the
existing parser, writing `DAY#` items, then compute all aggregates from those. Streaks
launch at their true historical values, not zero. The same code provides a `rebuild`
mode: recompute all aggregates from `DAY#` items whenever logic changes.

## Read paths / display

- **`store.py`** (new module, peer of `scoreboard.py`) owns all DynamoDB I/O. Each
  deploy workflow adds it to the zip line and path triggers. New env var `TABLE_NAME`.
  IAM: Query/GetItem/PutItem/UpdateItem/Scan on the table ARN for each lambda role.
- **Game ordering** (`game_sort_key`, one shared helper): today's live count desc →
  active server streak desc → all-time distinct players desc → title. Used
  everywhere games are listed — Play buttons and scoreboard sections — so the app
  presents one consistent order. Play labels get a streak suffix:
  `🔗 Connections (3) 🔥14`.
- **Scoreboard + Scores button** (`format_scoreboard_components`, shared path): grows
  an optional streaks argument — "🔥N" suffix on each game's title line, personal
  "🔥N" marker next to players at/above the display minimum, "💔 <Game> streak ended
  at N" callout on the day it breaks. No server-streak line in the header — the
  server streak's display surface is the sticky.
- **Sticky**: content line ends with the server-wide streak (≥1 result in any game,
  live-adjusted) as bare `🔥N`. Sticky-identity logic (Play-button matching) is
  untouched.

## Multi-server onboarding

- **`/setup`** (admin-only via `default_member_permissions` = Manage Server; the
  handler re-verifies `member.permissions` since servers can re-map the default).
  Subcommands: `show` · `input`/`output` (channel-type option, a `channel_id` string
  escape hatch for channels the picker can't show, or no args at all — the reply is
  then an ephemeral channel-select menu; every path validates the bot can actually
  see the channel and errors with instructions when it can't) · `daily on|off` ·
  `sticky on|off` (off also deletes the existing sticky) · `time` (timezone /
  day_start_hour / post_hour / window_hours) · `limits` (minimum_players /
  message_volume / wordle_bot).
- `post_hour` is the guild-local hour the board posts and the sticky wakes;
  `hours_after_midnight` stays the scoring-day cutoff (the two were conflated in env
  land — prod posted at 9 while the day started at 3). Unset post_hour = day start.
- With `daily_enabled` off nothing posts and the sticky drops its Yesterday link
  (whatever board is still in the channel is stale by definition).
- **`/games`**: the multi-select menu above.
- Global config stays env (bot token, public key, bot/app ID, TABLE_NAME,
  TEST_CHANNEL_ID). **Per-server config lives only in the table — there is no env
  fallback.** `infra_setup.py --migrate` copied the original server's legacy CONFIG
  item into the GUILDS partition.
- **Scheduling across timezones**: the daily EventBridge rule becomes **hourly**.
  Each tick loads all configs and posts for any guild whose local hour has reached
  its `post_hour` and whose `last_posted_day` is stale, plus a
  scoreboard-already-in-output-channel check as belt and braces (posting and
  finalizing are decoupled: test runs finalize — idempotently — but never post for
  real or advance `last_posted_day`). Sticky keeps its every-minute schedule, loops
  guilds the same way, and skips guilds outside their [post_hour, midnight) window.
  Onboarding is automatic: /setup writes the config item; the next tick picks the
  guild up with no deploy or schedule change.
- **Test mode stays event-driven**: `{'test': true}` on the daily posts every guild's
  board to the test channel (`test_channel_id` in the event overrides the
  TEST_CHANNEL_ID env; `guild_id` filters); on the sticky it runs the test channel
  under a default config, with any config field overridable straight from the event
  (e.g. `"daily_enabled": false` previews the linkless sticky).
- **Invite link**: add `applications.commands` scope; document Send Messages, Read
  Message History, Manage Messages (embed suppression), and pin permissions in README.

## Rollups (eventual phase)

Weekly/monthly mode on the same hourly tick (fires when guild-local time hits Sunday
evening / the 1st): Query the window's `DAY#` items, pivot per player — plays per
game, points totals from frozen per-day points, current/best streaks from aggregates,
participation leaders, most-improved. Post to the output channel. Per-player DM
version gated on `PROFILE.dm_opt_in` via `/stats dm on|off`. `/stats [@user]`
(ephemeral, on-demand) falls out nearly free — one partition Query.

## Free-tier math

- Storage: `DAY#` item ≈ 1–3 KB/day/guild → ~1 MB/year/guild. 25 GB free ≈ unlimited.
- Capacity: 5/5 provisioned covers the daily write burst (paced), frequent sticky
  reads, and interaction clicks, with burst credits absorbing spikes. Total provisioned
  across all tables must stay ≤ 25/25 to remain free.

## Phasing

1. **Store + writes** — SHIPPED: table, `store.py`, daily-lambda writes, backfill
   script, IAM.
2. **Display** — SHIPPED: streaks on scoreboard/Scores, reordered Play list, sticky
   flair.
3. **Multi-server** — BUILT (deploy steps below): `/setup` + `/games`,
   config-from-table only (no env fallback), hourly daily schedule with per-guild
   gating, current server migrated, invite scopes/README.
4. **Rollups**: weekly/monthly summaries, `/stats`, DM opt-in.

Phase 3 go-live order (config migration is already done and is invisible to the old
code): **1.** push/deploy all three lambdas → **2.** `python3 infra_setup.py
--hourly` (daily rule → `cron(0 * * * ? *)` + daily timeout 120s; before this the
still-daily rule just means the new code posts once at 13:00 UTC as before) →
**3.** `python3 infra_setup.py --prune-env` (strip the dead per-server vars) →
**4.** `dotenv run -- python3 register_commands.py` (needs DISCORD_APPLICATION_ID
in .env, falls back to DISCORD_BOT_ID) → **5.** test events + `/setup show` in the
server.

## Settled decisions

- **Player-count metric**: store all-time distinct players (string set) and rolling
  30-day distinct (`players_30d`, refreshed daily at finalize — kept for rollups);
  **Play sorts by the all-time count** (after today's count and streak).
- **Streak display minimum**: one threshold for every streak display (game/Play
  suffixes, sticky flair, personal markers, break callouts), configurable via the
  `MINIMUM_STREAK` env var, default 3. Shorter streaks still accrue and sort.
- **Game ordering (app-wide)**: today's live count first, then streak, then all-time
  distinct players, then title — identical on the Play list and scoreboard sections.
- **Per-server disabled games**: `GameSpec.disabled` is only each game's default;
  `config.game_overrides` stores explicit per-guild flips (diffs only), managed via
  the `/games` multi-select, historical data retained.
