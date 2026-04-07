"""Assembler-style (dis)assembly of the UnrealScript bytecode token stream.

The XML sidecars produced for ``UnStruct``/``UnFunction`` token streams are
faithful but hard to read: every token is a ``<Token>`` element and the
expression tree is buried under ``<Variable>``/``<Assignment>``/``<Params>``
wrappers.  This module renders the very same token dicts (as produced by
:meth:`TokenStreamParser.tokens_to_dict_list`) into a compact, line-oriented
"assembler" that round-trips losslessly back into those dicts.

Grammar (one token per line, nesting by indentation)::

    [@Role[]] [L_XXXX:] MNEMONIC [key=value ...]

* ``MNEMONIC`` is the token ``Type`` verbatim (e.g. ``NativeFunction``,
  ``JumpIfNot``).  It matches the token name exactly so import can look the
  class up in the registry.
* ``key=value`` operands carry the token's scalar fields.  Values are bare
  when safe and double-quoted (with ``\\n``/``\\xHH`` escapes) otherwise.
* A child token occupies its own, more-indented line and is tagged with the
  role it fills in its parent: ``@Role`` for a single sub-expression (e.g.
  ``@Condition``), ``@Role[]`` for each element of a list (e.g. ``@Params[]``).
* ``L_XXXX:`` is a purely cosmetic jump-target label mirroring the ``Label``
  field the exporter attaches to referenced tokens; it is ignored on import
  (jump offsets are resolved from the symbolic ``JumpTo``/``EndLabel`` values).
* A flat, ``Type``-less dict (a ``LabelTable`` entry) is written with the
  pseudo-mnemonic ``Entry`` and reconstructed without a ``Type`` key.

Lines that are blank or begin with ``;`` are comments and ignored on import.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Tuple

# Indentation unit for one nesting level.
_INDENT = "  "

# Pseudo-mnemonic used for flat, ``Type``-less dicts (e.g. label-table entries).
_ENTRY_MNEMONIC = "Entry"

# Cosmetic jump-target label, e.g. ``L_0034``.
_LABEL_RE = re.compile(r"L_[0-9A-Fa-f]+:")


# --------------------------------------------------------------------------- #
#  Value formatting / quoting
# --------------------------------------------------------------------------- #


def _format_scalar(value: Any) -> str:
    """Format a scalar field value for the operand list.

    Booleans become ``true``/``false``; everything else uses ``str()``.

    Args:
        value (Any): The scalar value to format.

    Returns:
        str: The string form of the value (before any quoting).
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _needs_quote(text: str) -> bool:
    """Return True if *text* cannot be written as a bare operand value.

    A value must be quoted when it is empty or contains whitespace, a double
    quote, a backslash, or any control character.

    Args:
        text (str): The candidate bare value.

    Returns:
        bool: True if the value must be quoted, else False.
    """
    if text == "":
        return True
    for ch in text:
        code = ord(ch)
        if ch in '"\\' or ch.isspace() or code < 0x20 or code == 0x7F:
            return True
    return False


