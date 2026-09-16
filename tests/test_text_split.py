"""拆条纯函数的边界测试。

拆条是「长报告能发出去」的唯一保障：一次生成结果超过微信单条上限时，
不拆就会被截断或静默丢弃。
"""

from __future__ import annotations

from app.utils.text_split import (
    WECHAT_SPLIT_WARN_PARTS,
    WECHAT_TEXT_MAX_BYTES,
    split_for_wechat,
)


def _b(text: str) -> int:
    """UTF-8 字节数。微信家族的文本上限都是字节口径，不是字数。"""
    return len(text.encode("utf-8"))


def _all_within(parts: list[str], limit: int) -> bool:
    return all(_b(p) <= limit for p in parts)


def _lossless(parts: list[str], original: str) -> bool:
    """忽略空白差异后内容无损（切分会规范化段间空白）。"""
    import re

    strip = lambda s: re.sub(r"\s+", "", s)  # noqa: E731
    return strip("".join(parts)) == strip(original)


# --- 短文本：零改动路径 ---


def test_empty_input_returns_no_parts() -> None:
    assert split_for_wechat("") == []
    assert split_for_wechat("   \n\t ") == []


def test_short_text_is_returned_unchanged() -> None:
    text = "在的，有什么事？"
    assert split_for_wechat(text) == [text]


def test_text_exactly_at_limit_is_single_part() -> None:
    # 666 汉字 = 1998 字节，加 "ab" = 2000，恰好等于上限
    text = "汉" * 666 + "ab"
    assert _b(text) == WECHAT_TEXT_MAX_BYTES
    assert split_for_wechat(text) == [text]


def test_text_one_byte_over_limit_splits() -> None:
    text = "汉" * 667  # 2001 字节
    parts = split_for_wechat(text)
    assert len(parts) == 2
    assert _all_within(parts, WECHAT_TEXT_MAX_BYTES)
    assert _lossless(parts, text)


# --- 核心保证：不超限 + 无损 ---


def test_all_parts_within_limit_and_content_lossless() -> None:
    report = "\n\n".join(
        [
            "# 舌象辨证分析",
            "## 证型\n脾虚痰湿证\n" + "脾虚痰湿证是指脾胃功能减弱，水液代谢失常。" * 10,
            "## 舌象详细解析\n- 舌质：淡红，胖大，边有齿痕。\n- 舌苔：白色，滑润，薄。" * 8,
            "## 状态分析\n### 根本原因\n" + "阳气不足，水湿内停。" * 25,
            "## 养生建议\n" + "建议早睡早起，避免熬夜。" * 40,
        ]
    )
    assert _b(report) > WECHAT_TEXT_MAX_BYTES  # 前提：确实超限

    parts = split_for_wechat(report)
    assert len(parts) > 1
    assert _all_within(parts, WECHAT_TEXT_MAX_BYTES)
    assert _lossless(parts, report)


def test_long_single_paragraph_splits_by_sentence() -> None:
    """没有空行的超长段落也要能拆，且不劈开句子。"""
    text = "这是一个很长的句子，需要被正确切分。" * 200
    parts = split_for_wechat(text)
    assert len(parts) > 1
    assert _all_within(parts, WECHAT_TEXT_MAX_BYTES)
    # 每段（除最后一段）都应以句末标点或逗号收尾，说明在语义边界断开
    for p in parts[:-1]:
        assert p.rstrip()[-1] in "。！？；，、：", f"未在语义边界断开: {p[-20:]!r}"


def test_multibyte_characters_never_cut_in_half() -> None:
    """按字节切必须保证多字节字符完整（不能出现乱码）。"""
    text = "汉" * 3000
    parts = split_for_wechat(text)
    assert _lossless(parts, text)
    for p in parts:
        assert "汉" * len(p) == p, "切分产生了残缺的多字节字符"


def test_custom_limit_is_respected() -> None:
    text = "汉" * 100
    parts = split_for_wechat(text, max_bytes=60)  # 20 个汉字
    assert _all_within(parts, 60)
    assert _lossless(parts, text)


def test_whitespace_only_segments_are_dropped() -> None:
    # 强制走拆分路径（否则短文本会原样返回单段，见上一个用例）
    filler = "汉" * 100
    text = f"  第一段  \n\n   \n\n  {filler}  \n\n  \n\n  第二段  "
    parts = split_for_wechat(text, max_bytes=120)
    assert len(parts) > 1
    assert all(p.strip() == p for p in parts), "段首尾空白应已去掉"
    assert all(p for p in parts), "纯空白段应被丢弃"
    assert "第一段" in parts[0]
    assert parts[-1].endswith("第二段")


def test_warn_threshold_is_positive_int() -> None:
    """段数告警阈值存在且合理（不截断内容，只提示提示词该收短了）。"""
    assert isinstance(WECHAT_SPLIT_WARN_PARTS, int)
    assert WECHAT_SPLIT_WARN_PARTS > 1
