import logging
import os
from typing import Any, AsyncGenerator

log = logging.getLogger("f2").addHandler(logging.NullHandler())

from f2.log.logger import LogManager  # noqa: E402
from f2.apps.tiktok.utils import SecUserIdFetcher, ClientConfManager  # noqa: E402
from f2.i18n.translator import TranslationManager  # noqa: E402

from tikdog.storage import Storage  # noqa: E402
from tikdog.structures import DownloadTask, ParsedTikTokPost  # noqa: E402

LogManager().setup_logging(logging.WARNING, log_to_console=False, log_path=None)


class TikTok:
    def __init__(self, username: str, browser_cookie: str, device_id: str, storage: Storage):
        self.log = logging.getLogger("tikdog.tiktok")
        self.storage = storage
        self.username = username
        TranslationManager.get_instance().set_language("en_US")
        ClientConfManager.tiktok_conf["BaseRequestModel"]["device"]["id"] = device_id
        from f2.apps.tiktok.handler import TiktokHandler, rich_console

        rich_console.quiet = True
        self.tt = TiktokHandler(
            {
                "headers": {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:142.0) Gecko/20100101 Firefox/142.0",
                    "referer": "https://www.tiktok.com/",
                    "origin": "https://www.tiktok.com",
                },
                "cookie": browser_cookie,
                "timeout": 10,
            }
        )
        self.uid = ""
        self.fetch_block_size = 25
        self.posts: dict[int, ParsedTikTokPost] = {}

    async def connect(self):
        self.uid = await SecUserIdFetcher.get_secuid(f"https://www.tiktok.com/@{self.username}")
        self.log.info(f"Connected to TikTok account {self.username}")

    async def check_video_download(self) -> bool:
        FISCH_ID = "7455398333754952967"
        try:
            os.remove("tmp/tmp.mp4")
        except FileNotFoundError:
            pass
        self.log.info("Trying to download test video to check device ID correctness")
        vid = (await self.tt.fetch_one_video(FISCH_ID))._to_dict()
        await self.tt.downloader.initiate_download("video", vid["video_playAddr"], "tmp", "tmp", ".mp4")
        await self.tt.downloader.download_tasks[-1]
        # no way to get download result directly
        if os.path.exists("tmp/tmp.mp4"):
            self.log.info("Download success, device data is fine!")
            success = True
            os.remove("tmp/tmp.mp4")
        else:
            self.log.error("Can't download test video. Probably, your device ID is invalid.")
            success = False
        return success

    async def fetch_items(self, items: list[DownloadTask]) -> None:
        data_dir = "tmp"
        for item in items:
            self.log.debug(f"downloading {item.type_} {item.id_}")
            for url in item.named_urls:
                if not os.path.exists(f"{data_dir}/{url['name']}"):
                    await self.tt.downloader.initiate_download(
                        item.type_, url["url"], data_dir, url["name"].split(".")[0], f".{item.ext}"
                    )
                    await self.tt.downloader.download_tasks[-1]

    def delete_items(self, items: list[DownloadTask]) -> None:
        # While it's enough to pass just the ID, it should be common that
        # deletion will be used along the download. So, it will accept the same
        # task object. Should be easy to make it different, though
        data_dir = "tmp"
        for item in items:
            for url in item.named_urls:
                if os.path.exists(f"{data_dir}/{url['name']}"):
                    os.remove(f"{data_dir}/{url['name']}")

    async def parse_items(self, block_items: list[dict[str, Any]]) -> list[ParsedTikTokPost]:
        items = []
        for item in block_items:
            try:
                new_item = {
                    "id_": int(item["id"]),
                    "type_": "photo" if "imagePost" in item else "video",
                }
                new_item["web_url"] = f"https://www.tiktok.com/@uSeRnAmE/{new_item['type_']}/{new_item['id_']}"
                if new_item["type_"] == "photo":
                    new_item["download_urls"] = [u["imageURL"]["urlList"] for u in item["imagePost"]["images"]]
                if new_item["type_"] == "video":
                    new_item["download_urls"] = [item["video"]["playAddr"]]
                post = ParsedTikTokPost(**new_item)
                items.append(post)
            except:
                self.log.error("Failed to parse TikTok post. Raw data below, bailing out.")
                self.log.error(item)
                raise
        return items

    async def fetch_liked(self) -> AsyncGenerator[dict[str, Any], None]:
        # From newest to oldest
        cntr = 0
        async for block in self.tt.fetch_user_like_videos(self.uid, 0, self.fetch_block_size, 0):
            block_raw = block._to_raw()
            fetched = len(block_raw["itemList"])
            cntr += fetched
            has_more = block_raw["hasMore"]
            self.log.debug(f"fetched {fetched} liked posts ({cntr} total), is there more - {has_more}")
            yield block_raw
            if not has_more:
                break

    async def fetch_favorite(self) -> AsyncGenerator[dict[str, Any], None]:
        # From newest to oldest
        cntr = 0
        async for block in self.tt.fetch_user_collect_videos(self.uid, 0, self.fetch_block_size, 0):
            block_raw = block._to_raw()
            fetched = len(block_raw["itemList"])
            cntr += fetched
            has_more = block_raw["hasMore"]
            self.log.debug(f"fetched {fetched} saved posts ({cntr} total), is there more - {has_more}")
            yield block_raw
            if not has_more:
                break

    async def update_data(self) -> None:
        # Return the latest saved post from correct dictionary, creating it if necessary
        def get_init_if_needs(item: ParsedTikTokPost) -> ParsedTikTokPost:
            if item.id_ not in self.posts and item.id_ not in new_posts:
                new_posts[item.id_] = item
            in_new_posts = new_posts.get(item.id_)
            if in_new_posts:
                return in_new_posts
            in_posts = self.posts.get(item.id_)
            if in_posts:
                return in_posts
            raise KeyError("item should be initialized, but somehow it's not")

        self.log.info("Fetching new posts")
        # As the order of posts is the newest -> oldest, we can't just append to the main dict
        new_posts: dict[int, ParsedTikTokPost] = {}
        # Probably, all favorited items are liked, so to keep proper order we start with liked ones
        should_stop = False
        async for block_raw in self.fetch_liked():
            parsed_items = await self.parse_items(block_raw["itemList"])
            for item in parsed_items:
                saved = get_init_if_needs(item)
                if saved.liked:
                    # Already fetched by this function.
                    # If the previous order hasn't changed (and it probably shouldn't),
                    # then this marks that we have reached previous fetch data
                    self.log.info(f"stopping at {saved.id_} as it's already fetched")
                    should_stop = True
                    break
                saved.liked = True
            if should_stop:
                break
        # However, in case there are a few that are not, we still account for them
        should_stop = False
        async for block_raw in self.fetch_favorite():
            parsed_items = await self.parse_items(block_raw["itemList"])
            for item in parsed_items:
                saved = get_init_if_needs(item)
                if saved.favorited:
                    self.log.info(f"stopping at {saved.id_} as it's already fetched")
                    should_stop = True
                    break
                saved.favorited = True
            if should_stop:
                break
        self.log.info(f"Fetched {len(new_posts)} new posts")

        # Recreate to keep new -> old order
        self.posts = new_posts | self.posts

        self.storage.add([p for id_, p in self.posts.items() if id_ not in self.storage])
