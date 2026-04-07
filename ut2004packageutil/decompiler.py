"""UnrealScript decompiler.

Turns the parsed object graph and bytecode token streams of a UT2004 ``.u``
package back into ``.uc`` source text.  The design follows the reference
implementation in `Eliot's UELib
<https://github.com/EliotVU/Unreal-Library>`_ (``ByteCodeDecompiler.cs`` and
the ``U*Decompiler.cs`` partial classes), ported to this package's object
model.

The public entry point is :class:`Decompiler`, which iterates a package's
class exports and writes one ``.uc`` file per class into an output folder.
"""

import io
import os
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from ut2004packageutil.package.flags import (
    UnClassFlags,
    UnFunctionFlags,
    UnNameMap,
    UnObjectFlags,
    UnPropertyFlags,
    UnStructFlags,
)
from ut2004packageutil.package.object import (
    UnArrayProperty,
    UnByteProperty,
    UnClass,
    UnClassProperty,
    UnConst,
    UnDefaultObject,
    UnDelegateProperty,
    UnEnum,
    UnField,
    UnFixedArrayProperty,
    UnFunction,
    UnObjectProperty,
    UnProperty,
    UnState,
    UnStruct,
    UnStructProperty,
    UnTextBuffer,
)
from ut2004packageutil.package.package import UnExport, resolve_item
from ut2004packageutil.package.token import (
    UnCastType,
    UnToken,
    UnTokenArrayElement,
    UnTokenAssert,
    UnTokenBoolVariable,
    UnTokenByteConst,
    UnTokenCase,
    UnTokenClassContext,
    UnTokenContext,
    UnTokenDebugInfo,
    UnTokenDefaultVariable,
    UnTokenDelegateFunction,
    UnTokenDelegateProperty,
    UnTokenDynamicCast,
    UnTokenDynArrayElement,
    UnTokenDynArrayInsert,
    UnTokenDynArrayLength,
    UnTokenDynArrayRemove,
    UnTokenEatString,
    UnTokenEndFunctionParms,
    UnTokenEndOfScript,
    UnTokenExtendedNativeFunction,
    UnTokenFalse,
    UnTokenFinalFunction,
    UnTokenFloatConst,
    UnTokenGlobalFunction,
    UnTokenGotoLabel,
    UnTokenInstanceVariable,
    UnTokenIntConst,
    UnTokenIntConstByte,
    UnTokenIntOne,
    UnTokenIntZero,
    UnTokenIterator,
    UnTokenIteratorNext,
    UnTokenIteratorPop,
    UnTokenJump,
    UnTokenJumpIfNot,
    UnTokenLabelTable,
    UnTokenLet,
    UnTokenLetBool,
    UnTokenLetDelegate,
    UnTokenLocalVariable,
    UnTokenMetaCast,
    UnTokenNameConst,
    UnTokenNativeFunction,
    UnTokenNativeParm,
    UnTokenNew,
    UnTokenNoObject,
    UnTokenNothing,
    UnTokenObjectConst,
    UnTokenPrimitiveCast,
    UnTokenReturn,
    UnTokenRotationConst,
    UnTokenSelf,
    UnTokenSkip,
    UnTokenStop,
    UnTokenStringConst,
    UnTokenStructCmpEq,
    UnTokenStructCmpNe,
    UnTokenStructMember,
    UnTokenSwitch,
    UnTokenTrue,
    UnTokenUnicodeStringConst,
    UnTokenVectorConst,
    UnTokenVirtualFunction,
)
from ut2004packageutil.utils.io_utils import read_index

# Cast opcode -> UnrealScript type keyword used in ``type(expr)``.
_CAST_TYPE_NAMES: Dict[int, str] = {
    int(UnCastType.RotatorToVector): "Vector",
    int(UnCastType.ByteToInt): "int",
    int(UnCastType.ByteToBool): "bool",
    int(UnCastType.ByteToFloat): "float",
    int(UnCastType.IntToByte): "byte",
    int(UnCastType.IntToBool): "bool",
    int(UnCastType.IntToFloat): "float",
    int(UnCastType.BoolToByte): "byte",
    int(UnCastType.BoolToInt): "int",
    int(UnCastType.BoolToFloat): "float",
    int(UnCastType.FloatToByte): "byte",
    int(UnCastType.FloatToInt): "int",
    int(UnCastType.FloatToBool): "bool",
    int(UnCastType.ObjectToBool): "bool",
    int(UnCastType.NameToBool): "bool",
    int(UnCastType.StringToByte): "byte",
    int(UnCastType.StringToInt): "int",
    int(UnCastType.StringToBool): "bool",
    int(UnCastType.StringToFloat): "float",
    int(UnCastType.StringToVector): "Vector",
    int(UnCastType.StringToRotator): "Rotator",
    int(UnCastType.VectorToBool): "bool",
    int(UnCastType.VectorToRotator): "Rotator",
    int(UnCastType.RotatorToBool): "bool",
    int(UnCastType.ByteToString): "string",
    int(UnCastType.IntToString): "string",
    int(UnCastType.BoolToString): "string",
    int(UnCastType.FloatToString): "string",
    int(UnCastType.ObjectToString): "string",
    int(UnCastType.NameToString): "string",
    int(UnCastType.VectorToString): "string",
    int(UnCastType.RotatorToString): "string",
}

# Cast opcode -> (source type, destination type), derived from the enum name
# (e.g. ``IntToBool`` -> ``("int", "bool")``).  Used by ``--simplify`` to fold
# constant casts and drop redundant round-trip casts.
_CAST_SRC_DST: Dict[int, tuple] = {}
for _ct in UnCastType:
    _parts = _ct.name.split("To")
    if len(_parts) == 2 and _parts[0] and _parts[1]:
        _CAST_SRC_DST[int(_ct)] = (_parts[0].lower(), _parts[1].lower())


# Numeric widening casts the compiler inserts implicitly (byte -> int,
# byte -> float, int -> float).  ``--simplify`` drops these, since the source
# never needed them; the reverse (narrowing) casts are always kept.
_IMPLICIT_WIDENING_CASTS: frozenset = frozenset(
    {
        int(UnCastType.ByteToInt),
        int(UnCastType.ByteToFloat),
        int(UnCastType.IntToFloat),
    }
)


# Sub-expression attribute names, used to walk an expression token tree.
_EXPR_CHILD_ATTRS = (
    "variable",
    "assignment",
    "condition",
    "expression",
    "iterator_expr",
    "object_expr",
    "context_expr",
    "index_expr",
    "base_expr",
    "count_expr",
    "left_expr",
    "right_expr",
    "inner_expr",
    "label_expr",
    "parent_expr",
    "name_expr",
    "flags_expr",
    "class_expr",
)


def _walk_expr(token: Optional[UnToken]):
    """Yield a token and every nested sub-expression / parameter token.

    Args:
        token (Optional[UnToken]): Root token to walk, or None.

    Yields:
        UnToken: The token itself followed by each nested child token.
    """
    if token is None:
        return
    yield token
    for attr in _EXPR_CHILD_ATTRS:
        child = getattr(token, attr, None)
        if isinstance(child, UnToken):
            yield from _walk_expr(child)
    for param in getattr(token, "params", None) or []:
        if isinstance(param, UnToken):
            yield from _walk_expr(param)


# ===================================================================== #
#  Literal formatting
# ===================================================================== #


def _write_uc(path: str, source: str) -> None:
    """Write decompiled/extracted ``.uc`` text to disk.

    Output is single-byte ``latin-1`` (ANSI), the encoding UT2004's ``ucc``
    expects for source: it rejects UTF-8 multi-byte sequences in string
    literals (``Unrecognized ...`` / ``Unterminated string constant``).
    ``latin-1`` is also the exact byte-inverse of how the package reader
    decodes strings (byte ``N`` <-> ``U+00NN``), so bytes round-trip exactly,
    and it is ANSI/Windows-1252-compatible for every printable high char
    (``0xA0``-``0xFF``). Code points above ``0xFF`` (e.g. ``™`` from a UTF-16
    Unicode string property) have no representation in an ANSI ``.uc`` and
    could not compile anyway, so they are replaced rather than allowed to
    crash the write. Newlines are kept as ``\\n``.

    Args:
        path (str): Destination file path.
        source (str): The ``.uc`` source text to write.
    """
    data = source.encode("latin-1", errors="replace")
    with open(path, "wb") as f:
        f.write(data)


def _format_float(value: float) -> str:
    """Format a float the way UnrealScript source expects (always a decimal).

    Args:
        value (float): The float to format.

    Returns:
        str: The float rendered as an UnrealScript literal (e.g. ``1.0``).
    """
    if value != value:  # NaN
        return "0.0"
    text = repr(float(value))
    if "e" in text or "E" in text:
        return text
    if "." not in text:
        text += ".0"
    return text


def _format_string(value: str) -> str:
    """Return a UnrealScript string literal for ``value``.

    ucc's lexer only honours ``\\"`` and ``\\\\`` escapes; for any other
    ``\\x`` it drops the backslash and keeps ``x`` (so ``"\\t"`` compiles to
    ``t``, ``"\\n"`` to ``n``, verified against ``UnScrCom.cpp`` and a live
    build). Control characters therefore cannot be written as backslash
    escapes:

    * Tab (0x09) is emitted as a raw 0x09 byte inside the quotes — the lexer
      stores it verbatim, reproducing the original constant.
    * LF (0x0A) and CR (0x0D) cannot be raw bytes (they end the line, giving
      an "Unterminated string constant" error) and have no working escape, so
      the literal is split and they are emitted as explicit ``Chr(10)`` /
      ``Chr(13)`` concatenation (e.g. ``"a" $ Chr(10) $ "b"``).

    Args:
        value (str): The raw string value to represent.

    Returns:
        str: A UnrealScript expression evaluating to ``value`` — a single
            double-quoted literal when it has no LF/CR, otherwise a ``$``
            concatenation of literals and ``Chr(...)`` calls.
    """
    # Build a sequence of ("str", text) literal runs and ("chr", code) breaks.
    seq: List[Tuple[str, object]] = []
    buf: List[str] = []
    for ch in value:
        if ch == "\n" or ch == "\r":
            seq.append(("str", "".join(buf)))
            buf.clear()
            seq.append(("chr", 10 if ch == "\n" else 13))
        elif ch == "\\":
            buf.append("\\\\")
        elif ch == '"':
            buf.append('\\"')
        elif ch == "\t":
            buf.append("\t")  # raw 0x09; "\t" would compile to the letter t
        else:
            buf.append(ch)
    seq.append(("str", "".join(buf)))

    parts: List[str] = []
    for kind, val in seq:
        if kind == "str":
            if val == "":
                continue  # drop empty runs around the Chr() breaks
            parts.append('"' + val + '"')
        else:
            parts.append("Chr(%d)" % val)
    if not parts:
        return '""'
    return " $ ".join(parts)


# ===================================================================== #
#  Native function / operator table
# ===================================================================== #


@dataclass
class _NativeInfo:
    """Decompilation metadata for a native function or operator.

    Attributes:
        name (str): The friendly name or operator symbol.
        kind (str): One of ``"function"``, ``"binary"``, ``"pre"`` or ``"post"``.
        precedence (int): Operator precedence (0 for functions/post-operators).
        coerce (tuple): Per-parameter CoerceParm flags (excludes the return).
        enums (tuple): Per-parameter enum type or ``None`` (excludes the return),
            so an integer-constant argument to an enum parameter can render as
            the enum member name (e.g. ``SetPhysics(PHYS_Falling)``).
    """

    name: str
    kind: str  # "function" | "binary" | "pre" | "post"
    precedence: int = 0
    coerce: tuple = ()  # per-parameter CoerceParm flag (excludes the return)
    enums: tuple = ()  # per-parameter enum type or None (excludes the return)


def _param_coerce_flags(fn: UnFunction) -> tuple:
    """Return the ``coerce`` flag of each parameter of a function, in order.

    Args:
        fn (UnFunction): The function whose parameters are inspected.

    Returns:
        tuple: A tuple of bools, one per non-return parameter, True when the
            parameter carries the CoerceParm flag.
    """
    flags: List[bool] = []
    child = fn.children
    while child is not None:
        obj = child.object
        if isinstance(obj, UnProperty) and (
            obj.property_flags & int(UnPropertyFlags.Parm)
        ):
            if not (obj.property_flags & int(UnPropertyFlags.ReturnParm)):
                flags.append(bool(obj.property_flags & int(UnPropertyFlags.CoerceParm)))
        child = obj.next_item if isinstance(obj, UnField) else None
    return tuple(flags)


def _param_enums_of(fn: UnFunction) -> tuple:
    """Return the enum type (or ``None``) of each non-return parameter, in order.

    Module-level twin of :meth:`_BodyDecompiler._param_enums`, used when building
    the native table so a native call can render an integer-constant argument to
    an enum-typed parameter as the enum member name (e.g. ``SetPhysics(2)`` ->
    ``SetPhysics(PHYS_Falling)``).

    Args:
        fn (UnFunction): The function whose parameters are inspected.

    Returns:
        tuple: One entry per non-return parameter (a UnEnum or None).
    """
    out: List[Optional[UnEnum]] = []
    child = fn.children
    while child is not None:
        obj = child.object
        if (
            isinstance(obj, UnProperty)
            and (obj.property_flags & int(UnPropertyFlags.Parm))
            and not (obj.property_flags & int(UnPropertyFlags.ReturnParm))
        ):
            enum = None
            if isinstance(obj, UnByteProperty) and obj.enum_item is not None:
                e = obj.enum_item.object
                if isinstance(e, UnEnum):
                    enum = e
            out.append(enum)
        child = obj.next_item if isinstance(obj, UnField) else None
    return tuple(out)


def _needs_space(operator_name: str) -> bool:
    """Report whether a word-like operator needs a surrounding space.

    Word-like operators (e.g. ``ClockwiseFrom``) are spaced; symbols aren't.

    Args:
        operator_name (str): The operator symbol or name.

    Returns:
        bool: True when the operator starts with a letter and needs a space.
    """
    return bool(operator_name) and operator_name[0].isalpha()


def build_native_table(package) -> Dict[int, _NativeInfo]:
    """Build a native-index to :class:`_NativeInfo` map from loaded packages.

    Every native function/operator is declared somewhere as a ``UnFunction``
    carrying a non-zero ``native_index``, the ``Operator``/``PreOperator``
    flags, the operator precedence, and a friendly name (the operator
    symbol).  Scanning the target package plus every loaded dependency
    reconstructs the table the game would use at runtime, so no external
    native table file is required.

    Args:
        package: The target package whose dependency graph is scanned.

    Returns:
        Dict[int, _NativeInfo]: Native index mapped to its decompilation info.
    """
    table: Dict[int, _NativeInfo] = {}
    seen: set = set()

    def scan(pkg) -> None:
        """Recursively add every native function of a package to the table.

        Args:
            pkg: The package to scan; ignored if None or already seen.
        """
        if pkg is None or id(pkg) in seen:
            return
        seen.add(id(pkg))
        for export in pkg.exports:
            obj = export.object
            if not isinstance(obj, UnFunction):
                continue
            if obj.native_index <= 0:
                continue
            name = (
                obj.friendly_name.name if obj.friendly_name else export.object_name.name
            )
            flags = obj.function_flags
            if flags & UnFunctionFlags.Operator:
                if flags & UnFunctionFlags.PreOperator:
                    kind = "pre"
                elif obj.operator_precedence == 0:
                    kind = "post"
                else:
                    kind = "binary"
            else:
                kind = "function"
            table.setdefault(
                obj.native_index,
                _NativeInfo(
                    name=name,
                    kind=kind,
                    precedence=obj.operator_precedence,
                    coerce=_param_coerce_flags(obj),
                    enums=_param_enums_of(obj),
                ),
            )
        for dep in pkg.imported_packages.values():
            scan(dep)

    scan(package)
    return table


# ===================================================================== #
#  Indentation state
# ===================================================================== #


class _Indent:
    """Tracks the current indentation depth (one tab per level)."""

    def __init__(self) -> None:
        """Initialize the indentation tracker at depth zero."""
        self.level = 0

    @property
    def tabs(self) -> str:
        """Return the tab string for the current indentation level.

        Returns:
            str: One tab character per indentation level.
        """
        return "\t" * self.level

    def add(self) -> None:
        """Increase the indentation depth by one level."""
        self.level += 1

    def remove(self) -> None:
        """Decrease the indentation depth by one level, not below zero."""
        if self.level > 0:
            self.level -= 1


# ===================================================================== #
#  Bytecode body decompiler
# ===================================================================== #


@dataclass
class _Nest:
    """A pending or active control-flow scope (mirrors UELib's NestManager).

    Attributes:
        kind (str): Either ``"begin"`` or ``"end"``.
        type (str): The scope type (``"if"``, ``"else"``, ``"foreach"``,
            ``"switch"``, ``"case"``, ``"default"``, ``"loop"`` or ``"scope"``).
        position (int): The bytecode offset at which the nest begins or ends.
        creator (Optional[UnToken]): The token that opened the scope.
        has_else (Optional[UnToken]): The Jump token ending the if-block, for
            if/else reconstruction.
    """

    kind: str  # "begin" | "end"
    type: str  # "if" | "else" | "foreach" | "switch" | "case" | "default" | "loop" | "scope"
    position: int
    creator: Optional[UnToken] = None
    has_else: Optional[UnToken] = None  # JumpToken ending the if-block (for if/else)


@dataclass
class _Label:
    """A goto/state label with its target offset and reference count.

    Attributes:
        name (str): The label identifier.
        position (int): The bytecode offset the label marks.
        refs (int): Number of jumps that reference the label.
    """

    name: str
    position: int
    refs: int = 1


