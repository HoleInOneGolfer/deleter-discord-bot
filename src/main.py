import os
import json
import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN") or os.environ.get("DISCORD_TOKEN")

# Default to /data/settings.json if running in Docker/container, otherwise local directory
default_settings_path = "/data/settings.json" if os.path.exists("/data") else "./settings.json"
SETTINGS_FILE = os.getenv("SETTINGS_FILE", default_settings_path)

# Configure structured console logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("Deleter")

# Rate Limit Safeguards
# Discord allows 5 reqs / 2 sec per route (~0.4s min).
# 1.0s delay provides a safety margin against IP limits.
GLOBAL_DELETE_SEMAPHORE = asyncio.Semaphore(2)
INDIVIDUAL_DELETE_DELAY = 1.0

# Tracks purge jobs currently running, keyed by channel ID, so /stop can
# signal a running job to cancel early.
ACTIVE_PURGE_STOP_EVENTS: dict[int, asyncio.Event] = {}

# Safety cap on how many old messages we'll pull back into memory for an
# uncapped purge/dry-run. purge_count doesn't need this since it stops as
# soon as it has `amount` matches.
MAX_SCAN_CANDIDATES = 5000


async def scan_candidates(channel: discord.TextChannel, cutoff: datetime, count_limit: int = None, stop_event: asyncio.Event = None):
    """
    Returns (messages, stopped_early). Messages are older than `cutoff`,
    newest-first.

    Instead of walking every message in the channel from "now" and filtering
    client-side (slow, and wasteful for channels with lots of recent traffic),
    this seeks directly to the cutoff point using Discord's snowflake IDs,
    which encode a timestamp. history(before=<snowflake at cutoff>) then
    returns messages that are already at or before the cutoff.

    If count_limit is given, stops as soon as that many candidates are found,
    since we're scanning newest-first and those are exactly the messages a
    capped purge would target.

    If stop_event fires mid-scan (relevant for large, uncapped purge_now
    scans), returns whatever was collected so far along with stopped_early=True.
    """
    seek_id = discord.utils.time_snowflake(cutoff, high=True)
    candidates = []

    async for msg in channel.history(
        limit=None if count_limit is not None else MAX_SCAN_CANDIDATES,
        before=discord.Object(id=seek_id),
        oldest_first=False,
    ):
        if stop_event is not None and stop_event.is_set():
            return candidates, True

        # Safety guard for snowflake boundary precision; nearly always true.
        if msg.created_at < cutoff:
            candidates.append(msg)
        if count_limit is not None and len(candidates) >= count_limit:
            break

    return candidates, False


async def bulk_delete_in_chunks(
    channel: discord.TextChannel,
    messages: list[discord.Message],
    stop_event: asyncio.Event = None
) -> tuple[int, bool]:
    """
    Deletes messages by ID using the actual bulk-delete endpoint, in chunks
    of up to 100 (Discord's bulk-delete limit). Falls back to a single
    delete() when a chunk has only one message, since bulk delete requires
    at least 2.

    A single chunk delete can't be interrupted mid-request, but for a large
    candidate list this can involve many chunks/requests, so stop_event is
    checked between chunks. Returns (deleted_count, stopped_early).
    """
    deleted = 0
    for i in range(0, len(messages), 100):
        if stop_event is not None and stop_event.is_set():
            return deleted, True

        chunk = messages[i:i + 100]
        try:
            if len(chunk) == 1:
                await chunk[0].delete()
            else:
                await channel.delete_messages(chunk)
            deleted += len(chunk)
        except discord.HTTPException as e:
            logger.error(f"Bulk delete batch failed: {e}")
    return deleted, False


async def interruptible_sleep(seconds: float, stop_event: asyncio.Event) -> bool:
    """
    Sleeps for `seconds`, but wakes immediately if stop_event is set instead
    of waiting out the full duration. Returns True if it was interrupted.
    """
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=seconds)
        return True  # stop_event fired before the timeout
    except asyncio.TimeoutError:
        return False  # slept the full duration, no stop requested


