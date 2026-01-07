import asyncio
import logging
import os

from dotenv import load_dotenv

from tikdog.storage import Storage
from tikdog.telegram import Telegram
from tikdog.tiktok import TikTok

logging.basicConfig(format="{asctime} | {levelname:<8} | {name} | {message}", style="{", level=logging.INFO)

load_dotenv()

tt_cookie = os.environ.get("TT_COOKIE")
tt_device_id = os.environ.get("TT_DEVICE_ID")
tt_username = os.environ.get("TT_USERNAME")

tg_app_id = os.environ.get("TG_APP_ID")
tg_app_hash = os.environ.get("TG_APP_HASH")
tg_bot_token = os.environ.get("TG_BOT_TOKEN")
tg_channel_id = os.environ.get("TG_CHANNEL_ID")

log = logging.getLogger("tikdog.dog")

SLEEP_TIME_SECS = 1800


async def dog() -> None:
    # Yeah, type checker. Get it.
    if (
        not tt_cookie
        or not tt_device_id
        or not tt_username
        or not tg_app_id
        or not tg_app_hash
        or not tg_bot_token
        or not tg_channel_id
    ):
        raise RuntimeError("Not all required parameters are set!")
    storage = Storage()
    tt = TikTok(tt_username, tt_cookie, tt_device_id, storage)
    tg = Telegram(int(tg_app_id), tg_app_hash, tg_bot_token, int(tg_channel_id), storage)
    await tt.connect()
    await tg.connect()

    if not await tt.check_video_download():
        return

    # First - fetch full TikTok data. It is used as a base for combined storage.
    # This can take a while.
    await tt.update_data()
    # Afterwards follow with Telegram update to prevent double posting.
    # Better to use new -> old scan as order doesn't matter for it, and
    # automatically fetch last post ID.
    await tg.update_data(max_count=0, reverse=True, determine_last_id=True)

    # Main loop. Update TikTok data (what will fetch only new posts), then post
    # them to Telegram. As corresponding objects will be updated, no need to
    # update Telegram data.
    while True:
        try:
            for post in storage.unposted()[::-1]:
                # Should be reversed, as it's stored in new -> old order, to prevent
                # breaking the "as in TikTok" order
                await tt.fetch_items(post.media)
                await tg.post(post)
                tt.delete_items(post.media)

            log.info(f"Done, sleeping for {SLEEP_TIME_SECS}")

            await asyncio.sleep(SLEEP_TIME_SECS)
            await tt.update_data()
        except Exception as e:
            log.warning("failed to do main loop. sleeping, will retry", exc_info=e)
            await asyncio.sleep(SLEEP_TIME_SECS)


def main() -> None:
    asyncio.run(dog())


if __name__ == "__main__":
    main()
