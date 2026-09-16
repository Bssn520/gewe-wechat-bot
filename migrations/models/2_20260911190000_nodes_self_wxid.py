from tortoise import BaseDBAsyncClient

RUN_IN_TRANSACTION = True


async def upgrade(db: BaseDBAsyncClient) -> str:
    return """
        CREATE TABLE IF NOT EXISTS "nodes" (
    "id" BIGSERIAL NOT NULL PRIMARY KEY,
    "self_wxid" VARCHAR(64) NOT NULL UNIQUE,
    "current_app_id" VARCHAR(64) NOT NULL UNIQUE,
    "circuit_open_until" TIMESTAMPTZ,
    "circuit_reason" VARCHAR(64),
    "created_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updated_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

        ALTER TABLE "inbox" ADD "self_wxid" VARCHAR(64);
        -- 占位：上线前无历史。app_id 当时是槽不是微信号，有真实数据时不能这么回填。
        UPDATE "inbox" SET "self_wxid" = "app_id" WHERE "self_wxid" IS NULL;
        ALTER TABLE "inbox" ALTER COLUMN "self_wxid" SET NOT NULL;
        ALTER TABLE "inbox" DROP CONSTRAINT IF EXISTS "uid_inbox_app_id_new_msg";
        ALTER TABLE "inbox" ADD CONSTRAINT "uid_inbox_self_wxid_new_msg"
            UNIQUE ("self_wxid", "new_msg_id");
        ALTER TABLE "inbox" ADD CONSTRAINT "uid_inbox_app_id_new_msg"
            UNIQUE ("app_id", "new_msg_id");
        DROP INDEX IF EXISTS "idx_inbox_app_friend_status_id";
        CREATE INDEX IF NOT EXISTS "idx_inbox_self_friend_status_id"
            ON "inbox" ("self_wxid", "friend_wxid", "status", "id");

        ALTER TABLE "outbox" ADD "self_wxid" VARCHAR(64);
        UPDATE "outbox" SET "self_wxid" = "app_id" WHERE "self_wxid" IS NULL;
        ALTER TABLE "outbox" ALTER COLUMN "self_wxid" SET NOT NULL;
        ALTER TABLE "outbox" ALTER COLUMN "app_id" DROP NOT NULL;
        DROP INDEX IF EXISTS "idx_outbox_app_status_id";
        DROP INDEX IF EXISTS "idx_outbox_app_sent";
        DROP INDEX IF EXISTS "uniq_outbox_sending_per_account";
        CREATE INDEX IF NOT EXISTS "idx_outbox_self_status_id"
            ON "outbox" ("self_wxid", "status", "id");
        CREATE INDEX IF NOT EXISTS "idx_outbox_self_sent" ON "outbox" ("self_wxid", "sent_at");
        CREATE UNIQUE INDEX IF NOT EXISTS "uniq_outbox_sending_per_account"
            ON "outbox" ("self_wxid") WHERE status = 'sending';

        ALTER TABLE "chat_messages" RENAME COLUMN "app_id" TO "self_wxid";
        DROP INDEX IF EXISTS "idx_chat_app_friend_id";
        DROP INDEX IF EXISTS "idx_chat_app_friend_delivered_id";
        CREATE INDEX IF NOT EXISTS "idx_chat_self_friend_id"
            ON "chat_messages" ("self_wxid", "friend_wxid", "id");
        CREATE INDEX IF NOT EXISTS "idx_chat_self_friend_delivered_id"
            ON "chat_messages" ("self_wxid", "friend_wxid", "delivered", "id");

        DROP TABLE IF EXISTS "node_circuit";
        """


async def downgrade(db: BaseDBAsyncClient) -> str:
    return """
        CREATE TABLE IF NOT EXISTS "node_circuit" (
    "app_id" VARCHAR(64) NOT NULL PRIMARY KEY,
    "open_until" TIMESTAMPTZ NOT NULL,
    "reason" VARCHAR(64) NOT NULL,
    "updated_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

        DROP INDEX IF EXISTS "idx_chat_self_friend_id";
        DROP INDEX IF EXISTS "idx_chat_self_friend_delivered_id";
        ALTER TABLE "chat_messages" RENAME COLUMN "self_wxid" TO "app_id";
        CREATE INDEX IF NOT EXISTS "idx_chat_app_friend_id"
            ON "chat_messages" ("app_id", "friend_wxid", "id");
        CREATE INDEX IF NOT EXISTS "idx_chat_app_friend_delivered_id"
            ON "chat_messages" ("app_id", "friend_wxid", "delivered", "id");

        DROP INDEX IF EXISTS "uniq_outbox_sending_per_account";
        DROP INDEX IF EXISTS "idx_outbox_self_status_id";
        DROP INDEX IF EXISTS "idx_outbox_self_sent";
        ALTER TABLE "outbox" ALTER COLUMN "app_id" SET NOT NULL;
        ALTER TABLE "outbox" DROP COLUMN "self_wxid";
        CREATE INDEX IF NOT EXISTS "idx_outbox_app_status_id"
            ON "outbox" ("app_id", "status", "id");
        CREATE INDEX IF NOT EXISTS "idx_outbox_app_sent" ON "outbox" ("app_id", "sent_at");
        CREATE UNIQUE INDEX IF NOT EXISTS "uniq_outbox_sending_per_account"
            ON "outbox" ("app_id") WHERE status = 'sending';

        DROP INDEX IF EXISTS "idx_inbox_self_friend_status_id";
        ALTER TABLE "inbox" DROP CONSTRAINT IF EXISTS "uid_inbox_self_wxid_new_msg";
        ALTER TABLE "inbox" DROP CONSTRAINT IF EXISTS "uid_inbox_app_id_new_msg";
        ALTER TABLE "inbox" DROP COLUMN "self_wxid";
        ALTER TABLE "inbox" ADD CONSTRAINT "uid_inbox_app_id_new_msg"
            UNIQUE ("app_id", "new_msg_id");
        CREATE INDEX IF NOT EXISTS "idx_inbox_app_friend_status_id"
            ON "inbox" ("app_id", "friend_wxid", "status", "id");

        DROP TABLE IF EXISTS "nodes";
        """
