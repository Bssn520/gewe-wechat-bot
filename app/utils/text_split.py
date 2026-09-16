"""长回复拆条：把一次生成的结果切成多条微信文本消息。

为什么必须有这个：一次生成可能超过微信单条文本长度上限，
直接发会被截断或静默丢弃。

边界：本模块是**纯函数**，不碰数据库、不认识 inbox/outbox，只管把字符串切成
若干段。放在 `app/utils/`（而非生成接缝 `generation.py`）的理由：微信长度是
**渠道**约束，而生成接缝的契约是「只吃数据、只吐文本、返回 str」——把渠道
约束塞进接缝会让「换生成框架」与「换渠道」纠缠。

## 为什么按字节而不是字数

微信家族有文档的长度限制都是**字节**口径（公众号文本 2048、企业微信文本
2048）。汉字在 UTF-8 下占 3 字节，所以「1000 字」= 3000 字节——按字数控制
会超限 50%。个人微信没有公开文档，但同属一套底层协议，按字节对齐更安全。

## 为什么按语义边界切

按字节硬切会把一行字或一个词劈成两半。切分优先级：
空行（段落）→ 换行 → 句末标点 → 逗号类 → 硬切。
"""

from __future__ import annotations

import structlog

logger = structlog.get_logger(__name__)

# 单条微信文本的字节上限。留出余量：微信家族有文档的限制是 2048 字节，
# 这里取 2000。**取值需真机实测校准**：GeWe 官方 postText 文档对 content
# 未给任何长度约束，超限行为（报错还是静默丢）只能实测确认。
WECHAT_TEXT_MAX_BYTES: int = 2000

# 拆出的段数超过这个值只告警、不截断内容。段数异常多通常说明提示词该收短了，
# 而不是该丢内容——静默丢内容比多发几条消息更糟。
WECHAT_SPLIT_WARN_PARTS: int = 5

# 句末标点：优先在此断开，保证每段语义完整
_SENTENCE_ENDS = "。！？；!?;"
# 次级断点：句子过长时退而求其次
_CLAUSE_ENDS = "，、：,.:）)】」』"


def _size(text: str) -> int:
    return len(text.encode("utf-8"))


def _cut_by_breakpoints(text: str, limit: int) -> tuple[str, str]:
    """把 text 切成 (放得下的前缀, 剩余)。按优先级寻找最后一个合适断点。

    全部断点都找不到时按字节硬切（此时用 UTF-8 安全的方式，不劈开多字节字符）。
    """
    # 优先句末标点，其次逗号类，最后换行/空格
    for chars in (_SENTENCE_ENDS, _CLAUSE_ENDS, "\n "):
        best = -1
        for i, ch in enumerate(text):
            if ch in chars:
                candidate = text[: i + 1]
                if _size(candidate) <= limit:
                    best = i + 1
                else:
                    break
        if best > 0:
            return text[:best], text[best:]

    # 没有可用断点：按字节硬切，且保证不截断多字节字符
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if _size(text[:mid]) <= limit:
            lo = mid
        else:
            hi = mid - 1
    if lo == 0:
        # 单个字符就超限（limit 过小），强制取一个字符避免死循环
        return text[:1], text[1:]
    return text[:lo], text[lo:]


def _split_paragraph(para: str, limit: int) -> list[str]:
    """切分单个超限段落：先按换行，再按断点，都不行则硬切。"""
    out: list[str] = []
    lines = para.split("\n")
    buf = ""
    for line in lines:
        candidate = f"{buf}\n{line}" if buf else line
        if _size(candidate) <= limit:
            buf = candidate
            continue
        if buf:
            out.append(buf)
            buf = ""
        # 单行自身就超限：继续按断点拆
        rest = line
        while _size(rest) > limit:
            head, rest = _cut_by_breakpoints(rest, limit)
            out.append(head)
        buf = rest
    if buf:
        out.append(buf)
    return out


def split_for_wechat(text: str, *, max_bytes: int | None = None) -> list[str]:
    """把一条长文本切成若干条可独立发送的微信消息。

    段落（空行分隔）优先保持完整；段落内超限才继续按换行/标点/字节切。
    返回的段已去掉首尾空白，空段被丢弃；全空输入返回空列表。
    """
    limit = max_bytes or WECHAT_TEXT_MAX_BYTES
    stripped = (text or "").strip()
    if not stripped:
        return []

    # 已在限内：原样返回一段（绝大多数日常回复走这条路径，零改动）
    if _size(stripped) <= limit:
        return [stripped]

    # 空行分段（保留段落内部的单换行）
    paragraphs = [p for p in stripped.split("\n\n") if p.strip()]

    parts: list[str] = []
    buf = ""
    for para in paragraphs:
        candidate = f"{buf}\n\n{para}" if buf else para
        if _size(candidate) <= limit:
            buf = candidate
            continue
        if buf:
            parts.append(buf)
            buf = ""
        if _size(para) <= limit:
            buf = para
        else:
            chunks = _split_paragraph(para, limit)
            parts.extend(chunks[:-1])
            buf = chunks[-1] if chunks else ""
    if buf:
        parts.append(buf)

    parts = [p.strip() for p in parts if p.strip()]
    if len(parts) > WECHAT_SPLIT_WARN_PARTS:
        logger.warning(
            "split.too_many_parts",
            parts=len(parts),
            bytes=_size(stripped),
            warn_at=WECHAT_SPLIT_WARN_PARTS,
        )
    return parts


__all__ = [
    "WECHAT_SPLIT_WARN_PARTS",
    "WECHAT_TEXT_MAX_BYTES",
    "split_for_wechat",
]
