# Daily Scoreboard Bot
## Summary
Reads a channel in a discord server for daily puzzle games and posts a scoreboard for yesterday's games.
## Games Supported
1. [Connections](https://www.nytimes.com/games/connections)
2. [Bandle](https://bandle.app/daily)
3. [Pips](https://www.nytimes.com/games/pips)
4. [Connections: Sports Edition](https://www.nytimes.com/athletic/connections-sports-edition)
5. [MapTap](https://maptap.gg)
6. [Globle](https://globle.org)
7. [Flagle](https://flagle.org)
8. [Worldle](https://worldlegame.io)

Note: Wordle has official support. So, it is left out of this project.
## Setup
1. Download the code into a directory.
2. Create a `.env` file in the directory.
    - Set Discord to Developer Mode to copy the input and output channel IDs.
    - Set `INPUT_CHANNEL_ID`, `OUTPUT_CHANNEL_ID`
3. Create a bot on [Discord Developer Page](https://discord.com/developers/applications)
    - Copy the token from the `Bot` page and paste it into your `.env` file as `DISCORD_BOT_TOKEN`
    - On the `OAuth2` page, under `OAuth URL Generator`, select `bot`
    - Select `Text Permissions`: `Send Messages` and `Read Message History`
    - Click the link and add it to your server
4. Run it daily from your machine or set up a schedule of your choosing
    - I hosted on AWS Lambda with a scheduled EventBridge kickoff everyday. You get 1 million free Lambda invocations a month.

### Optional Setup
- `DISCORD_BOT_ID` - prevent Lambda from double sending messages by checking the last sent message
- Games with no identifiers (e.g. Globle, Flagle, and Worldle) are counted based on the timestamp. Discord's timestamps are in UTC, which may not work for your server. Set the following variables to your preference:
    1. `UTC_OFFSET` - your timezones offset from UTC time (default: 0)
    2. `HOURS_AFTER_MIDNIGHT` - time after midnight to start "yesterday" window for counting the game (default: 0)
    3. `TIME_WINDOW_HOURS` - hours of time to keep submissions open for yesterday (default: 24)
    
    For example, I have mine set to -4 for `UTC_OFFSET` (EDT), 3 for `HOURS_AFTER_MIDNIGHT`, and 21 for `TIME_WINDOW_HOURS`. This allows submissions from 3AM - 12 AM and prevents the bot from counting the wrong submission if someone were to submit a puzzle score in UTC -7 during that window, where it would still be the day prior.