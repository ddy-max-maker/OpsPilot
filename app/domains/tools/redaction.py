"""敏感信息脱敏与长度截断（CONTRACTS §3：summary ≤ 2KB、payload ≤ 256KB）。

外部返回内容一律按不可信文本处理（PRD §8）：先脱敏、后截断、标记 truncated。
"""

from __future__ import annotations

import re
from typing import Any

# 凭据模式（命中值替换为 ***REDACTED***）：顺序敏感——Bearer/连接串/前缀型优先于通用 KV
_REDACTION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)\b(bearer\s+)\S+"),
    re.compile(r"(?i)\b(?:redis|postgres(?:ql)?|mysql|mongodb(?:\+srv)?)://[^\s/:@]+:[^\s/@]+@"),
    re.compile(r"(?i)\b(sk-|ghp_|gho_|xox[bpars]-)[A-Za-z0-9_-]{8,}"),
    re.compile(
        r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|"
        r"private[_-]?key|authorization|credentials?)\b\s*[:=]\s*\S+"
    ),
)

_REDACTED = "***REDACTED***"

# D4：Prompt Injection 模式检测（知识/日志等不可信文本进入模型上下文前的标记）。
# 命中不阻断（内容仍按不可信数据返回），但会在 Evidence 摘要/载荷中显式标记，
# 供审计与 Agent 识别"该内容试图改变我的行为"。
_INJECTION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)ignore\s+(all\s+)?(previous|prior|above|earlier)\s+instructions"),
    re.compile(r"(?i)disregard\s+(all\s+)?(previous|prior|above)\s+instructions"),
    re.compile(r"(?i)(you\s+are\s+now|act\s+as|pretend\s+to\s+be)\s+(a|an|the)\s+"),
    re.compile(r"(?i)(system\s+prompt|developer\s+message|new\s+instructions?)\s*[:：]"),
    re.compile(r"(?i)(grant|give)\s+(me\s+)?(admin|root|execute|write)\s+(access|permission)"),
    re.compile(r"忽略(上述|之前|以上|前面)(的)?(所有)?(指令|提示|规则|约束)"),
    re.compile(r"(你现在是|请扮演|假装你是)(一个)?"),
    re.compile(r"(授予|给予)(我)?(管理员|执行|写入|root|admin)权限"),
)


def detect_injection(text: str) -> list[str]:
    """检测不可信文本中的注入模式，返回命中的模式名列表（空 = 未检出）。"""
    names: list[str] = []
    for idx, pattern in enumerate(_INJECTION_PATTERNS):
        if pattern.search(text):
            names.append(f"injection_pattern_{idx}")
    return names


def redact_text(text: str) -> str:
    """对文本执行凭据模式脱敏。"""
    result = text
    for pattern in _REDACTION_PATTERNS:
        result = pattern.sub(lambda m: _mask_match(m), result)
    return result


def _mask_match(match: re.Match[str]) -> str:
    """保留结构前缀（键名/协议），仅抹去敏感值。"""
    value = match.group(0)
    separator = re.search(r"[:=]", value)
    if separator is not None and "://" not in value[: separator.start() + 1]:
        return value[: separator.end()] + _REDACTED
    # 协议型（redis://user:pass@host）与 Bearer：整体结构保留、凭据抹除
    at = value.rfind("@")
    if at > 0:
        scheme_end = value.find("://")
        prefix = value[: scheme_end + 3] if scheme_end >= 0 else ""
        return f"{prefix}{_REDACTED}{value[at:]}"
    return _REDACTED


def redact_payload(payload: Any, max_bytes: int) -> tuple[Any, bool]:
    """递归脱敏 JSON 载荷并按字节上限截断，返回 (载荷, truncated)。

    超限策略：字符串截断、列表/字典元素级丢弃，保证返回值始终可序列化。
    """
    redacted = _redact_value(payload)
    serialized_len = len(_to_text(redacted).encode("utf-8"))
    if serialized_len <= max_bytes:
        return redacted, False
    truncated = _truncate_value(redacted, max_bytes)
    return truncated, True


def truncate_text(text: str, max_bytes: int) -> tuple[str, bool]:
    """按字节截断 UTF-8 文本（不切断多字节字符）。"""
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text, False
    clipped = encoded[:max_bytes]
    # 回退到完整字符边界
    while clipped:
        try:
            return clipped.decode("utf-8"), True
        except UnicodeDecodeError:
            clipped = clipped[:-1]
    return "", True


_SENSITIVE_KEY_RE = re.compile(
    r"(?i)(password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|"
    r"private[_-]?key|authorization|credentials?|cookie)"
)


def _redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            # 键名本身敏感（如 {"password": "..."}）→ 值整体抹除
            if isinstance(key, str) and _SENSITIVE_KEY_RE.search(key):
                out[key] = _REDACTED
            else:
                out[key] = _redact_value(item)
        return out
    if isinstance(value, list):
        return [_redact_value(v) for v in value]
    return value


def _truncate_value(value: Any, max_bytes: int) -> Any:
    """递归截断：字符串按字节截断、列表元素级丢弃、字典逐键递归（保留前部）。"""
    if isinstance(value, str):
        clipped, _ = truncate_text(value, max_bytes)
        return clipped
    if isinstance(value, list):
        kept: list[Any] = []
        used = 2  # "[]"
        for item in value:
            candidate = _to_text(item)
            cost = len(candidate.encode("utf-8")) + (1 if kept else 0)
            if used + cost > max_bytes:
                break
            kept.append(item)
            used += cost
        return kept
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        used = 2  # "{}"
        for key, item in value.items():
            key_cost = len(_to_text(key).encode("utf-8")) + 4
            remaining = max_bytes - used - key_cost
            if remaining <= 0:
                break
            child = _truncate_value(item, remaining)
            entry = _to_text({key: child})
            entry_cost = len(entry.encode("utf-8")) + (1 if out else 0)
            if used + entry_cost > max_bytes:
                break
            out[key] = child
            used += entry_cost
        return out
    return value


def _to_text(value: Any) -> str:
    import json

    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(value)
