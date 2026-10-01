import io
import json
import logging
import sys
from pathlib import Path

import discord

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("guildproxy")

CONFIG_PATH = Path(__file__).resolve().parent / "config.json"

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".avif"}


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        raise SystemExit(f"config file not found: {CONFIG_PATH}")
    with CONFIG_PATH.open(encoding="utf-8") as f:
        config = json.load(f)
    for key in ("token", "webhook"):
        if not config.get(key):
            raise SystemExit(f"missing '{key}' in {CONFIG_PATH}")
    return config


def is_image(attachment: discord.Attachment) -> bool:
    if attachment.content_type and attachment.content_type.startswith("image/"):
        return True
    return Path(attachment.filename).suffix.lower() in IMAGE_EXTENSIONS


def build_embeds(message: discord.Message) -> list[discord.Embed]:
    def new_embed() -> discord.Embed:
        embed = discord.Embed(timestamp=message.created_at)
        embed.set_author(
            name=message.author.display_name,
            icon_url=message.author.display_avatar.url,
        )
        embed.set_footer(text=f"#{message.channel.name} | {message.guild.name}")
        return embed

    images = [a for a in message.attachments if is_image(a)]
    first = new_embed()
    if message.content:
        first.description = message.content

    if not images:
        return [first]

    first.set_image(url=images[0].url)
    embeds = [first]
    for attachment in images[1:10]:
        extra = new_embed()
        extra.set_image(url=attachment.url)
        embeds.append(extra)
    return embeds


class GuildProxy(discord.Client):
    def __init__(self, guild_id: int, webhook_url: str, **options) -> None:
        super().__init__(**options)
        self.guild_id = guild_id
        self.webhook_url = webhook_url
        self.webhook: discord.Webhook | None = None

    async def setup_hook(self) -> None:
        self.webhook = discord.Webhook.from_url(self.webhook_url, client=self)

    async def on_ready(self) -> None:
        log.info("logged in as %s (%s)", self.user, self.user.id)
        log.info("watching guild %s", self.guild_id)

    async def on_message(self, message: discord.Message) -> None:
        if self.webhook is None:
            return
        if message.guild is None or message.guild.id != self.guild_id:
            return
        if message.author.bot or message.webhook_id is not None:
            return
        if not message.content and not message.attachments:
            return

        files = []
        for attachment in message.attachments:
            if is_image(attachment):
                continue
            try:
                data = await attachment.read()
            except discord.HTTPException:
                log.exception("failed to download attachment %s", attachment.filename)
                continue
            files.append(discord.File(io.BytesIO(data), filename=attachment.filename))

        try:
            await self.webhook.send(embeds=build_embeds(message), files=files)
        except discord.HTTPException:
            log.exception("failed to send webhook for message %s", message.id)


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit("usage: uv run main.py GUILD_ID")
    try:
        guild_id = int(sys.argv[1])
    except ValueError:
        raise SystemExit(f"invalid GUILD_ID: {sys.argv[1]!r}")

    config = load_config()
    client = GuildProxy(
        guild_id=guild_id,
        webhook_url=config["webhook"],
    )
    client.run(config["token"])


if __name__ == "__main__":
    main()
