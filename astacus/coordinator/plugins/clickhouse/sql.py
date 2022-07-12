"""
Copyright (c) 2022 Aiven Ltd
See LICENSE for details
"""
from typing import Union

import dataclasses
import enum


# These patterns are only useful to parse ClickHouse queries, which we should avoid
# as much as possible, but sometimes there is no better option.
# Node: this does not handle "Single quotes can be escaped with the single quote",
# but this is OK (for the intended usage of these patterns), quote-escaped-by-quote
# are understood by ClickHouse but not generated by ClickHouse.
class TokenType(enum.Enum):
    Integer = rb"-?[0-9]+"
    QuotedIdentifier = rb"`(?:\\x[0-9a-fA-F]{2}|\\[^x]|[^\\`])*`"
    RawIdentifier = rb"[0-9a-zA-Z_]+"
    String = rb"'(?:\\x[0-9a-fA-F]{2}|\\[^x]|[^\\'])*'"
    Equal = rb"="
    Comma = rb","
    OpenParenthesis = rb"\("
    CloseParenthesis = rb"\)"
    Space = rb"\s+"
    EOF = None

    def __repr__(self) -> str:
        return str(self)


@dataclasses.dataclass(frozen=True)
class Token:
    token_type: TokenType
    value: bytes


def named_group(name: str, pattern: Union[bytes, TokenType]) -> bytes:
    return b"(?P<" + name.encode() + b">" + _to_bytes_pattern(pattern) + b")"


def chain_of(*patterns: Union[bytes, TokenType]) -> bytes:
    return rb"\s*".join(_to_bytes_pattern(pattern) for pattern in patterns)


def one_of(*patterns: Union[bytes, TokenType]) -> bytes:
    return _group(b"|".join(_group(_to_bytes_pattern(pattern)) for pattern in patterns))


def _to_bytes_pattern(pattern: Union[bytes, TokenType]) -> bytes:
    return pattern.value if isinstance(pattern, TokenType) else TokenType.Space.value.join(pattern.split(b" "))


def _group(pattern: bytes) -> bytes:
    return b"(?:" + pattern + b")"
