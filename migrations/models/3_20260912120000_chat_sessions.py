from tortoise import BaseDBAsyncClient

RUN_IN_TRANSACTION = True


async def upgrade(db: BaseDBAsyncClient) -> str:
    return """
        CREATE TABLE IF NOT EXISTS "chat_sessions" (
    "id" BIGSERIAL NOT NULL PRIMARY KEY,
    "self_wxid" VARCHAR(64) NOT NULL,
    "friend_wxid" VARCHAR(64) NOT NULL,
    "messages" JSONB NOT NULL DEFAULT '[]'::jsonb,
    "created_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updated_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT "uid_chat_sessions_self_friend" UNIQUE ("self_wxid", "friend_wxid")
);
        DROP TABLE IF EXISTS "chat_messages";
        """


async def downgrade(db: BaseDBAsyncClient) -> str:
    return """
        DROP TABLE IF EXISTS "chat_sessions";
        CREATE TABLE IF NOT EXISTS "chat_messages" (
    "id" BIGSERIAL NOT NULL PRIMARY KEY,
    "self_wxid" VARCHAR(64) NOT NULL,
    "friend_wxid" VARCHAR(64) NOT NULL,
    "role" VARCHAR(16) NOT NULL,
    "content" TEXT NOT NULL,
    "delivered" BOOL NOT NULL,
    "created_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
        CREATE INDEX IF NOT EXISTS "idx_chat_self_friend_id"
            ON "chat_messages" ("self_wxid", "friend_wxid", "id");
        CREATE INDEX IF NOT EXISTS "idx_chat_self_friend_delivered_id"
            ON "chat_messages" ("self_wxid", "friend_wxid", "delivered", "id");
        """
