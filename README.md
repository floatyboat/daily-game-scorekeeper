# Daily Scoreboard Bot
## Summary
Reads a channel in a discord server for daily puzzle games and posts a scoreboard for yesterday's games
## Games Supported
1. [Connections](https://www.nytimes.com/games/connections)
2. [Bandle](https://bandle.app/daily)
3. [Pips](https://www.nytimes.com/games/pips)
4. [Connections: Sports Edition](https://www.nytimes.com/athletic/connections-sports-edition)
5. [MapTap](https://maptap.gg)

Note: Wordle has official support. So, it is left out of this project.
## Setup
1. Download the code into a directory
2. Create a `.env` file in the directory
    - Set Discord to Developer Mode to copy the input and output channel IDs
    - Set `INPUT_CHANNEL_ID`, `OUTPUT_CHANNEL_ID`
3. Create a bot on [Discord Developer Page](https://discord.com/developers/applications)
    - Copy the token from the `Bot` page and paste it into your `.env` file as `DISCORD_BOT_TOKEN`
    - On the `OAuth2` page, under `OAuth URL Generator`, select `bot`
    - Select `Text Permissions`: `Send Messages` and `Read Message History`
    - Click the link and add it to your server
4. Run it daily from your machine or set up a schedule of your choosing
    - I hosted on AWS Lambda with a scheduled EventBridge kickoff everyday. You get 1 million free Lambda invocations a month.