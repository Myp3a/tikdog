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
    def __init__(
        self,
        username: str,
        browser_cookie: str,
        device_id: str,
        install_id: str,
        mobile_url: str,
        sid_tt: str,
        storage: Storage,
    ):
        self.log = logging.getLogger("tikdog.tiktok")
        self.storage = storage
        self.mobile_url = mobile_url
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
        self.mobile_cookies = {
            "sid_tt": sid_tt,
        }
        self.mobile_headers = {
            "user-agent": "com.zhiliaoapp.musically/2023501030 (Linux; U; Android 13; en_US; Pixel 7; Build/TD1A.220804.031; Cronet/58.0.2991.0)"
        }
        self.mobile_params = {
            "device_id": device_id,
            "iid": install_id,
        }
        self.sec_uid = ""
        self.fetch_block_size = 20
        self.posts: dict[int, ParsedTikTokPost] = {}
        self.request_delay_sec = 5
        self.retry_count = 3

    async def web_request(
        self, method: Literal["GET", "POST"], url: str, headers: dict[str, str] | None = None
    ) -> httpx.Response:
        if headers is None:
            headers = {}
        async with httpx.AsyncClient(follow_redirects=True) as cli:
            req_headers = {**self.browser_headers, **headers}
            for _ in range(self.retry_count):
                try:
                    resp = await cli.request(method, url, headers=req_headers)
                except (httpx.ReadTimeout, httpx.ConnectTimeout):
                    await asyncio.sleep(self.request_delay_sec)
                else:
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

                        existing_cookies = req_headers.get("Cookie", "")
                        retry_cookies = f"{existing_cookies}; {waf_cookie}" if existing_cookies else waf_cookie
                        retry_headers = {**req_headers, "Cookie": retry_cookies}

                        try:
                            resp2 = await cli.request(method, url, headers=retry_headers)
                        except (httpx.ReadTimeout, httpx.ConnectTimeout):
                            await asyncio.sleep(self.request_delay_sec)
                            continue
                        else:
                            return resp2
                    else:
                        return resp
            raise RuntimeError(f"Request failed after {self.retry_count} retries")

    async def mobile_request(
        self, path: str, params: dict[str, Any] | None = None, headers: dict[str, str] | None = None
    ) -> httpx.Response:
        if params is None:
            params = self.mobile_params
        else:
            params = self.mobile_params | params
        if headers is None:
            headers = self.mobile_headers
        else:
            headers = self.mobile_headers | headers
        async with httpx.AsyncClient(follow_redirects=True) as cli:
            for _ in range(self.retry_count):
                try:
                    resp = await cli.request(
                        "GET",
                        f"https://{self.mobile_url}{path}",
                        params=params,
                        headers=headers,
                        cookies=self.mobile_cookies,
                    )
                except (httpx.ReadTimeout, httpx.ConnectTimeout):
                    await asyncio.sleep(self.request_delay_sec)
                else:
                    return resp
            raise RuntimeError(f"Request failed after {self.retry_count} retries")

    async def connect_web(self) -> None:
        user_resp = await self.web_request("GET", f"https://www.tiktok.com/@{self.username}")
        user_resp.raise_for_status()
        m = re.search(r'"secUid":"([^"]+)"', user_resp.text)
        if not m:
            raise RuntimeError("Couldn't fetch secUid for user!")
        self.sec_uid = m.group(1)
        self.log.info(f"Connected to TikTok account {self.username}")

    async def connect_mobile(self) -> None:
        user_resp = await self.mobile_request(
            "/aweme/v1/user/profile/self/",
            {"aid": 1233, "app_name": "musical_ly", "version_code": 350103, "version_name": "35.1.3"},
        )
        user_resp.raise_for_status()
        js = user_resp.json()
        uid = js.get("user", {}).get("sec_uid")
        if not uid:
            self.log.warning("raw user API response:")
            self.log.warning(js)
            raise RuntimeError("Failed to fetch secUid!")
        self.sec_uid = uid
        self.log.info(f"Connected to TikTok account {self.username}")

    async def connect(self) -> None:
        await self.connect_mobile()

    async def fetch_post_metadata_web(self, video_id: int) -> ParsedTikTokPost:
        # Shouldn't be used.
        # Sometimes blocked server-side, not returning video data. Not ratelimited - plain retry help, but not always.
        post_resp = await self.web_request("GET", f"https://www.tiktok.com/@user/video/{video_id}")
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
        post = await self.parse_item_web(data)
        return post

    async def fetch_post_metadata_mobile(self, video_id: int) -> ParsedTikTokPost:
        # TODO: might need to rotate mobile URLs
        post_resp = await self.mobile_request("/aweme/v1/feed/", {"aweme_id": video_id}, headers={"x-argus": "why."})
        try:
            post_resp.raise_for_status()
        except Exception as e:
            raise RuntimeError("Mobile API response != 200") from e
        js = post_resp.json()
        target_post = next((v for v in js["aweme_list"] if int(v["aweme_id"]) == video_id), None)
        if not target_post:
            self.log.error(f"Failed to fetch post {video_id} data. Raw data below.")
            self.log.error(json.dumps(js))
            raise RuntimeError("Failed to fetch post data!")
        post = await self.parse_item_mobile(target_post)
        return post

    async def check_video_download(self) -> bool:
        FISCH_ID = 7455398333754952967
        self.log.info("Trying to download test video to check device ID correctness")
        try:
            vid = await self.fetch_post_metadata_mobile(FISCH_ID)
            await self.download_post(vid)
            self.log.info("Test video download fine")
            self.delete_items(vid)
            return True
        except RuntimeError:
            self.log.error("Can't download test video. Probably, your device ID is invalid.")
            return False

    async def check_copyrighted_video_download(self) -> bool:
        KITTY_ID = 7651360277778255111
        self.log.info("Trying to download test copyrighted video to check mobile path download")
        try:
            vid = await self.fetch_post_metadata_mobile(KITTY_ID)
            await self.download_post(vid)
            self.log.info("Test copyrighted video download fine")
            self.delete_items(vid)
            return True
        except RuntimeError:
            self.log.warning(
                "Can't download copyrighted video. This could lead to missing videos (pretty rare).", exc_info=True
            )
            return False

    async def check_photo_post(self) -> bool:
        PHOTO_ID = 7667189357815532808
        self.log.info("Trying to download photo post")
        try:
            post = await self.fetch_post_metadata_mobile(PHOTO_ID)
            await self.download_post(post)
            self.log.info("Test photo post download fine")
            self.delete_items(post)
            return True
        except RuntimeError:
            self.log.warning("Can't download photo post.", exc_info=True)
            return False

    async def download_post(self, post: ParsedTikTokPost) -> None:
        def validate(resp: httpx.Response) -> bool:
            if resp.status_code != 200:
                return False
            if resp.headers.get("Content-Type", "") == "text/html":
                return False
            if len(resp.content) < 512:
                return False
            return True

        # Metadata fetch is costly - defer until actual download
        post = await self.fetch_post_metadata_mobile(post.id_)
        data_dir = "tmp"
        for item in post.media:
            self.log.debug(f"downloading {item.type_} {item.filename}")
            if not os.path.exists(f"{data_dir}/{item.filename}"):
                if isinstance(item.download_url, str):
                    download_url = item.download_url
                elif isinstance(item.download_url, list):
                    download_url = item.download_url[0]
                else:
                    self.log.error(f"Raw post data: {post}")
                    raise RuntimeError(f"Unsupported download url type: {type(item.download_url)}")
                resp = await self.web_request("GET", download_url)
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

    async def parse_item_web(self, item: dict[str, Any]) -> ParsedTikTokPost:
        # Web API has no content URLs in it, so it requires a refetch via mobile
        try:
            new_item = {
                "id_": int(item["id"]),
                "type_": "photo" if "imagePost" in item else "video",
            }
            new_item["web_url"] = f"https://www.tiktok.com/@uSeRnAmE/{new_item['type_']}/{new_item['id_']}"
            new_item["media"] = []
            post = ParsedTikTokPost(**new_item)
        except:
            self.log.error("Failed to parse TikTok post. Raw data below, bailing out.")
            self.log.error(json.dumps(item))
            raise
        return post

    async def parse_item_mobile(self, item: dict[str, Any]) -> ParsedTikTokPost:
        try:
            new_item = {
                "id_": int(item["aweme_id"]),
                "type_": "photo" if "image_post_info" in item else "video",
            }
            new_item["web_url"] = f"https://www.tiktok.com/@uSeRnAmE/{new_item['type_']}/{new_item['id_']}"
            if new_item["type_"] == "photo":
                new_item["media"] = [
                    DownloadTask(
                        post_id=new_item["id_"],
                        type_="photo",
                        number=num,
                        download_url=img["display_image"]["url_list"][0],
                    )
                    for num, img in enumerate(item["image_post_info"]["images"])
                ]
                if "play_url" in item["music"]:
                    new_item["media"].append(
                        DownloadTask(
                            post_id=new_item["id_"],
                            type_="music",
                            number=len(new_item["media"]),
                            download_url=item["music"]["play_url"]["url_list"][0],
                            media_name=item["music"]["title"],
                            media_cover_url=item["music"]["cover_large"]["url_list"][0],
                            media_format="mp3" if "mp3" in item["music"]["play_url"]["url_list"][0] else "m4a",
                        )
                    )
                else:
                    self.log.warning(f"Post {new_item['id_']}: music is unavailable")
            if new_item["type_"] == "video":
                new_item["media"] = [
                    DownloadTask(
                        post_id=new_item["id_"],
                        type_="video",
                        number=0,
                        download_url=item["video"]["play_addr"]["url_list"][0],
                    )
                ]
            post = ParsedTikTokPost(**new_item)
        except:
            self.log.error("Failed to parse mobile TikTok post. Raw data below, bailing out.")
            self.log.error(json.dumps(item))
            raise
        return post

    async def fetch_liked_web(self) -> AsyncGenerator[list[dict[str, Any]], None]:
        # From newest to oldest
        cntr = 0
        cur = 0
        has_more = True
        while has_more:
            params = {**self.browser_params, "secUid": self.sec_uid, "count": self.fetch_block_size, "cursor": cur}
            resp = await self.web_request("GET", f"https://www.tiktok.com/api/favorite/item_list/?{urlencode(params)}")
            resp.raise_for_status()
            data = resp.json()
            cur = data["cursor"]
            has_more = data["hasMore"]
            cntr += len(data["itemList"])
            self.log.debug(f"fetched {len(data['itemList'])} liked posts ({cntr} total), is there more - {has_more}")
            yield data["itemList"]
            await asyncio.sleep(self.request_delay_sec)

    async def fetch_favorite_web(self) -> AsyncGenerator[list[dict[str, Any]], None]:
        # From newest to oldest
        cntr = 0
        cur = 0
        has_more = True
        while has_more:
            params = {**self.browser_params, "secUid": self.sec_uid, "count": self.fetch_block_size, "cursor": cur}
            resp = await self.web_request(
                "GET", f"https://www.tiktok.com/api/user/collect/item_list/?{urlencode(params)}"
            )
            resp.raise_for_status()
            data = resp.json()
            cur = data["cursor"]
            has_more = data["hasMore"]
            cntr += len(data["itemList"])
            self.log.debug(
                f"fetched {len(data['itemList'])} favorited posts ({cntr} total), is there more - {has_more}"
            )
            yield data["itemList"]
            await asyncio.sleep(self.request_delay_sec)

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
        async for block in self.fetch_liked_web():
            for raw_post in block:
                item = await self.parse_item_web(raw_post)
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
        async for block in self.fetch_favorite_web():
            for raw_post in block:
                item = await self.parse_item_web(raw_post)
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
