from typing import Iterator

from tikdog.structures import ParsedTikTokPost, ParsedTelegramPost, CombinedPost


class Storage:
    def __init__(self):
        self.tg_synced = False
        self.posts: dict[int, CombinedPost] = {}

    def __contains__(self, id_) -> bool:
        return id_ in self.posts

    def __getitem__(self, id_) -> CombinedPost:
        return self.posts[id_]

    def __iter__(self) -> Iterator:
        return list(self.posts.values()).__iter__()

    def add(self, posts: ParsedTikTokPost | list[ParsedTikTokPost]) -> None:
        # No existence check as TikTok handler does that
        if isinstance(posts, ParsedTikTokPost):
            posts = [posts]
        # Keep new -> old order
        self.posts = {
            p.id_: CombinedPost(
                _raw_tt=p,
                _raw_tg=None,
                telegram_id=0,
                tiktok_id=p.id_,
                tiktok_url=p.web_url,
                tiktok_type=p.type_,
                media=p.media,
                liked=p.liked,
                favorited=p.favorited,
            )
            for p in posts
        } | self.posts

    def link_with_tg(self, posts: ParsedTelegramPost | list[ParsedTelegramPost]) -> None:
        if isinstance(posts, ParsedTelegramPost):
            posts = [posts]
        for tg_post in posts:
            comb_post = self.posts.get(tg_post.tiktok_id)
            if comb_post:
                comb_post._raw_tg = tg_post
                comb_post.telegram_id = tg_post.id_

    def unposted(self) -> list[CombinedPost]:
        # new -> old
        unp = [p for p in self.posts.values() if not p.telegram_id]
        return unp