class SettingsManager:
    """Handles per-guild and per-channel persistence in JSON."""

    @staticmethod
    def load_all():
        if not os.path.exists(SETTINGS_FILE):
            SettingsManager.save_all({})
            return {}
        try:
            with open(SETTINGS_FILE, "r") as f:
                return json.load(f)
        except json.JSONDecodeError:
            logger.error("Error reading settings.json. Reverting to empty state.")
            return {}

    @staticmethod
    def save_all(data):
        with open(SETTINGS_FILE, "w") as f:
            json.dump(data, f, indent=4)

    @staticmethod
    def get_guild_settings(guild_id: int) -> dict:
        data = SettingsManager.load_all()
        return data.get(str(guild_id), {})

    @staticmethod
    def set_channel_rule(guild_id: int, channel_id: int, max_age_days: int, auto_purge: bool = None):
        """
        Saves the age rule for a channel. `auto_purge`, if given, sets whether
        this channel is included in the daily scheduled purge; if omitted, any
        existing auto_purge setting for the channel is left as-is (defaults to
        False for a brand new rule).
        """
        data = SettingsManager.load_all()
        g_id, c_id = str(guild_id), str(channel_id)

        if g_id not in data:
            data[g_id] = {"channels": {}}
        if "channels" not in data[g_id]:
            data[g_id]["channels"] = {}

        existing = data[g_id]["channels"].get(c_id, {})
        resolved_auto_purge = existing.get("auto_purge", False) if auto_purge is None else auto_purge

        data[g_id]["channels"][c_id] = {
            "max_age_days": max_age_days,
            "auto_purge": resolved_auto_purge,
            "configured_at": datetime.now(timezone.utc).isoformat()
        }
        SettingsManager.save_all(data)
        logger.info(f"Saved Rule -> Guild: {guild_id} | Channel: {channel_id} | Max Age: {max_age_days}d | Auto-purge: {resolved_auto_purge}")

    @staticmethod
    def iter_auto_purge_channels():
        """Yields (guild_id, channel_id, max_age_days) for every channel with auto_purge enabled, across all guilds."""
        data = SettingsManager.load_all()
        for g_id, guild_data in data.items():
            for c_id, cfg in guild_data.get("channels", {}).items():
                if cfg.get("auto_purge"):
                    yield int(g_id), int(c_id), cfg["max_age_days"]

    @staticmethod
    def set_log_channel(guild_id: int, channel_id: int = None):
        """Sets (or, with channel_id=None, clears) the guild's purge-log channel."""
        data = SettingsManager.load_all()
        g_id = str(guild_id)

        if g_id not in data:
            data[g_id] = {"channels": {}}

        if channel_id is None:
            data[g_id].pop("log_channel_id", None)
        else:
            data[g_id]["log_channel_id"] = channel_id

        SettingsManager.save_all(data)
        logger.info(f"Log channel {'cleared' if channel_id is None else 'set to ' + str(channel_id)} -> Guild: {guild_id}")

    @staticmethod
    def get_log_channel(guild_id: int) -> int | None:
        return SettingsManager.get_guild_settings(guild_id).get("log_channel_id")


