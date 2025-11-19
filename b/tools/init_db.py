import asyncio
from db import engine, Base

async def init_db():
    async with engine.begin() as conn:
        print("🔧 Создаю таблицы...")
        await conn.run_sync(Base.metadata.create_all)
        print("✅ Таблицы созданы")

if __name__ == "__main__":
    asyncio.run(init_db())
