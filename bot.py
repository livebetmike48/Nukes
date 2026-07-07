import os
import logging
import asyncio
from datetime import datetime, timezone

import discord
from discord.ext import tasks
from dotenv import load_dotenv

import keywords
import storage

load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
TWITTERAPI_KEY = os.getenv("TWITTERAPI_KEY")
LIST_ID = os.getenv("LIST_ID")  # the X List ID containing all monitored beat reporters

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("twitter_bot")

intents = discord.Intents.default()


def build_tweet_embed(tweet: dict, matches: list[dict] = None) -> discord.Embed:
    labels = " ".join(f"{m['emoji']} {m['label']}" for m in matches) if matches else None
    author = tweet.get("author") or {}

    tweet_dt = None
    created_at = tweet.get("createdAt")
    if created_at:
        try:
            tweet_dt = datetime.strptime(created_at, "%a %b %d %H:%M:%S %z %Y")
        except Exception:
            tweet_dt = datetime.now(timezone.utc)

    embed = discord.Embed(
        description=tweet.get("text", ""),
        url=tweet.get("twitterUrl") or tweet.get("url"),
        color=discord.Color.blue(),
        timestamp=tweet_dt or datetime.now(timezone.utc),
    )
    embed.set_author(
        name=f"{author.get('name', 'Unknown')} (@{author.get('userName', 'unknown')})",
        icon_url=author.get("profilePicture"),
        url=tweet.get("twitterUrl") or tweet.get("url"),
    )
    if labels:
        embed.title = labels
    return embed


