import asyncio
import logging

from .service import Service
from .settings import load_settings


def main():
    s = load_settings()
    logging.basicConfig(level=s.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    asyncio.run(Service(s).run())


if __name__ == "__main__":
    main()
