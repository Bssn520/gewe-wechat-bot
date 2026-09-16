"""队列状态集合。条件更新传集合，不散落字符串比较。"""

from __future__ import annotations

from typing import Final

INBOX_PENDING: Final = "pending"
INBOX_PROCESSING: Final = "processing"
INBOX_DONE: Final = "done"
INBOX_FAILED: Final = "failed"

INBOX_TERMINAL: Final[frozenset[str]] = frozenset({INBOX_DONE, INBOX_FAILED})
INBOX_NON_TERMINAL: Final[frozenset[str]] = frozenset({INBOX_PENDING, INBOX_PROCESSING})

OUTBOX_PENDING: Final = "pending"
OUTBOX_SENDING: Final = "sending"
OUTBOX_SENT: Final = "sent"
OUTBOX_FAILED: Final = "failed"

OUTBOX_TERMINAL: Final[frozenset[str]] = frozenset({OUTBOX_SENT, OUTBOX_FAILED})
OUTBOX_NON_TERMINAL: Final[frozenset[str]] = frozenset({OUTBOX_PENDING, OUTBOX_SENDING})

ROLE_USER: Final = "user"
ROLE_ASSISTANT: Final = "assistant"

# 媒体静默窗（毫秒）：会话的媒体全部解析完成后再等这么久才允许认领，
# 把「解析期间用户补发的问题」收进同一批，避免图文被拆成两轮各自回复。
#
# 为什么放在 core 而不是 app/services/media.py（那里是媒体常量的家）：
# `crud/inbox.py` 的 peek/claim 两处窗口都要用它，而 crud 不能 import services
# （分层约束）。放 core 同层，两侧都能引用。
#
# 下载期间 defer 一直按着，本身就是免费的等待窗；这里覆盖「解析完成之后
# 才打进来的字」。开到 LLM 量级会把「已看到回复之后」发的消息也吞进上一轮。
MEDIA_QUIET_MS: Final = 10000