class TwitterMonitorBot(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = discord.app_commands.CommandTree(self)

    async def setup_hook(self):
        storage.init_db()

        setchannel_cmd = discord.app_commands.Command(
            name="setchannel",
            description="Set this channel to receive beat reporter alerts",
            callback=self._setchannel_callback,
        )
        self.tree.add_command(setchannel_cmd)

        recenttweets_cmd = discord.app_commands.Command(
            name="recenttweets",
            description="Show the most recent tweets from your monitored list",
            callback=self._recenttweets_callback,
        )
        self.tree.add_command(recenttweets_cmd)

        search_cmd = discord.app_commands.Command(
            name="search",
            description="Search recent tweets from your list for a specific word or phrase",
            callback=self._search_callback,
        )
        self.tree.add_command(search_cmd)

        addtolist_cmd = discord.app_commands.Command(
            name="addtolist",
            description="Add one or more comma-separated handles to the monitored X List",
            callback=self._addtolist_callback,
        )
        self.tree.add_command(addtolist_cmd)

        try:
            synced = await self.tree.sync()
            log.info("Synced %d slash commands", len(synced))
        except Exception as e:
            log.error("Slash command sync failed: %s", e)

    async def _setchannel_callback(self, interaction: discord.Interaction):
        storage.set_config("announce_channel_id", str(interaction.channel_id))
        await interaction.response.send_message(
            f"✅ Beat reporter alerts will post in {interaction.channel.mention}."
        )

    async def _recenttweets_callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        import requests

        url = "https://api.twitterapi.io/twitter/list/tweets"
        headers = {"X-API-Key": TWITTERAPI_KEY}
        params = {"listId": LIST_ID}

        try:
            resp = await asyncio.to_thread(requests.get, url, headers=headers, params=params, timeout=15)
            data = resp.json()
        except Exception as e:
            await interaction.followup.send(f"Request failed: {e}")
            return

        tweets = data.get("tweets", [])
        if not tweets:
            await interaction.followup.send(f"Status {resp.status_code}, but no tweets in response:\n```{resp.text[:1500]}```")
            return

        await interaction.followup.send(f"Showing the {min(3, len(tweets))} most recent tweets from your list, in the actual clean format:")
        for tweet in tweets[:3]:
            await interaction.channel.send(embed=build_tweet_embed(tweet))

    async def _search_callback(self, interaction: discord.Interaction, term: str):
        await interaction.response.defer()
        import requests

        url = "https://api.twitterapi.io/twitter/list/tweets"
        headers = {"X-API-Key": TWITTERAPI_KEY}
        params = {"listId": LIST_ID}

        try:
            resp = await asyncio.to_thread(requests.get, url, headers=headers, params=params, timeout=15)
            data = resp.json()
        except Exception as e:
            await interaction.followup.send(f"Request failed: {e}")
            return

        tweets = data.get("tweets", [])
        term_lower = term.lower()
        matches = [t for t in tweets if term_lower in t.get("text", "").lower()]

        if not matches:
            await interaction.followup.send(f"No recent tweets in your list mention '{term}'.")
            return

        await interaction.followup.send(f"Found {len(matches)} recent tweet(s) mentioning '{term}':")
        for tweet in matches[:5]:
            await interaction.channel.send(embed=build_tweet_embed(tweet))

    async def _addtolist_callback(self, interaction: discord.Interaction, handles: str):
        await interaction.response.defer()
        import requests

        usernames = [h.strip().lstrip("@") for h in handles.split(",") if h.strip()]
        url = "https://api.twitterapi.io/twitter/list/add_member"
        headers = {"X-API-Key": TWITTERAPI_KEY}

        results = []
        for username in usernames:
            try:
                resp = await asyncio.to_thread(
                    requests.post, url, headers=headers,
                    json={"listId": LIST_ID, "userName": username}, timeout=15,
                )
                if resp.status_code == 200:
                    results.append(f"✅ @{username}")
                else:
                    results.append(f"❌ @{username} — status {resp.status_code}: {resp.text[:150]}")
            except Exception as e:
                results.append(f"❌ @{username} — {e}")

        await interaction.followup.send("\n".join(results)[:2000])

    async def on_ready(self):
        log.info("Logged in as %s", self.user)
        if not poll_list_tweets.is_running():
            poll_list_tweets.start(self)
        if not watchdog.is_running():
            watchdog.start()


client = TwitterMonitorBot()

POLL_SECONDS = int(os.getenv("POLL_SECONDS", "90"))


@tasks.loop(seconds=POLL_SECONDS)
async def poll_list_tweets(bot: TwitterMonitorBot):
    try:
        await _poll_list_tweets_body(bot)
    except Exception as e:
        # Top-level safety net -- an unhandled exception here would
        # otherwise permanently stop this loop with no automatic recovery.
        log.error("poll_list_tweets cycle failed unexpectedly, will retry next cycle: %s", e)


async def _poll_list_tweets_body(bot: TwitterMonitorBot):
    import requests

    channel_id = storage.get_config("announce_channel_id")
    if not channel_id:
        return
    channel = bot.get_channel(int(channel_id))
    if channel is None:
        return

    url = "https://api.twitterapi.io/twitter/list/tweets"
    headers = {"X-API-Key": TWITTERAPI_KEY}
    params = {"listId": LIST_ID}

    try:
        resp = await asyncio.to_thread(requests.get, url, headers=headers, params=params, timeout=15)
        data = resp.json()
    except Exception as e:
        log.error("Failed to fetch list tweets: %s", e)
        return

    tweets = data.get("tweets", [])
    # Process oldest-first so if multiple new tweets arrived since last
    # check, they post to Discord in chronological order.
    for tweet in reversed(tweets):
        text = tweet.get("text", "")
        tweet_id = tweet.get("id")
        if not text or not tweet_id:
            continue
        if storage.already_posted(tweet_id):
            continue

        matches = keywords.classify_tweet(text)
        storage.mark_posted(tweet_id)  # mark seen regardless of match, so we never re-check it
        if not matches:
            continue  # not betting-relevant, skip silently

        try:
            await channel.send(embed=build_tweet_embed(tweet, matches))
            log.info("Posted tweet %s (categories: %s)", tweet_id, [m["key"] for m in matches])
        except Exception as e:
            log.error("Failed to post tweet %s: %s", tweet_id, e)


@poll_list_tweets.before_loop
async def before_poll():
    await client.wait_until_ready()


@tasks.loop(minutes=2)
async def watchdog():
    """If the poll loop somehow stops for any reason not already caught
    above, this notices within 2 minutes and restarts it."""
    if not poll_list_tweets.is_running():
        log.error("poll_list_tweets was found stopped -- restarting it now")
        poll_list_tweets.start(client)


@watchdog.before_loop
async def before_watchdog():
    await client.wait_until_ready()


if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise SystemExit("Set DISCORD_TOKEN in your .env file.")
    if not TWITTERAPI_KEY:
        raise SystemExit("Set TWITTERAPI_KEY in your .env file.")
    if not LIST_ID:
        raise SystemExit("Set LIST_ID in your .env file (the X List containing your beat reporters).")
    client.run(DISCORD_TOKEN)
