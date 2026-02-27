import logging

logging.getLogger("asyncio").setLevel(logging.INFO)
logging.getLogger("httpx").setLevel(logging.INFO)
logging.getLogger("httpcore").setLevel(logging.INFO)
logging.getLogger("telethon").setLevel(logging.INFO)

logging.basicConfig(
    format="[{asctime}] [{levelname:<8}] {name}: {message}",
    style="{",
    level=logging.DEBUG,
    datefmt="%Y-%m-%d %H:%M:%S",
)
