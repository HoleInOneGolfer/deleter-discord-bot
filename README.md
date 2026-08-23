# docker-python-discord-bot

Just a simple Discord bot written in Python (discord.py) that runs inside a single Docker container.

## How to run it with Docker Compose

Create a `docker-compose.yml` file, throw your bot token in there, and point the volume to wherever you want to store the data files.

```yaml
services:
  discord-bot:
    image: ghcr.io/holeinonegolfer/docker-python-discord-bot:latest
    container_name: discord-bot
    restart: always
    environment:
      - DISCORD_TOKEN=YOUR_DISCORD_BOT_TOKEN
    volumes:
      - ./data:/data
```

Once that's set up, just run:

```bash
docker-compose up -d
```
