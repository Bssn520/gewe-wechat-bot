from tortoise import BaseDBAsyncClient

RUN_IN_TRANSACTION = True


async def upgrade(db: BaseDBAsyncClient) -> str:
    return """
        ALTER TABLE "inbox" ADD COLUMN IF NOT EXISTS "claimed_at" TIMESTAMPTZ;
        """


async def downgrade(db: BaseDBAsyncClient) -> str:
    return """
        ALTER TABLE "inbox" DROP COLUMN IF EXISTS "claimed_at";
        """
