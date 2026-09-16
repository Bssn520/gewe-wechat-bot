from tortoise import BaseDBAsyncClient

RUN_IN_TRANSACTION = True


async def upgrade(db: BaseDBAsyncClient) -> str:
    return """
        CREATE TABLE IF NOT EXISTS "aerich" (
    "id" SERIAL NOT NULL PRIMARY KEY,
    "version" VARCHAR(255) NOT NULL,
    "app" VARCHAR(100) NOT NULL,
    "content" JSONB NOT NULL
);"""


async def downgrade(db: BaseDBAsyncClient) -> str:
    return """
        """


MODELS_STATE = (
    "eJzdlNFv2jAQxv8VlKdW2iaa0RX1DVCrbtpAalE1aaosJzmChWOn9mVr1fG/1+cADqFF5W"
    "XS+ka+++z77ifOT1GhM5D20wCMSOfReecpUrwA96NV+dCJeFkGnQTkifRWHjyJRcNTdOqM"
    "SwtOysCmRpQotHKqqqQkUafOKFQepEqJ+woY6hxwDsYVft05WagMHsCuP8sFmwmQ2VZUkV"
    "FvrzN8LL32VeGlN1K3hKVaVoUK5vIR51pt3EIhqTkoMByBrkdTUXxKt5pzPVGdNFjqiI0z"
    "Gcx4JbExbsKCFjE2nkzZzcWUsegAQKlWBNdFtX76nCJ8jE96Z73+5y+9vrP4mBvlbFm3Dm"
    "Dqgx7PeBotfZ0jrx2ecYD6G4ylSDtkR3NuXkbbONLi64K3+a5p7gO8FgLh8K/6F4gL/sAk"
    "qBxpNeLT0z1AbwfXo6vB9ZFzHVNL7dag3o7xqhTXNaIeKNNSHUB4ZX+HdE+63TfQda5X6f"
    "raNl3XEaFe7W3C324m45cJN460KGcixc7fjhR25634D2jvgUsw6ObC2nvZZHr0Y/CzjXv0"
    "fTL0cLTF3Phb/AVDh54e6Nmi8ZqQkPB08YebjO1UdKxf8+6WirhoK1zx3IOkiZfLZxu/KJ"
    "A="
)
