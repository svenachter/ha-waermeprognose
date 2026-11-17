import logging
import os

import uvicorn

from .api import create_app
from .settings import load_settings


def main():
    settings = load_settings()
    logging.basicConfig(level=settings.log_level.upper(),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    app = create_app(settings)
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8099")),
                log_level=settings.log_level, access_log=False)


if __name__ == "__main__":
    main()
