import os
from pathlib import Path

import uvicorn


def main():
    runtime = os.getenv("RUNTIME_DIRECTORY")
    listener = (
        {"uds": str(Path(runtime) / "http.sock")}
        if runtime
        else {"host": os.getenv("HOST", "127.0.0.1"), "port": int(os.getenv("PORT", "8000"))}
    )
    uvicorn.run(
        "draft_backend.app:create_app",
        factory=True,
        workers=1,
        **listener,
        # Select explicitly: current Uvicorn ignores Windows event-loop policies.
        # Psycopg async connections require a selector, including on Windows.
        loop="asyncio:SelectorEventLoop",
        http="h11",
        # Production socket permissions restrict callers to the app and proxy.
        proxy_headers=bool(runtime),
        forwarded_allow_ips="*" if runtime else "127.0.0.1",
        timeout_graceful_shutdown=5,
    )


if __name__ == "__main__":
    main()
