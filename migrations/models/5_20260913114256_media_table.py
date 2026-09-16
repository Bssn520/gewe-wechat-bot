from tortoise import BaseDBAsyncClient

RUN_IN_TRANSACTION = True


async def upgrade(db: BaseDBAsyncClient) -> str:
    return """
        CREATE TABLE IF NOT EXISTS "media" (
    "id" BIGSERIAL NOT NULL PRIMARY KEY,
    "self_wxid" VARCHAR(64) NOT NULL,
    "friend_wxid" VARCHAR(64) NOT NULL,
    "media_type" VARCHAR(16) NOT NULL,
    "status" VARCHAR(16) NOT NULL,
    "source" JSONB NOT NULL,
    "asset_ref" TEXT,
    "text_repr" TEXT,
    "attempt" INT NOT NULL,
    "error_code" VARCHAR(64),
    "error_message" VARCHAR(500),
    "created_at" TIMESTAMPTZ NOT NULL,
    "updated_at" TIMESTAMPTZ NOT NULL,
    "inbox_id" BIGINT NOT NULL REFERENCES "inbox" ("id") ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS "idx_media_status_89af23" ON "media" ("status", "id");
CREATE INDEX IF NOT EXISTS "idx_media_self_wx_8e546f" ON "media" ("self_wxid", "status", "id");
CREATE INDEX IF NOT EXISTS "idx_media_self_wx_9fe6f8" ON "media" ("self_wxid", "friend_wxid", "status");
COMMENT ON COLUMN "media"."media_type" IS 'image|voice|video';
COMMENT ON COLUMN "media"."status" IS 'pending|ready|failed|skipped';
COMMENT ON COLUMN "media"."attempt" IS '取用次数，不是重试策略';
COMMENT ON COLUMN "media"."inbox_id" IS '来源入站消息';
COMMENT ON TABLE "media" IS '一条待取/已取的媒体，1:1 挂在其入站消息上。';
        ALTER TABLE "inbox" ALTER COLUMN "content" DROP NOT NULL;
        COMMENT ON COLUMN "inbox"."content" IS '文本；媒体消息为 NULL，源参数在 media.source';"""


async def downgrade(db: BaseDBAsyncClient) -> str:
    return """
        ALTER TABLE "inbox" ALTER COLUMN "content" SET NOT NULL;
        COMMENT ON COLUMN "inbox"."content" IS '文本';
        DROP TABLE IF EXISTS "media";"""


MODELS_STATE = (
    "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"
    "qYff8/H9wO3Q=="
)
