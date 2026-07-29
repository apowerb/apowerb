import asyncio
from th2agent.helpers.database import Base, sessionmanager
import th2agent.models  # loads User, Transaction, CreditPurchase

async def create_all_tables():
    async with sessionmanager.connect() as conn:
        await conn.run_sync(Base.metadata.create_all)
        print("✅ All tables created successfully!")

asyncio.run(create_all_tables())