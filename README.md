# deleter-discord-bot

A simple bot that purges old messages in specified channels on your discord server.

## Commnds

`/configure_channel channel:<channel> max_age_days:<number> auto_purge:<true/false>` - Configures a channel to be automatically purged of messages older than the specified number of days. If auto_purge is set to true, the bot will automatically purge the channel every 24 hours.

`/purge_now channel:<channel>` - Immediately purges the specified channel of messages older than the configured max_age_days.

`/purge_count channel:<channel> amount:<number>` - Purges the specified channel of the specified number of messages, regardless of age.

`/set_log_channel channel:<channel>` - Sets the channel where the bot will log its actions.

`/dry_run channel:<channel>` - Performs a dry run of the purge, showing how many messages would be deleted without actually deleting them.

`/stop channel:<channel>` - Stops the automatic purging of the specified channel.

## How to run it with Docker Compose

Create a `docker-compose.yml` file, throw your bot token in there, and point the volume to wherever you want to store the data files.

```yaml
services:
  deleter:
    image: ghcr.io/holeinonegolfer/deleter-discord-bot:latest
    container_name: deleter
    restart: always
    environment:
      - DISCORD_TOKEN=YOUR_DISCORD_BOT_TOKEN
    volumes:
      - ./Deleter:/data
```

Once that's set up, just run:

```bash
docker-compose up -d
```