class _BodyDecompiler:
    """Decompiles one function/state token stream into UnrealScript statements.

    This is a port of ``UStruct.UByteCodeDecompiler`` restricted to the
    UT2004 token set.  It reconstructs control flow (if/else, loops,
    switch/case, foreach) by tracking nest begin/end positions against the
    byte offset (``icode_start``) of each top-level statement token.
    """

    def __init__(
        self, decompiler: "Decompiler", struct_obj: UnStruct, container: UnExport
    ) -> None:
        """Initialize the body decompiler for one struct's token stream.

        Args:
            decompiler (Decompiler): The owning class decompiler, providing
                shared package, native table, indentation and simplify state.
            struct_obj (UnStruct): The function/state whose tokens are decoded.
            container (UnExport): The export that owns the struct (used to
                detect super calls by name).
        """
        self.dec = decompiler
        self.pkg = decompiler.pkg
        self.natives = decompiler.natives
        self.ind = decompiler.ind
        self.simplify = decompiler.simplify
        # Loop reconstruction (goto/label -> while/for/do-until) is part of
        # --simplify but separately gateable: when off, loops render in raw
        # goto/label form (byte-exact round-trip). Only meaningful under simplify.
        self.reconstruct_loops = self.simplify and decompiler.reconstruct_loops
        self.struct = struct_obj
        self.container = container

        parser = struct_obj.token_parser
        self.tokens: List[UnToken] = list(parser.tokens) if parser else []
        # Statement byte ranges: position[i] = icode_start, size[i] = span.
        self.positions: List[int] = [t.icode_start for t in self.tokens]
        script_size = getattr(struct_obj, "script_size", 0) or 0
        self.sizes: List[int] = []
        for i, pos in enumerate(self.positions):
            end = self.positions[i + 1] if i + 1 < len(self.positions) else script_size
            self.sizes.append(max(end - pos, 0))

        self.index = -1
        self.nests: List[_Nest] = []
        self.nest_chain: List[_Nest] = []
        self.lines: List[str] = []
        self.within_class_context = False
        self.within_context = False
        self.pre_comment = ""

        self._labels: List[_Label] = []
        self._temp_labels: List[_Label] = []
        # Positions of ``JumpIfNot`` loop heads (a later backward ``Jump``
        # targets them).  Used by ``--simplify`` to reconstruct ``while``.
        self._loop_heads: set = set()
        # ``--simplify`` for-loop reconstruction:
        #   _for_info[head_pos] = (init_token, increment_token, continue_pos)
        #   _skip_ids           = ids of init/increment tokens to elide
        #   _for_continue_pos   = increment positions (continue targets)
        self._for_info: Dict[int, tuple] = {}
        self._skip_ids: set = set()
        self._for_continue_pos: set = set()
        # ``--simplify`` do/until reconstruction:
        #   _do_until_heads[head_pos] = back-edge JumpIfNot token
        #   _do_until_ends[head_pos]  = byte offset just past the back-edge
        #   _do_until_back_ids        = ids of back-edge tokens (elided/render "}")
        #   _do_open_done             = head positions already opened as ``do {``
        self._do_until_heads: Dict[int, UnToken] = {}
        self._do_until_ends: Dict[int, int] = {}
        self._do_until_back_ids: set = set()
        self._do_open_done: set = set()

    def _compute_loop_heads(self) -> None:
        """Find every JumpIfNot that a backward plain Jump loops back to."""
        self._loop_heads = set()
        for t in self.tokens:
            if (
                type(t) is UnTokenJump
                and t.offset < t.icode_start
                and isinstance(self._token_at(t.offset), UnTokenJumpIfNot)
            ):
                self._loop_heads.add(t.offset)

    # Assignment/increment operators that mark a loop-control variable.
    _ASSIGN_OPS = {"++", "--", "+=", "-=", "*=", "/=", "%=", "@=", "$="}

    def _var_ref(self, token: Optional[UnToken]):
        """Return the object ref of a plain variable expression, else ``None``.

        Args:
            token (Optional[UnToken]): The candidate variable expression.

        Returns:
            The variable's object reference, or None if not a plain variable.
        """
        if isinstance(token, UnTokenBoolVariable):
            token = token.expression
        if isinstance(token, (UnTokenLocalVariable, UnTokenInstanceVariable)):
            return token.object_ref
        return None

    def _assigned_var_ref(self, token: Optional[UnToken]):
        """Return the ref of the variable assigned/incremented, else ``None``.

        Args:
            token (Optional[UnToken]): A statement token to inspect.

        Returns:
            The object reference of the assigned/incremented variable, or None.
        """
        if isinstance(token, (UnTokenLet, UnTokenLetBool)):
            return self._var_ref(token.variable)
        if isinstance(token, (UnTokenNativeFunction, UnTokenExtendedNativeFunction)):
            info = self.natives.get(token.native_index)
            if info is not None and info.name in self._ASSIGN_OPS:
                ops = self._operands(token.params)
                if ops:
                    return self._var_ref(ops[0])
        return None

    def _expr_uses_var(self, token: Optional[UnToken], ref) -> bool:
        """Report whether a variable ref is read inside an expression.

        Args:
            token (Optional[UnToken]): The expression to search.
            ref: The variable object reference to look for.

        Returns:
            bool: True if the ref is read anywhere inside the expression.
        """
        for node in _walk_expr(token):
            if (
                isinstance(node, (UnTokenLocalVariable, UnTokenInstanceVariable))
                and node.object_ref == ref
            ):
                return True
        return False

    def _analyze_loops(self) -> None:
        """Detect ``for`` loops so the streaming pass can reconstruct them.

        A ``while`` loop is a ``for`` when the statement right before the head
        assigns the loop variable, the last body statement (the ``continue``
        target, just before the back-edge) modifies that same variable, and
        the variable appears in the condition.
        """
        if not self.reconstruct_loops:
            return
        for i, head in enumerate(self.tokens):
            if not (
                isinstance(head, UnTokenJumpIfNot)
                and self._pos_of(head) in self._loop_heads
            ):
                continue
            head_pos = self._pos_of(head)
            end = head.offset
            back_idx = None
            for j in range(i + 1, len(self.tokens)):
                tj = self.tokens[j]
                if type(tj) is UnTokenJump and tj.offset == head_pos:
                    back_idx = j
            if back_idx is None or back_idx == 0 or i == 0:
                continue
            back_edge = self.tokens[back_idx]
            if self._pos_of(back_edge) + self._size_of_index(back_idx) != end:
                continue
            incr = self.tokens[back_idx - 1]
            init = self.tokens[i - 1]
            v_init = self._assigned_var_ref(init)
            v_incr = self._assigned_var_ref(incr)
            if v_init is None or v_init != v_incr:
                continue
            if not self._expr_uses_var(head.condition, v_init):
                continue
            self._for_info[head_pos] = (init, incr, self._pos_of(incr))
            self._skip_ids.add(id(init))
            self._skip_ids.add(id(incr))
            self._for_continue_pos.add(self._pos_of(incr))

    def _analyze_do_until(self) -> None:
        """Detect ``do { … } until(cond)`` loops for the streaming pass.

        A do/until compiles to: a loop head, the body, then a *backward*
        ``JumpIfNot`` whose condition is the ``until`` test (it jumps back to
        the head while the condition is false). We reconstruct one only when
        the head is referenced solely by that back-edge (a clean single-entry
        loop) and the head is not itself a ``while`` head; otherwise the
        back-edge falls back to the ``if(!(cond)) goto`` rendering.
        """
        if not self.reconstruct_loops:
            return
        for idx, t in enumerate(self.tokens):
            if not isinstance(t, UnTokenJumpIfNot):
                continue
            target = t.offset & 0xFFFF
            pos = self._pos_of(t)
            if target >= pos:  # only backward conditional jumps
                continue
            if target in self._loop_heads:  # that's a while head, not a do head
                continue
            if self._token_at(target) is None:  # must land on a real statement
                continue
            # The head's synthetic label must be created *only* by this
            # back-edge, so suppressing it is safe. Only label-creating refs
            # count: unconditional Jumps and *backward* JumpIfNots. A *forward*
            # JumpIfNot that targets the head (e.g. a preceding `if(){}` whose
            # false branch falls through to the loop) is fine — it renders as an
            # `if` boundary, never a label/goto — so it is not counted.
            refs = 0
            for u in self.tokens:
                if type(u) is UnTokenJump and (u.offset & 0xFFFF) == target:
                    refs += 1
                elif isinstance(u, UnTokenJumpIfNot):
                    ut = u.offset & 0xFFFF
                    if ut == target and ut < u.icode_start:  # backward only
                        refs += 1
            if refs != 1:
                continue
            self._do_until_heads[target] = t
            self._do_until_ends[target] = pos + self._size_of_index(idx)
            self._do_until_back_ids.add(id(t))

    # ------------------------------------------------------------------ #
    #  Token-stream navigation helpers
    # ------------------------------------------------------------------ #

    def _pos_of(self, token: UnToken) -> int:
        """Return the bytecode start offset of a token.

        Args:
            token (UnToken): The token to locate.

        Returns:
            int: The token's ``icode_start`` byte offset.
        """
        return token.icode_start

    def _size_of_index(self, idx: int) -> int:
        """Return the byte span of the statement at a token index.

        Args:
            idx (int): Index into the token list.

        Returns:
            int: The statement's byte size, or 0 if out of range.
        """
        return self.sizes[idx] if 0 <= idx < len(self.sizes) else 0

    def _index_of(self, token: UnToken) -> int:
        """Return the list index of a token by identity.

        Args:
            token (UnToken): The token to find.

        Returns:
            int: The token's index, or -1 if not present.
        """
        for i, t in enumerate(self.tokens):
            if t is token:
                return i
        return -1

    def _token_at(self, offset: int) -> Optional[UnToken]:
        """Return the token starting at a given bytecode offset, if any.

        Args:
            offset (int): The bytecode offset to match.

        Returns:
            Optional[UnToken]: The token at that offset, or None.
        """
        for t in self.tokens:
            if t.icode_start == offset:
                return t
        return None

    # ------------------------------------------------------------------ #
    #  Labels
    # ------------------------------------------------------------------ #

    @staticmethod
    def _offset_label(offset: int) -> str:
        """Return the synthetic label name for a bytecode offset.

        Args:
            offset (int): The bytecode offset.

        Returns:
            str: A label of the form ``J0x<HEX>``.
        """
        return f"J0x{offset:02X}"

    def _add_label(self, name: str, position: int) -> None:
        """Append a label to the raw label list.

        Args:
            name (str): The label name.
            position (int): The bytecode offset the label marks.
        """
        self._labels.append(_Label(name=name, position=position))

    def _build_labels(self) -> None:
        """Collect goto/state labels the same way UELib's PostDeserialized does."""
        for t in self.tokens:
            if type(t) is UnTokenJump:
                # Under loop reconstruction the back-edge/continue jumps of a
                # reconstructed loop are elided, so their labels are dropped.
                if self.reconstruct_loops and (
                    t.offset in self._loop_heads or t.offset in self._for_continue_pos
                ):
                    continue
                self._add_label(self._offset_label(t.offset), t.offset)
            elif isinstance(t, UnTokenJumpIfNot):
                if (t.offset & 0xFFFF) < t.icode_start:
                    # A do/until back-edge implies its head via ``do {`` — the
                    # head label is elided, not emitted.
                    if self.reconstruct_loops and (
                        (t.offset & 0xFFFF) in self._do_until_heads
                    ):
                        continue
                    self._add_label(self._offset_label(t.offset), t.offset)
            elif isinstance(t, UnTokenLabelTable):
                for entry in t.entries:
                    if 0 <= entry.name_index < len(self.pkg.names):
                        nm = self.pkg.names[entry.name_index].name
                        if nm != "None":
                            self._add_label(nm, entry.icode)

        # Deduplicate by position, counting references.
        self._temp_labels = []
        for lbl in self._labels:
            existing = next(
                (t for t in self._temp_labels if t.position == lbl.position), None
            )
            if existing is None:
                self._temp_labels.append(_Label(lbl.name, lbl.position, 1))
            else:
                existing.refs += 1

    def _no_jump_label(self, offset: int) -> None:
        """Consume one reference to the label at an offset (jump inlined).

        When a jump is rendered inline (e.g. as ``break``/``continue``), its
        label reference is dropped, removing the label entirely once unused.

        Args:
            offset (int): The bytecode offset whose label reference to drop.
        """
        for i, lbl in enumerate(self._temp_labels):
            if lbl.position == offset:
                if lbl.refs <= 1:
                    self._temp_labels.pop(i)
                else:
                    lbl.refs -= 1
                return

    def _emit_label_for(self, token: UnToken) -> None:
        """Emit any pending label that marks a token's position.

        State labels are emitted flush-left; synthetic jump labels are
        indented to the current level.  The label is consumed once emitted.

        Args:
            token (UnToken): The token about to be rendered.
        """
        pos = self._pos_of(token)
        for i, lbl in enumerate(self._temp_labels):
            if lbl.position == pos:
                is_state_label = not lbl.name.startswith("J0x")
                if is_state_label:
                    self.lines.append(f"{lbl.name}:")
                else:
                    self.lines.append(f"{self.ind.tabs}{lbl.name}:")
                self._temp_labels.pop(i)
                return

    # ------------------------------------------------------------------ #
    #  Nest helpers
    # ------------------------------------------------------------------ #

    def _add_nest(
        self, ntype: str, begin: int, end: int, creator: Optional[UnToken] = None
    ) -> None:
        """Register a matching begin/end nest pair.

        Args:
            ntype (str): The nest type (e.g. ``"if"``, ``"loop"``).
            begin (int): The bytecode offset where the scope opens.
            end (int): The bytecode offset where the scope closes.
            creator (Optional[UnToken]): The token that opened the scope.
        """
        self.nests.append(_Nest("begin", ntype, begin, creator))
        self.nests.append(_Nest("end", ntype, end, creator))

    def _add_nest_begin(
        self, ntype: str, begin: int, creator: Optional[UnToken] = None
    ) -> None:
        """Register only the begin half of a nest.

        Args:
            ntype (str): The nest type.
            begin (int): The bytecode offset where the scope opens.
            creator (Optional[UnToken]): The token that opened the scope.
        """
        self.nests.append(_Nest("begin", ntype, begin, creator))

    def _try_add_nest_end(self, ntype: str, pos: int) -> bool:
        """Register a nest end unless one of that type already exists there.

        Args:
            ntype (str): The nest type to close.
            pos (int): The bytecode offset where the scope closes.

        Returns:
            bool: True if a new end was added, False if one already existed.
        """
        for n in self.nests:
            if n.type == ntype and n.position == pos:
                return False
        self.nests.append(_Nest("end", ntype, pos))
        return True

    def _is_in_nest(self, ntype: str) -> Optional[_Nest]:
        """Return the innermost open nest if it is of a given type.

        Args:
            ntype (str): The nest type to test against the innermost scope.

        Returns:
            Optional[_Nest]: The innermost nest if it matches, else None.
        """
        if not self.nest_chain:
            return None
        top = self.nest_chain[-1]
        return top if top.type == ntype else None

    def _is_within_nest(self, ntype: str) -> Optional[_Nest]:
        """Return the nearest open nest of a given type on the stack.

        Args:
            ntype (str): The nest type to search for.

        Returns:
            Optional[_Nest]: The matching open nest, or None.
        """
        for n in reversed(self.nest_chain):
            if n.type == ntype:
                return n
        return None

    def _emit(self, text: str) -> None:
        """Append a line of text indented to the current level.

        Args:
            text (str): The line content (without leading indentation).
        """
        self.lines.append(f"{self.ind.tabs}{text}")

    def _decompile_nests(self, current: UnToken, flush: bool = False) -> None:
        """Open/close pending nest scopes around the current statement.

        Emits braces and indentation as scope begins/ends are reached against
        the current statement's byte range, and materializes any ``else``
        block linked to a closing ``if``.

        Args:
            current (UnToken): The statement token just rendered.
            flush (bool): When True, force all remaining nests to open/close
                (used at end of stream).
        """
        cur_pos = self._pos_of(current)
        cur_end = (
            cur_pos + self.sizes[self.index]
            if 0 <= self.index < len(self.sizes)
            else cur_pos
        )

        # Open pending begins whose position we have reached.
        i = 0
        while i < len(self.nests):
            n = self.nests[i]
            if n.kind == "begin" and (flush or cur_pos >= n.position):
                if n.type not in ("case", "default"):
                    self._emit("{")
                self.ind.add()
                self.nest_chain.append(n)
                self.nests.pop(i)
                continue
            i += 1

        # Close ends whose position we have passed (reverse order).
        for i in range(len(self.nests) - 1, -1, -1):
            n = self.nests[i]
            if n.kind != "end":
                continue
            if not (flush or cur_end >= n.position):
                continue
            if not self.nest_chain:
                self.nests.pop(i)
                continue
            top = self.nest_chain[-1]

            # Auto-close a default case (and its switch) when an outer nest ends.
            if top.type == "default" and n.type != "default":
                self._emit("break;")
                self.ind.remove()
                self.nest_chain.pop()
                if (
                    n.type != "switch"
                    and self.nest_chain
                    and self.nest_chain[-1].type == "switch"
                ):
                    self.ind.remove()
                    self._emit("}")
                    self.nest_chain.pop()

            self.ind.remove()
            if n.type == "do":
                cond = self._expr(n.creator.condition) if n.creator else ""
                self._emit(f"}} until({cond});")
            elif n.type not in ("case", "default"):
                self._emit("}")
            if self.nest_chain:
                self.nest_chain.pop()
            self.nests.pop(i)

            if n.has_else is not None:
                self._emit("else")
                self._emit("{")
                self.ind.add()
                begin = _Nest("begin", "else", n.position, n.has_else)
                end = _Nest("end", "else", n.has_else.offset, n.has_else)
                self.nest_chain.append(begin)
                self.nests.append(end)

        # At end of stream, force-close any scopes still open. A switch whose
        # last (``default``) case runs to the end of the body never registers a
        # switch-end (there is no trailing break to mark it), so it would
        # otherwise leak an unclosed ``{`` and an over-indented outer block.
        if flush:
            for n in reversed(self.nest_chain):
                self.ind.remove()
                if n.type not in ("case", "default"):
                    self._emit("}")
            self.nest_chain.clear()

    # ------------------------------------------------------------------ #
    #  Main driver
    # ------------------------------------------------------------------ #

    def decompile(self) -> str:
        """Decompile the struct's token stream into UnrealScript statements.

        Runs loop/label analysis, then streams each token through the
        statement dispatcher, emitting labels, semicolons and nest braces.

        Returns:
            str: The newline-joined UnrealScript body, or "" if no tokens.
        """
        if not self.tokens:
            return ""
        self._compute_loop_heads()
        self._analyze_loops()
        self._analyze_do_until()
        self._build_labels()

        while self.index + 1 < len(self.tokens):
            self.index += 1
            token = self.tokens[self.index]
            self._emit_label_for(token)

            # --simplify: open a ``do {`` block just before the loop head of a
            # reconstructed do/until (the head is a plain body statement, so it
            # cannot be opened via the position-driven nest pass).
            head_pos = self._pos_of(token)
            if (
                self.reconstruct_loops
                and head_pos in self._do_until_heads
                and head_pos not in self._do_open_done
            ):
                self._do_open_done.add(head_pos)
                self._emit("do")
                self._emit("{")
                self.ind.add()
                back = self._do_until_heads[head_pos]
                self.nest_chain.append(_Nest("begin", "do", head_pos, back))
                self.nests.append(
                    _Nest("end", "do", self._do_until_ends[head_pos], back)
                )

            self._can_semicolon = False
            self.pre_comment = ""
            if self.reconstruct_loops and id(token) in self._skip_ids:
                # Init/increment statements are absorbed into the for-header.
                text = ""
            else:
                try:
                    text = self._stmt(token)
                except Exception as exc:  # pragma: no cover - defensive
                    text = f"/* decompile error: {exc} */"
                    self._can_semicolon = False

            if isinstance(token, (UnTokenEndOfScript, UnTokenNothing)):
                text = ""

            if self.pre_comment:
                for _cline in self.pre_comment.split("\n"):
                    self._emit(_cline)
            if text:
                line = text
                if self._can_semicolon:
                    line += ";"
                # Statements may already carry embedded newlines (e.g. the
                # inverted do/until pattern); indent only the first line.
                for j, piece in enumerate(line.split("\n")):
                    if j == 0:
                        self._emit(piece)
                    else:
                        self.lines.append(piece)

            self._decompile_nests(token)

        self._decompile_nests(self.tokens[self.index], flush=True)
        if self.simplify:
            self.lines = self._simplify_dead_after_return(self.lines)
            self.lines = self._simplify_empty_then(self.lines)
            self.lines = self._simplify_empty_else(self.lines)
            if self.reconstruct_loops:
                # After dead-code removal (so the back-edge ``goto`` is its
                # block's last statement) and after empty-then/else folding (so a
                # ``if(C){}else{BODY;goto}`` head is already ``if(!C){BODY;goto}``).
                self.lines = self._reconstruct_while_with_step(self.lines)
            self.lines = self._elide_single_statement_braces(self.lines)
        return "\n".join(self.lines)

    @classmethod
    def _negate_condition(cls, cond: str) -> str:
        """Return the logical negation of a rendered condition expression.

        Unwraps a single balanced outer ``!( … )`` instead of double-negating;
        inverts a single top-level comparison (``A == B`` -> ``A != B``) so the
        negation folds into the operator; otherwise wraps in ``!( … )``.
        """
        c = cond.strip()
        if c.startswith("!(") and c.endswith(")"):
            inner = c[2:-1]
            depth = 0
            for ch in inner:
                if ch == "(":
                    depth += 1
                elif ch == ")":
                    depth -= 1
                    if depth < 0:
                        break
            if depth == 0:  # the outer !( ) wrapped the whole expression
                return inner
        inverted = cls._invert_single_comparison(c)
        if inverted is not None:
            return inverted
        return f"!({cond})"

    # Operator scanning for _invert_single_comparison (longest match first).
    _NEG_MULTI_OPS = (
        "<<=",
        ">>=",
        "**",
        "<<",
        ">>",
        "==",
        "!=",
        "<=",
        ">=",
        "~=",
        "&&",
        "||",
        "$=",
        "@=",
        "+=",
        "-=",
        "*=",
        "/=",
        "%=",
    )
    _NEG_SINGLE_OPS = frozenset("+-*/%$@&|^<>!~=")

    @staticmethod
    def _is_paren_wrapped(s: str) -> bool:
        """Report whether ``s`` is a single balanced ``( … )`` around the whole."""
        if not (s.startswith("(") and s.endswith(")")):
            return False
        depth = 0
        for k, ch in enumerate(s):
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    return k == len(s) - 1
        return False

    @classmethod
    def _invert_single_comparison(cls, expr: str):
        """Invert ``A <cmp> B`` -> ``A <inv-cmp> B`` when safe, else ``None``.

        Fires only when the expression has exactly ONE top-level (paren/bracket
        depth 0, outside string/name literals) operator and it is a directly
        invertible comparison — so no ``!`` is introduced inside and short-circuit
        (``&&``/``||``) or non-invertible (``~=``) operators are left alone. Any
        extra top-level operator (incl. a unary ``-``) makes the count != 1 and
        conservatively bails, so a wrong inversion can never be produced.
        """
        s = expr.strip()
        while cls._is_paren_wrapped(s):
            s = s[1:-1].strip()
        ops = []
        depth = 0
        i = 0
        n = len(s)
        in_str = None
        while i < n:
            ch = s[i]
            if in_str is not None:
                if ch == "\\":
                    i += 2
                    continue
                if ch == in_str:
                    in_str = None
                i += 1
                continue
            if ch == '"' or ch == "'":
                in_str = ch
                i += 1
                continue
            if ch in "([":
                depth += 1
                i += 1
                continue
            if ch in ")]":
                depth -= 1
                i += 1
                continue
            if depth == 0 and ch in cls._NEG_SINGLE_OPS:
                matched = None
                for mo in cls._NEG_MULTI_OPS:
                    if s.startswith(mo, i):
                        matched = mo
                        break
                if matched:
                    ops.append((i, matched))
                    i += len(matched)
                    continue
                ops.append((i, ch))
                i += 1
                continue
            i += 1
        if len(ops) != 1:
            return None
        pos, op = ops[0]
        inv = cls._INVERSE_OPS.get(op)
        if inv is None:
            return None
        left = s[:pos].strip()
        right = s[pos + len(op) :].strip()
        if not left or not right:
            return None
        return f"{left} {inv} {right}"

    def _reconstruct_while_with_step(self, lines: List[str]) -> List[str]:
        """Fold a ``label: STEP; if(C){ BODY; goto label; }`` into a while loop.

        A back-edge whose target is a *statement* (not the ``JumpIfNot`` guard)
        is a while loop whose head recomputes one or more values before the
        test — i.e. the loop's init and step are the same statement(s). The
        token-level loop analysis only recognises heads that are the guard
        itself, so this shape streams out as a raw ``goto``/label; rewrite it::

            J0x6F:                          midIdx = (lo + hi) >> 1;
            midIdx = (lo + hi) >> 1;        while(!(midIdx == lo))
            if(!(midIdx == lo))      -->    {
            {                                   BODY
                BODY                            midIdx = (lo + hi) >> 1;
                goto J0x6F;                 }
            }

        The while condition is exactly the ``if`` condition; STEP is emitted
        once before the loop (init) and again at the end of the body (step).
        Only fires when the label is targeted by that single trailing ``goto``.
        """
        label_re = re.compile(r"^(\t*)(J0x[0-9A-Fa-f]+):$")
        _CTRL = ("if(", "for(", "while(", "foreach ", "else", "do", "switch")
        out: List[str] = []
        i, n = 0, len(lines)
        while i < n:
            m = label_re.match(lines[i])
            if m:
                ind, label = m.group(1), m.group(2)
                # Collect the STEP statements (siblings at `ind`) up to the guard.
                j = i + 1
                step: List[str] = []
                valid = True
                while j < n:
                    lj = lines[j]
                    if lj.startswith(ind + "if("):
                        break
                    if not lj.startswith(ind) or lj[len(ind) : len(ind) + 1] == "\t":
                        valid = False
                        break
                    c = lj[len(ind) :].strip()
                    if (
                        c == ""
                        or c.endswith(("{", ":"))
                        or c in ("{", "}")
                        or c.startswith("goto ")
                        or c.startswith(_CTRL)
                    ):
                        valid = False
                        break
                    step.append(lj)
                    j += 1
                if (
                    valid
                    and step
                    and j + 1 < n
                    and lines[j].startswith(ind + "if(")
                    and lines[j].rstrip().endswith(")")
                    and lines[j + 1] == ind + "{"
                ):
                    close = self._match_brace(lines, j + 1)
                    if close is not None:
                        gi = close - 1
                        while gi > j + 1 and lines[gi].strip() == "":
                            gi -= 1
                        goto = "goto " + label + ";"
                        if lines[gi].strip() == goto and (
                            sum(1 for L in lines if L.strip() == goto) == 1
                        ):
                            cond = lines[j].strip()[3:-1]
                            body = lines[j + 2 : gi]
                            out.extend(step)  # init
                            out.append(ind + "while(" + cond + ")")
                            out.append(ind + "{")
                            out.extend(body)
                            out.extend("\t" + s for s in step)  # step (one deeper)
                            out.append(ind + "}")
                            i = close + 1
                            continue
            out.append(lines[i])
            i += 1
        return out

    def _simplify_dead_after_return(self, lines: List[str]) -> List[str]:
        """Drop unreachable statements after an unconditional control transfer.

        Obfuscated packages insert junk after a ``return`` (and after
        ``break``/``continue``/``goto``, which in simplified output come from
        the same construct) — e.g. an unresolvable ``GetMapFileName()`` call —
        to trip up decompilers; the recompiler then rejects it. Everything after
        such a terminal statement up to the end of its block is unreachable, so
        it is removed. Removal stops at a lower indent (the enclosing block's
        ``}``) or at a reachable jump target — a ``J0x..:`` label or a
        ``case``/``default:`` — which must be preserved.
        """
        ret_re = re.compile(r"^(\s*)(?:return|break|continue|goto)\b.*;\s*$")
        target_re = re.compile(r"^\s*(?:J0x[0-9A-Fa-f]+:|case\b|default\s*:)")
        out: List[str] = []
        i, n = 0, len(lines)
        while i < n:
            out.append(lines[i])
            m = ret_re.match(lines[i])
            if m:
                ind = m.group(1)
                j = i + 1
                while j < n:
                    lj = lines[j]
                    if lj.strip() == "":  # blank line inside the dead region
                        j += 1
                        continue
                    ljws = re.match(r"^(\s*)", lj).group(1)
                    if len(ljws) < len(ind):  # dedented out of the block
                        break
                    if target_re.match(lj):  # reachable jump/switch target
                        break
                    j += 1  # unreachable statement -> drop
                i = j
                continue
            i += 1
        return out

    def _simplify_empty_else(self, lines: List[str]) -> List[str]:
        """Drop empty ``else { }`` clauses.

        An empty ``else`` is a no-op (``if(C){body}else{}`` == ``if(C){body}``).
        It also removes the invalid *doubled* ``else`` scaffolding left when a
        break-target label that lived inside an ``else`` block is elided under
        ``--simplify`` (its ``goto`` having become a ``break``), which would
        otherwise be a compile error ("'Else' is not allowed here").
        """
        else_re = re.compile(r"^(\s*)else$")
        out: List[str] = []
        i, n = 0, len(lines)
        while i < n:
            m = else_re.match(lines[i])
            if m and i + 2 < n:
                ind = m.group(1)
                if lines[i + 1] == ind + "{" and lines[i + 2] == ind + "}":
                    i += 3
                    continue
            out.append(lines[i])
            i += 1
        return out

    def _simplify_empty_then(self, lines: List[str]) -> List[str]:
        """Fold ``if(C){}else{X}`` into ``if(!(C)){X}`` (drop the empty then-block).

        A compiler often emits a mid-test loop / guard as an empty then-branch
        with the real work in the ``else``; inverting the condition and dropping
        the empty branch is equivalent and far more readable (and exposes the
        loop body directly under a single ``if``).
        """
        if_re = re.compile(r"^(\s*)if\((.*)\)$")
        out: List[str] = []
        i, n = 0, len(lines)
        while i < n:
            m = if_re.match(lines[i])
            if m and i + 4 < n:
                ind, cond = m.group(1), m.group(2)
                if (
                    lines[i + 1] == ind + "{"
                    and lines[i + 2] == ind + "}"
                    and lines[i + 3] == ind + "else"
                    and lines[i + 4] == ind + "{"
                ):
                    out.append(f"{ind}if({self._negate_condition(cond)})")
                    out.append(ind + "{")
                    i += 5
                    continue
            out.append(lines[i])
            i += 1
        return out

    # Headers whose single-statement body may shed its braces. ``else if`` is
    # deliberately absent: its braces are kept (see _merge_else_if), matching the
    # convention that only a leading/standalone header goes braceless.
    _BRACE_ELIDE_HDR_RE = re.compile(
        r"^(\t*)(?:if\(.*\)|for\(.*\)|while\(.*\)|foreach .+|else)$"
    )
    # A body that must NOT go braceless: another control construct (dangling-else
    # / ambiguity risk), a jump target, a brace, or a comment. A genuine leaf
    # block (``{`` / one line / ``}``) can only hold a simple statement, but this
    # is belt-and-suspenders.
    _NON_SIMPLE_BODY_RE = re.compile(
        r"^(?:if\(|for\(|while\(|foreach |else\b|do\b|switch\b|case\b|default\b"
        r"|J0x[0-9A-Fa-f]+:|\{|\}|//|/\*)"
    )

    def _match_brace(self, lines: List[str], open_idx: int) -> Optional[int]:
        """Return the line index of the ``}`` matching the ``{`` at ``open_idx``.

        Braces are emitted on their own lines; a ``do`` block closes with
        ``} until(...);``. Returns None if unbalanced.
        """
        depth = 0
        for k in range(open_idx, len(lines)):
            s = lines[k].strip()
            if s == "{":
                depth += 1
            elif s == "}" or s.startswith("} until("):
                depth -= 1
                if depth == 0:
                    return k
        return None

    def _stmt_extent(self, lines: List[str], start: int, indent: str) -> int:
        """Return the index just past the single statement starting at ``start``.

        ``indent`` is the leading-tab prefix of the statement's header. Handles a
        braced or braceless control statement plus any ``else``/``else if`` chain
        hanging off it at the same indent; a plain simple statement is one line.
        """
        n = len(lines)
        k = start + 1
        if k < n and lines[k] == indent + "{":
            c = self._match_brace(lines, k)
            k = (c + 1) if c is not None else n
        elif not self._BRACE_ELIDE_HDR_RE.match(lines[start]) and not lines[
            start
        ].startswith(indent + "else if("):
            # simple (non-control) statement: exactly this one line.
            return start + 1
        else:
            # braceless control body: a single deeper statement.
            k = self._stmt_extent(lines, start + 1, indent + "\t")
        # Follow an else / else-if chain at the same indent.
        while k < n:
            if lines[k] == indent + "else":
                k2 = k + 1
                if k2 < n and lines[k2] == indent + "{":
                    c = self._match_brace(lines, k2)
                    k = (c + 1) if c is not None else n
                else:
                    k = self._stmt_extent(lines, k2, indent + "\t") if k2 < n else k2
                break  # a bare else terminates the chain
            if lines[k].startswith(indent + "else if("):
                k2 = k + 1
                if k2 < n and lines[k2] == indent + "{":
                    c = self._match_brace(lines, k2)
                    k = (c + 1) if c is not None else n
                else:
                    k = self._stmt_extent(lines, k2, indent + "\t") if k2 < n else k2
                continue
            break
        return k

    def _merge_else_if(self, lines: List[str]) -> List[str]:
        """Collapse ``else { if(C) … }`` into ``else if(C) …``.

        Only fires when the ``else`` block's *sole* content is a single ``if``
        statement (with any of its own else-chain). The inner ``if`` is dedented
        one level and joined onto the ``else``; its braces are preserved, so no
        dangling-else ambiguity is introduced.
        """
        else_re = re.compile(r"^(\t*)else$")
        out: List[str] = []
        i, n = 0, len(lines)
        while i < n:
            m = else_re.match(lines[i])
            if m and i + 2 < n and lines[i + 1] == m.group(1) + "{":
                ind = m.group(1)
                inner = ind + "\t"
                close = self._match_brace(lines, i + 1)
                if (
                    close is not None
                    and lines[close] == ind + "}"
                    and lines[i + 2].startswith(inner + "if(")
                ):
                    ext = self._stmt_extent(lines, i + 2, inner)
                    j = ext
                    while j < close and lines[j].strip() == "":
                        j += 1
                    if j == close:  # the if-statement is the whole else body
                        merged = [
                            (ln[1:] if ln.startswith("\t") else ln)
                            for ln in lines[i + 2 : ext]
                        ]
                        merged[0] = ind + "else " + merged[0][len(ind) :]
                        out.extend(merged)
                        i = close + 1
                        continue
            out.append(lines[i])
            i += 1
        return out

    def _elide_single_statement_braces(self, lines: List[str]) -> List[str]:
        """Drop the braces around single-simple-statement control bodies.

        A ``if(C)`` / ``for(…)`` / ``while(…)`` / ``foreach …`` / ``else`` whose
        block holds exactly one *simple* (non-control) statement renders more
        readably without braces::

            if(C)             if(C)
            {          -->        DoThing();
                DoThing();
            }

        Braces are only shed around a simple statement, so this never creates a
        dangling ``else`` (a nested ``if`` keeps its braces). ``else { if … }`` is
        handled first by _merge_else_if, and ``else if`` keeps its braces.
        """
        lines = self._merge_else_if(lines)
        out: List[str] = []
        i, n = 0, len(lines)
        while i < n:
            m = self._BRACE_ELIDE_HDR_RE.match(lines[i])
            if m and i + 3 < n:
                ind = m.group(1)
                body = ind + "\t"
                if (
                    lines[i + 1] == ind + "{"
                    and lines[i + 3] == ind + "}"
                    and lines[i + 2].startswith(body)
                    and not self._NON_SIMPLE_BODY_RE.match(lines[i + 2][len(ind) + 1 :])
                ):
                    out.append(lines[i])  # header
                    out.append(lines[i + 2])  # the single simple statement
                    i += 4
                    continue
            out.append(lines[i])
            i += 1
        return out

    def _stmt(self, token: UnToken) -> str:
        """Decompile a statement-level token (may register nests).

        Args:
            token (UnToken): The statement token to render.

        Returns:
            str: The rendered UnrealScript statement.
        """
        return self._expr(token, statement=True)

    def _resolve_item_name(self, ref: int) -> str:
        """Resolve an object reference to its object name.

        Args:
            ref (int): A package item reference.

        Returns:
            str: The object's name, or ``"None"`` if unresolved.
        """
        item = resolve_item(self.pkg, ref)
        if item is None:
            return "None"
        name = item.object_name.name
        # Delegate references/assignments target the compiler-generated backing
        # property `__<Delegate>__Delegate`; UnrealScript source uses the
        # delegate name itself (`OnClick = Handler`, not
        # `__OnClick__Delegate = Handler`). Strip the mangling.
        if (
            name.startswith("__")
            and name.endswith("__Delegate")
            and item.class_name_string.split(".")[-1] == "DelegateProperty"
        ):
            return name[2 : -len("__Delegate")]
        return name

    def _resolve_item(self, ref: int):
        """Resolve an object reference to its package item.

        Args:
            ref (int): A package item reference.

        Returns:
            The resolved package item, or None if unresolved.
        """
        return resolve_item(self.pkg, ref)

    def _resolve_name(self, name_index: int) -> str:
        """Resolve a name-table index to its string.

        Args:
            name_index (int): Index into the package name table.

        Returns:
            str: The name string, or ``"None"`` if out of range.
        """
        if 0 <= name_index < len(self.pkg.names):
            return self.pkg.names[name_index].name
        return "None"

    def _coerce_operand(
        self,
        token: UnToken,
        is_coerce: bool,
        precedence: bool,
        parent_group: Optional[str] = None,
        is_left: bool = False,
    ) -> str:
        """Render an argument, dropping a ``*ToString`` cast on a coerce param.

        String operators/functions declared with ``coerce`` parameters
        auto-convert their arguments, so the compiler-inserted
        ``string(...)`` cast is redundant in source.

        Args:
            token (UnToken): The argument expression token.
            is_coerce (bool): Whether the parameter has the CoerceParm flag.
            precedence (bool): Whether to parenthesize by operator precedence.
            parent_group (Optional[str]): The enclosing binary operator's group
                (see :meth:`_op_group`), for same-operator paren elision.
            is_left (bool): Whether this is the enclosing operator's left operand.

        Returns:
            str: The rendered argument expression.
        """
        if (
            self.simplify
            and is_coerce
            and isinstance(token, UnTokenPrimitiveCast)
            and _CAST_TYPE_NAMES.get(token.cast_type) == "string"
            and not self._expr_is_array(token.expression)
        ):
            inner = token.expression
            return (
                self._precedence_operand(inner, parent_group, is_left)
                if precedence
                else self._expr(inner)
            )
        return (
            self._precedence_operand(token, parent_group, is_left)
            if precedence
            else self._expr(token)
        )

    def _params_text(
        self, params: List[UnToken], coerce: tuple = (), enums: tuple = ()
    ) -> str:
        """Render a comma-separated argument list, honouring coerce flags.

        Trailing empty (omitted optional) arguments are dropped.

        Args:
            params (List[UnToken]): The parameter tokens (terminated by an
                EndFunctionParms token).
            coerce (tuple): Per-parameter CoerceParm flags.
            enums (tuple): Per-parameter enum type (or None); an integer-constant
                argument to an enum parameter renders as the enum member name.

        Returns:
            str: The rendered ``arg1, arg2, ...`` text.
        """
        rendered: List[str] = []
        idx = 0
        # Arguments are in the caller's scope, never the callee's context, so a
        # context member's argument list must not inherit the context flag.
        prev_ctx = self.within_context
        self.within_context = False
        for p in params:
            if isinstance(p, UnTokenEndFunctionParms):
                break
            is_coerce = idx < len(coerce) and coerce[idx]
            text = None
            enum = enums[idx] if idx < len(enums) else None
            if enum is not None:
                value = self._const_int(p)
                if value is not None:
                    text = self._enum_member(enum, value)
            if text is None:
                text = self._coerce_operand(p, is_coerce, precedence=False)
            rendered.append(text)
            idx += 1
        self.within_context = prev_ctx
        while rendered and rendered[-1] == "":
            rendered.pop()
        return ", ".join(rendered)

    def _is_operator_call(self, token: UnToken) -> Optional[_NativeInfo]:
        """Return the native info if a token is a native operator/function call.

        Args:
            token (UnToken): The token to inspect.

        Returns:
            Optional[_NativeInfo]: The native metadata, or None.
        """
        if isinstance(token, (UnTokenNativeFunction, UnTokenExtendedNativeFunction)):
            return self.natives.get(token.native_index)
        return None

    # Operators that reassociate, so a same-operator right operand needs no
    # parentheses (``A + (B + C)`` == ``A + B + C``; float rounding differences
    # are acceptable). Non-associative subtraction/division/modulo/shifts are
    # absent — their right operand keeps its parens (``A - (B - C)`` differs).
    # ``$``/``@`` (concat) reassociate and share one group (see _op_group).
    _ASSOCIATIVE_OPS = frozenset({"||", "&&", "^^", "+", "*", "&", "|", "^", "$", "@"})

    @staticmethod
    def _op_group(symbol: str) -> str:
        """Return the paren-elision group of a binary operator symbol.

        ``$`` and ``@`` (string concat) share one group — they have equal
        precedence and reassociate together — so mixed concat chains lose their
        redundant parentheses. Every other operator is its own group.
        """
        return "concat" if symbol in ("$", "@") else symbol

    def _precedence_operand(
        self,
        token: UnToken,
        parent_group: Optional[str] = None,
        is_left: bool = False,
    ) -> str:
        """Wrap an operand in parentheses when the reference decompiler would.

        A binary-operator operand is parenthesised, EXCEPT a same-operator-group
        chain where dropping the parens keeps the value: the *left* operand is
        always safe (operators are left-associative -> identical parse), and the
        *right* operand is safe when the group reassociates (see
        :data:`_ASSOCIATIVE_OPS`; float rounding differences are accepted). So
        ``((A || B) || C)`` and ``(A || (B || C))`` both render ``A || B || C``,
        and ``((A $ B) @ C)`` renders ``A $ B @ C`` — but ``((A && B) || C)`` (a
        different group) and ``A - (B - C)`` (non-associative right) keep theirs.

        Args:
            token (UnToken): The operand expression token.
            parent_group (Optional[str]): The enclosing operator's group, or None
                when there is no enclosing binary operator.
            is_left (bool): Whether this is the enclosing operator's left operand.

        Returns:
            str: The rendered operand, parenthesized when required.
        """
        text = self._expr(token)
        # Decide parenthesisation from the token that actually determines the
        # rendered form: a cast elided by --simplify (implicit widening, a
        # redundant pair, or an unnamed cast) renders as its bare inner
        # expression, so an inner binary operator would otherwise be exposed
        # unparenthesised (e.g. ``float(a + b) % 7`` -> ``a + b % 7``, which
        # reparses as ``a + (b % 7)`` since ``%`` binds tighter than ``+``).
        eff = self._effective_operand_token(token)
        wrap = False
        if isinstance(eff, (UnTokenNativeFunction, UnTokenExtendedNativeFunction)):
            info = self.natives.get(eff.native_index)
            if info is not None and info.kind == "binary":
                wrap = True
                # Same operator group chained: drop the redundant parens when the
                # value is preserved — a left operand always (left-associative),
                # a right operand only when the group reassociates.
                if (
                    parent_group is not None
                    and self._op_group(info.name) == parent_group
                    and (
                        is_left
                        or parent_group == "concat"
                        or parent_group in self._ASSOCIATIVE_OPS
                    )
                ):
                    wrap = False
        elif isinstance(eff, UnTokenFinalFunction):
            item = self._resolve_item(eff.function_ref)
            fn = item.object if item else None
            wrap = isinstance(fn, UnFunction) and bool(
                fn.function_flags & UnFunctionFlags.Operator
            )
        elif isinstance(
            eff,
            (UnTokenVirtualFunction, UnTokenGlobalFunction, UnTokenDelegateFunction),
        ):
            wrap = True
        return f"({text})" if wrap else text

    def _effective_operand_token(self, token: UnToken) -> UnToken:
        """Follow through primitive casts that render as just their inner text.

        A cast that ``--simplify`` elides (implicit widening, redundant pair, or
        an unnamed cast type) renders as its bare inner expression, so precedence
        decisions must see that inner operator rather than the cast wrapper. A
        cast that renders as ``type(expr)`` (named, kept) is self-parenthesizing
        and is returned as-is, as is a cast folded to a literal constant.

        Args:
            token (UnToken): The operand token to unwrap.

        Returns:
            UnToken: The token that actually determines the rendered form.
        """
        # Some tokens render as *just* their inner expression (no wrapper text):
        # a ``Skip`` (the ``&&``/``||`` short-circuit marker), an ``EatString``,
        # and a ``BoolVariable``. Like an elided cast, they expose the inner
        # operator to the parent, so precedence decisions must see through them —
        # otherwise a ``Skip``-wrapped ``||`` operand of ``&&`` renders without
        # parentheses and, since ``&&`` binds tighter than ``||``, recompiles to
        # a different tree (``A && (X || Y)`` -> ``(A && X) || Y``).
        while True:
            if isinstance(token, (UnTokenSkip, UnTokenEatString, UnTokenBoolVariable)):
                token = token.expression
                continue
            if not isinstance(token, UnTokenPrimitiveCast):
                break
            if self.simplify:
                if self._const_value(token) is not None:
                    return token  # renders as a literal constant
                inner = token.expression
                if isinstance(inner, UnTokenPrimitiveCast) and (
                    self._is_redundant_cast_pair(token.cast_type, inner.cast_type)
                ):
                    token = inner.expression
                    continue
                if token.cast_type in _IMPLICIT_WIDENING_CASTS:
                    token = token.expression
                    continue
            if token.cast_type in _CAST_TYPE_NAMES:
                return token  # renders as `type(expr)` — self-parenthesizing
            token = token.expression  # unnamed cast: renders bare inner
        return token

    def _is_binary_operator_token(self, token: UnToken) -> bool:
        """Report whether a token is a binary-operator expression.

        A prefix/postfix operator binds tighter than any binary operator, so a
        binary-operator operand must be parenthesized: ``!(a ~= b)`` — not
        ``!a ~= b``, which parses as ``(!a) ~= b`` and applies ``!`` to the
        wrong (non-bool) operand.

        Args:
            token (UnToken): The operand token to classify.

        Returns:
            bool: True if the token renders as a binary-operator expression.
        """
        token = self._effective_operand_token(token)
        if isinstance(token, (UnTokenNativeFunction, UnTokenExtendedNativeFunction)):
            info = self.natives.get(token.native_index)
            return info is not None and info.kind == "binary"
        if isinstance(token, UnTokenFinalFunction):
            item = self._resolve_item(token.function_ref)
            fn = item.object if item else None
            if isinstance(fn, UnFunction) and (
                fn.function_flags & UnFunctionFlags.Operator
            ):
                is_pre = bool(fn.function_flags & UnFunctionFlags.PreOperator)
                is_post = fn.operator_precedence == 0
                return not is_pre and not is_post
        return False

    def _call_name(self, name: str) -> str:
        """Prefix a call name with ``static.`` inside a class context.

        Consumes the pending class-context flag when present.

        Args:
            name (str): The bare call name.

        Returns:
            str: The (possibly ``static.``-prefixed) call name.
        """
        if self.within_class_context:
            name = f"static.{name}"
            self.within_class_context = False
        return name

    def _expr(self, token: Optional[UnToken], statement: bool = False) -> str:
        """Render any expression token via the dispatch table.

        Args:
            token (Optional[UnToken]): The token to render, or None.
            statement (bool): True when rendered at statement level.

        Returns:
            str: The rendered UnrealScript, or a ``/* ... */`` comment for an
                unhandled token type, or "" for None.
        """
        if token is None:
            return ""
        handler = _EXPR_DISPATCH.get(type(token))
        if handler is None:
            return f"/* {type(token).__name__} */"
        return handler(self, token, statement)

    # -- individual token handlers ------------------------------------- #

    def _h_nothing(self, t, s):
        """Render a no-op token as empty text.

        Args:
            t (UnToken): The token being rendered.
            s (bool): The statement flag.

        Returns:
            str: An empty string.
        """
        return ""

    def _h_int_zero(self, t, s):
        """Render the integer-zero token.

        Args:
            t (UnToken): The token being rendered.
            s (bool): The statement flag.

        Returns:
            str: The literal ``"0"``.
        """
        return "0"

    def _h_int_one(self, t, s):
        """Render the integer-one token.

        Args:
            t (UnToken): The token being rendered.
            s (bool): The statement flag.

        Returns:
            str: The literal ``"1"``.
        """
        return "1"

    def _h_true(self, t, s):
        """Render the boolean-true token.

        Args:
            t (UnToken): The token being rendered.
            s (bool): The statement flag.

        Returns:
            str: The literal ``"true"``.
        """
        return "true"

    def _h_false(self, t, s):
        """Render the boolean-false token.

        Args:
            t (UnToken): The token being rendered.
            s (bool): The statement flag.

        Returns:
            str: The literal ``"false"``.
        """
        return "false"

    def _h_self(self, t, s):
        """Render the ``self`` reference token.

        Args:
            t (UnToken): The token being rendered.
            s (bool): The statement flag.

        Returns:
            str: The literal ``"self"``.
        """
        return "self"

    def _h_no_object(self, t, s):
        """Render the null-object token.

        Args:
            t (UnToken): The token being rendered.
            s (bool): The statement flag.

        Returns:
            str: The literal ``"none"``.
        """
        return "none"

    def _h_int_const(self, t, s):
        """Render an integer constant token.

        Args:
            t (UnToken): The integer-constant token being rendered.
            s (bool): The statement flag.

        Returns:
            str: The decimal value.
        """
        return str(t.value)

    def _h_byte_const(self, t, s):
        """Render a byte constant token.

        Args:
            t (UnToken): The byte-constant token being rendered.
            s (bool): The statement flag.

        Returns:
            str: The decimal value.
        """
        return str(t.value)

    def _h_float_const(self, t, s):
        """Render a float constant token.

        Args:
            t (UnToken): The float-constant token being rendered.
            s (bool): The statement flag.

        Returns:
            str: The float rendered as an UnrealScript literal.
        """
        return _format_float(t.value)

    def _h_string_const(self, t, s):
        """Render a string constant token.

        Args:
            t (UnToken): The string-constant token being rendered.
            s (bool): The statement flag.

        Returns:
            str: The escaped, double-quoted string literal.
        """
        return _format_string(t.value)

    def _h_name_const(self, t, s):
        """Render a name constant token as a quoted name literal.

        Args:
            t (UnToken): The name-constant token being rendered.
            s (bool): The statement flag.

        Returns:
            str: The name wrapped in single quotes.
        """
        return f"'{self._resolve_name(t.name_index)}'"

    def _h_object_const(self, t, s):
        """Render an object constant as ``Class'ObjectName'``.

        Args:
            t (UnToken): The object-constant token being rendered.
            s (bool): The statement flag.

        Returns:
            str: The object literal, or ``"none"`` if unresolved.
        """
        item = self._resolve_item(t.object_ref)
        if item is None:
            return "none"
        cls = item.class_name_string.split(".")[-1] or "Object"
        return f"{cls}'{item.object_name_string}'"

    def _h_rotation_const(self, t, s):
        """Render a rotation constant as ``rot(pitch, yaw, roll)``.

        Args:
            t (UnToken): The rotation-constant token being rendered.
            s (bool): The statement flag.

        Returns:
            str: The ``rot(...)`` literal.
        """
        return f"rot({t.pitch}, {t.yaw}, {t.roll})"

    def _h_vector_const(self, t, s):
        """Render a vector constant as ``vect(x, y, z)``.

        Args:
            t (UnToken): The vector-constant token being rendered.
            s (bool): The statement flag.

        Returns:
            str: The ``vect(...)`` literal.
        """
        return f"vect({_format_float(t.x)}, {_format_float(t.y)}, {_format_float(t.z)})"

    def _h_variable(self, t, s):
        """Render a local/instance/native-parm variable reference.

        Args:
            t (UnToken): The variable token being rendered.
            s (bool): The statement flag.

        Returns:
            str: The variable's name.
        """
        return self._resolve_item_name(t.object_ref)

    def _h_default_variable(self, t, s):
        """Render a default-property variable as ``default.Name``.

        Args:
            t (UnToken): The default-variable token being rendered.
            s (bool): The statement flag.

        Returns:
            str: The ``default.<Name>`` reference.
        """
        return f"default.{self._resolve_item_name(t.object_ref)}"

    def _h_delegate_property(self, t, s):
        """Render a delegate-property reference by its function name.

        Args:
            t (UnToken): The delegate-property token being rendered.
            s (bool): The statement flag.

        Returns:
            str: The delegate function name.
        """
        return self._resolve_name(t.function_name)

    def _h_bool_variable(self, t, s):
        """Render a bool-variable token by unwrapping its inner expression.

        Args:
            t (UnToken): The bool-variable token being rendered.
            s (bool): The statement flag.

        Returns:
            str: The rendered inner expression.
        """
        return self._expr(t.expression)

    def _h_let(self, t, s):
        """Render an assignment as ``variable = value``.

        Args:
            t (UnToken): The let/let-bool/let-delegate token being rendered.
            s (bool): The statement flag.

        Returns:
            str: The rendered assignment (semicolon-eligible).
        """
        self._can_semicolon = True
        lhs = self._expr(t.variable)
        # Assigning an integer/byte constant to an enum-typed target must render
        # the enum member name (`Role = ROLE_SimulatedProxy`, not `Role = 2`);
        # UnrealScript rejects a raw int assigned to an enum property.
        enum = self._enum_of_expr(t.variable)
        if enum is not None:
            value = self._const_int(t.assignment)
            if value is not None:
                member = self._enum_member(enum, value)
                if member is not None:
                    return f"{lhs} = {member}"
        return f"{lhs} = {self._expr(t.assignment)}"

    def _h_context(self, t, s):
        """Render a member-access context as ``object.member``.

        Args:
            t (UnToken): The context token being rendered.
            s (bool): The statement flag.

        Returns:
            str: The ``object.member`` expression.
        """
        obj = self._expr(t.object_expr)
        prev = self.within_context
        self.within_context = True
        ctx = self._expr(t.context_expr)
        self.within_context = prev
        return f"{obj}.{ctx}"

    def _h_class_context(self, t, s):
        """Render a class-context access as ``object.static.member``.

        Sets the class-context flag so nested calls render as ``static.``.

        Args:
            t (UnToken): The class-context token being rendered.
            s (bool): The statement flag.

        Returns:
            str: The ``object.member`` expression.
        """
        obj = self._expr(t.object_expr)
        self.within_class_context = True
        ctx = self._expr(t.context_expr)
        self.within_class_context = False
        return f"{obj}.{ctx}"

    def _h_struct_member(self, t, s):
        """Render a struct member access as ``inner.Member``.

        Args:
            t (UnToken): The struct-member token being rendered.
            s (bool): The statement flag.

        Returns:
            str: The ``inner.Member`` expression.
        """
        return f"{self._expr(t.inner_expr)}.{self._resolve_item_name(t.property_ref)}"

    def _h_array_element(self, t, s):
        """Render an array element access as ``base[index]``.

        Args:
            t (UnToken): The array/dyn-array element token being rendered.
            s (bool): The statement flag.

        Returns:
            str: The ``base[index]`` expression.
        """
        return f"{self._expr(t.base_expr)}[{self._expr(t.index_expr)}]"

    def _h_dynarray_length(self, t, s):
        """Render a dynamic-array length access as ``base.Length``.

        Args:
            t (UnToken): The dyn-array-length token being rendered.
            s (bool): The statement flag.

        Returns:
            str: The ``base.Length`` expression.
        """
        return f"{self._expr(t.base_expr)}.Length"

    def _h_dynarray_insert(self, t, s):
        """Render a dynamic-array insert as ``base.Insert(index, count)``.

        Args:
            t (UnToken): The dyn-array-insert token being rendered.
            s (bool): The statement flag.

        Returns:
            str: The ``base.Insert(...)`` call (semicolon-eligible).
        """
        self._can_semicolon = True
        return (
            f"{self._expr(t.base_expr)}.Insert("
            f"{self._expr(t.index_expr)}, {self._expr(t.count_expr)})"
        )

    def _h_dynarray_remove(self, t, s):
        """Render a dynamic-array remove as ``base.Remove(index, count)``.

        Args:
            t (UnToken): The dyn-array-remove token being rendered.
            s (bool): The statement flag.

        Returns:
            str: The ``base.Remove(...)`` call (semicolon-eligible).
        """
        self._can_semicolon = True
        return (
            f"{self._expr(t.base_expr)}.Remove("
            f"{self._expr(t.index_expr)}, {self._expr(t.count_expr)})"
        )

    def _h_struct_cmp_eq(self, t, s):
        """Render a struct equality comparison as ``left == right``.

        Args:
            t (UnToken): The struct-compare-equal token being rendered.
            s (bool): The statement flag.

        Returns:
            str: The ``left == right`` expression.
        """
        return f"{self._expr(t.left_expr)} == {self._expr(t.right_expr)}"

    def _h_struct_cmp_ne(self, t, s):
        """Render a struct inequality comparison as ``left != right``.

        Args:
            t (UnToken): The struct-compare-not-equal token being rendered.
            s (bool): The statement flag.

        Returns:
            str: The ``left != right`` expression.
        """
        return f"{self._expr(t.left_expr)} != {self._expr(t.right_expr)}"

    def _literal_value(self, token: UnToken):
        """Return ``(kind, value)`` for a literal token, else ``None``.

        Used to fold casts of constants (e.g. ``float(false)`` -> ``0.0``).

        Args:
            token (UnToken): The candidate literal token.

        Returns:
            A ``(kind, value)`` tuple where kind is ``"bool"``, ``"int"`` or
            ``"float"``, or None if the token is not a literal.
        """
        if isinstance(token, UnTokenTrue):
            return ("bool", True)
        if isinstance(token, UnTokenFalse):
            return ("bool", False)
        if isinstance(token, UnTokenIntOne):
            return ("int", 1)
        if isinstance(token, UnTokenIntZero):
            return ("int", 0)
        if isinstance(token, (UnTokenIntConst, UnTokenIntConstByte, UnTokenByteConst)):
            return ("int", int(token.value))
        if isinstance(token, UnTokenFloatConst):
            return ("float", float(token.value))
        return None

    def _const_value(self, token: Optional[UnToken]):
        """Return ``(kind, value)`` for a compile-time-constant expression.

        Recurses through casts, so ``int(bool(0))`` folds all the way to
        ``("int", 0)``.

        Args:
            token (Optional[UnToken]): The candidate constant expression.

        Returns:
            A ``(kind, value)`` tuple where kind is ``"bool"``, ``"int"`` or
            ``"float"``, or None for anything not constant-foldable.
        """
        lit = self._literal_value(token)
        if lit is not None:
            return lit
        if isinstance(token, UnTokenPrimitiveCast):
            inner = self._const_value(token.expression)
            if inner is None:
                return None
            target = _CAST_TYPE_NAMES.get(token.cast_type)
            value = inner[1]
            if target == "bool":
                return ("bool", bool(value))
            if target in ("int", "byte"):
                folded = int(value) & 0xFF if target == "byte" else int(value)
                return ("int", folded)
            if target == "float":
                return ("float", float(value))
        if self.simplify and isinstance(
            token, (UnTokenNativeFunction, UnTokenExtendedNativeFunction)
        ):
            info = self.natives.get(token.native_index)
            if (
                info is not None
                and info.kind == "binary"
                and (info.name in self._FOLD_BIN_OPS)
            ):
                ops = self._operands(token.params)
                if len(ops) == 2:
                    a = self._const_value(ops[0])
                    b = self._const_value(ops[1])
                    if a is not None and b is not None:
                        return self._fold_const_binary(info.name, a, b)
            if info is not None and info.kind == "pre" and info.name == "-":
                ops = self._operands(token.params)
                if len(ops) == 1:
                    a = self._const_value(ops[0])
                    if a is not None and a[0] != "bool":
                        if a[0] == "float":
                            return ("float", -float(a[1]))
                        return ("int", self._wrap_int(-int(a[1])))
        return None

    # Arithmetic operators folded to a literal under --simplify. String
    # (``$``/``@``), comparison and bitwise operators are deliberately excluded.
    _FOLD_BIN_OPS = frozenset({"+", "-", "*", "/"})

    @staticmethod
    def _wrap_int(value: int) -> int:
        """Wrap an integer to the engine's signed 32-bit width."""
        return ((int(value) + 0x80000000) & 0xFFFFFFFF) - 0x80000000

    def _fold_const_binary(self, name: str, a, b):
        """Fold ``a <op> b`` for two constant ``(kind, value)``s, or ``None``.

        Follows UnrealScript arithmetic: float if either operand is float
        (else int with 32-bit wrap), integer ``/`` truncates toward zero, and
        division by zero is left unfolded.
        """
        ak, av = a
        bk, bv = b
        if ak == "bool" or bk == "bool":
            return None
        if ak == "float" or bk == "float":
            x, y = float(av), float(bv)
            if name == "+":
                return ("float", x + y)
            if name == "-":
                return ("float", x - y)
            if name == "*":
                return ("float", x * y)
            if name == "/":
                return ("float", x / y) if y != 0.0 else None
            return None
        x, y = int(av), int(bv)
        if name == "+":
            r = x + y
        elif name == "-":
            r = x - y
        elif name == "*":
            r = x * y
        elif name == "/":
            if y == 0:
                return None
            q = abs(x) // abs(y)  # truncate toward zero
            r = -q if (x < 0) != (y < 0) else q
        else:
            return None
        return ("int", self._wrap_int(r))

    @staticmethod
    def _format_const_value(cv) -> str:
        """Render a folded constant value as UnrealScript source.

        Args:
            cv: A ``(kind, value)`` tuple from :meth:`_const_value`.

        Returns:
            str: The value rendered as ``true``/``false``, a float, or an int.
        """
        kind, value = cv
        if kind == "bool":
            return "true" if value else "false"
        if kind == "float":
            return _format_float(float(value))
        return str(int(value))

    def _is_redundant_cast_pair(self, outer_type: int, inner_type: int) -> bool:
        """Report whether ``outer(inner(x))`` cancels back to ``x`` losslessly.

        Only round-trips through a wider type that preserve the value are
        removed (``bool``/``byte`` sources), so e.g. ``bool(int(bExpr))``
        collapses but ``int(float(x))`` is left intact.

        Args:
            outer_type (int): The outer cast opcode.
            inner_type (int): The inner cast opcode.

        Returns:
            bool: True if the cast pair is a lossless round-trip.
        """
        so = _CAST_SRC_DST.get(outer_type)
        si = _CAST_SRC_DST.get(inner_type)
        if so is None or si is None:
            return False
        return so[1] == si[0] and so[0] == si[1] and si[0] in ("bool", "byte")

    def _h_primitive_cast(self, t, s):
        """Render a primitive cast, folding/eliding it under ``--simplify``.

        Args:
            t (UnToken): The primitive-cast token being rendered.
            s (bool): The statement flag.

        Returns:
            str: The ``type(expr)`` cast, a folded constant, or the bare inner
                expression when the cast is redundant.
        """
        if self.simplify:
            cv = self._const_value(t)
            if cv is not None:
                return self._format_const_value(cv)
            inner = t.expression
            if isinstance(inner, UnTokenPrimitiveCast) and self._is_redundant_cast_pair(
                t.cast_type, inner.cast_type
            ):
                return self._expr(inner.expression)
            # Numeric widening casts (byte -> int, byte -> float, int -> float)
            # are implicit in UnrealScript, so drop them; the narrowing casts
            # (e.g. int -> byte, float -> int) are always kept.
            if t.cast_type in _IMPLICIT_WIDENING_CASTS:
                return self._expr(t.expression)
        if t.cast_type in _CAST_TYPE_NAMES:
            return f"{_CAST_TYPE_NAMES[t.cast_type]}({self._expr(t.expression)})"
        return self._expr(t.expression)

    def _h_meta_cast(self, t, s):
        """Render a metaclass cast as ``Class<Name>(expr)``.

        Args:
            t (UnToken): The meta-cast token being rendered.
            s (bool): The statement flag.

        Returns:
            str: The ``Class<Name>(expr)`` cast expression.
        """
        return (
            f"Class<{self._resolve_item_name(t.class_ref)}>({self._expr(t.expression)})"
        )

    def _h_dynamic_cast(self, t, s):
        """Render a dynamic object cast as ``ClassName(expr)``.

        Args:
            t (UnToken): The dynamic-cast token being rendered.
            s (bool): The statement flag.

        Returns:
            str: The ``ClassName(expr)`` cast expression.
        """
        return f"{self._resolve_item_name(t.class_ref)}({self._expr(t.expression)})"

    def _h_eat_string(self, t, s):
        """Render an eat-string token by rendering its inner expression.

        Args:
            t (UnToken): The eat-string token being rendered.
            s (bool): The statement flag.

        Returns:
            str: The rendered inner expression.
        """
        return self._expr(t.expression)

    def _h_skip(self, t, s):
        """Render a skip token by rendering its inner expression.

        Args:
            t (UnToken): The skip token being rendered.
            s (bool): The statement flag.

        Returns:
            str: The rendered inner expression.
        """
        return self._expr(t.expression)

    def _h_assert(self, t, s):
        """Render an assert statement as ``assert(expr)``.

        Under ``--simplify`` an assert with a constant-true condition (e.g.
        ``assert(true)``) is dropped: it can never fire, so it is pure
        anti-decompilation/debug noise. A constant-false assert always fires and
        is kept.

        Args:
            t (UnToken): The assert token being rendered.
            s (bool): The statement flag.

        Returns:
            str: The ``assert(expr)`` statement (semicolon-eligible), or ``""``
                when a constant-true assert is elided.
        """
        self._can_semicolon = True
        if self.simplify:
            cv = self._const_value(t.expression)
            if cv is not None and cv[1]:  # constant-true: assert never fires
                self._can_semicolon = False
                return ""
        return f"assert({self._expr(t.expression)})"

    def _h_goto_label(self, t, s):
        """Render a computed goto as ``goto label``.

        Args:
            t (UnToken): The goto-label token being rendered.
            s (bool): The statement flag.

        Returns:
            str: The ``goto <label>`` statement (semicolon-eligible).
        """
        self._can_semicolon = True
        return f"goto {self._expr(t.label_expr)}"

    def _is_last_statement(self) -> bool:
        """Return whether the current token is the final real statement.

        True when every token after the current index is only a stream
        terminator, label table, debug-info, or no-op token — i.e. nothing
        executable follows.

        Returns:
            bool: True if the current token is the last meaningful statement.
        """
        return all(
            isinstance(
                self.tokens[i],
                (
                    UnTokenEndOfScript,
                    UnTokenLabelTable,
                    UnTokenDebugInfo,
                    UnTokenNothing,
                ),
            )
            for i in range(self.index + 1, len(self.tokens))
        )

    def _h_stop(self, t, s):
        """Render a ``stop`` statement, dropping the implicit terminator.

        A trailing ``stop`` at the very end of state code is the implicit
        state terminator emitted by the compiler and is omitted.

        Args:
            t (UnToken): The stop token being rendered.
            s (bool): The statement flag.

        Returns:
            str: The ``stop`` statement, or "" when it is the implicit
                terminator.
        """
        # A trailing ``stop`` at the very end of state code is the implicit
        # state terminator emitted by the compiler — drop it.
        if self._is_last_statement():
            return ""
        self._can_semicolon = True
        return "stop"

    def _h_new(self, t, s):
        """Render a ``new (...) Class`` object-construction expression.

        Args:
            t (UnToken): The new token being rendered.
            s (bool): The statement flag.

        Returns:
            str: The ``new`` expression (semicolon-eligible).
        """
        outer = self._expr(t.parent_expr)
        name = self._expr(t.name_expr)
        flags = self._expr(t.flags_expr)
        cls = self._expr(t.class_expr)
        parts = [p for p in (outer, name, flags) if p]
        head = f" ({', '.join(parts)})" if parts else ""
        tail = f" {cls}" if cls else ""
        self._can_semicolon = True
        return f"new{head}{tail}"

    def _h_return(self, t, s):
        """Render a ``return`` statement, closing an enclosing default case.

        A bare ``return`` that is the final statement of the body is the
        implicit terminator the compiler appends; it is dropped, since it is
        redundant (and would be invalid in a value-returning function).

        Args:
            t (UnToken): The return token being rendered.
            s (bool): The statement flag.

        Returns:
            str: The ``return`` statement, with value if present, or "" for
                the redundant trailing bare return (semicolon-eligible).
        """
        if self._is_in_nest("default"):
            self._try_add_nest_end("switch", self._pos_of(t) + self.sizes[self.index])
        value = self._expr(t.expression)
        # Drop the implicit terminal ``return;`` the compiler appends.
        if not value and self._is_last_statement():
            self._can_semicolon = False
            return ""
        self._can_semicolon = True
        return "return" + (f" {value}" if value else "")

    # -- function calls ------------------------------------------------- #

    def _resolve_named_function(self, name_index: int):
        """Resolve a by-name call target to its ``UnFunction``.

        Virtual/global calls reference the callee only by name, so — unlike a
        ``FinalFunction`` (direct ref) — the parameter metadata (coerce flags,
        enum-typed params) must be recovered by searching the container class and
        its super chain for a function of that name. The first match walking from
        the most-derived class up is an override with the same signature, which
        is all the caller needs to render enum-constant args (e.g.
        ``LongClientAdjustPosition(..., PHYS_Falling, ...)``).

        Args:
            name_index (int): The name-table index of the called function.

        Returns:
            Optional[UnFunction]: The resolved function, or None if not found.
        """
        name = self._resolve_name(name_index)
        if not name:
            return None
        # Climb from the container (function, possibly nested in a state) to the
        # owning class, then walk the class's super chain.
        owner = self.container.group_item
        while owner is not None and not isinstance(
            getattr(owner, "object", None), UnClass
        ):
            owner = owner.group_item
        cls_obj = owner.object if owner else None
        seen: set = set()
        while isinstance(cls_obj, UnClass) and id(cls_obj) not in seen:
            seen.add(id(cls_obj))
            child = cls_obj.children
            while child is not None:
                obj = child.object
                if (
                    isinstance(obj, UnFunction)
                    and getattr(child, "object_name", None) is not None
                    and child.object_name.name.lower() == name.lower()
                ):
                    return obj
                child = obj.next_item if isinstance(obj, UnField) else None
            sup = getattr(cls_obj, "super_item", None)
            cls_obj = getattr(sup, "object", None) if sup is not None else None
        return None

    def _h_virtual_function(self, t, s):
        """Render a virtual function call as ``Name(args)``.

        Args:
            t (UnToken): The virtual-function token being rendered.
            s (bool): The statement flag.

        Returns:
            str: The rendered call (semicolon-eligible).
        """
        self._can_semicolon = True
        fn = self._resolve_named_function(t.function_name)
        coerce = _param_coerce_flags(fn) if isinstance(fn, UnFunction) else ()
        enums = self._param_enums(fn) if isinstance(fn, UnFunction) else ()
        return (
            f"{self._call_name(self._resolve_name(t.function_name))}"
            f"({self._params_text(t.params, coerce, enums)})"
        )

    def _h_global_function(self, t, s):
        """Render a global function call as ``global.Name(args)``.

        Args:
            t (UnToken): The global-function token being rendered.
            s (bool): The statement flag.

        Returns:
            str: The rendered ``global.`` call (semicolon-eligible).
        """
        self._can_semicolon = True
        fn = self._resolve_named_function(t.function_name)
        coerce = _param_coerce_flags(fn) if isinstance(fn, UnFunction) else ()
        enums = self._param_enums(fn) if isinstance(fn, UnFunction) else ()
        return (
            f"global.{self._call_name(self._resolve_name(t.function_name))}"
            f"({self._params_text(t.params, coerce, enums)})"
        )

    def _h_delegate_function(self, t, s):
        """Render a delegate function call as ``Name(args)``.

        Args:
            t (UnToken): The delegate-function token being rendered.
            s (bool): The statement flag.

        Returns:
            str: The rendered call (semicolon-eligible).
        """
        self._can_semicolon = True
        return f"{self._call_name(self._resolve_name(t.function_name))}({self._params_text(t.params)})"

    def _h_final_function(self, t, s):
        """Render a final function call, operator, or super call.

        Operator functions render in operator form.  A final call whose target
        lives in an ancestor class becomes a ``super.`` (immediate parent) or
        ``super(ClassName).`` (higher ancestor) call *when* a bare call would be
        shadowed by an override of the same name in the current class — this is
        how a non-virtual call to a parent's version is expressed, even when the
        enclosing function has a different name.  A final call to a method of an
        unrelated class (a non-static method invoked non-virtually, e.g. an
        AntiTCC foreign-native trap) is emitted as ``super(Owner).Method(...)``
        with a comment above: standard UnrealScript has no form for it, but
        UT2004's ``ucc`` binds ``Super(<class>)`` to a non-ancestor's method (a
        compiler quirk that may be fixed).

        Args:
            t (UnToken): The final-function token being rendered.
            s (bool): The statement flag.

        Returns:
            str: The rendered call/operator expression (semicolon-eligible).
        """
        self._can_semicolon = True
        # Consume the instance-context flag: a call that is itself a context
        # member (`obj.Func(...)`) needs no qualifier, but its arguments are in
        # the caller's scope, so clear the flag before rendering params.
        in_context = self.within_context
        self.within_context = False
        item = self._resolve_item(t.function_ref)
        fn = item.object if item else None
        coerce = _param_coerce_flags(fn) if isinstance(fn, UnFunction) else ()
        if isinstance(fn, UnFunction) and (
            fn.function_flags & UnFunctionFlags.Operator
        ):
            symbol = (
                fn.friendly_name.name if fn.friendly_name else item.object_name.name
            )
            if fn.function_flags & UnFunctionFlags.PreOperator:
                return self._render_pre(symbol, t.params, coerce)
            if fn.operator_precedence == 0:
                return self._render_post(symbol, t.params, coerce)
            return self._render_binary(symbol, t.params, coerce)

        name = item.object_name.name if item else "None"
        prefix = ""
        if not self.within_class_context and not in_context and item is not None:
            prefix = self._final_call_prefix(item, fn, name)
        elif not self.within_class_context and not in_context:
            prefix = self._static_call_qualifier(item, fn)
        return f"{prefix}{self._call_name(name)}({self._params_text(t.params, coerce, self._param_enums(fn))})"

    def _final_call_prefix(self, item, fn, name: str) -> str:
        """Return the qualifier for a final call: ``super.``/``super(X).``/``""``.

        Resolves the target's owning class relative to the enclosing class:

        * self / an inherited method reachable by a bare call -> no prefix;
        * an ancestor method shadowed by a same-named override -> ``super.``
          (immediate parent) or ``super(Owner).`` (higher ancestor);
        * a method of an unrelated class -> either a cross-class ``static.``
          qualifier (for a static function) or, for a non-static method invoked
          non-virtually, a bare call with a ``// ... not valid`` comment above.

        Args:
            item: The resolved target function export/import item.
            fn: The resolved target function object (or None if unresolved).
            name (str): The target function's name.

        Returns:
            str: The call qualifier prefix (possibly empty).
        """
        owner = item.group_item
        cur_cls = self._enclosing_class_item()
        chain = self._ancestor_chain(cur_cls) if cur_cls is not None else []

        idx = None
        for i, node in enumerate(chain):
            if node is owner or (
                owner is not None
                and node.object_name is not None
                and owner.object_name is not None
                and node.object_name.name.lower() == owner.object_name.name.lower()
            ):
                idx = i
                break

        if idx is None:
            # Target is neither the current class nor one of its ancestors.
            if (
                owner is not None
                and self._is_class_item(owner)
                and isinstance(fn, UnFunction)
                and not (fn.function_flags & UnFunctionFlags.Static)
            ):
                # A non-virtual call to an unrelated class's method (e.g. the
                # AntiTCC foreign-native traps: `Engine.Security.LocalPerform`
                # from `MutAntiTCCFinal`, which extends Mutator). There is no
                # standard UnrealScript form for this, but UT2004's `ucc` binds
                # `Super(OwnerClass).Method(...)` to the named class's method
                # even when it is not an ancestor of the current class. We emit
                # exactly that form, with a comment recording why; OldUnreal's
                # `ucc` honours the binding, so AntiTCC_LDG ships this
                # directly (no stub/redirect scaffolding needed).
                cur_name = (
                    cur_cls.object_name.name if cur_cls is not None else "this class"
                )
                self._add_pre_comment(
                    f"// non-virtual call to {self._owner_qualified(owner)}.{name} "
                    f"is not valid here ({owner.object_name.name} is not a parent "
                    f"of {cur_name}); uses a ucc Super(<class>) binding quirk"
                )
                return f"super({owner.object_name.name})."
            return self._static_call_qualifier(item, fn)

        if idx == 0:
            # A final call to the current class's own (final) method — bare.
            return ""

        # Ancestor method.  A bare call compiles to a *virtual* dispatch unless
        # the method is final, so a final-function token targeting a non-final
        # ancestor method can only have come from an explicit super call; a
        # final ancestor method is reached idiomatically by a bare call.
        if isinstance(fn, UnFunction) and (fn.function_flags & UnFunctionFlags.Final):
            return ""
        return "super." if idx == 1 else f"super({owner.object_name.name})."

    def _enclosing_class_item(self):
        """Return the class item that owns the function being decompiled.

        Returns:
            The enclosing ``UnClass`` export item, or None.
        """
        cur = self.container
        while cur is not None and not isinstance(getattr(cur, "object", None), UnClass):
            cur = cur.group_item
        return cur

    def _ancestor_chain(self, cls_item) -> list:
        """Return ``[cls_item, parent, grandparent, ...]`` up the super chain.

        Walks ``super_item`` across imported classes.  A class item is included
        even when its class object did not resolve (e.g. ``Core.Object``, an
        intrinsic root whose import yields no ``UnClass``) so that a call to one
        of its methods is still recognised as an ancestor call rather than a
        foreign one; the walk can only continue upward past a *resolved* class,
        so an unresolved item terminates the chain.  Cycle-guarded by name.

        Args:
            cls_item: The class item to start from (or None).

        Returns:
            list: The class items from *cls_item* to its topmost ancestor.
        """
        chain: list = []
        node = cls_item
        seen: set = set()
        while node is not None and self._is_class_item(node):
            chain.append(node)
            nm = node.object_name.name.lower() if node.object_name else ""
            if nm in seen:
                break
            seen.add(nm)
            obj = getattr(node, "object", None)
            node = (
                getattr(obj, "super_item", None) if isinstance(obj, UnClass) else None
            )
        return chain

    def _is_class_item(self, item) -> bool:
        """Return True when *item* denotes a class (export object or import).

        Args:
            item: The package item to test.

        Returns:
            bool: True if the item is a class export or a class import.
        """
        obj = getattr(item, "object", None)
        cls = getattr(item, "class_name", None)
        return isinstance(obj, UnClass) or (cls is not None and cls.name == "Class")

    def _owner_qualified(self, owner) -> str:
        """Return the package-qualified name of a class item (``Pkg.Class``).

        Args:
            owner: The class item.

        Returns:
            str: The dotted qualified name.
        """
        parts = [owner.object_name.name]
        g = owner.group_item
        while g is not None:
            parts.append(g.object_name.name)
            g = g.group_item
        return ".".join(reversed(parts))

    def _add_pre_comment(self, text: str) -> None:
        """Queue a comment line to be emitted above the current statement.

        Duplicate lines are collapsed: an expression may be rendered more than
        once per statement (e.g. an operand is rendered once to decide coercion
        and again to emit), and each render re-adds the same comment.

        Args:
            text (str): The comment text (including its ``//`` prefix).
        """
        if not self.pre_comment:
            self.pre_comment = text
        elif text not in self.pre_comment.split("\n"):
            self.pre_comment += "\n" + text

    def _static_call_qualifier(self, item, fn) -> str:
        """Return ``class'Owner'.static.`` for a cross-class static call.

        A static function of an unrelated class is not in scope for a bare
        call, so it must be qualified (UT2004 requires the ``class'Name'``
        literal form, not a bare class identifier). When the owner is the
        current class or one of its ancestors (where the source called it
        bare), or the target is not static, returns "".

        Args:
            item: The resolved function export (or None).
            fn: The resolved function object (or None).

        Returns:
            str: ``class'Owner'.static.`` or an empty string.
        """
        if item is None:
            return ""
        owner = item.group_item
        if owner is None:
            return ""
        # The owner must be a class: a resolved export whose object is a UnClass,
        # or an import whose class is "Class" (its object may be unresolved when
        # the dependency isn't fully loaded, e.g. Engine.GameStats).
        owner_obj = getattr(owner, "object", None)
        owner_cls = getattr(owner, "class_name", None)
        if not (
            isinstance(owner_obj, UnClass)
            or (owner_cls is not None and owner_cls.name == "Class")
        ):
            return ""
        # A resolved-but-non-static function is an own/inherited method reached by
        # a bare call — no qualifier. When fn is unresolved (import), a bare
        # FinalFunction call to an unrelated class is necessarily a static call.
        if isinstance(fn, UnFunction) and not (
            fn.function_flags & UnFunctionFlags.Static
        ):
            return ""
        # Current class = walk the enclosing function's outer chain to its class.
        cur = self.container
        while cur is not None and not isinstance(getattr(cur, "object", None), UnClass):
            cur = cur.group_item
        # Owner is self or an ancestor of the current class -> bare call is fine.
        node = cur
        seen: set = set()
        while node is not None:
            if node.object_name.name == owner.object_name.name:
                return ""
            nm = node.object_name.name
            if nm in seen:
                break
            seen.add(nm)
            obj = node.object
            node = (
                getattr(obj, "super_item", None) if isinstance(obj, UnClass) else None
            )
        return f"class'{owner.object_name.name}'.static."

    def _native_call(self, t, s):
        """Render a native function call as an operator or ``Name(args)``.

        Args:
            t (UnToken): The native/extended-native function token.
            s (bool): The statement flag.

        Returns:
            str: The rendered operator or function call (semicolon-eligible);
                unknown indices fall back to ``__NFUN_<n>__(...)``.
        """
        self._can_semicolon = True
        info = self.natives.get(t.native_index)
        if info is None:
            return f"__NFUN_{t.native_index}__({self._params_text(t.params)})"
        # Fold a constant arithmetic expression (1 + 1 -> 2, (4 + 4) * 2 -> 16).
        if self.simplify and info.kind in ("binary", "pre"):
            cv = self._const_value(t)
            if cv is not None:
                return self._format_const_value(cv)
        if info.kind == "binary":
            return self._render_binary(info.name, t.params, info.coerce)
        if info.kind == "pre":
            return self._render_pre(info.name, t.params, info.coerce)
        if info.kind == "post":
            return self._render_post(info.name, t.params, info.coerce)
        return (
            f"{self._call_name(info.name)}("
            f"{self._params_text(t.params, info.coerce, info.enums)})"
        )

    def _operands(self, params: List[UnToken]) -> List[UnToken]:
        """Return the operand tokens of a call, dropping the terminator.

        Args:
            params (List[UnToken]): The raw parameter token list.

        Returns:
            List[UnToken]: The parameters excluding any EndFunctionParms token.
        """
        return [p for p in params if not isinstance(p, UnTokenEndFunctionParms)]

    # Comparison operators whose operands share a type — used to render enum
    # comparisons with their symbolic member names.
    _COMPARISON_OPS = {"==", "!=", "<", ">", "<=", ">=", "~="}

    def _enum_from_property(self, obj) -> Optional[UnEnum]:
        """Return the enum a byte property is typed as, else ``None``.

        Args:
            obj: The property object to inspect.

        Returns:
            Optional[UnEnum]: The enum type, or None.
        """
        if isinstance(obj, UnByteProperty) and obj.enum_item is not None:
            enum = obj.enum_item.object
            if isinstance(enum, UnEnum):
                return enum
        return None

    def _param_enums(self, fn) -> tuple:
        """Return the enum type (or ``None``) of each parameter, in order.

        Parallels :func:`_param_coerce_flags` but yields the parameter's enum
        type so an integer-constant argument passed to an enum-typed parameter
        can render as the enum member name.

        Args:
            fn: The callee function object.

        Returns:
            tuple: One entry per non-return parameter (a UnEnum or None).
        """
        if not isinstance(fn, UnFunction):
            return ()
        out: List[Optional[UnEnum]] = []
        child = fn.children
        while child is not None:
            obj = child.object
            if (
                isinstance(obj, UnProperty)
                and (obj.property_flags & int(UnPropertyFlags.Parm))
                and not (obj.property_flags & int(UnPropertyFlags.ReturnParm))
            ):
                out.append(self._enum_from_property(obj))
            child = obj.next_item if isinstance(obj, UnField) else None
        return tuple(out)

    def _enum_operand(self, token: Optional[UnToken]):
        """Return ``(render_token, enum)`` if a token is an enum value.

        Unwraps an ``int(...)`` (``ByteToInt``) cast around the enum so the
        cast can be dropped; also matches a bare enum expression.

        Args:
            token (Optional[UnToken]): The candidate operand token.

        Returns:
            A ``(render_token, enum)`` tuple, or None if not an enum value.
        """
        if isinstance(token, UnTokenPrimitiveCast) and token.cast_type == int(
            UnCastType.ByteToInt
        ):
            enum = self._enum_of_expr(token.expression)
            if enum is not None:
                return (token.expression, enum)
            return None
        enum = self._enum_of_expr(token)
        if enum is not None:
            return (token, enum)
        return None

    def _enum_of_expr(self, token: Optional[UnToken]) -> Optional[UnEnum]:
        """Resolve the enum type of a variable/member expression, else ``None``.

        Args:
            token (Optional[UnToken]): The expression token to resolve.

        Returns:
            Optional[UnEnum]: The enum type, or None.
        """
        if isinstance(token, UnTokenBoolVariable):
            return self._enum_of_expr(token.expression)
        if isinstance(
            token,
            (
                UnTokenLocalVariable,
                UnTokenInstanceVariable,
                UnTokenNativeParm,
                UnTokenDefaultVariable,
            ),
        ):
            item = self._resolve_item(token.object_ref)
            return self._enum_from_property(item.object if item else None)
        if isinstance(token, (UnTokenContext, UnTokenClassContext)):
            return self._enum_of_expr(token.context_expr)
        if isinstance(token, UnTokenStructMember):
            item = self._resolve_item(token.property_ref)
            return self._enum_from_property(item.object if item else None)
        if isinstance(token, (UnTokenArrayElement, UnTokenDynArrayElement)):
            return self._enum_of_expr(token.base_expr)
        return None

    def _expr_is_array(self, token: Optional[UnToken]) -> bool:
        """Report whether ``token`` is a bare array reference (dynamic or fixed).

        A ``*ToString`` cast on a coerce parameter is normally redundant (string
        operators auto-convert their args), so ``_coerce_operand`` elides it. But
        a whole array — ``array<T>`` or a fixed ``T x[N]`` — has no implicit
        string coercion: old UT2004 ``ucc`` tolerated ``"x" @ someArray`` by
        inserting the cast, while OldUnreal rejects the bare array. So when the
        coerced operand is a whole array we must keep the explicit
        ``string(...)``. A subscripted element (``arr[i]``) is a scalar, not the
        array, and returns False.

        Args:
            token (Optional[UnToken]): The operand expression under the cast.

        Returns:
            bool: True if the operand resolves to a (dynamic or fixed) array
                property.
        """
        if isinstance(
            token,
            (
                UnTokenLocalVariable,
                UnTokenInstanceVariable,
                UnTokenNativeParm,
                UnTokenDefaultVariable,
            ),
        ):
            item = self._resolve_item(token.object_ref)
        elif isinstance(token, UnTokenStructMember):
            item = self._resolve_item(token.property_ref)
        else:
            return False
        return isinstance(
            getattr(item, "object", None), (UnArrayProperty, UnFixedArrayProperty)
        )

    def _const_int(self, token: Optional[UnToken]) -> Optional[int]:
        """Return the integer value of a constant expression, else ``None``.

        Args:
            token (Optional[UnToken]): The candidate constant expression.

        Returns:
            Optional[int]: The integer value, or None for non-integer or
                non-constant expressions.
        """
        cv = self._const_value(token)
        if cv is None or cv[0] == "float":
            return None
        return int(cv[1])

    @staticmethod
    def _enum_member(enum: UnEnum, value: int) -> Optional[str]:
        """Return the enum member name for an integer value, else ``None``.

        Args:
            enum (UnEnum): The enum type.
            value (int): The ordinal value.

        Returns:
            Optional[str]: The member name, or None if out of range.
        """
        if 0 <= value < len(enum.names):
            return enum.names[value].name
        return None

    def _try_enum_comparison(self, symbol: str, ops: List[UnToken]) -> Optional[str]:
        """Render ``int(Role) < 4`` as ``Role < ROLE_Authority`` when applicable.

        Args:
            symbol (str): The comparison operator symbol.
            ops (List[UnToken]): The operand tokens.

        Returns:
            Optional[str]: The rendered enum comparison, or None when the
                operands don't form an enum comparison.
        """
        if symbol not in self._COMPARISON_OPS or len(ops) < 2:
            return None
        left_enum = self._enum_operand(ops[0])
        right_enum = self._enum_operand(ops[1])
        if left_enum is not None and right_enum is None:
            value = self._const_int(ops[1])
            member = (
                self._enum_member(left_enum[1], value) if value is not None else None
            )
            if member is not None:
                return f"{self._precedence_operand(left_enum[0])} {symbol} {member}"
        elif right_enum is not None and left_enum is None:
            value = self._const_int(ops[0])
            member = (
                self._enum_member(right_enum[1], value) if value is not None else None
            )
            if member is not None:
                return f"{member} {symbol} {self._precedence_operand(right_enum[0])}"
        elif left_enum is not None and right_enum is not None:
            # Both sides are enum values — just drop the redundant int() casts.
            return (
                f"{self._precedence_operand(left_enum[0])} {symbol} "
                f"{self._precedence_operand(right_enum[0])}"
            )
        return None

    def _render_binary(
        self, symbol: str, params: List[UnToken], coerce: tuple = ()
    ) -> str:
        """Render a binary operator call as ``left symbol right``.

        Falls back to function-call form when fewer than two operands exist,
        and to enum-comparison form when applicable.

        Args:
            symbol (str): The operator symbol.
            params (List[UnToken]): The operator's parameter tokens.
            coerce (tuple): Per-parameter CoerceParm flags.

        Returns:
            str: The rendered binary expression.
        """
        ops = self._operands(params)
        if len(ops) < 2:
            return f"{symbol}({self._params_text(params, coerce)})"
        enum_form = self._try_enum_comparison(symbol, ops)
        if enum_form is not None:
            return enum_form
        group = self._op_group(symbol)
        left = self._coerce_operand(
            ops[0],
            len(coerce) > 0 and coerce[0],
            precedence=True,
            parent_group=group,
            is_left=True,
        )
        right = self._coerce_operand(
            ops[1],
            len(coerce) > 1 and coerce[1],
            precedence=True,
            parent_group=group,
            is_left=False,
        )
        return f"{left} {symbol} {right}"

    # Comparisons whose negation is a single, directly-invertible operator
    # (no ``!`` introduced inside; none short-circuit/``skip`` their operands).
    # ``~=`` has no ``!~=`` inverse and ``&&``/``||`` would need De Morgan, so
    # both are absent — leaving those negations as-is.
    _INVERSE_OPS = {
        "==": "!=",
        "!=": "==",
        "<": ">=",
        ">": "<=",
        "<=": ">",
        ">=": "<",
    }

    def _render_pre(
        self, symbol: str, params: List[UnToken], coerce: tuple = ()
    ) -> str:
        """Render a prefix operator call as ``symbol operand``.

        Args:
            symbol (str): The operator symbol.
            params (List[UnToken]): The operator's parameter tokens.
            coerce (tuple): Per-parameter CoerceParm flags.

        Returns:
            str: The rendered prefix expression (spaced for word operators).
        """
        ops = self._operands(params)
        if not ops:
            return symbol
        # --simplify: fold ``!(A <cmp> B)`` to ``A <inv-cmp> B`` when the negated
        # operator is a single, directly-invertible comparison. The inverse map
        # holds only non-skip operators with a real inverse, so this never has to
        # introduce a ``!`` inside the expression to keep parity.
        if self.simplify and symbol == "!":
            inner = self._effective_operand_token(ops[0])
            if isinstance(
                inner, (UnTokenNativeFunction, UnTokenExtendedNativeFunction)
            ):
                info = self.natives.get(inner.native_index)
                if (
                    info is not None
                    and info.kind == "binary"
                    and info.name in self._INVERSE_OPS
                ):
                    inner_ops = self._operands(inner.params)
                    if len(inner_ops) == 2 and not any(
                        isinstance(o, UnTokenSkip) for o in inner.params
                    ):
                        return self._render_binary(
                            self._INVERSE_OPS[info.name], inner.params, info.coerce
                        )
        operand = self._coerce_operand(
            ops[0], len(coerce) > 0 and coerce[0], precedence=False
        )
        # A binary-operator operand binds looser than this prefix operator, so
        # parenthesize it (`!(a ~= b)`, not `!a ~= b`).
        if self._is_binary_operator_token(ops[0]):
            operand = f"({operand})"
        return f"{symbol} {operand}" if _needs_space(symbol) else f"{symbol}{operand}"

    def _render_post(
        self, symbol: str, params: List[UnToken], coerce: tuple = ()
    ) -> str:
        """Render a postfix operator call as ``operand symbol``.

        Args:
            symbol (str): The operator symbol.
            params (List[UnToken]): The operator's parameter tokens.
            coerce (tuple): Per-parameter CoerceParm flags.

        Returns:
            str: The rendered postfix expression (spaced for word operators).
        """
        ops = self._operands(params)
        if not ops:
            return symbol
        operand = self._coerce_operand(
            ops[0], len(coerce) > 0 and coerce[0], precedence=False
        )
        # A binary-operator operand binds looser than this postfix operator.
        if self._is_binary_operator_token(ops[0]):
            operand = f"({operand})"
        return f"{operand} {symbol}" if _needs_space(symbol) else f"{operand}{symbol}"

    # -- control flow --------------------------------------------------- #

    def _h_switch(self, t, s):
        """Render a ``switch(expr)`` header and open a switch nest.

        Args:
            t (UnToken): The switch token being rendered.
            s (bool): The statement flag.

        Returns:
            str: The ``switch(expr)`` header.
        """
        self._add_nest_begin("switch", self._pos_of(t), t)
        expr = self._expr(t.expression)
        self._can_semicolon = False
        return f"switch({expr})"

    def _h_case(self, t, s):
        """Render a ``case value:`` or ``default:`` label.

        Args:
            t (UnToken): The case token being rendered.
            s (bool): The statement flag.

        Returns:
            str: The ``case <value>:`` or ``default:`` label.
        """
        if t.offset != UnTokenCase.DEFAULT_OFFSET:
            self._add_nest("case", self._pos_of(t), t.offset, t)
            # If the enclosing switch is over an enum, render the case as the
            # enum member name (`case KT_Kick:`, not `case 1:`) — a raw int
            # constant mismatches the enum type.
            enum = None
            for nest in reversed(self.nest_chain):
                if nest.type == "switch" and nest.creator is not None:
                    enum = self._enum_of_expr(nest.creator.expression)
                    break
            label = None
            if enum is not None:
                value = self._const_int(t.expression)
                member = self._enum_member(enum, value) if value is not None else None
                if member is not None:
                    label = f"case {member}:"
            if label is None:
                label = f"case {self._expr(t.expression)}:"
            # Set AFTER rendering the value: an expression case (`case 0 + 0:`)
            # runs an operator handler that resets the flag to True, which would
            # otherwise append a stray `;` after the colon.
            self._can_semicolon = False
            return label
        self._add_nest_begin("default", self._pos_of(t), t)
        self._can_semicolon = False
        return "default:"

    def _h_iterator(self, t, s):
        """Render a ``foreach`` header and open a foreach nest.

        Args:
            t (UnToken): The iterator token being rendered.
            s (bool): The statement flag.

        Returns:
            str: The ``foreach <expr>`` header.
        """
        self._add_nest("foreach", self._pos_of(t), t.end_offset, t)
        expr = self._expr(t.iterator_expr)
        self._can_semicolon = False
        return f"foreach {expr}"

    def _h_iterator_next(self, t, s):
        """Render a foreach iteration advance as ``continue`` or nothing.

        Args:
            t (UnToken): The iterator-next token being rendered.
            s (bool): The statement flag.

        Returns:
            str: ``"continue"`` (semicolon-eligible), or "" when immediately
                followed by an iterator-pop.
        """
        peek = (
            self.tokens[self.index + 1] if self.index + 1 < len(self.tokens) else None
        )
        if isinstance(peek, UnTokenIteratorPop):
            return ""
        self._can_semicolon = True
        return "continue"

    def _h_iterator_pop(self, t, s):
        """Render a foreach exit as ``break`` or nothing.

        Args:
            t (UnToken): The iterator-pop token being rendered.
            s (bool): The statement flag.

        Returns:
            str: ``"break"`` (semicolon-eligible), or "" when it follows an
                iterator-next or precedes a return.
        """
        prev = self.tokens[self.index - 1] if self.index > 0 else None
        peek = (
            self.tokens[self.index + 1] if self.index + 1 < len(self.tokens) else None
        )
        if isinstance(prev, UnTokenIteratorNext) or isinstance(peek, UnTokenReturn):
            return ""
        self._can_semicolon = True
        return "break"

    def _h_jump_if_not(self, t, s):
        """Render a conditional jump as ``if``/``while``/``for`` or inverted goto.

        Reconstructs if/else, loops and (under ``--simplify``) for-loops from
        the jump's direction and target.

        Args:
            t (UnToken): The jump-if-not token being rendered.
            s (bool): The statement flag.

        Returns:
            str: The rendered control-flow header or inverted goto statement.
        """
        pos = self._pos_of(t)

        # A JumpIfNot that targets *itself* is an empty-body loop: the condition
        # (which carries the side effects, e.g. ``++i``) is re-evaluated, and the
        # jump loops back while the condition is false. That is an empty-body
        # ``while(!cond) {}`` — the loop runs until the condition becomes true.
        # Recovering it here is correct control flow (not a simplification), so
        # it applies in raw mode too; rendering it as ``if(cond){}`` would
        # silently drop the loop.
        if (t.offset & 0xFFFF) == pos:
            neg = self._negate_condition(self._expr(t.condition))
            self._can_semicolon = False
            return f"while({neg}) {{}}"

        # A reconstructed do/until back-edge is rendered as the block's closing
        # ``} until(cond);`` by the nest pass, so emit nothing here.
        if self.reconstruct_loops and id(t) in self._do_until_back_ids:
            self._can_semicolon = False
            return ""

        condition = self._expr(t.condition)

        # If some later jump targets this token's position, it's a loop head.
        is_loop = pos in self._loop_heads
        if not is_loop:
            for i in range(self.index + 1, len(self.tokens)):
                nxt = self.tokens[i]
                if isinstance(nxt, UnTokenJump) and nxt.offset == pos:
                    is_loop = True
                    break

        # Backward jump -> inverted "if(!(cond)) goto Label" pattern.
        if (t.offset & 0xFFFF) < pos:
            label = self._offset_label(t.offset)
            self._can_semicolon = True
            return f"if(!({condition}))\n{self.ind.tabs}\tgoto {label}"

        self._can_semicolon = False
        if not is_loop:
            # Locate the first statement at/after the jump target.
            idx = self._index_of(t)
            while idx < len(self.tokens) and self.positions[idx] < t.offset:
                idx += 1
            if 0 < idx < len(self.tokens):
                prev_token = self.tokens[idx - 1]
                else_start = self.tokens[idx]
                if self.positions[idx] == t.offset and isinstance(
                    prev_token, UnTokenJump
                ):
                    if isinstance(
                        else_start, UnTokenCase
                    ) and self._jumps_out_of_switch(prev_token):
                        prev_token._marked_switch_break = True
                    elif prev_token.offset != self.positions[idx]:
                        # if / else
                        self.nests.append(_Nest("begin", "if", pos, t))
                        end = _Nest("end", "if", t.offset, t, has_else=prev_token)
                        self.nests.append(end)
                        prev_token._linked_if_nest = end
                        return f"if({condition})"

        self._add_nest("loop" if is_loop else "if", pos, t.offset, t)
        if is_loop and self.reconstruct_loops:
            if pos in self._for_info:
                init, incr, _cont = self._for_info[pos]
                init_text = self._expr(init)
                incr_text = self._expr(incr)
                self._can_semicolon = False
                return f"for({init_text}; {condition}; {incr_text})"
            self._can_semicolon = False
            return f"while({condition})"
        return f"if({condition})"

    def _jumps_out_of_switch(self, jump: UnTokenJump) -> bool:
        """Report whether a jump exits the enclosing switch (a ``break``).

        Args:
            jump (UnTokenJump): The jump token to analyze.

        Returns:
            bool: True if the jump crosses the switch's default boundary.
        """
        start = self._index_of(jump)
        i = start + 1
        while i < len(self.tokens) and self.positions[i] <= jump.offset:
            t = self.tokens[i]
            if isinstance(t, UnTokenSwitch):
                balance = 1
                i += 1
                while (
                    i < len(self.tokens)
                    and balance > 0
                    and self.positions[i] <= jump.offset
                ):
                    t = self.tokens[i]
                    if (
                        isinstance(t, UnTokenCase)
                        and t.offset == UnTokenCase.DEFAULT_OFFSET
                    ):
                        balance -= 1
                    elif isinstance(t, UnTokenSwitch):
                        balance += 1
                    i += 1
                continue
            if (
                isinstance(t, UnTokenCase)
                and t.offset == UnTokenCase.DEFAULT_OFFSET
                and jump.offset > self.positions[i]
            ):
                return True
            i += 1
        return False

    def _h_jump(self, t, s):
        """Render an unconditional jump as break/continue/goto or nothing.

        Interprets the jump against the current nest stack to reconstruct
        ``break``, ``continue``, if/else linkage, or an explicit ``goto``.

        Args:
            t (UnToken): The jump token being rendered.
            s (bool): The statement flag.

        Returns:
            str: The rendered control-flow statement, or "" when the jump is
                implicit (e.g. a fall-through back-edge).
        """
        pos = self._pos_of(t)
        linked = getattr(t, "_linked_if_nest", None)
        temp_linked_if = linked.has_else if linked is not None else None
        if linked is not None:
            linked.has_else = None

        # Loop reconstruction: a backward jump to a while-loop head is either
        # the implicit back-edge (elided) or an explicit ``continue``.
        if self.reconstruct_loops:
            # Inside a reconstructed do/until, a jump to the loop exit (just
            # past the back-edge) is a ``break``; a jump to the back-edge (the
            # condition test) is a ``continue``. Without this they would leak
            # as raw ``goto`` into the do-block.
            do_nest = self._is_within_nest("do")
            if do_nest is not None and do_nest.creator is not None:
                exit_pos = self._do_until_ends.get(do_nest.position)
                if exit_pos is not None and t.offset == exit_pos:
                    self._no_jump_label(t.offset)
                    self._can_semicolon = True
                    return "break"
                if t.offset == self._pos_of(do_nest.creator):
                    self._no_jump_label(t.offset)
                    self._can_semicolon = True
                    return "continue"
            # A forward jump to a reconstructed for-loop's increment is a
            # ``continue`` (the increment runs, then the loop repeats).
            if (
                t.offset in self._for_continue_pos
                and self._is_within_nest("loop") is not None
            ):
                self._no_jump_label(t.offset)
                self._can_semicolon = True
                return "continue"
            loop = self._is_within_nest("loop")
            if (
                loop is not None
                and isinstance(loop.creator, UnTokenJumpIfNot)
                and t.offset == self._pos_of(loop.creator)
                and t.offset < pos
            ):
                self._no_jump_label(t.offset)
                if pos + self.sizes[self.index] == loop.creator.offset:
                    self._can_semicolon = False
                    return ""
                self._can_semicolon = True
                return "continue"

        if t.offset >= pos:
            if (
                getattr(t, "_marked_switch_break", False)
                or (self._jumps_out_of_switch(t) and self._is_in_nest("case"))
                or self._is_in_nest("default")
            ):
                self._no_jump_label(t.offset)
                switch_end = (
                    pos + self.sizes[self.index]
                    if self._is_in_nest("default")
                    else t.offset
                )
                self._try_add_nest_end("switch", switch_end)
                self._can_semicolon = True
                return "break"

            foreach = self._is_within_nest("foreach")
            if foreach is not None and isinstance(foreach.creator, UnTokenIterator):
                if t.offset == foreach.creator.end_offset:
                    prev = self.tokens[self.index - 1] if self.index > 0 else None
                    if isinstance(prev, UnTokenIteratorNext):
                        self._no_jump_label(t.offset)
                        return ""
                    self._no_jump_label(t.offset)
                    self._can_semicolon = True
                    return "break"
                if isinstance(self._token_at(t.offset), UnTokenIteratorNext):
                    self._no_jump_label(t.offset)
                    self._can_semicolon = True
                    return "continue"

            loop = self._is_within_nest("loop")
            if loop is not None and isinstance(loop.creator, UnTokenJumpIfNot):
                dest = loop.creator.offset
                if self.reconstruct_loops and t.offset == dest:
                    self._no_jump_label(t.offset)
                    self._can_semicolon = True
                    return "break"
                if t.offset + 10 == dest or t.offset == dest:
                    return self._jump_goto(t)

            if temp_linked_if is not None:
                # Verify this jump doesn't escape an outer scope (then it's a continue).
                for nest in self.nests:
                    if (
                        nest.kind == "end"
                        and t.offset > nest.position
                        and (linked is None or linked.creator is not nest.creator)
                    ):
                        return self._jump_goto(t)
                linked.has_else = temp_linked_if
                self._no_jump_label(t.offset)
                self._can_semicolon = False
                return ""

            if self._jumps_out_of_switch(t):
                self._no_jump_label(t.offset)
                self._try_add_nest_end("switch", t.offset)
                self._can_semicolon = True
                return "break"

        return self._jump_goto(t)

    def _jump_goto(self, t: UnTokenJump) -> str:
        """Render a jump as an explicit ``goto``, or "" for a fall-through.

        Args:
            t (UnTokenJump): The jump token to render.

        Returns:
            str: The ``goto <label>`` statement (semicolon-eligible), or "" if
                the jump merely falls through to the next statement.
        """
        pos = self._pos_of(t)
        if pos + self.sizes[self.index] == t.offset:
            self._no_jump_label(t.offset)
            return ""
        self._can_semicolon = True
        return f"goto {self._offset_label(t.offset)}"


