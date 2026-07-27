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

Config is env vars per lambda. Deploys are zip uploads from three GitHub Actions
workflows (us-east-1). No IaC. `realtime_lambda` is dead and stays dead.

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
GUILD#<guild_id>            CONFIG             input_channel_id, output_channel_id, timezone,
                                               hours_after_midnight, time_window_hours,
                                               minimum_players, hundreds_of_messages,
                                               wordle_bot_id, disabled_games (string set),
                                               last_finalized_day, enabled
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
- **Multi-guild fan-out**: scheduled lambdas Scan for `SK = CONFIG`. Fine to dozens of
  guilds; SQS fan-out is the escape hatch if that ever changes.
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

- Global `GameSpec.disabled` stays as the code-level kill switch (e.g. broken parser).
- `CONFIG.disabled_games` (string set of game keys) overlays per guild. The effective
  game list for a guild = `build_games(...)` minus global-disabled minus guild-disabled,
  resolved by one shared helper used by **all** paths: daily parse, aggregate updates,
  sticky counts, Play list, Scores, scoreboard render.
- Historical `DAY#`/aggregate data for a disabled game is retained, just not displayed
  or accrued while disabled.
- Managed by `/games` command (below).

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
- **Play list ordering** (`build_play_response`): active server streak desc →
  `players_30d` desc → today's live count desc → title. Streak suffix on the label:
  `🔗 Connections (3) 🔥14`.
- **Scoreboard + Scores button** (`format_scoreboard_components`, shared path): grows
  an optional streaks argument — "🔥 N-day streak" line per game section, personal
  "🔥N" marker next to players on **≥3-day** streaks, "💔 <Game> streak ended at N"
  callout on the day it breaks, overall server-streak line in the header.
- **Sticky**: content line may gain flair ("🔥 3 streaks alive"). Sticky-identity
  logic (Play-button matching) is untouched.

## Multi-server onboarding

- **`/setup`** (admin-only via `default_member_permissions` = Manage Server; handler
  re-verifies the permission bit): channel-type options for input/output channels,
  string choice for timezone, ints for hours_after_midnight / time_window_hours /
  minimum_players. Writes CONFIG, replies ephemerally with resulting config. `/setup`
  with no args shows current settings.
- **`/games`** (same admin gating): `enable <game>` / `disable <game>` / `list`
  subcommands; choices generated from `GAME_SPECS` in `register_commands.py`
  (re-run on new games; 25-choice Discord limit is far away).
- Global config stays env (bot token, public key, app ID). Per-server config moves to
  the table, env as fallback during migration. One-off script onboards the current
  server from `.env`.
- **Scheduling across timezones**: the daily EventBridge rule becomes **hourly**. Each
  tick: Scan configs, finalize any guild whose local hour has crossed its
  `hours_after_midnight` and whose `last_finalized_day` is stale — the conditional
  write claims the run, so hourly firing is safe and each guild posts at its own local
  time. Sticky keeps its frequent schedule, loops guilds the same way.
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

1. **Store + writes** (invisible): table, `store.py`, daily-lambda writes, backfill
   script, IAM. Verify with the dotenv test event (per CLAUDE.md: output goes to the
   test channel in this env).
2. **Display**: streaks on scoreboard/Scores, reordered Play list, sticky flair.
3. **Multi-server**: `/setup` + `/games`, config-from-table (env fallback), hourly
   daily schedule with per-guild gating, onboard-current-server migration, invite
   scopes/README.
4. **Rollups**: weekly/monthly summaries, `/stats`, DM opt-in.

Each phase ships independently; Phase 1 has zero user-facing risk.

## Settled decisions

- **Player-count metric**: store all-time distinct players (string set, for stats);
  **sort by rolling 30-day distinct** (`players_30d`, refreshed daily at finalize) so
  the ordering keeps discriminating as history grows.
- **Personal streak display threshold**: show 🔥 only at ≥3 days.
- **Play-list sort priority**: streak first, then players_30d, then today's live
  count, then title.
- **Per-server disabled games**: `CONFIG.disabled_games` overlay on top of the global
  `GameSpec.disabled` flag, managed via `/games`, historical data retained.
