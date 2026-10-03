import asyncio
import json
import logging
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

import discord

from config import load_config

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("guildproxy.dump")

DUMP_ROOT = Path(__file__).resolve().parent / "dump"
PAGE_SIZE = 100

EMOJI_RE = re.compile(r"<(a)?:([a-zA-Z0-9_]+):(\d+)>")

# Discord sticker format types -> file extension
STICKER_EXT = {1: "png", 2: "png", 3: "json", 4: "gif"}


def is_discord_host(host: str) -> bool:
    if host in {"cdn.discordapp.com", "media.discordapp.net"}:
        return True
    return host.endswith(".discordapp.net") and host.startswith("images-ext-")


def avatar_url(user_id: int, avatar_hash: str | None) -> str:
    if avatar_hash:
        ext = "gif" if avatar_hash.startswith("a_") else "png"
        return f"https://cdn.discordapp.com/avatars/{user_id}/{avatar_hash}.{ext}"
    return f"https://cdn.discordapp.com/embed/avatars/{(user_id >> 22) % 6}.png"


def banner_url(user_id: int, banner_hash: str | None) -> str | None:
    if not banner_hash:
        return None
    ext = "gif" if banner_hash.startswith("a_") else "png"
    return f"https://cdn.discordapp.com/banners/{user_id}/{banner_hash}.{ext}"


def emoji_url(emoji_id: str, animated: bool) -> str:
    return f"https://cdn.discordapp.com/emojis/{emoji_id}.{'gif' if animated else 'png'}"


def sticker_url(sticker_id: str, format_type: int) -> str:
    ext = STICKER_EXT.get(format_type, "png")
    return f"https://media.discordapp.net/stickers/{sticker_id}.{ext}"


class AssetStore:
    def __init__(self, root: Path, client: discord.Client) -> None:
        self.root = root
        self.client = client
        self.known: set[str] = set()

    def rel_path(self, url: str) -> str:
        parsed = urlparse(url)
        return (Path("assets") / parsed.netloc / parsed.path.lstrip("/")).as_posix()

    async def fetch(self, url: str) -> str | None:
        parsed = urlparse(url)
        if not is_discord_host(parsed.netloc):
            return None
        rel = self.rel_path(url)
        dest = self.root / rel
        if rel in self.known or dest.exists():
            self.known.add(rel)
            return rel
        try:
            data = await self.client.http.get_from_cdn(url)
        except discord.HTTPException:
            log.exception("failed to download asset %s", url)
            return None
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        self.known.add(rel)
        log.debug("saved asset %s", rel)
        return rel


class UserRegistry:
    def __init__(self, root: Path, client: discord.Client, store: AssetStore) -> None:
        self.root = root
        self.client = client
        self.store = store
        self.queue: list[int] = []
        self.queued: set[int] = set()
        self.path = root / "users.jsonl"
        self.seen: set[int] = self._load_seen()

    def _load_seen(self) -> set[int]:
        seen: set[int] = set()
        if not self.path.exists():
            return seen
        with self.path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                user_id = self._record_id(record)
                if user_id is not None:
                    seen.add(user_id)
        return seen

    @staticmethod
    def _record_id(record: dict) -> int | None:
        user = record.get("user")
        if isinstance(user, dict) and user.get("id"):
            return int(user["id"])
        if record.get("id"):
            return int(record["id"])
        return None

    def add(self, user_id: int | str | None) -> None:
        if user_id is None:
            return
        user_id = int(user_id)
        if user_id in self.queued:
            return
        self.queued.add(user_id)
        self.queue.append(user_id)

    async def _fetch_profile(self, user_id: int) -> dict | None:
        try:
            return await self.client.http.get_user_profile(
                user_id,
                with_mutual_guilds=False,
                with_mutual_friends=False,
                with_mutual_friends_count=False,
            )
        except discord.HTTPException:
            log.info("profile fetch failed for %s, falling back to user", user_id)
        try:
            return {"user": await self.client.http.get_user(user_id)}
        except discord.HTTPException:
            log.exception("failed to fetch user %s", user_id)
            return None

    async def flush(self) -> None:
        pending = [uid for uid in self.queue if uid not in self.seen]
        if not pending:
            return
        with self.path.open("a", encoding="utf-8") as f:
            for user_id in pending:
                record = await self._fetch_profile(user_id)
                if record is None:
                    continue
                user = record.get("user") if isinstance(record.get("user"), dict) else {}
                record["avatar_local"] = await self.store.fetch(
                    avatar_url(user_id, user.get("avatar"))
                )
                profile = record.get("user_profile")
                banner_hash = profile.get("banner") if isinstance(profile, dict) else None
                b_url = banner_url(user_id, banner_hash)
                record["banner_local"] = await self.store.fetch(b_url) if b_url else None
                f.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
                f.flush()
                self.seen.add(user_id)
                log.info("saved profile %s", user_id)


