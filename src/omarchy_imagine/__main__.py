"""``python -m omarchy_imagine`` serves the API on 127.0.0.1:8010."""

from __future__ import annotations

import uvicorn

from omarchy_imagine.config import API_HOST, API_PORT


def main() -> None:
    uvicorn.run(
        "omarchy_imagine.app:app",
        host=API_HOST,
        port=API_PORT,
        reload=False,
    )


if __name__ == "__main__":
    main()
