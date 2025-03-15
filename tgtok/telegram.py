import logging
import os
import re
from typing import Any, AsyncGenerator

from telethon import TelegramClient
from telethon.tl.custom.message import Message

from tgtok.storage import Storage
from tgtok.structures import ParsedTelegramPost, CombinedPost


class Telegram:
    TEMPLATE_POST_ID = ("**_id:** `", "`")
    TEMPLATE_LINK = ("**lnk:** [here](", ")")
    TEMPLATE_LIKED = ("**lkd:** `", "`")
    TEMPLATE_FAVORITED = ("**fav:** `", "`")

    def __init__(self, app_id: int, app_hash: str, bot_token: str, channel_id: int, storage: Storage):
        self.storage = storage
        self.log = logging.getLogger("tgtok.telegram")
        self.bot_token = bot_token
        self.channel_id = channel_id
        self.bot = TelegramClient("bot", app_id, app_hash)
        self.RE_POST_ID = self.create_regex(self.TEMPLATE_POST_ID)
        self.RE_URL = self.create_regex(self.TEMPLATE_LINK)
        self.RE_LIKED = self.create_regex(self.TEMPLATE_LIKED)
        self.RE_FAVORITED = self.create_regex(self.TEMPLATE_FAVORITED)
        self.posts: dict[int, ParsedTelegramPost] = {}
        self.allowed_empty_posts = 30

    def create_regex(self, template: tuple[str, str]) -> re.Pattern:
        p = re.compile(f"(?:{re.escape(template[0])})(.*?)(?:{re.escape(template[1])})")
        return p

    async def connect(self):
        await self.bot.start(bot_token=self.bot_token)  # type: ignore
        me = await self.bot.get_me()
        self.log.info(f"Connected to Telegram account {me.username}")  # type: ignore

    async def load_messages(
        self, start_id: int = 0, reverse: bool = False, max_count: int = 30
    ) -> AsyncGenerator[Message, None]:
        # From oldest to newest (by default)
        if reverse and not start_id:
            raise RuntimeError("Going from new ones requires setting start ID")
        if not max_count:
            max_count = 4242133769  # Just a random big number that would be bigger than any channel post count
        channel = await self.bot.get_entity(self.channel_id)
        failed_count = 0
        cntr = 0
        while failed_count < self.allowed_empty_posts and cntr < max_count:
            offset = -cntr if reverse else cntr
            msg: Message | None = await self.bot.get_messages(channel, ids=start_id + offset)  # type: ignore
            if msg:
                yield msg
                failed_count = 0
            else:
                failed_count += 1
            cntr += 1

    def parse_message_text(self, msg: str | None) -> dict[str, Any]:
        if not msg:
            return {}
        data = {}
        data["id"] = re.findall(self.RE_POST_ID, msg)
        data["url"] = re.findall(self.RE_URL, msg)
        data["liked"] = re.findall(self.RE_LIKED, msg)
        data["favorited"] = re.findall(self.RE_FAVORITED, msg)
        if not data["id"] or not data["url"]:
            # Should never be changed. TikTok ID serves as "primary key", and URL derives from it.
            # Other fields could be empty (if format changes), messages will be updated accordingly
            return {}
        if data["liked"] is None:
            data["liked"] = False
        if data["favorited"] is None:
            data["favorited"] = False
        return data

    def parse_message(self, msg: Message) -> ParsedTelegramPost:
        text_data = self.parse_message_text(msg.raw_text)
        if not text_data:
            # Assume follow-up picture post, as there could be multiple
            post = ParsedTelegramPost(
                id_=msg.id,
                tiktok_id=0,
                web_url="",
                liked=False,
                favorited=False,
                description="",
            )
            return post
        post = ParsedTelegramPost(
            id_=msg.id,
            tiktok_id=text_data["id"],
            web_url=text_data["url"],
            liked=text_data["liked"],
            favorited=text_data["favorited"],
            description="",
        )
        return post

    async def fetch_last_id(self) -> int:
        self.log.info("fetching last message ID")
        channel = await self.bot.get_entity(self.channel_id)
        msg = await self.bot.send_message(channel, ".", silent=True)  # type: ignore
        last_id = msg.id
        await self.bot.delete_messages(channel, message_ids=last_id)  # type: ignore
        return last_id - 1

    async def update_data(
        self, start_id: int = 0, reverse: bool = False, max_count: int = 30, determine_last_id: bool = False
    ) -> None:
        # If not start_id - fall back to auto
        if reverse and not (determine_last_id or start_id):
            raise RuntimeError("Going from new ones requires setting start ID or auto determining it")

        self.log.info("Fetching new posts")

        if determine_last_id:
            start_id = await self.fetch_last_id()
        if not start_id:
            srt = sorted(self.posts.values(), key=lambda p: p.id_, reverse=True)
            if not srt:
                srt = 0
            else:
                srt = srt[0].id_
            start_id = srt + 1

        cntr = 0
        async for msg in self.load_messages(start_id, reverse, max_count):
            post = self.parse_message(msg)
            if post.id_ in self.posts:
                break
            self.posts[post.id_] = post
            cntr += 1

        self.log.info(f"Fetched {cntr} new posts")

        self.storage.link_with_tg(list(self.posts.values()))

    async def post(self, item: CombinedPost) -> CombinedPost:
        data_dir = "tmp"
        if item.telegram_id:
            raise RuntimeError("Already posted")
        self.log.info(f"Posting {item.tiktok_type} {item.tiktok_id}")
        channel = await self.bot.get_entity(self.channel_id)
        text = (
            f"{self.TEMPLATE_POST_ID[0]}{item.tiktok_id}{self.TEMPLATE_POST_ID[1]}\n"
            f"{self.TEMPLATE_LINK[0]}{item.tiktok_url}{self.TEMPLATE_LINK[1]}\n"
            f"{self.TEMPLATE_LIKED[0]}{item.liked}{self.TEMPLATE_LIKED[1]}\n"
            f"{self.TEMPLATE_FAVORITED[0]}{item.favorited}{self.TEMPLATE_FAVORITED[1]}"
        )
        filenames_sorted = []
        filenames = [f"{data_dir}/{f}" for f in os.listdir(data_dir) if f.startswith(f"{item.tiktok_id}_")]
        filenames_sorted = sorted(filenames, key=lambda s: int(s.split(".")[0].split("_")[2]))
        msgs = await self.bot.send_file(channel, filenames_sorted, caption=text)  # type: ignore
        sent_w_caption = self.parse_message(msgs[0])
        item._raw_tg = sent_w_caption
        item.telegram_id = sent_w_caption.id_
        return item