# Dispatch table: token class -> bound handler name on _BodyDecompiler.
_EXPR_DISPATCH = {
    UnTokenNothing: _BodyDecompiler._h_nothing,
    UnTokenEndOfScript: _BodyDecompiler._h_nothing,
    UnTokenEndFunctionParms: _BodyDecompiler._h_nothing,
    UnTokenIntZero: _BodyDecompiler._h_int_zero,
    UnTokenIntOne: _BodyDecompiler._h_int_one,
    UnTokenTrue: _BodyDecompiler._h_true,
    UnTokenFalse: _BodyDecompiler._h_false,
    UnTokenSelf: _BodyDecompiler._h_self,
    UnTokenNoObject: _BodyDecompiler._h_no_object,
    UnTokenIntConst: _BodyDecompiler._h_int_const,
    UnTokenIntConstByte: _BodyDecompiler._h_int_const,
    UnTokenByteConst: _BodyDecompiler._h_byte_const,
    UnTokenFloatConst: _BodyDecompiler._h_float_const,
    UnTokenStringConst: _BodyDecompiler._h_string_const,
    UnTokenUnicodeStringConst: _BodyDecompiler._h_string_const,
    UnTokenNameConst: _BodyDecompiler._h_name_const,
    UnTokenObjectConst: _BodyDecompiler._h_object_const,
    UnTokenRotationConst: _BodyDecompiler._h_rotation_const,
    UnTokenVectorConst: _BodyDecompiler._h_vector_const,
    UnTokenLocalVariable: _BodyDecompiler._h_variable,
    UnTokenInstanceVariable: _BodyDecompiler._h_variable,
    UnTokenNativeParm: _BodyDecompiler._h_variable,
    UnTokenDefaultVariable: _BodyDecompiler._h_default_variable,
    UnTokenDelegateProperty: _BodyDecompiler._h_delegate_property,
    UnTokenBoolVariable: _BodyDecompiler._h_bool_variable,
    UnTokenLet: _BodyDecompiler._h_let,
    UnTokenLetBool: _BodyDecompiler._h_let,
    UnTokenLetDelegate: _BodyDecompiler._h_let,
    UnTokenContext: _BodyDecompiler._h_context,
    UnTokenClassContext: _BodyDecompiler._h_class_context,
    UnTokenStructMember: _BodyDecompiler._h_struct_member,
    UnTokenArrayElement: _BodyDecompiler._h_array_element,
    UnTokenDynArrayElement: _BodyDecompiler._h_array_element,
    UnTokenDynArrayLength: _BodyDecompiler._h_dynarray_length,
    UnTokenDynArrayInsert: _BodyDecompiler._h_dynarray_insert,
    UnTokenDynArrayRemove: _BodyDecompiler._h_dynarray_remove,
    UnTokenStructCmpEq: _BodyDecompiler._h_struct_cmp_eq,
    UnTokenStructCmpNe: _BodyDecompiler._h_struct_cmp_ne,
    UnTokenPrimitiveCast: _BodyDecompiler._h_primitive_cast,
    UnTokenMetaCast: _BodyDecompiler._h_meta_cast,
    UnTokenDynamicCast: _BodyDecompiler._h_dynamic_cast,
    UnTokenEatString: _BodyDecompiler._h_eat_string,
    UnTokenSkip: _BodyDecompiler._h_skip,
    UnTokenAssert: _BodyDecompiler._h_assert,
    UnTokenGotoLabel: _BodyDecompiler._h_goto_label,
    UnTokenStop: _BodyDecompiler._h_stop,
    UnTokenNew: _BodyDecompiler._h_new,
    UnTokenReturn: _BodyDecompiler._h_return,
    UnTokenVirtualFunction: _BodyDecompiler._h_virtual_function,
    UnTokenGlobalFunction: _BodyDecompiler._h_global_function,
    UnTokenDelegateFunction: _BodyDecompiler._h_delegate_function,
    UnTokenFinalFunction: _BodyDecompiler._h_final_function,
    UnTokenNativeFunction: _BodyDecompiler._native_call,
    UnTokenExtendedNativeFunction: _BodyDecompiler._native_call,
    UnTokenSwitch: _BodyDecompiler._h_switch,
    UnTokenCase: _BodyDecompiler._h_case,
    UnTokenIterator: _BodyDecompiler._h_iterator,
    UnTokenIteratorNext: _BodyDecompiler._h_iterator_next,
    UnTokenIteratorPop: _BodyDecompiler._h_iterator_pop,
    UnTokenJumpIfNot: _BodyDecompiler._h_jump_if_not,
    UnTokenJump: _BodyDecompiler._h_jump,
    UnTokenLabelTable: _BodyDecompiler._h_nothing,
    UnTokenDebugInfo: _BodyDecompiler._h_nothing,
}