def _quote(text: str) -> str:
    """Return *text* as a double-quoted, escaped operand value.

    Backslash, quote, newline, carriage return and tab use their canonical
    escapes; any other control character uses a ``\\xHH`` escape.

    Args:
        text (str): The value to quote.

    Returns:
        str: The quoted, escaped value including surrounding quotes.
    """
    out = ['"']
    for ch in text:
        code = ord(ch)
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif code < 0x20 or code == 0x7F:
            out.append(f"\\x{code:02X}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def _encode_value(value: Any) -> str:
    """Encode a scalar field value as a bare or quoted operand string.

    Args:
        value (Any): The scalar value to encode.

    Returns:
        str: The bare or quoted operand form.
    """
    text = _format_scalar(value)
    return _quote(text) if _needs_quote(text) else text


# --------------------------------------------------------------------------- #
#  Serialisation: token dicts -> assembler text
# --------------------------------------------------------------------------- #


def _emit_token(
    token: Dict[str, Any],
    indent: int,
    role: str,
    is_list: bool,
    lines: List[str],
) -> None:
    """Append the assembler lines for *token* (and its children) to *lines*.

    Args:
        token (Dict[str, Any]): The token dict to render.
        indent (int): The current nesting depth.
        role (str): The role this token fills in its parent, or ``""`` for a
            top-level token.
        is_list (bool): True if this token is one element of a list-valued
            parent field.
        lines (List[str]): The accumulator to append rendered lines to.
    """
    parts: List[str] = []
    if role:
        parts.append("@" + role + ("[]" if is_list else ""))

    label = token.get("Label")
    if label:
        parts.append(f"{label}:")

    mnemonic = token.get("Type", _ENTRY_MNEMONIC)
    parts.append(str(mnemonic))

    # Children are emitted after the head line, preserving field order.
    children: List[Tuple[str, bool, Dict[str, Any]]] = []
    for key, value in token.items():
        if key in ("Type", "Label"):
            continue
        if isinstance(value, dict):
            if value:  # empty dict = null sub-token, omit
                children.append((key, False, value))
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    children.append((key, True, item))
                else:
                    # No current token emits scalar lists; keep the value by
                    # wrapping it as a single-field entry so nothing is lost.
                    children.append((key, True, {"Value": item}))
        else:
            parts.append(f"{key}={_encode_value(value)}")

    lines.append(_INDENT * indent + " ".join(parts))
    for child_role, child_is_list, child in children:
        _emit_token(child, indent + 1, child_role, child_is_list, lines)


def tokens_to_asm(token_dicts: List[Dict[str, Any]]) -> str:
    """Render a list of top-level token dicts as assembler text.

    Args:
        token_dicts (List[Dict[str, Any]]): Token dicts as produced by
            :meth:`TokenStreamParser.tokens_to_dict_list`.

    Returns:
        str: The assembler text, one token per line, terminated by a newline.
    """
    lines: List[str] = []
    for token in token_dicts:
        _emit_token(token, 0, "", False, lines)
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
#  Parsing: assembler text -> token dicts
# --------------------------------------------------------------------------- #


def _read_quoted(text: str, index: int) -> Tuple[str, int]:
    """Decode a double-quoted, escaped value starting at ``text[index]``.

    Args:
        text (str): The line remainder being scanned.
        index (int): The index of the opening double quote.

    Returns:
        Tuple[str, int]: The decoded value and the index just past the value.
    """
    index += 1  # skip opening quote
    out: List[str] = []
    length = len(text)
    while index < length:
        ch = text[index]
        if ch == "\\":
            index += 1
            esc = text[index]
            if esc == "n":
                out.append("\n")
            elif esc == "r":
                out.append("\r")
            elif esc == "t":
                out.append("\t")
            elif esc == "x":
                out.append(chr(int(text[index + 1 : index + 3], 16)))
                index += 2
            else:
                out.append(esc)  # \\ and \" and any other escaped char
            index += 1
        elif ch == '"':
            index += 1  # skip closing quote
            break
        else:
            out.append(ch)
            index += 1
    return "".join(out), index


def _parse_operands(text: str) -> Dict[str, str]:
    """Parse the ``key=value`` operand list from a head-line remainder.

    Args:
        text (str): The portion of a line after the mnemonic.

    Returns:
        Dict[str, str]: The parsed operands as string-valued fields.
    """
    operands: Dict[str, str] = {}
    index = 0
    length = len(text)
    while index < length:
        if text[index].isspace():
            index += 1
            continue
        eq = text.index("=", index)
        key = text[index:eq]
        index = eq + 1
        if index < length and text[index] == '"':
            value, index = _read_quoted(text, index)
        else:
            start = index
            while index < length and not text[index].isspace():
                index += 1
            value = text[start:index]
        operands[key] = value
    return operands


def _parse_line(text: str) -> Tuple[str, bool, Dict[str, Any]]:
    """Parse one non-blank assembler line into (role, is_list, token dict).

    Args:
        text (str): The line content with indentation already stripped.

    Returns:
        Tuple[str, bool, Dict[str, Any]]: The role the token fills in its
            parent (``""`` for top-level), whether it is a list element, and
            the reconstructed token dict.
    """
    role = ""
    is_list = False
    if text.startswith("@"):
        space = text.find(" ")
        tag = text[1:space]
        text = text[space + 1 :].lstrip()
        if tag.endswith("[]"):
            is_list = True
            tag = tag[:-2]
        role = tag

    # Skip the cosmetic jump-target label, if present.
    match = re.match(r"(L_[0-9A-Fa-f]+:)\s+", text)
    if match:
        text = text[match.end() :]

    space = text.find(" ")
    if space == -1:
        mnemonic, remainder = text, ""
    else:
        mnemonic, remainder = text[:space], text[space + 1 :]

    operands = _parse_operands(remainder)
    if mnemonic == _ENTRY_MNEMONIC:
        token: Dict[str, Any] = dict(operands)
    else:
        token = {"Type": mnemonic}
        token.update(operands)
    return role, is_list, token


def asm_to_tokens(text: str) -> List[Dict[str, Any]]:
    """Parse assembler text back into a list of top-level token dicts.

    Args:
        text (str): The assembler text produced by :func:`tokens_to_asm`.

    Returns:
        List[Dict[str, Any]]: The reconstructed top-level token dicts, ready
            for :meth:`TokenStreamParser.tokens_from_dict_list`.
    """
    result: List[Dict[str, Any]] = []
    # Stack of (indent, token dict) tracking the current ancestry.
    stack: List[Tuple[int, Dict[str, Any]]] = []

    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith(";"):
            continue
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        role, is_list, token = _parse_line(stripped)

        while stack and stack[-1][0] >= indent:
            stack.pop()

        if not stack:
            result.append(token)
        else:
            parent = stack[-1][1]
            if is_list:
                parent.setdefault(role, []).append(token)
            else:
                parent[role] = token

        stack.append((indent, token))

    return result
