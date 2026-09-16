from tortoise import BaseDBAsyncClient

RUN_IN_TRANSACTION = True


async def upgrade(db: BaseDBAsyncClient) -> str:
    return """
        CREATE TABLE IF NOT EXISTS "inbox" (
    "id" BIGSERIAL NOT NULL PRIMARY KEY,
    "app_id" VARCHAR(64) NOT NULL,
    "friend_wxid" VARCHAR(64) NOT NULL,
    "new_msg_id" VARCHAR(64) NOT NULL,
    "content" TEXT NOT NULL,
    "status" VARCHAR(16) NOT NULL,
    "attempt" INT NOT NULL DEFAULT 0,
    "lease_gen" INT NOT NULL DEFAULT 0,
    "leased_until" TIMESTAMPTZ,
    "worker_id" VARCHAR(64),
    "error_code" VARCHAR(64),
    "error_message" VARCHAR(500),
    "created_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updated_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT "uid_inbox_app_id_new_msg" UNIQUE ("app_id", "new_msg_id")
);
CREATE INDEX IF NOT EXISTS "idx_inbox_status_id" ON "inbox" ("status", "id");
CREATE INDEX IF NOT EXISTS "idx_inbox_app_friend_status_id"
    ON "inbox" ("app_id", "friend_wxid", "status", "id");
CREATE INDEX IF NOT EXISTS "idx_inbox_status_leased" ON "inbox" ("status", "leased_until");

CREATE TABLE IF NOT EXISTS "outbox" (
    "id" BIGSERIAL NOT NULL PRIMARY KEY,
    "app_id" VARCHAR(64) NOT NULL,
    "friend_wxid" VARCHAR(64) NOT NULL,
    "content" TEXT NOT NULL,
    "source_inbox_ids" JSONB,
    "status" VARCHAR(16) NOT NULL,
    "attempt" INT NOT NULL DEFAULT 0,
    "lease_gen" INT NOT NULL DEFAULT 0,
    "leased_until" TIMESTAMPTZ,
    "error_code" VARCHAR(64),
    "error_message" VARCHAR(500),
    "external_id" VARCHAR(64),
    "next_retry_at" TIMESTAMPTZ,
    "sending_at" TIMESTAMPTZ,
    "sent_at" TIMESTAMPTZ,
    "created_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updated_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS "idx_outbox_app_status_id" ON "outbox" ("app_id", "status", "id");
CREATE INDEX IF NOT EXISTS "idx_outbox_status_leased" ON "outbox" ("status", "leased_until");
CREATE INDEX IF NOT EXISTS "idx_outbox_app_sent" ON "outbox" ("app_id", "sent_at");
CREATE INDEX IF NOT EXISTS "idx_outbox_next_retry" ON "outbox" ("next_retry_at");
CREATE UNIQUE INDEX IF NOT EXISTS "uniq_outbox_sending_per_account"
    ON "outbox" ("app_id") WHERE status = 'sending';

CREATE TABLE IF NOT EXISTS "chat_messages" (
    "id" BIGSERIAL NOT NULL PRIMARY KEY,
    "app_id" VARCHAR(64) NOT NULL,
    "friend_wxid" VARCHAR(64) NOT NULL,
    "role" VARCHAR(16) NOT NULL,
    "content" TEXT NOT NULL,
    "delivered" BOOL NOT NULL,
    "created_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS "idx_chat_app_friend_id"
    ON "chat_messages" ("app_id", "friend_wxid", "id");
CREATE INDEX IF NOT EXISTS "idx_chat_app_friend_delivered_id"
    ON "chat_messages" ("app_id", "friend_wxid", "delivered", "id");

CREATE TABLE IF NOT EXISTS "node_circuit" (
    "app_id" VARCHAR(64) NOT NULL PRIMARY KEY,
    "open_until" TIMESTAMPTZ NOT NULL,
    "reason" VARCHAR(64) NOT NULL,
    "updated_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
        """


async def downgrade(db: BaseDBAsyncClient) -> str:
    return """
        DROP TABLE IF EXISTS "node_circuit";
        DROP TABLE IF EXISTS "chat_messages";
        DROP TABLE IF EXISTS "outbox";
        DROP TABLE IF EXISTS "inbox";
        """