async def process_message(
    raw: dict,
    store: AssetStore,
    users: UserRegistry,
) -> dict:
    author = raw.get("author")
    if isinstance(author, dict):
        users.add(author.get("id"))
        raw["author_id"] = author.get("id")
        del raw["author"]

    members = raw.get("member")
    if isinstance(members, dict) and isinstance(members.get("user"), dict):
        users.add(members["user"].get("id"))

    for mention in raw.get("mentions") or []:
        if isinstance(mention, dict):
            users.add(mention.get("id"))

    reference = raw.get("referenced_message")
    if isinstance(reference, dict) and isinstance(reference.get("author"), dict):
        users.add(reference["author"].get("id"))

    for attachment in raw.get("attachments") or []:
        url = attachment.get("url")
        if not url:
            continue
        local = await store.fetch(url)
        if local:
            attachment["url"] = local
            if "proxy_url" in attachment:
                attachment["proxy_url"] = local

    for sticker in raw.get("sticker_items") or []:
        sticker_id = sticker.get("id")
        if not sticker_id:
            continue
        local = await store.fetch(sticker_url(sticker_id, sticker.get("format_type", 1)))
        if local:
            sticker["local_path"] = local

    emojis: dict[str, tuple[str, bool]] = {}
    for animated, name, emoji_id in EMOJI_RE.findall(raw.get("content") or ""):
        emojis[emoji_id] = (name, bool(animated))
    for reaction in raw.get("reactions") or []:
        emoji = reaction.get("emoji") if isinstance(reaction, dict) else None
        if isinstance(emoji, dict) and emoji.get("id"):
            emojis[str(emoji["id"])] = (emoji.get("name") or "", bool(emoji.get("animated")))
    if emojis:
        custom = []
        for emoji_id, (name, animated) in emojis.items():
            local = await store.fetch(emoji_url(emoji_id, animated))
            custom.append(
                {"id": emoji_id, "name": name, "animated": animated, "local_path": local}
            )
        raw["custom_emojis"] = custom

    return raw


def read_last_id(path: Path) -> int:
    if not path.exists():
        return 0
    last = ""
    with path.open(encoding="utf-8") as f:
        for line in f:
            stripped = line.strip()
            if stripped:
                last = stripped
    if not last:
        return 0
    try:
        return int(json.loads(last)["id"])
    except (json.JSONDecodeError, KeyError, ValueError):
        return 0


async def dump_channel(client: discord.Client, channel_id: int) -> None:
    root = DUMP_ROOT / str(channel_id)
    root.mkdir(parents=True, exist_ok=True)
    store = AssetStore(root, client)
    users = UserRegistry(root, client, store)
    messages_path = root / "messages.jsonl"

    cursor = read_last_id(messages_path)
    if cursor:
        log.info("resuming channel %s after message %s", channel_id, cursor)
    else:
        log.info("dumping channel %s from the beginning", channel_id)

    count = 0
    with messages_path.open("a", encoding="utf-8") as f:
        while True:
            data = await client.http.logs_from(channel_id, PAGE_SIZE, after=cursor)
            if not data:
                break
            cursor = int(data[0]["id"])
            for raw in reversed(data):
                raw = await process_message(raw, store, users)
                f.write(json.dumps(raw, ensure_ascii=False, separators=(",", ":")) + "\n")
                count += 1
            f.flush()
            log.info("channel %s: %d new messages (cursor=%s)", channel_id, count, cursor)
            if len(data) < PAGE_SIZE:
                break

    await users.flush()
    log.info("channel %s: done, %d messages total this run", channel_id, count)


async def run(channel_id: int) -> None:
    config = load_config("token")
    client = discord.Client()
    await client.login(config["token"])
    try:
        await dump_channel(client, channel_id)
    finally:
        await client.close()


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit("usage: uv run dump.py CHANNEL_ID")
    try:
        channel_id = int(sys.argv[1])
    except ValueError:
        raise SystemExit(f"invalid CHANNEL_ID: {sys.argv[1]!r}")
    asyncio.run(run(channel_id))


if __name__ == "__main__":
    main()
