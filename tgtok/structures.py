from dataclasses import dataclass
from typing import Literal


@dataclass
class DownloadTask:
    id_: int
    type_: Literal["photo", "video"]
    download_urls: list[str]

    @property
    def basename(self) -> str:
        return f"{self.id_}_{self.type_}"

    @property
    def ext(self) -> str:
        return "mp4" if self.type_ == "video" else "jpg"

    @property
    def named_urls(self) -> list[dict[str, str]]:
        urls = []
        for ind, url in enumerate(self.download_urls):
            urls.append({"name": f"{self.id_}_{self.type_}_{ind}.{self.ext}", "url": url})
        return urls


@dataclass
class ParsedTikTokPost:
    id_: int
    type_: Literal["photo", "video"]
    web_url: str
    download_urls: list[str]
    liked: bool = False
    favorited: bool = False


@dataclass
class ParsedTelegramPost:
    id_: int
    tiktok_id: int
    web_url: str
    liked: bool
    favorited: bool
    description: str


@dataclass
class CombinedPost:
    telegram_id: int
    tiktok_id: int
    tiktok_url: str
    tiktok_type: Literal["photo", "video"]
    download_urls: list[str]
    liked: bool = False
    favorited: bool = False
    description: str = ""
    _raw_tt: ParsedTikTokPost | None = None
    _raw_tg: ParsedTelegramPost | None = None
