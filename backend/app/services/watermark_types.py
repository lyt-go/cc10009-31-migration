"""水位类型识别与格式转换。

旧版水位以无类型字符串保存；识别只接受能明确判定的格式，不做“看起来像”的猜测。
识别不了的原始值一律返回错误，由调用方送人工处理队列。
"""
from __future__ import annotations

import re
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation

INT_PATTERN = re.compile(r"^[+-]?\d+$")
DECIMAL_PATTERN = re.compile(r"^[+-]?\d+\.\d+$")
DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
DATETIME_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(:\d{2})?(\.\d{1,6})?(Z|[+-]\d{2}:?\d{2})?$"
)

AUTO_TYPES = ("整数", "小数", "日期", "日期时间")
MANUAL_TYPES = AUTO_TYPES + ("字符串",)


def convert_with_type(raw: str, value_type: str) -> tuple[str | None, str | None]:
    """按明确指定的类型转换；转换不了时返回 (None, 原因)。

    人工指定“字符串”只表示把该值当作字符串水位，不做任何数值/时间猜测。
    """
    text = str(raw).strip()
    if value_type == "字符串":
        return (text, None) if text else (None, "字符串水位不能为空")
    if value_type == "整数":
        if not INT_PATTERN.match(text):
            return None, f"「{raw}」不是合法整数"
        return str(int(text)), None
    if value_type == "小数":
        if not DECIMAL_PATTERN.match(text):
            return None, f"「{raw}」不是合法小数"
        try:
            number = Decimal(text)
        except InvalidOperation:
            return None, f"「{raw}」不是合法小数"
        return format(number.normalize(), "f"), None
    if value_type == "日期":
        if not DATE_PATTERN.match(text):
            return None, f"「{raw}」不是 YYYY-MM-DD 格式的日期"
        try:
            parsed = date.fromisoformat(text)
        except ValueError:
            return None, f"「{raw}」不是合法日期"
        return parsed.isoformat(), None
    if value_type == "日期时间":
        return _convert_datetime(text)
    return None, f"不支持的水位类型「{value_type}」"


def _convert_datetime(text: str) -> tuple[str | None, str | None]:
    if not DATETIME_PATTERN.match(text):
        return None, f"「{text}」不是可识别的时间格式"
    normalized = text.replace(" ", "T")
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None, f"「{text}」不是合法时间"
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc)
        return parsed.isoformat(timespec="seconds").replace("+00:00", "Z"), None
    return parsed.isoformat(timespec="seconds"), None


def classify(raw: object) -> tuple[str | None, str | None, str | None]:
    """识别旧版字符串水位。

    返回 (类型, 转换后水位, 错误原因)。三个值中要么类型与转换值同时存在，
    要么只有错误原因——不会带着不确定的类型继续迁移。
    """
    text = str(raw).strip() if raw is not None else ""
    if not text:
        return None, None, "水位为空，无法识别类型"
    if INT_PATTERN.match(text):
        return "整数", str(int(text)), None
    if DECIMAL_PATTERN.match(text):
        converted, error = convert_with_type(text, "小数")
        return ("小数", converted, None) if error is None else (None, None, error)
    if DATE_PATTERN.match(text):
        converted, error = convert_with_type(text, "日期")
        return ("日期", converted, None) if error is None else (None, None, error)
    if DATETIME_PATTERN.match(text):
        converted, error = convert_with_type(text, "日期时间")
        return ("日期时间", converted, None) if error is None else (None, None, error)
    return None, None, f"「{text}」不属于整数、小数、日期或日期时间，需人工确认"


def preview(raw: object) -> dict[str, object]:
    """迁移前预览：不改数据，只给出识别结论，便于对照输入逐项检查。"""
    value_type, converted, error = classify(raw)
    return {
        "原字符串水位": "" if raw is None else str(raw),
        "识别类型": value_type,
        "转换后水位": converted,
        "可直接迁移": error is None,
        "待人工处理原因": error,
    }