async def post_purge_log(guild_id: int, embed: discord.Embed) -> discord.Message | None:
    """
    Posts a new embed message to the guild's configured log channel, if one is set.
    Returns None (and no-ops, aside from a console log) if none is
    configured or the channel can't be resolved/posted to, so a missing or
    broken log channel never breaks the actual purge job.
    """
    log_channel_id = SettingsManager.get_log_channel(guild_id)
    if log_channel_id is None:
        return None

    channel = bot.get_channel(log_channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(log_channel_id)
        except discord.HTTPException as e:
            logger.warning(f"Could not resolve log channel {log_channel_id} for guild {guild_id}: {e}")
            return None

    try:
        return await channel.send(embed=embed)
    except discord.HTTPException as e:
        logger.warning(f"Failed to post to log channel {log_channel_id} for guild {guild_id}: {e}")
        return None


async def execute_rate_safe_purge(
    guild_id: int,
    channel: discord.TextChannel,
    max_age_days: int,
    requested_by: str,
    limit: int = None,
    status_update=None
) -> int:
    """
    Finds messages created BEFORE the target cutoff (older than X days),
    sorts them newest-first, and deletes them safely using Bulk Delete (<14d)
    or Individual Paced Delete (>=14d).

    Can be interrupted early by /stop, which sets the channel's stop event.

    `requested_by` is a display string for logs/embeds (a user mention for
    slash-command runs, or a label like "Scheduled daily purge" for the
    background task). `status_update`, if given, is an async callable that
    receives progress text — slash commands wire this to
    interaction.edit_original_response; scheduled runs can omit it and rely
    solely on the log channel.
    """
    async def _status(content: str):
        if status_update is not None:
            await status_update(content)

    if channel.id in ACTIVE_PURGE_STOP_EVENTS:
        await _status(f"A purge job is already running in {channel.mention}. Use `/stop` first if you want to cancel it.")
        return 0

    stop_event = asyncio.Event()
    ACTIVE_PURGE_STOP_EVENTS[channel.id] = stop_event

    try:
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(days=max_age_days)
        fourteen_days_ago = now - timedelta(days=14)

        logger.info(f"Scanning #{channel.name} ({channel.id}) | Cutoff: messages before {cutoff.strftime('%Y-%m-%d %H:%M:%S')} UTC")
        await _status(f"Scanning history in {channel.mention} for messages older than **{max_age_days} day(s)**...")

        # Seeks directly to the cutoff point via snowflake ID instead of
        # scanning every recent message, stops early once `limit` matches
        # are found, and bails out immediately if /stop fires mid-scan.
        candidate_messages, scan_stopped_early = await scan_candidates(channel, cutoff, count_limit=limit, stop_event=stop_event)

        if scan_stopped_early:
            logger.info(f"Stop requested during scan. Found {len(candidate_messages)} candidate(s) before stopping; nothing deleted.")
            await _status(f"Purge stopped before any deletions in {channel.mention}.")
            return 0

        logger.info(f"Found {len(candidate_messages)} matching message(s) (> {max_age_days} days old)." + (f" Capped to requested limit of {limit}." if limit is not None else ""))

        total_target = len(candidate_messages)
        if total_target == 0:
            await _status(f"No messages found in {channel.mention} older than **{max_age_days} day(s)**.")
            return 0

        # Bucket candidates into Bulk (<14 days old) and Individual (>=14 days old)
        bulk_candidates = [m for m in candidate_messages if m.created_at > fourteen_days_ago]
        individual_candidates = [m for m in candidate_messages if m.created_at <= fourteen_days_ago]

        await _status(f"Found **{total_target}** target message(s) in {channel.mention}. Starting deletion...")

        def build_log_embed(title: str, color: discord.Color, count_value: str) -> discord.Embed:
            embed = discord.Embed(
                title=title,
                description=f"Purge in {channel.mention}, requested by {requested_by}",
                color=color,
                timestamp=datetime.now(timezone.utc)
            )
            embed.add_field(name="Age rule", value=f"> {max_age_days} day(s)")
            embed.add_field(name="Limit", value=str(limit) if limit is not None else "None")
            embed.add_field(name="Messages", value=count_value)
            return embed

        # 1. Post "Purge Started" Log
        await post_purge_log(
            guild_id,
            build_log_embed("Purge Started", discord.Color.blue(), f"{total_target} targeted")
        )

        total_deleted = 0
        stopped_early = False

        # 2. Execute Bulk Deletion (<14 days old)
        if bulk_candidates:
            deleted_bulk_count, stopped_early = await bulk_delete_in_chunks(channel, bulk_candidates, stop_event)
            total_deleted += deleted_bulk_count
            logger.info(f"Bulk delete finished. Removed {deleted_bulk_count} message(s). (stopped_early={stopped_early})")
            if not stopped_early and individual_candidates:
                await _status(f"Removed {deleted_bulk_count} newer message(s). Now working through **{len(individual_candidates)}** older message(s) (paced, ~1/sec)...")

        # 3. Execute Rate-Safe Individual Deletion (>=14 days old)
        if individual_candidates and not stopped_early:
            total_ind = len(individual_candidates)
            logger.info(f"Starting individual deletion sequence for {total_ind} message(s) >=14 days old...")
            last_progress_update = time.monotonic()

            for idx, msg in enumerate(individual_candidates, start=1):
                if stop_event.is_set():
                    stopped_early = True
                    logger.info(f"Stop requested before msg {idx}/{total_ind}. Halting.")
                    break

                async with GLOBAL_DELETE_SEMAPHORE:
                    success = False
                    while not success:
                        # Fast check before initiating an HTTP request
                        if stop_event.is_set():
                            stopped_early = True
                            break

                        try:
                            await msg.delete()
                            total_deleted += 1
                            success = True
                            logger.info(f"[{idx}/{total_ind}] Deleted MSG {msg.id} ({msg.created_at.strftime('%Y-%m-%d')})")

                            # Throttled status updates for user interaction
                            if time.monotonic() - last_progress_update >= 4.0:
                                last_progress_update = time.monotonic()
                                await _status(f"Deleting older messages in {channel.mention}... **{idx}/{total_ind}** removed so far.")

                            # Interruptible sleep: halts instantly if stop signal fires
                            if await interruptible_sleep(INDIVIDUAL_DELETE_DELAY, stop_event):
                                stopped_early = True
                                break

                        except discord.NotFound:
                            logger.warning(f"[{idx}/{total_ind}] MSG {msg.id} was already removed.")
                            success = True
                        except discord.HTTPException as e:
                            if e.status == 429:  # Rate Limit
                                retry_after = getattr(e, 'retry_after', 5.0)
                                logger.warning(f"Rate limited (429)! Backing off for {retry_after:.2f}s...")
                                # Halt immediately if /stop is called while waiting on rate-limit cooldowns
                                if await interruptible_sleep(retry_after + 0.5, stop_event):
                                    stopped_early = True
                                    break
                            else:
                                logger.error(f"Failed to delete MSG {msg.id}: {e}")
                                success = True

                if stopped_early:
                    logger.info(f"Stop requested. Halting after {idx}/{total_ind} individual deletions.")
                    break

        # 4. Post Final State Log (Stopped or Finished)
        if stopped_early:
            await _status(f"Purge stopped early. Removed **{total_deleted}** message(s) in {channel.mention} before cancellation.")
            await post_purge_log(
                guild_id,
                build_log_embed("Purge Stopped Early", discord.Color.orange(), f"{total_deleted} / {total_target} deleted")
            )
        else:
            await _status(f"Purge complete! Successfully removed **{total_deleted}** message(s) older than {max_age_days} day(s) in {channel.mention}.")
            await post_purge_log(
                guild_id,
                build_log_embed("Purge Completed", discord.Color.green(), f"{total_deleted} / {total_target} deleted")
            )

        logger.info(f"Finished purge job in #{channel.name}. Total deleted: {total_deleted} (stopped_early={stopped_early})")
        return total_deleted

    finally:
        ACTIVE_PURGE_STOP_EVENTS.pop(channel.id, None)


# Initialize Bot
intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    logger.info(f"Logged in as {bot.user} (ID: {bot.user.id})")
    try:
        synced = await bot.tree.sync()
        logger.info(f"Synced {len(synced)} slash command(s).")
    except Exception as e:
        logger.error(f"Failed to sync slash commands: {e}")


# Command 1: Configure Channel Age Settings
@bot.tree.command(name="configure_channel", description="Set a target message age rule for a channel.")
@app_commands.describe(
    channel="Target channel",
    max_age_days="Purge messages older than this many days",
    auto_purge="Include this channel in the daily scheduled auto-purge (default: off)"
)
@app_commands.checks.has_permissions(administrator=True)
async def configure_channel(interaction: discord.Interaction, channel: discord.TextChannel, max_age_days: int, auto_purge: bool = None):
    logger.info(f"/configure_channel invoked by {interaction.user} (Guild: {interaction.guild_id}, Channel: #{channel.name}, Age: {max_age_days}d, Auto-purge: {auto_purge})")

    if max_age_days <= 0:
        await interaction.response.send_message("Age must be greater than 0 days.", ephemeral=True)
        return

    SettingsManager.set_channel_rule(interaction.guild_id, channel.id, max_age_days, auto_purge=auto_purge)
    resolved_auto_purge = SettingsManager.get_guild_settings(interaction.guild_id)["channels"][str(channel.id)]["auto_purge"]
    await interaction.response.send_message(
        f"Configured {channel.mention}: messages older than **{max_age_days} day(s)** will be targeted for purges. "
        f"Daily auto-purge is **{'ON' if resolved_auto_purge else 'OFF'}** for this channel.",
        ephemeral=True
    )


# Command 2: Purge Now
@bot.tree.command(name="purge_now", description="Purge messages using the channel's saved JSON age setting.")
@app_commands.describe(channel="Target channel to purge")
@app_commands.checks.has_permissions(administrator=True)
async def purge_now(interaction: discord.Interaction, channel: discord.TextChannel):
    logger.info(f"/purge_now invoked by {interaction.user} for channel #{channel.name}")
    await interaction.response.defer(ephemeral=True)

    guild_data = SettingsManager.get_guild_settings(interaction.guild_id)
    channel_cfg = guild_data.get("channels", {}).get(str(channel.id))

    if not channel_cfg:
        await interaction.followup.send(f"{channel.mention} has no rule set. Run `/configure_channel` first.", ephemeral=True)
        return

    max_age_days = channel_cfg["max_age_days"]
    await execute_rate_safe_purge(
        guild_id=interaction.guild_id,
        channel=channel,
        max_age_days=max_age_days,
        requested_by=interaction.user.mention,
        status_update=lambda content: interaction.edit_original_response(content=content)
    )


# Command 3: Purge Count Limit
@bot.tree.command(name="purge_count", description="Purge up to N messages matching the age rule (newest first).")
@app_commands.describe(channel="Target channel", amount="Maximum number of messages to delete")
@app_commands.checks.has_permissions(administrator=True)
async def purge_count(interaction: discord.Interaction, channel: discord.TextChannel, amount: int):
    logger.info(f"/purge_count invoked by {interaction.user} for channel #{channel.name} (Amount limit: {amount})")

    if amount <= 0:
        await interaction.response.send_message("Amount must be greater than 0.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    guild_data = SettingsManager.get_guild_settings(interaction.guild_id)
    channel_cfg = guild_data.get("channels", {}).get(str(channel.id))

    if not channel_cfg:
        await interaction.followup.send(f"{channel.mention} has no rule set. Run `/configure_channel` first.", ephemeral=True)
        return

    max_age_days = channel_cfg["max_age_days"]
    await execute_rate_safe_purge(
        guild_id=interaction.guild_id,
        channel=channel,
        max_age_days=max_age_days,
        requested_by=interaction.user.mention,
        limit=amount,
        status_update=lambda content: interaction.edit_original_response(content=content)
    )


# Command 4: Dry Run / Preview Messages
@bot.tree.command(name="dry_run", description="Preview messages that would be purged without actually deleting them.")
@app_commands.describe(channel="Target channel to evaluate")
@app_commands.checks.has_permissions(administrator=True)
async def dry_run(interaction: discord.Interaction, channel: discord.TextChannel):
    logger.info(f"/dry_run invoked by {interaction.user} for channel #{channel.name}")
    await interaction.response.defer(ephemeral=True)

    guild_data = SettingsManager.get_guild_settings(interaction.guild_id)
    channel_cfg = guild_data.get("channels", {}).get(str(channel.id))

    if not channel_cfg:
        await interaction.followup.send(f"{channel.mention} has no rule set. Run `/configure_channel` first.", ephemeral=True)
        return

    max_age_days = channel_cfg["max_age_days"]
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=max_age_days)

    await interaction.edit_original_response(content=f"Scanning history in {channel.mention} for messages older than **{max_age_days} day(s)**...")

    candidate_messages, _ = await scan_candidates(channel, cutoff)

    logger.info(f"Dry run on #{channel.name} | Rule: >{max_age_days} days old | Matched: {len(candidate_messages)}")

    if not candidate_messages:
        await interaction.edit_original_response(
            content=f"Dry run complete. No messages found in {channel.mention} older than **{max_age_days} day(s)**."
        )
        return

    for idx, msg in enumerate(candidate_messages, start=1):
        logger.info(f"[{idx}] Sent: {msg.created_at.strftime('%Y-%m-%d %H:%M:%S')} UTC | Author: {msg.author} | Link: {msg.jump_url}")

    # Build Discord response preview (showing first 10 matched messages)
    preview_list = []
    for idx, msg in enumerate(candidate_messages[:10], start=1):
        preview_list.append(
            f"**{idx}.** Sent: `{msg.created_at.strftime('%Y-%m-%d')}` by {msg.author.mention} ➔ [Jump to Message]({msg.jump_url})"
        )

    response_text = (
        f"**Dry Run Results for {channel.mention}:**\n"
        f"Found **{len(candidate_messages)}** message(s) older than {max_age_days} day(s).\n\n"
        f"**First 10 matched messages (Newest to Oldest):**\n" + "\n".join(preview_list)
    )

    if len(candidate_messages) > 10:
        response_text += f"\n\n*...and {len(candidate_messages) - 10} more. Full list printed to the bot's console log.*"

    await interaction.edit_original_response(content=response_text)


# Command 5: Stop an in-progress purge
@bot.tree.command(name="stop", description="Cancel an in-progress purge job in a channel.")
@app_commands.describe(channel="Channel whose purge job should be cancelled")
@app_commands.checks.has_permissions(administrator=True)
async def stop(interaction: discord.Interaction, channel: discord.TextChannel):
    logger.info(f"/stop invoked by {interaction.user} for channel #{channel.name}")

    stop_event = ACTIVE_PURGE_STOP_EVENTS.get(channel.id)
    if stop_event is None:
        await interaction.response.send_message(f"No purge job is currently running in {channel.mention}.", ephemeral=True)
        return

    stop_event.set()
    await interaction.response.send_message(
        f"Stop signal sent. The purge in {channel.mention} will halt immediately.",
        ephemeral=True
    )


# Command 6: Configure Log Channel
@bot.tree.command(name="set_log_channel", description="Set (or clear) the channel where purge job start/completion is logged.")
@app_commands.describe(channel="Channel to post purge logs to. Leave empty to turn logging off.")
@app_commands.checks.has_permissions(administrator=True)
async def set_log_channel(interaction: discord.Interaction, channel: discord.TextChannel = None):
    logger.info(f"/set_log_channel invoked by {interaction.user} (Guild: {interaction.guild_id}, Channel: {'#' + channel.name if channel else 'None (clearing)'})")

    SettingsManager.set_log_channel(interaction.guild_id, channel.id if channel else None)

    if channel:
        await interaction.response.send_message(
            f"Purge job logs (started/completed) will now be posted to {channel.mention}.",
            ephemeral=True
        )
    else:
        await interaction.response.send_message("Log channel cleared. Purge jobs will no longer be logged.", ephemeral=True)


# Permission Error Handler
@configure_channel.error
@purge_now.error
@purge_count.error
@dry_run.error
@stop.error
@set_log_channel.error
async def on_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        msg = "You need Administrator permissions to use this command."
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    else:
        logger.error(f"Unhandled command error: {error}")
        msg = "Something went wrong running that command."
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)


bot.run(DISCORD_TOKEN)
