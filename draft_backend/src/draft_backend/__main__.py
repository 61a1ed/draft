import os

import uvicorn


def main():
    uvicorn.run(
        "draft_backend.app:create_app",
        factory=True,
        host=os.getenv("HOST", "127.0.0.1"),
        port=int(os.getenv("PORT", "8000")),
        # Select explicitly: current Uvicorn ignores Windows event-loop policies.
        # Psycopg async connections require a selector, including on Windows.
        loop="asyncio:SelectorEventLoop",
        http="h11",
        proxy_headers=False,
    )


if __name__ == "__main__":
    main()
