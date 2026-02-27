import asyncio
import base64
import hashlib
import json
import logging
import os
import re
from typing import Any, AsyncGenerator, Literal
from urllib.parse import urlencode

import httpx
from mutagen.id3._frames import APIC, TIT2
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4, MP4Cover

from tikdog.storage import Storage
from tikdog.structures import DownloadTask, ParsedTikTokPost


class TikTok:
    def __init__(self, username: str, browser_cookie: str, device_id: str, storage: Storage):
        self.log = logging.getLogger("tikdog.tiktok")
        self.storage = storage
        self.username = username
        self.browser_params = {
            "aid": "1988",
            "app_language": "en",
            "app_name": "tiktok_web",
            "browser_language": "en-US",
            "browser_name": "Mozilla",
            "browser_online": "true",
            "browser_platform": "Win32",
            "browser_version": "5.0 (Windows)",
            "channel": "tiktok_web",
            "device_id": device_id,
            "device_platform": "web_pc",
            "os": "windows",
            "priority_region": "",
            "region": "US",
            "screen_height": "1440",
            "screen_width": "2560",
            "webcast_language": "en",
        }
        self.browser_headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/136.0.0.0 Safari/537.36"
            ),
            "Cookie": browser_cookie,
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "sec-ch-ua": '"Chromium";v="136", "Google Chrome";v="136"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "Referer": "https://www.tiktok.com/",
        }
        self.sec_uid = ""
        self.fetch_block_size = 25
        self.posts: dict[int, ParsedTikTokPost] = {}
        self.request_delay_sec = 5

    async def request(self, method: Literal["GET", "POST"], url: str) -> httpx.Response:
        async with httpx.AsyncClient(follow_redirects=True) as cli:
            resp = await cli.request(method, url, headers=self.browser_headers)
            if (
                resp.status_code == 200
                and "text/html" in resp.headers.get("Content-Type", "")
                and "SlardarWAF" in resp.text
                and 'id="cs"' in resp.text
            ):  # WAF
                m_wci = re.search(r'<p id="wci" class="([^"]*)"', resp.text)
                m_cs = re.search(r'<p id="cs" class="([^"]*)"', resp.text)
                if not m_wci or not m_cs:
                    raise RuntimeError("WAF challenge HTML is missing wci/cs fields")
                cookie_name = m_wci.group(1)
                cs_b64 = m_cs.group(1)

                m_rci = re.search(r'<p id="rci" class="([^"]*)"', resp.text)
                m_rs = re.search(r'<p id="rs" class="([^"]*)"', resp.text)
                rci = m_rci.group(1) if m_rci else ""
                rs = m_rs.group(1) if m_rs else ""

                def _b64d(s: str) -> bytes:
                    return base64.b64decode(s + "=" * (-len(s) % 4))

                c = json.loads(_b64d(cs_b64))
                prefix = _b64d(c["v"]["a"])
                expected = _b64d(c["v"]["c"]).hex()

                self.log.info(f"  solving WAF challenge (cookie={cookie_name})...")
                solution = None
                for i in range(1_000_001):
                    h = hashlib.sha256(prefix + str(i).encode()).hexdigest()
                    if h == expected:
                        solution = i
                        break

                if solution is None:
                    raise RuntimeError("WAF challenge: no solution found in 0..1_000_000")

                c["d"] = base64.b64encode(str(solution).encode()).decode()
                cookie_value = base64.b64encode(json.dumps(c, separators=(",", ":")).encode()).decode()
                waf_cookie = f"{cookie_name}={cookie_value}"
                if rci and rs:
                    waf_cookie += f"; {rci}={rs}"

                existing_cookies = self.browser_headers.get("Cookie", "")
                retry_cookies = f"{existing_cookies}; {waf_cookie}" if existing_cookies else waf_cookie
                retry_headers = {**self.browser_headers, "Cookie": retry_cookies}

                resp2 = await cli.request(method, url, headers=retry_headers)
                return resp2
            else:
                return resp

    async def connect(self) -> None:
        user_resp = await self.request("GET", f"https://www.tiktok.com/@{self.username}")
        user_resp.raise_for_status()
        m = re.search(r'"secUid":"([^"]+)"', user_resp.text)
        if not m:
            raise RuntimeError("Couldn't fetch secUid for user!")
        self.sec_uid = m.group(1)
        self.log.info(f"Connected to TikTok account {self.username}")

    async def fetch_post_metadata(self, video_id: int) -> ParsedTikTokPost:
        post_resp = await self.request("GET", f"https://www.tiktok.com/@user/video/{video_id}")
        post_resp.raise_for_status()
        post_html = post_resp.text
        m = re.search(r'<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.+?)</script>', post_html)
        if not m:
            raise RuntimeError("Could not parse video metadata")
        data = (
            json.loads(m.group(1))
            .get("__DEFAULT_SCOPE__", {})
            .get("webapp.video-detail", {})
            .get("itemInfo", {})
            .get("itemStruct", {})
        )
        if not data:
            raise RuntimeError("Could not parse video metadata")
        post = await self.parse_items([data])
        return post[0]

    async def check_video_download(self) -> bool:
        FISCH_ID = 7455398333754952967
        self.log.info("Trying to download test video to check device ID correctness")
        vid = await self.fetch_post_metadata(FISCH_ID)
        try:
            await self.fetch_items(vid)
            self.log.info("Test video download fine")
            return True
        except RuntimeError:
            self.log.error("Can't download test video. Probably, your device ID is invalid.")
            return False

    async def fetch_items(self, post: ParsedTikTokPost) -> None:
        def validate(resp: httpx.Response) -> bool:
            if resp.status_code != 200:
                return False
            if resp.headers.get("Content-Type", "") == "text/html":
                return False
            if len(resp.content) < 512:
                return False
            return True

        web_post = await self.fetch_post_metadata(post.id_)
        data_dir = "tmp"
        for item in web_post.media:
            self.log.debug(f"downloading {item.type_} {item.filename}")
            if not os.path.exists(f"{data_dir}/{item.filename}"):
                if isinstance(item.download_url, str):
                    download_url = item.download_url
                elif isinstance(item.download_url, list):
                    download_url = item.download_url[0]
                else:
                    self.log.error(f"Raw post data: {web_post}")
                    raise RuntimeError(f"Unsupported download url type: {type(item.download_url)}")
                resp = await self.request("GET", download_url)
                if not validate(resp):
                    raise RuntimeError(f"Failed to download {item.type_} {item.post_id}")
                with open(f"{data_dir}/{item.filename}", "wb") as outf:
                    outf.write(resp.content)
            if item.type_ == "music":
                if item.filename.endswith(".m4a"):
                    music_file = MP4(f"{data_dir}/{item.filename}")
                    assert music_file.tags
                    music_file.tags["\xa9nam"] = item.media_name
                    assert isinstance(item.media_cover_url, str)
                    cover = httpx.get(item.media_cover_url).content
                    music_file.tags["covr"] = [MP4Cover(data=cover)]
                    music_file.save()
                else:
                    music_file = MP3(f"{data_dir}/{item.filename}")
                    assert music_file.tags
                    music_file.tags["TIT2"] = TIT2(encoding=3, text=item.media_name)
                    assert isinstance(item.media_cover_url, str)
                    cover = httpx.get(item.media_cover_url).content
                    music_file.tags["APIC"] = APIC(encoding=3, mime="image/jpg", type=3, data=cover)
                    music_file.save()

    def delete_items(self, post: ParsedTikTokPost) -> None:
        data_dir = "tmp"
        for item in post.media:
            if os.path.exists(f"{data_dir}/{item.filename}"):
                os.remove(f"{data_dir}/{item.filename}")

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
                    new_item["media"] = [
                        DownloadTask(
                            post_id=new_item["id_"], type_="photo", number=num, download_url=u["imageURL"]["urlList"]
                        )
                        for num, u in enumerate(item["imagePost"]["images"])
                    ]
                    if "playUrl" in item["music"]:
                        new_item["media"].append(
                            DownloadTask(
                                post_id=new_item["id_"],
                                type_="music",
                                number=len(new_item["media"]),
                                download_url=item["music"]["playUrl"],
                                media_name=item["music"]["title"],
                                media_cover_url=item["music"]["coverLarge"],
                                media_format="mp3" if "audio_mpeg" in item["music"]["playUrl"] else "m4a",
                            )
                        )
                    else:
                        self.log.warning(f"Post {new_item['id_']}: music is unavailable")
                if new_item["type_"] == "video":
                    new_item["media"] = [
                        DownloadTask(
                            post_id=new_item["id_"], type_="video", number=0, download_url=item["video"]["playAddr"]
                        )
                    ]
                post = ParsedTikTokPost(**new_item)
                items.append(post)
            except:
                self.log.error("Failed to parse TikTok post. Raw data below, bailing out.")
                self.log.error(json.dumps(item))
                raise
        return items

    async def fetch_liked(self) -> AsyncGenerator[list[dict[str, Any]], None]:
        # From newest to oldest
        cntr = 0
        cur = 0
        has_more = True
        while has_more:
            params = {**self.browser_params, "secUid": self.sec_uid, "count": 20, "cursor": cur}
            resp = await self.request("GET", f"https://www.tiktok.com/api/favorite/item_list/?{urlencode(params)}")
            resp.raise_for_status()
            data = resp.json()
            cur = data["cursor"]
            has_more = data["hasMore"]
            cntr += len(data["itemList"])
            self.log.debug(f"fetched {len(data['itemList'])} liked posts ({cntr} total), is there more - {has_more}")
            yield data["itemList"]
            await asyncio.sleep(5)

    async def fetch_favorite(self) -> AsyncGenerator[list[dict[str, Any]], None]:
        # From newest to oldest
        cntr = 0
        cur = 0
        has_more = True
        while has_more:
            params = {**self.browser_params, "secUid": self.sec_uid, "count": 20, "cursor": cur}
            resp = await self.request("GET", f"https://www.tiktok.com/api/user/collect/item_list/?{urlencode(params)}")
            resp.raise_for_status()
            data = resp.json()
            cur = data["cursor"]
            has_more = data["hasMore"]
            cntr += len(data["itemList"])
            self.log.debug(
                f"fetched {len(data['itemList'])} favorited posts ({cntr} total), is there more - {has_more}"
            )
            yield data["itemList"]
            await asyncio.sleep(5)

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
        async for block in self.fetch_liked():
            parsed_items = await self.parse_items(block)
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
        async for block in self.fetch_favorite():
            parsed_items = await self.parse_items(block)
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
