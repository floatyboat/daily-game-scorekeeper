# Daily Scoreboard Bot
## Summary
Reads a channel in a discord server for daily puzzle games and posts a scoreboard for yesterday's games.
## Games Supported
1. [Bandle](https://bandle.app/daily)
2. [Chronophoto](https://www.chronophoto.app/daily.html)
3. [Connections](https://www.nytimes.com/games/connections)
4. [Connections: Sports Edition](https://www.nytimes.com/athletic/connections-sports-edition)
5. [Enclose](https://enclose.horse)
6. [Flagle](https://flagle.org)
7. [Globle](https://globle.org)
8. [MapTap](https://maptap.gg)
9. [Pips](https://www.nytimes.com/games/pips)
10. [Quizl](https://quizl.io)
11. [Wordle](https://www.nytimes.com/games/wordle)
12. [Worldle](https://worldlegame.io)

Wordle supports both pasted share text and image recognition from the official Wordle Discord bot.
## Setup

Configuration is **per server**, stored in DynamoDB and managed with slash commands —
environment variables carry only the bot's global identity. The bot serves any number
of servers from one deployment: adding it to a new server and running `/setup` there
is the whole onboarding.

1. Create a bot on the [Discord Developer Page](https://discord.com/developers/applications)
    - Copy the token from the `Bot` page into your `.env` as `DISCORD_BOT_TOKEN`; set
      `DISCORD_BOT_ID`, `DISCORD_APPLICATION_ID`, and `DISCORD_PUBLIC_KEY` from the
      `General Information`/`Bot` pages
    - On the `OAuth2` page select the `bot` and `applications.commands` scopes with
      `Send Messages`, `Read Message History`, `Manage Messages` (embed suppression +
      pinning); open the generated link to add the bot to your server
2. Deploy the three lambdas (see `.github/workflows/`) with EventBridge schedules:
   the daily scoreboard rule fires **hourly** (each server posts when its own local
   post hour comes around), the sticky rule every minute. Point your Discord app's
   Interactions Endpoint URL at the interaction lambda's Function URL, then register
   the commands: `dotenv run -- python3 register_commands.py`
3. In your server (needs **Manage Server**):
    - `/setup input` — channel scores are read in (and where the sticky lives).
      Pick from the menu, or pass `channel` / `channel_id:<id>` for channels the
      picker can't show. The bot verifies it can actually see the channel.
    - `/setup output` — channel the daily scoreboard posts to (can be the same)
    - `/setup show` — everything at a glance
4. Optional per-server tuning (all via `/setup`, defaults in parentheses):
    - `/setup time timezone:<IANA name> day_start_hour:<0-23> post_hour:<0-23> window_hours:<1-24>`
      — e.g. timezone `America/New_York`, day start 3, post 9, window 21 keeps
      submissions open 3AM–midnight local and posts the board at 9AM (UTC / 0 / day
      start / 24)
    - `/setup limits minimum_players:<n> message_volume:<hundreds per day> wordle_bot:<user>` (1 / 1 / unset)
    - `/setup daily enabled:false` — pause the daily post (the sticky drops its
      Yesterday link while paused); `/setup sticky enabled:false` — remove the sticky
    - `/games` — a multi-select of every supported game; games the code marks
      default-off (e.g. timestamp-only games) can be enabled here per server

### Streak & Stats Store (Optional)
The bot can persist daily results to DynamoDB to track server/player streaks and per-game player stats (see `SPEC.md` for the full design). Without the table the bot still works — persistence failures are logged and skipped.

1. Run `python3 infra_setup.py` with AWS credentials to create the `daily-game-tracker` table (provisioned 5/5, inside the always-free tier), grant each Lambda's role access, and set `TABLE_NAME` on each function. Steps it lacks permission for are printed as commands to run with an admin identity.
2. Seed history so streaks start at their true values: `dotenv run -- python3 backfill.py --all` (or `--days N`). Re-running is safe; `--rebuild-only` recomputes aggregates from the archived days without touching Discord.

### Wordle Image Recognition (Optional)
The bot can parse Wordle result images posted by the official [Wordle Discord bot](https://support.nytimes.com/s/article/wordle-discord-bot). This requires [Pillow](https://pypi.org/project/Pillow/) to be available in the runtime.

1. Point the server at the Wordle bot: `/setup limits wordle_bot:@Wordle`.
2. Make Pillow available to your Lambda function. Either:
   - **Lambda Layer**: Attach a pre-built Pillow layer (e.g. from [Klayers](https://github.com/keithrozario/Klayers)) matching your Python version and region.
   - **Bundled in deploy zip**: Build Pillow in a Lambda-compatible container during CI (see `deploy.yml` for an example).

Without Pillow, the bot still tracks Wordle via pasted share text — image recognition is skipped gracefully.