# ===================================================================== #
#  Class / field decompiler
# ===================================================================== #


class Decompiler:
    """Decompiles a loaded package's classes into UnrealScript source text."""

    def __init__(
        self, package, simplify: bool = False, reconstruct_loops: bool = True
    ) -> None:
        """Initialize the decompiler for a loaded package.

        Args:
            package: The loaded package to decompile.
            simplify (bool): When True, apply cosmetic simplifications
                (loop/cast folding, dropping obfuscation noise).
            reconstruct_loops (bool): When True (and simplify is on),
                reconstruct ``while``/``for``/``do-until`` from the raw
                goto/label form. When False, loops are left in goto/label form
                (which round-trips byte-exactly); all other simplifications
                still apply.
        """
        self.pkg = package
        self.ind = _Indent()
        self.simplify = simplify
        self.reconstruct_loops = reconstruct_loops
        self.natives = build_native_table(package)
        # Per-class context, (re)set in decompile_class: the names of the class
        # currently being decompiled plus its ancestors (so an inherited
        # struct/enum type renders unqualified), and the ordered set of
        # non-ancestor same-package classes whose types it references (emitted as
        # the header's ``dependsOn(...)``).
        self._cur_ancestors: set = set()
        self._depends_on: List[str] = []
        # The class export currently being decompiled and the names of its
        # embedded subobjects (components), so defaultproperties can emit
        # ``begin object`` blocks and reference them by bare name.
        self._cur_class_export: Optional[UnExport] = None
        self._cur_subobject_names: set = set()
        # name -> UnEnum for enum-typed byte properties in the class being
        # rendered, so defaultproperties emit the enum member (not a raw int).
        self._cur_enum_props: dict = {}

    # ------------------------------------------------------------------ #
    #  Field enumeration
    # ------------------------------------------------------------------ #

    @staticmethod
    def _iter_field_items(struct_obj: UnStruct) -> List[UnExport]:
        """Return the child field items of a struct in stored order.

        Args:
            struct_obj (UnStruct): The struct whose children are enumerated.

        Returns:
            List[UnExport]: The child field exports in stored order.
        """
        items: List[UnExport] = []
        child = struct_obj.children
        while child is not None:
            items.append(child)
            obj = child.object
            child = obj.next_item if isinstance(obj, UnField) else None
        return items

    def _fields_of_type(self, struct_obj: UnStruct, predicate) -> List[UnExport]:
        """Return child field items whose object satisfies a predicate.

        Args:
            struct_obj (UnStruct): The struct to enumerate.
            predicate: A callable taking a field object and returning bool.

        Returns:
            List[UnExport]: The matching field exports.
        """
        return [it for it in self._iter_field_items(struct_obj) if predicate(it.object)]

    # ------------------------------------------------------------------ #
    #  Type names
    # ------------------------------------------------------------------ #

    def _ancestor_class_names(self, class_obj: UnClass) -> set:
        """Return the names of ``class_obj`` and all its ancestors.

        Walks the super chain (resolving cross-package supers). Used to decide
        whether a struct/enum type owned by another class is in scope
        unqualified — an ancestor's (or the class's own) types are inherited, so
        ``EPhysics`` needs no ``Actor.`` prefix — versus a non-ancestor's, which
        must be qualified and pulled in via ``dependsOn``.

        Args:
            class_obj (UnClass): The class whose ancestry is collected.

        Returns:
            set: The set of class names in the ancestry (including itself).
        """
        names: set = set()
        exp = getattr(class_obj, "export", None)
        if exp is not None:
            names.add(exp.object_name.name)
        sup = getattr(class_obj, "super_item", None)
        seen: set = set()
        while sup is not None:
            nm = sup.object_name.name
            if nm in seen:
                break
            seen.add(nm)
            names.add(nm)
            so = getattr(sup, "object", None)
            sup = getattr(so, "super_item", None) if isinstance(so, UnClass) else None
        return names

    def _qualify_type(self, outer: UnExport, type_name: str) -> str:
        """Qualify a struct/enum type name with its owning class as needed.

        The owner is in scope unqualified when it is the current class or one of
        its ancestors (``EPhysics`` rather than ``Actor.EPhysics``). Otherwise the
        type is qualified ``Owner.Type``, and a same-package owner is recorded so
        the class header can emit ``dependsOn(Owner)`` (required for the compiler
        to see another class's type). Cross-package owners are qualified but not
        added to ``dependsOn`` (the package import already provides them).

        Args:
            outer (UnExport): The owning class's package item (export or import).
            type_name (str): The bare struct/enum name.

        Returns:
            str: The (possibly qualified) type name.
        """
        owner = outer.object_name.name
        if owner in self._cur_ancestors:
            return type_name
        if isinstance(outer, UnExport) and owner not in self._depends_on:
            self._depends_on.append(owner)
        return f"{owner}.{type_name}"

    def _friendly_type(self, prop: UnProperty) -> str:
        """Return the UnrealScript source type name for a property.

        Args:
            prop (UnProperty): The property whose type is rendered.

        Returns:
            str: The type name (e.g. ``int``, ``array<Foo>``, ``class<Bar>``).
        """
        if isinstance(prop, UnByteProperty):
            if prop.enum_item is not None:
                # Qualify an enum with its owning class unless that class is the
                # current one or an ancestor (then it is in scope unqualified).
                # The owner is an export for same-package enums and an import for
                # cross-package ones; in both cases its object is the owning
                # UnClass.
                outer = prop.enum_item.group_item
                if outer is not None and isinstance(
                    getattr(outer, "object", None), UnClass
                ):
                    return self._qualify_type(outer, prop.enum_item.object_name.name)
                return prop.enum_item.object_name.name
            return "byte"
        if isinstance(prop, UnClassProperty):
            meta = prop.meta_class_item
            if meta is not None and meta.object_name.name != "Object":
                return f"class<{meta.object_name.name}>"
            return "class"
        if isinstance(prop, UnObjectProperty):
            return (
                prop.property_class_item.object_name.name
                if prop.property_class_item
                else "Object"
            )
        if isinstance(prop, UnStructProperty):
            if prop.struct_item is None:
                return "@NULL"
            # Qualify a struct type with its owning class unless that class is
            # the current one or an ancestor (then it is in scope unqualified).
            # A non-ancestor same-package owner is pulled in via dependsOn. The
            # owner (group_item) is an export for same-package structs and an
            # import for cross-package ones; in both cases its object is the
            # owning UnClass.
            outer = prop.struct_item.group_item
            if outer is not None and isinstance(
                getattr(outer, "object", None), UnClass
            ):
                return self._qualify_type(outer, prop.struct_item.object_name.name)
            return prop.struct_item.object_name.name
        if isinstance(prop, UnDelegateProperty):
            return (
                f"delegate<{prop.function_item.object_name.name}>"
                if prop.function_item
                else "delegate"
            )
        if isinstance(prop, UnFixedArrayProperty):
            inner = prop.inner_item.object if prop.inner_item else None
            inner_type = (
                self._friendly_type(inner) if isinstance(inner, UnProperty) else "@NULL"
            )
            return f"array<{inner_type}>"
        # Dynamic array
        from ut2004packageutil.package.object import UnArrayProperty

        if isinstance(prop, UnArrayProperty):
            inner = prop.inner_item.object if prop.inner_item else None
            inner_type = (
                self._friendly_type(inner) if isinstance(inner, UnProperty) else "@NULL"
            )
            return f"array<{inner_type}>"

        cls = prop.export.class_name_string.split(".")[-1]
        simple = {
            "IntProperty": "int",
            "BoolProperty": "bool",
            "FloatProperty": "float",
            "StrProperty": "string",
            "NameProperty": "name",
            "PointerProperty": "pointer",
        }
        return simple.get(cls, cls)

    # ------------------------------------------------------------------ #
    #  Property flag formatting
    # ------------------------------------------------------------------ #

    def _property_flags(self, prop: UnProperty, is_parm: bool) -> str:
        """Render a property's modifier keywords as a trailing-spaced string.

        Args:
            prop (UnProperty): The property whose flags are rendered.
            is_parm (bool): True to render parameter modifiers (coerce,
                optional, out, ...); False for member-variable modifiers.

        Returns:
            str: The space-joined keywords with a trailing space, or "".
        """
        f = prop.property_flags
        P = UnPropertyFlags
        out: List[str] = []

        if is_parm:
            if f & P.CoerceParm:
                out.append("coerce")
            if f & P.OptionalParm:
                out.append("optional")
            if f & P.OutParm:
                out.append("out")
            if f & P.SkipParm:
                out.append("skip")
            if f & P.Const:
                out.append("const")
            return (" ".join(out) + " ") if out else ""

        if f & P.Native:
            out.append("native")
        if f & P.Const:
            out.append("const")
        if f & P.EditConst and not self.simplify:
            out.append("editconst")
        if f & P.GlobalConfig:
            out.append("globalconfig")
        elif f & P.Config:
            out.append("config")
        if f & P.Localized:
            out.append("localized")
        if f & P.Transient:
            out.append("transient")
        if f & P.Travel:
            out.append("travel")
        if f & P.Input:
            out.append("input")
        if f & P.Deprecated:
            out.append("deprecated")
        if f & P.ExportObject:
            out.append("export")
        if f & P.EditInline:
            out.append("editinline")
        if f & P.EdFindable:
            out.append("edfindable")
        return (" ".join(out) + " ") if out else ""

    def _format_variable_name(self, prop: UnProperty) -> str:
        """Render a property name with its static-array dimension if any.

        Args:
            prop (UnProperty): The property to name.

        Returns:
            str: The variable name, suffixed with ``[N]`` for fixed arrays.
        """
        name = prop.export.object_name.name
        if prop.array_dim and prop.array_dim > 1:
            name += f"[{prop.array_dim}]"
        return name

    def _format_property(
        self, prop: UnProperty, is_parm: bool, no_flags: bool = False
    ) -> str:
        """Render a property declaration as ``flags type name``.

        Args:
            prop (UnProperty): The property to render.
            is_parm (bool): True when rendering a function parameter.
            no_flags (bool): When True, omit modifier keywords.

        Returns:
            str: The rendered ``[flags ]type name`` declaration fragment.
        """
        flags = "" if no_flags else self._property_flags(prop, is_parm)
        return (
            flags + self._friendly_type(prop) + " " + self._format_variable_name(prop)
        )

    # ------------------------------------------------------------------ #
    #  Members
    # ------------------------------------------------------------------ #

    def _format_consts(self, struct_obj: UnStruct) -> List[str]:
        """Render the ``const`` declarations of a struct.

        Args:
            struct_obj (UnStruct): The struct whose consts are rendered.

        Returns:
            List[str]: One source line per const declaration.
        """
        lines: List[str] = []
        consts = self._fields_of_type(struct_obj, lambda o: isinstance(o, UnConst))
        for it in reversed(consts):
            lines.append(
                f"{self.ind.tabs}const {it.object_name.name} = {it.object.value.strip()};"
            )
        return lines

    def _format_enums(self, struct_obj: UnStruct) -> List[str]:
        """Render the ``enum`` definitions of a struct.

        Args:
            struct_obj (UnStruct): The struct whose enums are rendered.

        Returns:
            List[str]: The source lines of all enum definitions.
        """
        lines: List[str] = []
        enums = self._fields_of_type(struct_obj, lambda o: isinstance(o, UnEnum))
        for it in reversed(enums):
            enum = it.object
            lines.append(f"{self.ind.tabs}enum {it.object_name.name}")
            lines.append(f"{self.ind.tabs}{{")
            self.ind.add()
            for i, entry in enumerate(enum.names):
                comma = "," if i != len(enum.names) - 1 else ""
                lines.append(f"{self.ind.tabs}{entry.name}{comma}")
            self.ind.remove()
            lines.append(f"{self.ind.tabs}}};")
            lines.append("")
        return lines

    def _format_structs(self, struct_obj: UnStruct) -> List[str]:
        """Render the nested ``struct`` definitions of a struct.

        Args:
            struct_obj (UnStruct): The struct whose nested structs are rendered.

        Returns:
            List[str]: The source lines of all nested struct definitions.
        """
        lines: List[str] = []
        structs = self._fields_of_type(struct_obj, lambda o: type(o) is UnStruct)
        for it in reversed(structs):
            lines.extend(self._format_struct_def(it))
            lines.append("")
        return lines

    def _struct_flag_str(self, struct_obj: UnStruct) -> str:
        """Render a struct's modifier keywords as a trailing-spaced string.

        Args:
            struct_obj (UnStruct): The struct whose flags are rendered.

        Returns:
            str: The space-joined keywords with a trailing space, or "".
        """
        f = struct_obj.struct_flags
        S = UnStructFlags
        out: List[str] = []
        if f & S.Native:
            out.append("native")
        if f & S.Export:
            out.append("export")
        if f & S.Long:
            out.append("long")
        if f & S.Init:
            out.append("init")
        return (" ".join(out) + " ") if out else ""

    def _format_struct_def(self, item: UnExport) -> List[str]:
        """Render a single ``struct`` definition with its member variables.

        Args:
            item (UnExport): The struct export to render.

        Returns:
            List[str]: The struct definition's source lines.
        """
        struct_obj = item.object
        header = f"struct {self._struct_flag_str(struct_obj)}{item.object_name.name}"
        if struct_obj.super_item is not None:
            header += f" extends {struct_obj.super_item.object_name.name}"
        lines = [f"{self.ind.tabs}{header}", f"{self.ind.tabs}{{"]
        self.ind.add()
        lines.extend(self._format_member_vars(struct_obj))
        self.ind.remove()
        lines.append(f"{self.ind.tabs}}};")
        return lines

    def _format_member_vars(
        self, struct_obj: UnStruct, keyword: str = "var"
    ) -> List[str]:
        """Render the member-variable declarations of a struct.

        Only direct properties are rendered (parameters/locals live under
        functions).

        Args:
            struct_obj (UnStruct): The struct whose members are rendered.
            keyword (str): The declaration keyword (``"var"`` by default).

        Returns:
            List[str]: One source line per member-variable declaration.
        """
        lines: List[str] = []
        owner_name = struct_obj.export.object_name.name
        props = self._fields_of_type(
            struct_obj, lambda o: type(o) is UnProperty or isinstance(o, UnProperty)
        )
        # Only direct properties (parameters/locals live under functions).
        props = [
            it
            for it in props
            if isinstance(it.object, UnProperty)
            and not (it.object.property_flags & UnPropertyFlags.Parm)
        ]
        for it in props:
            prop = it.object
            # Skip compiler-synthesized delegate backing properties. For every
            # `delegate Foo()` declaration Unreal auto-creates a hidden
            # `delegate<Foo> __Foo__Delegate` property; it is implied by the
            # delegate declaration and must not be emitted as a `var` (doing so
            # would be a redefinition on recompile and is not authored source).
            if isinstance(prop, UnDelegateProperty):
                pname = prop.export.object_name.name
                if pname.startswith("__") and pname.endswith("__Delegate"):
                    continue
            decl = keyword
            cat = prop.category_name_entry
            if cat is not None and cat.name not in ("None", ""):
                decl += "()" if cat.name == owner_name else f"({cat.name})"
            lines.append(
                f"{self.ind.tabs}{decl} {self._format_property(prop, is_parm=False)};"
            )
        return lines

    # ------------------------------------------------------------------ #
    #  Functions
    # ------------------------------------------------------------------ #

    def _function_flags(self, fn: UnFunction) -> str:
        """Render a function's modifier keywords as a trailing-spaced string.

        Includes the ``function``/``event``/``operator`` category keyword.

        Args:
            fn (UnFunction): The function whose flags are rendered.

        Returns:
            str: The space-joined keywords with a trailing space.
        """
        f = fn.function_flags
        F = UnFunctionFlags
        out: List[str] = []
        if f & F.Private:
            out.append("private")
        elif f & F.Protected:
            out.append("protected")
        # Note: net-replication (reliable/unreliable/server) is NOT a function
        # declaration modifier in UE2/UT2004 — replicated functions are listed
        # in the `replication` block instead — so it is deliberately not emitted
        # here (the Net/NetReliable/NetServer flags drive `_format_replication`).
        if f & F.Native:
            out.append(
                f"native({fn.native_index})" if fn.native_index > 0 else "native"
            )
        if f & F.Static:
            out.append("static")
        if f & F.Final:
            out.append("final")
        if f & F.Iterator:
            out.append("iterator")
        if f & F.Latent:
            out.append("latent")
        if f & F.Singular:
            out.append("singular")
        if f & F.Simulated:
            out.append("simulated")
        if f & F.Exec:
            out.append("exec")

        is_normal = True
        if f & F.Event:
            out.append("event")
            is_normal = False
        if f & F.Delegate:
            out.append("delegate")
            is_normal = False
        if f & F.Operator:
            if f & F.PreOperator:
                out.append("preoperator")
            elif fn.operator_precedence == 0:
                out.append("postoperator")
            else:
                out.append(f"operator({fn.operator_precedence})")
            is_normal = False
        if is_normal:
            out.append("function")
        return " ".join(out) + " "

    def _function_parms(self, fn: UnFunction):
        """Split a function's parameters into the return value and arguments.

        Args:
            fn (UnFunction): The function to inspect.

        Returns:
            A ``(ret, args)`` tuple where ret is the return UnProperty (or
            None) and args is the list of argument UnProperty objects.
        """
        parms = [
            it
            for it in self._iter_field_items(fn)
            if isinstance(it.object, UnProperty)
            and (it.object.property_flags & UnPropertyFlags.Parm)
        ]
        ret = None
        args = []
        for it in parms:
            if it.object.property_flags & UnPropertyFlags.ReturnParm:
                ret = it.object
            else:
                args.append(it.object)
        return ret, args

    def _function_locals(self, fn: UnFunction) -> List[UnProperty]:
        """Return a function's local variable properties.

        Args:
            fn (UnFunction): The function to inspect.

        Returns:
            List[UnProperty]: The non-parameter local properties.
        """
        return [
            it.object
            for it in self._iter_field_items(fn)
            if isinstance(it.object, UnProperty)
            and not (it.object.property_flags & UnPropertyFlags.Parm)
        ]

    def _format_function(self, item: UnExport) -> List[str]:
        """Render a single function declaration and decompiled body.

        Native functions carry no script body and render to a ``header;``
        declaration. Every non-native function renders with a body block
        (possibly empty), since collapsing an empty body to ``header;`` would
        wrongly imply a native or forward declaration.

        Args:
            item (UnExport): The function export to render.

        Returns:
            List[str]: The function's source lines (declaration or full body).
        """
        fn = item.object
        ret, args = self._function_parms(fn)
        ret_type = (self._friendly_type(ret) + " ") if ret is not None else ""
        # A function's source name is its object_name (the real identifier).
        # Only operators use friendly_name, which holds the operator symbol
        # (e.g. `%`) rather than the descriptive object_name (`Percent_IntInt`).
        # These normally coincide, but an obfuscated package tokenizes
        # object_name (and every call site / the name map keys off that token)
        # while friendly_name may be tampered (e.g. `yyColorCode`); using
        # object_name keeps the declaration consistent with its call sites.
        if fn.function_flags & UnFunctionFlags.Operator:
            name = fn.friendly_name.name if fn.friendly_name else item.object_name.name
        else:
            name = item.object_name.name
        arg_text = ", ".join(self._format_property(a, is_parm=True) for a in args)
        header = f"{self._function_flags(fn)}{ret_type}{name}({arg_text})"
        if fn.function_flags & UnFunctionFlags.Const:
            header += " const"

        # Body: locals + decompiled tokens.  Native functions are implemented
        # by the engine and carry no script body, so they decompile to a
        # declaration.
        is_native = bool(fn.function_flags & UnFunctionFlags.Native)
        if is_native:
            return [f"{self.ind.tabs}{header};"]

        self.ind.add()
        local_lines: List[str] = []
        for local in self._function_locals(fn):
            local_lines.append(
                f"{self.ind.tabs}local "
                f"{self._format_property(local, is_parm=False, no_flags=self.simplify)};"
            )
        body_text = ""
        if fn.token_parser and fn.token_parser.tokens:
            body_text = _BodyDecompiler(self, fn, item).decompile()
        self.ind.remove()

        # A non-native function always gets a body block, even an empty one —
        # collapsing it to ``header;`` would wrongly imply a native/forward
        # declaration.
        lines = [f"{self.ind.tabs}{header}", f"{self.ind.tabs}{{"]
        lines.extend(local_lines)
        if local_lines and body_text:
            lines.append("")
        if body_text:
            lines.extend(body_text.split("\n"))
        lines.append(f"{self.ind.tabs}}}")
        return lines

    def _format_functions(self, struct_obj: UnStruct) -> List[str]:
        """Render all functions of a struct/state.

        Args:
            struct_obj (UnStruct): The struct whose functions are rendered.

        Returns:
            List[str]: The source lines for all functions (blank-separated).
        """
        lines: List[str] = []
        fns = self._fields_of_type(struct_obj, lambda o: isinstance(o, UnFunction))
        for it in reversed(fns):
            lines.append("")
            lines.extend(self._format_function(it))
        return lines

    # ------------------------------------------------------------------ #
    #  States
    # ------------------------------------------------------------------ #

    def _format_state(self, item: UnExport) -> List[str]:
        """Render a single ``state`` block with its functions and code body.

        Args:
            item (UnExport): The state export to render.

        Returns:
            List[str]: The state block's source lines.
        """
        state = item.object
        f = state.state_flags
        from ut2004packageutil.package.flags import UnStateFlags

        prefix = ""
        if f & UnStateFlags.Auto:
            prefix += "auto "
        if f & UnStateFlags.Simulated:
            prefix += "simulated "
        edit = "()" if f & UnStateFlags.Editable else ""
        header = f"{prefix}state{edit} {item.object_name.name}"
        if (
            state.super_item is not None
            and state.super_item.object_name.name != item.object_name.name
        ):
            header += f" extends {state.super_item.object_name.name}"

        lines = [f"{self.ind.tabs}{header}", f"{self.ind.tabs}{{"]
        self.ind.add()
        lines.extend(self._format_functions(state))
        if state.token_parser and state.token_parser.tokens:
            body = _BodyDecompiler(self, state, item).decompile()
            if body.strip():
                lines.append("")
                lines.extend(body.split("\n"))
        self.ind.remove()
        lines.append(f"{self.ind.tabs}}}")
        return lines

    def _format_states(self, class_obj: UnClass) -> List[str]:
        """Render all ``state`` blocks of a class.

        Args:
            class_obj (UnClass): The class whose states are rendered.

        Returns:
            List[str]: The source lines for all states (blank-separated).
        """
        lines: List[str] = []
        states = self._fields_of_type(
            class_obj, lambda o: isinstance(o, UnState) and not isinstance(o, UnClass)
        )
        for it in reversed(states):
            lines.append("")
            lines.extend(self._format_state(it))
        return lines

    # ------------------------------------------------------------------ #
    #  Replication
    # ------------------------------------------------------------------ #

    def _format_replication(self, class_obj: UnClass) -> List[str]:
        """Render the ``replication`` block from Net properties/functions.

        Each replicated member stores the bytecode offset of its condition
        (``rep_offset``) into the class's own token stream; members sharing
        an offset (and reliability) are grouped under one ``if`` statement.

        Args:
            class_obj (UnClass): The class whose replication block is rendered.

        Returns:
            List[str]: The ``replication`` block source lines, or [] if none.
        """
        parser = class_obj.token_parser
        if parser is None or not parser.tokens:
            return []

        groups: Dict[tuple, List[str]] = {}
        for it in self._iter_field_items(class_obj):
            obj = it.object
            if isinstance(obj, UnProperty) and (
                obj.property_flags & int(UnPropertyFlags.Net)
            ):
                if obj.rep_offset == 0xFFFF:
                    continue
                reliable = True  # UE2 stores no per-variable reliability bit
                groups.setdefault((obj.rep_offset, reliable), []).append(
                    it.object_name.name
                )
            elif isinstance(obj, UnFunction) and (
                obj.function_flags & UnFunctionFlags.Net
            ):
                if obj.rep_offset == 0xFFFF:
                    continue
                reliable = bool(obj.function_flags & UnFunctionFlags.NetReliable)
                groups.setdefault((obj.rep_offset, reliable), []).append(
                    it.object_name.name
                )

        if not groups:
            return []

        cond_decompiler = _BodyDecompiler(self, class_obj, class_obj.export)

        # --simplify: drop replication blocks that can never do anything —
        # a non-Actor class cannot replicate, and a block whose every
        # condition is a constant ``false`` is dead.
        if self.simplify and not self._class_is_actor(class_obj):
            return []

        entries = []
        for offset, reliable in sorted(groups):
            token = cond_decompiler._token_at(offset)
            condition = cond_decompiler._expr(token) if token is not None else ""
            entries.append((reliable, condition, sorted(groups[(offset, reliable)])))

        if self.simplify and all(cond == "false" for _, cond, _ in entries):
            return []

        lines = ["", "replication", "{"]
        for reliable, condition, names in entries:
            keyword = "reliable" if reliable else "unreliable"
            lines.append(f"\t{keyword} if({condition})")
            lines.append("\t\t" + ", ".join(names) + ";")
        lines.append("}")
        return lines

    def _class_is_actor(self, class_obj: UnClass) -> bool:
        """Report whether a class descends from ``Actor``.

        Walks the super chain, resolving each super (including cross-package
        imports) to its class object. Conservative: if the chain cannot be
        fully resolved, returns True so a genuine ``replication`` block is
        never dropped by mistake. Only an explicitly reached ``Object`` (with
        no intervening ``Actor``) yields False.

        Args:
            class_obj (UnClass): The class whose ancestry is inspected.

        Returns:
            bool: True if ``Actor`` is an ancestor (or ancestry is unknown).
        """
        obj = class_obj
        seen: set = set()
        while obj is not None:
            sup = getattr(obj, "super_item", None)
            if sup is None:
                return True
            name = sup.object_name.name
            if name == "Actor":
                return True
            if name == "Object":
                return False
            if name in seen:
                return True
            seen.add(name)
            nxt = getattr(sup, "object", None)
            obj = nxt if isinstance(nxt, UnClass) else None
        return True

    # ------------------------------------------------------------------ #
    #  Class
    # ------------------------------------------------------------------ #

    def _class_flags(self, class_obj: UnClass) -> List[str]:
        """Render a class's modifier keyword lines (each tab-indented).

        Args:
            class_obj (UnClass): The class whose flags are rendered.

        Returns:
            List[str]: One tab-prefixed keyword per line (e.g. ``\tabstract``).
        """
        f = class_obj.class_flags
        C = UnClassFlags
        out: List[str] = []
        if f & C.Abstract:
            out.append("\tabstract")
        if f & C.Transient:
            out.append("\ttransient")
        if class_obj.export.flags & UnObjectFlags.Native:
            out.append("\tnative")
        if f & C.NativeReplication:
            out.append("\tnativereplication")
        if f & C.Config:
            cfg = (
                class_obj.class_config_name_entry.name
                if class_obj.class_config_name_entry
                else ""
            )
            if cfg.lower() in ("none", "system", ""):
                out.append("\tconfig")
            else:
                out.append(f"\tconfig({cfg})")
        if f & C.ParseConfig:
            out.append("\tparseconfig")
        if f & C.PerObjectConfig:
            out.append("\tperobjectconfig")
        if f & C.EditInlineNew:
            out.append("\teditinlinenew")
        if f & C.CollapseCategories:
            out.append("\tcollapsecategories")
        if f & C.NoExport:
            out.append("\tnoexport")
        if f & C.Placeable:
            out.append("\tplaceable")
        if f & C.HideDropDown:
            out.append("\thidedropdown")
        if class_obj.hide_category_names:
            cats = ",".join(n.name for n in class_obj.hide_category_names)
            out.append(f"\thidecategories({cats})")
        return out

    def _format_class_header(self, item: UnExport) -> List[str]:
        """Render the ``class ... extends ... within ...;`` header lines.

        Args:
            item (UnExport): The class export to render.

        Returns:
            List[str]: The class header source lines, terminated with ``;``.
        """
        class_obj = item.object
        header = f"class {item.object_name.name}"
        if class_obj.super_item is not None:
            header += f" extends {class_obj.super_item.object_name.name}"
        within = self._resolve_within(class_obj)
        if within:
            header += f" within {within}"
        flags = self._class_flags(class_obj)
        # A class referencing another (non-ancestor, same-package) class's
        # struct/enum types must declare `dependsOn(...)` so the compiler builds
        # that class first. Collected while decompiling the body.
        if self._depends_on:
            flags = flags + [f"\tdependsOn({','.join(self._depends_on)})"]
        if flags:
            return [header] + flags[:-1] + [flags[-1] + ";"]
        return [header + ";"]

    def _resolve_within(self, class_obj: UnClass) -> str:
        """Return the ``within`` outer-class name, or "" if trivial.

        Args:
            class_obj (UnClass): The class whose ``within`` clause is resolved.

        Returns:
            str: The outer class name, or "" when it is ``Object``/unresolved.
        """
        item = resolve_item(self.pkg, class_obj.class_within)
        if item is None:
            return ""
        name = item.object_name.name
        return "" if name in ("Object", "") else name

    # ------------------------------------------------------------------ #
    #  Default properties
    # ------------------------------------------------------------------ #

    def _object_literal(self, text: str) -> str:
        """Format an object reference string as ``Class'Package.Group.Name'``.

        A reference to a subobject of the class currently being decompiled
        renders as its bare name (e.g. ``InvisSkin``), since that subobject is
        defined inline via a ``begin object`` block in the same
        defaultproperties and is referenced by name.

        Args:
            text (str): The raw object reference string.

        Returns:
            str: The object literal, or ``"None"`` for empty/unresolved refs.
        """
        if not text or text == "0":
            return "None"
        ref = self.pkg.link_item_ref(text)
        item = resolve_item(self.pkg, ref)
        if item is None:
            return "None"
        if (
            item.group_item is self._cur_class_export
            and item.object_name.name in self._cur_subobject_names
        ):
            return item.object_name.name
        cls = item.class_name_string.split(".")[-1] or "Object"
        return f"{cls}'{item.object_name_string}'"

    @staticmethod
    def _dp_name(name: str) -> str:
        """Strip a ``@N`` name-table occurrence marker from a defaultproperties key.

        :meth:`UnPackage.resolve_name_index` appends ``@N`` when a name string is
        duplicated in the name table (a fidelity marker for XML round-trips), but
        ``@`` is not a legal identifier character, so a defaultproperties key/field
        must render as the bare name. The referenced entry is unambiguous by
        string, so dropping the marker is safe.

        Args:
            name (str): A property/field name, possibly suffixed with ``@N``.

        Returns:
            str: The name without any trailing ``@N`` occurrence marker.
        """
        at = name.rfind("@")
        if at > 0 and name[at + 1 :].isdigit():
            return name[:at]
        return name

    def _dp_struct(self, sf: dict) -> str:
        """Render a decoded struct value as ``(Field=value,...)``.

        The UT2004 defaultproperties parser reads struct-literal members with no
        whitespace skipping after the comma, so a ``", "`` separator would make
        every member after the first parse as ``" Name"`` ("Unknown member").
        Members are therefore joined with a bare comma.

        Args:
            sf (dict): The decoded struct fields (tags or fields form).

        Returns:
            str: The parenthesized struct-literal text.
        """
        parts: List[str] = []
        if sf.get("tags") is not None:
            for t in sf["tags"]:
                idx = t.get("array_index", 0)
                nm = self._dp_name(t["name"])
                key = f"{nm}({idx})" if idx else nm
                parts.append(f"{key}={self._dp_value(t)}")
        elif sf.get("fields") is not None:
            for k, v in sf["fields"].items():
                k = self._dp_name(k)
                if isinstance(v, dict):
                    parts.append(f"{k}={self._dp_struct(v)}")
                else:
                    parts.append(f"{k}={v}")
        return "(" + ",".join(parts) + ")"

    def _dp_value(self, d: dict) -> str:
        """Render a scalar/struct tag dict as a defaultproperties value.

        Args:
            d (dict): The decoded property tag dict.

        Returns:
            str: The rendered value text.
        """
        t = d.get("type", "")
        if t == "BoolProperty":
            return "True" if d.get("bool_value") else "False"
        if t in ("ObjectProperty", "ClassProperty"):
            return self._object_literal(d.get("_text", ""))
        if t == "StrProperty":
            return _format_string(d.get("_text", ""))
        if t == "NameProperty":
            return _format_string(self._dp_name(d.get("_text", "")))
        if t == "StructProperty":
            sf = d.get("_struct_fields")
            if isinstance(sf, dict):
                return self._dp_struct(sf)
            return d.get("_text", "")
        if t == "ArrayProperty":
            return self._dp_array_inline(d)
        return d.get("_text", "")

    def _dp_array_inline(self, d: dict) -> str:
        """Render a dynamic array as an inline ``(e0,e1,...)`` literal.

        Used for arrays nested inside a struct literal (where the top-level
        ``Name(i)=`` per-element form is not valid). An empty array renders as
        ``()``; a decode failure falls back to whatever ``_text`` held.

        Args:
            d (dict): The decoded ArrayProperty tag dict.

        Returns:
            str: The parenthesized element list.
        """
        elements = d.get("_elements")
        if elements is None:
            return d.get("_text", "")
        inner = d.get("inner_type", "")
        # Bare comma separator — the defaultproperties parser does not skip
        # whitespace after commas inside a struct/array literal.
        return "(" + ",".join(self._dp_element(e, inner) for e in elements) + ")"

    def _dp_delegate_lines(self, tag, subobject: bool = False) -> List[str]:
        """Render a DelegateProperty default as ``Delegate=Handler``.

        A delegate default serializes as an object reference (the bound
        object, normally the class default object) followed by the bound
        function's name index. UnrealScript source assigns the delegate by
        name (``OnClose=InternalOnClose``), so the object part is dropped and
        the ``__<Delegate>__Delegate`` backing-property name is unmangled.

        Inside a subobject block the handler is resolved in the *outer* class's
        scope, so a handler the outer class doesn't define (a component's own
        default handler, e.g. ``GUIButton.InternalOnKeyEvent``) is unbindable
        and redundant — it is dropped.

        Args:
            tag: The DelegateProperty tag to render.
            subobject (bool): True when rendering inside a ``begin object`` block.

        Returns:
            List[str]: A ``\tDelegate=Handler`` line, or [] when dropped.
        """
        name = tag.tag_name.name if tag.tag_name else "?"
        if name.startswith("__") and name.endswith("__Delegate"):
            name = name[2 : -len("__Delegate")]
        buf = io.BytesIO(tag.property_data)
        try:
            read_index(buf)  # bound object (implicit self in source)
            name_idx = read_index(buf)
            func = self._dp_name(self.pkg.resolve_name_index(name_idx))
        except Exception:  # pragma: no cover - defensive
            func = "None"
        if not func or func == "None":
            func = "None"
        if subobject and func != "None" and not self._current_class_has_function(func):
            return []
        return [f"\t{name}={func}"]

    def _dp_element(self, elem, inner_type: str) -> str:
        """Render one dynamic-array element.

        Args:
            elem: The decoded element (dict for structs, else a scalar).
            inner_type (str): The array's inner property type name.

        Returns:
            str: The rendered element value text.
        """
        if isinstance(elem, dict):
            return self._dp_struct(elem)
        short = inner_type.split(".")[-1]
        if short in ("ObjectProperty", "ClassProperty"):
            return self._object_literal(elem)
        if short == "StrProperty":
            return _format_string(elem)
        if short == "NameProperty":
            return _format_string(self._dp_name(elem))
        return str(elem)

    def _enum_prop_names(self, class_obj: UnClass) -> dict:
        """Map name -> UnEnum for every enum-typed byte property in scope.

        A ``var ENetRole RemoteRole;`` (a ``ByteProperty`` with an ``enum_item``)
        serialises its default as a raw ordinal, but UT2004's ``ucc`` rejects a
        bare integer for an enum property in ``defaultproperties`` and silently
        drops the line (so ``RemoteRole=2`` is lost, reverting the actor to the
        inherited role). Callers use this to render the ordinal as its enum
        member name (``RemoteRole=ROLE_SimulatedProxy``) instead. Walks the
        class' own fields and its super chain (resolving cross-package supers).

        Args:
            class_obj (UnClass): The class whose properties are inspected.

        Returns:
            dict: ``property-name -> UnEnum`` for each enum-typed byte property.
        """
        out: dict = {}
        obj = class_obj
        seen: set = set()
        while isinstance(obj, UnStruct):
            for it in self._iter_field_items(obj):
                p = it.object
                if (
                    isinstance(p, UnByteProperty)
                    and getattr(p, "enum_item", None) is not None
                ):
                    enum = p.enum_item.object
                    # A subclass's own (nearer) declaration wins; do not
                    # overwrite it with an ancestor's same-named property.
                    out.setdefault(it.object_name.name, enum)
            sup = getattr(obj, "super_item", None)
            if sup is None:
                break
            nm = sup.object_name.name
            if nm in seen:
                break
            seen.add(nm)
            nxt = getattr(sup, "object", None)
            obj = nxt if isinstance(nxt, UnStruct) else None
        return out

    def _static_array_prop_names(self, class_obj: UnClass) -> set:
        """Collect names of fixed-size (static) array properties on a class.

        A ``var T Foo[N];`` static array serializes its defaults as one tag per
        element, with ``array_index`` 0..N-1. Element 0 must still render as
        ``Foo(0)=`` (not bare ``Foo=``, which is the scalar form) so the array
        round-trips. Callers use this set to force the index on element 0.
        Walks the class' own fields and its super chain (resolving
        cross-package supers where the object is available).

        Args:
            class_obj (UnClass): The class whose properties are inspected.

        Returns:
            set: Names of properties declared with ``array_dim > 1``.
        """
        names: set = set()
        obj = class_obj
        seen: set = set()
        while isinstance(obj, UnStruct):
            for it in self._iter_field_items(obj):
                p = it.object
                if (
                    isinstance(p, UnProperty)
                    and getattr(p, "array_dim", 0)
                    and p.array_dim > 1
                ):
                    names.add(it.object_name.name)
            sup = getattr(obj, "super_item", None)
            if sup is None:
                break
            nm = sup.object_name.name
            if nm in seen:
                break
            seen.add(nm)
            nxt = getattr(sup, "object", None)
            obj = nxt if isinstance(nxt, UnStruct) else None
        return names

    def _dp_lines(
        self, tag, static_arrays: Optional[set] = None, subobject: bool = False
    ) -> List[str]:
        """Return the ``defaultproperties`` line(s) for a single property tag.

        Dynamic arrays (``array<T>``) expand to one ``Name(i)=`` line per
        element. Static arrays (``T Foo[N]``) serialize one tag per element,
        so element 0 is forced to ``Name(0)=`` when ``Name`` is in
        ``static_arrays`` (otherwise a bare ``array_index == 0`` reads as a
        scalar and drops the index).

        Args:
            tag: The property tag to render.
            static_arrays (Optional[set]): Names of static-array properties on
                the owning class; used to force the index on element 0.

        Returns:
            List[str]: One or more ``\tName=value`` source lines.
        """
        if tag.type == UnNameMap.DelegateProperty:
            return self._dp_delegate_lines(tag, subobject=subobject)
        d = tag.to_dict(self.pkg)
        name = self._dp_name(d.get("name", ""))
        t = d.get("type", "")
        if t == "ArrayProperty":
            elements = d.get("_elements")
            if elements is None:
                return [f"\t{name}={self._dp_value(d)}"]
            inner = d.get("inner_type", "")
            return [
                f"\t{name}({i})={self._dp_element(e, inner)}"
                for i, e in enumerate(elements)
            ]
        idx = d.get("array_index", 0)
        is_static = bool(static_arrays) and name in static_arrays
        key = f"{name}({idx})" if (idx or is_static) else name
        # An enum-typed byte property must render its ordinal as the enum member
        # name — ucc rejects a bare int for an enum in defaultproperties and
        # silently drops the line (e.g. `RemoteRole=2` -> the value is lost).
        if t == "ByteProperty":
            enum = self._cur_enum_props.get(name)
            if enum is not None:
                raw = d.get("_text", "")
                if raw.isdigit():
                    member = _BodyDecompiler._enum_member(enum, int(raw))
                    if member is not None:
                        return [f"\t{key}={member}"]
        return [f"\t{key}={self._dp_value(d)}"]

    def _current_class_has_function(self, func_name: str) -> bool:
        """Report whether the class being decompiled defines ``func_name``.

        Walks the current class and its super chain. Used to decide whether a
        subobject's delegate default (``Delegate=Handler``) can compile: the
        defaultproperties parser resolves the handler in the *outer* class's
        scope, so a handler that lives on the component's own class (e.g.
        ``GUIButton.InternalOnKeyEvent``) is unbindable here and is the
        component's inherited default anyway — it must be dropped.

        Args:
            func_name (str): The handler function name to look for.

        Returns:
            bool: True if the current class (or an ancestor) defines it.
        """
        if not func_name:
            return False
        exp = self._cur_class_export
        cls_obj = exp.object if exp else None
        seen: set = set()
        while isinstance(cls_obj, UnClass) and id(cls_obj) not in seen:
            seen.add(id(cls_obj))
            child = cls_obj.children
            while child is not None:
                obj = child.object
                if (
                    isinstance(obj, UnFunction)
                    and getattr(child, "object_name", None) is not None
                    and child.object_name.name.lower() == func_name.lower()
                ):
                    return True
                child = obj.next_item if isinstance(obj, UnField) else None
            sup = getattr(cls_obj, "super_item", None)
            cls_obj = getattr(sup, "object", None) if sup is not None else None
        return False

    def _class_subobjects(self, class_obj: UnClass) -> List[UnExport]:
        """Return the embedded subobject (component) exports of a class.

        These are exports whose outer is the class but which are plain object
        instances (a ``Shader``, ``SpriteEmitter``, ``GUIButton``, …) rather
        than code fields (functions, vars, structs, enums, states). They must be
        emitted as ``begin object`` blocks in the class's defaultproperties so
        that property defaults referencing them resolve.

        Args:
            class_obj (UnClass): The class whose subobjects are collected.

        Returns:
            List[UnExport]: The subobject exports, in package (export) order.
        """
        class_export = getattr(class_obj, "export", None)
        if class_export is None:
            return []
        subs: List[UnExport] = []
        for exp in self.pkg.exports:
            if exp.group_item is not class_export:
                continue
            if isinstance(exp.object, UnField):
                continue  # code field (function/var/struct/enum/state), not a component
            if exp.class_name_string == "Core.TextBuffer":
                continue  # the class's script-source buffer, not a component
            subs.append(exp)
        return subs

    def _subobject_tags(self, exp: UnExport) -> list:
        """Return a subobject export's tagged properties, parsing on demand.

        Component classes (Shader, GUIButton, …) usually have no dedicated
        object type, so ``exp.object`` is ``None`` after loading. In that case
        parse the tagged properties from the raw export bytes with a throwaway
        :class:`UnDefaultObject` — this never touches ``exp.object`` or the
        stored bytes, so serialization/round-tripping is unaffected.

        Args:
            exp (UnExport): The subobject export.

        Returns:
            list: The subobject's tagged-property list (possibly empty).
        """
        tags = getattr(exp.object, "tagged_properties", None)
        if tags:
            return tags
        if getattr(exp, "export_data", None):
            probe = UnDefaultObject(exp)
            try:
                probe.parse()
            except Exception:  # pragma: no cover - defensive
                return []
            return probe.tagged_properties
        return []

    def _format_subobjects(self, subs: List[UnExport]) -> List[str]:
        """Render ``begin object … end object`` blocks for a class's subobjects.

        Args:
            subs (List[UnExport]): The subobject exports to render.

        Returns:
            List[str]: The block source lines (tab-indented for inside
                ``defaultproperties``).
        """
        lines: List[str] = []
        for exp in subs:
            cls = exp.class_name_string or "Object"
            lines.append(f'\tbegin object name="{exp.object_name.name}" class={cls}')
            for tag in self._subobject_tags(exp):
                try:
                    for ln in self._dp_lines(tag, subobject=True):
                        lines.append("\t" + ln)  # extra indent inside the block
                except Exception as exc:  # pragma: no cover - defensive
                    nm = tag.tag_name.name if tag.tag_name else "?"
                    lines.append(f"\t\t// {nm}: {exc}")
            lines.append("\tend object")
        return lines

    def _format_defaultproperties(self, class_obj: UnClass) -> List[str]:
        """Render the ``defaultproperties`` block of a class.

        Args:
            class_obj (UnClass): The class whose default properties are
                rendered.

        Returns:
            List[str]: The block's source lines, or [] if there are none.
        """
        props = class_obj.default_properties
        subs = self._class_subobjects(class_obj)
        # Record subobject names first so property values that reference them
        # render as bare names (via _object_literal).
        self._cur_subobject_names = {e.object_name.name for e in subs}
        self._cur_enum_props = self._enum_prop_names(class_obj)
        if not props and not subs:
            return []
        lines = ["", "defaultproperties", "{"]
        # Subobject definitions go first — the property assignments below refer
        # to them by name.
        lines.extend(self._format_subobjects(subs))
        static_arrays = self._static_array_prop_names(class_obj)
        for tag in props:
            try:
                lines.extend(self._dp_lines(tag, static_arrays))
            except Exception as exc:  # pragma: no cover - defensive
                nm = tag.tag_name.name if tag.tag_name else "?"
                lines.append(f"\t// {nm}: {exc}")
        lines.append("}")
        return lines

    def decompile_class(self, item: UnExport) -> str:
        """Decompile a single class export into ``.uc`` source text.

        Args:
            item (UnExport): The class export to decompile.

        Returns:
            str: The complete ``.uc`` source for the class.
        """
        self.ind.level = 0
        class_obj = item.object
        # Reset per-class type-qualification context. The body is built first so
        # every struct/enum type reference can register a dependsOn target that
        # the header (built afterwards) emits.
        self._cur_ancestors = self._ancestor_class_names(class_obj)
        self._cur_ancestors.add(item.object_name.name)
        self._depends_on = []
        self._cur_class_export = item
        self._cur_subobject_names = set()
        self._cur_enum_props = {}

        consts = self._format_consts(class_obj)
        enums = self._format_enums(class_obj)
        structs = self._format_structs(class_obj)
        member_vars = self._format_member_vars(class_obj)
        replication = self._format_replication(class_obj)
        functions = self._format_functions(class_obj)
        states = self._format_states(class_obj)
        defaults = self._format_defaultproperties(class_obj)

        lines: List[str] = []
        lines.extend(self._format_class_header(item))

        if consts:
            lines.append("")
            lines.extend(consts)

        if enums:
            lines.append("")
            lines.extend(enums)

        if structs:
            lines.extend(structs)

        if member_vars:
            lines.append("")
            lines.extend(member_vars)

        if replication:
            lines.extend(replication)

        if functions:
            lines.extend(functions)

        if states:
            lines.extend(states)

        lines.extend(defaults)

        source = "\n".join(lines).rstrip() + "\n"
        if self.simplify:
            source = self._strip_unused_consts(source)
        return source

    @staticmethod
    def _strip_unused_consts(source: str) -> str:
        """Drop ``const`` declarations whose name appears nowhere else.

        Const references compile to inline literals, so a genuinely-used
        const still shows up in array dimensions, other const values, or
        default properties; a declaration whose identifier occurs only once
        (its own definition) is obfuscation noise and is removed.

        Args:
            source (str): The full class source text.

        Returns:
            str: The source with unused const declarations removed.
        """
        const_re = re.compile(r"^\s*const\s+(\S+)\s*=")
        kept: List[str] = []
        for line in source.split("\n"):
            m = const_re.match(line)
            if m:
                name = m.group(1)
                if re.fullmatch(r"\w+", name):
                    occurrences = len(
                        re.findall(r"\b" + re.escape(name) + r"\b", source)
                    )
                else:
                    occurrences = source.count(name)
                if occurrences <= 1:
                    continue
            kept.append(line)
        return "\n".join(kept)

    # ------------------------------------------------------------------ #
    #  Package-level entry point
    # ------------------------------------------------------------------ #

    def class_exports(self) -> List[UnExport]:
        """Return every locally-defined class export in the package.

        Returns:
            List[UnExport]: The top-level class exports (no group parent).
        """
        return [
            exp
            for exp in self.pkg.exports
            if isinstance(exp.object, UnClass) and exp.group_item is None
        ]

    def decompile_to_folder(self, output_dir: str) -> List[str]:
        """Decompile every class into ``<output_dir>/<ClassName>.uc``.

        Args:
            output_dir (str): The destination directory (created if missing).

        Returns:
            List[str]: The list of written file paths.
        """
        os.makedirs(output_dir, exist_ok=True)
        written: List[str] = []
        for item in self.class_exports():
            source = self.decompile_class(item)
            path = os.path.join(output_dir, f"{item.object_name.name}.uc")
            _write_uc(path, source)
            written.append(path)
        return written

    # ------------------------------------------------------------------ #
    #  Source extraction (embedded ScriptText, not decompiled bytecode)
    # ------------------------------------------------------------------ #

    @staticmethod
    def _class_source_text(item: UnExport) -> Optional[str]:
        """Return the original UnrealScript source embedded in a class.

        A compiled class keeps the source it was built from in a
        ``ScriptText`` text buffer (unless it was stripped, e.g. by
        obfuscation).  The buffer holds the class body only — the
        ``defaultproperties`` block is compiled into binary defaults and is
        not part of this text.  A buffer that is missing or only whitespace
        is treated as stripped and reported as ``None``.

        Args:
            item (UnExport): The class export to read source from.

        Returns:
            Optional[str]: The embedded source text, or ``None`` when the
                class carries no meaningful script buffer.
        """
        class_obj = item.object
        st = getattr(class_obj, "script_text", None)
        buf = st.object if st is not None else None
        if isinstance(buf, UnTextBuffer) and buf.script_text.strip():
            return buf.script_text
        return None

    def extract_source_class(self, item: UnExport) -> Optional[str]:
        """Reassemble a class's ``.uc`` from its embedded source and defaults.

        Unlike :meth:`decompile_class`, the class body is taken verbatim from
        the embedded ``ScriptText`` rather than lifted from bytecode; only the
        ``defaultproperties`` block is reconstructed (exactly as the
        decompiler does), since it is not stored as text.

        Args:
            item (UnExport): The class export to extract.

        Returns:
            Optional[str]: The complete ``.uc`` source, or ``None`` when the
                class carries no embedded source.
        """
        source = self._class_source_text(item)
        if source is None:
            return None
        self.ind.level = 0
        class_obj = item.object
        # Normalise the embedded line endings to LF to match written output.
        body = source.replace("\r\n", "\n").replace("\r", "\n").rstrip()
        lines: List[str] = [body]
        lines.extend(self._format_defaultproperties(class_obj))
        return "\n".join(lines).rstrip() + "\n"

    def extract_source_to_folder(self, output_dir: str) -> List[str]:
        """Write each class's embedded source to ``<output_dir>/<ClassName>.uc``.

        Classes whose source was stripped (no ``ScriptText`` buffer) are
        skipped rather than emitted empty.

        Args:
            output_dir (str): The destination directory (created if missing).

        Returns:
            List[str]: The list of written file paths.
        """
        os.makedirs(output_dir, exist_ok=True)
        written: List[str] = []
        for item in self.class_exports():
            source = self.extract_source_class(item)
            if source is None:
                continue
            path = os.path.join(output_dir, f"{item.object_name.name}.uc")
            _write_uc(path, source)
            written.append(path)
        return written
