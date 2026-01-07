from dataclasses import dataclass
from typing import Literal


@dataclass
class DownloadTask:
    post_id: int
    type_: Literal["photo", "video", "music"]
    number: int
    download_url: str
    media_name: str | None = None
    media_cover_url: str | None = None

    @property
    def filename(self) -> str:
        match self.type_:
            case "photo":
                ext = "jpg"
            case "video":
                ext = "mp4"
            case "music":
                ext = "m4a"
        return f"{self.post_id}_{self.number}_{self.type_}.{ext}"


@dataclass
class ParsedTikTokPost:
    id_: int
    type_: Literal["photo", "video"]
    web_url: str
    media: list[DownloadTask]
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
    media: list[DownloadTask]
    liked: bool = False
    favorited: bool = False
    description: str = ""
    _raw_tt: ParsedTikTokPost | None = None
    _raw_tg: ParsedTelegramPost | None = None
