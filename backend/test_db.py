import asyncio
import asyncpg
from asyncpg.exceptions import InvalidPasswordError

async def test_conn():
    urls = [
        "postgresql://postgres:postgres@127.0.0.1:55432/forgeops",
        "postgresql://postgres:@127.0.0.1:55432/forgeops",
        "postgresql://forgeops_app:change-me-locally@127.0.0.1:55432/forgeops",
        "postgresql://forgeops:change-me-locally@127.0.0.1:55432/forgeops",
        "postgresql://postgres:postgres@127.0.0.1:55432/postgres",
        "postgresql://postgres:@127.0.0.1:55432/postgres",
    ]
    for url in urls:
        print(f"Trying {url}")
        try:
            conn = await asyncpg.connect(url)
            print(f"SUCCESS: {url}")
            await conn.close()
            return
        except Exception as e:
            print(f"Failed: {type(e).__name__} - {e}")

asyncio.run(test_conn())
