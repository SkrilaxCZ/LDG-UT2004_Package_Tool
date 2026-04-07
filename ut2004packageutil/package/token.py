"""Unreal script token types, classes, and stream parser.

Implements the token serialization system used by UT2004 packages.

In-memory type sizes for the 32-bit UT2004 archive format:
    byte         1 byte      serialized as 1 raw byte
    word         2 bytes     serialized as 2 raw bytes LE
    int          4 bytes     serialized as 4 raw bytes LE
    float        4 bytes     serialized as 4 raw bytes LE
    object ref   4 bytes     serialized as compact index
    name ref     4 bytes     serialized as compact index
    label entry  8 bytes     serialized as compact index + 4-byte int
"""

import io
import struct
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import IntEnum
from typing import Any, BinaryIO, Dict, Iterator, List, Optional, Tuple, Type

from ut2004packageutil.utils.io_utils import read_index, write_index
from ut2004packageutil.utils.struct_utils import (
    read_byte,
    read_float,
    read_int,
    read_word,
    write_byte,
    write_float,
    write_int,
    write_word,
)

# ===================================================================== #
#  Enums
# ===================================================================== #


class UnScriptTokenType(IntEnum):
    """Bytecode token type identifiers for UnrealScript expressions."""

    # Variable references
    LocalVariable = 0x00
    InstanceVariable = 0x01
    DefaultVariable = 0x02

    # Tokens
    Return = 0x04
    Switch = 0x05
    Jump = 0x06
    JumpIfNot = 0x07
    Stop = 0x08
    Assert = 0x09
    Case = 0x0A
    Nothing = 0x0B
    LabelTable = 0x0C
    GotoLabel = 0x0D
    EatString = 0x0E
    Let = 0x0F
    DynArrayElement = 0x10
    New = 0x11
    ClassContext = 0x12
    MetaCast = 0x13
    LetBool = 0x14
    EndFunctionParms = 0x16
    Self = 0x17
    Skip = 0x18
    Context = 0x19
    ArrayElement = 0x1A
    VirtualFunction = 0x1B
    FinalFunction = 0x1C
    IntConst = 0x1D
    FloatConst = 0x1E
    StringConst = 0x1F
    ObjectConst = 0x20
    NameConst = 0x21
    RotationConst = 0x22
    VectorConst = 0x23
    ByteConst = 0x24
    IntZero = 0x25
    IntOne = 0x26
    TrueToken = 0x27
    FalseToken = 0x28
    NativeParm = 0x29
    NoObject = 0x2A
    IntConstByte = 0x2C
    BoolVariable = 0x2D
    DynamicCast = 0x2E
    Iterator = 0x2F
    IteratorPop = 0x30
    IteratorNext = 0x31
    StructCmpEq = 0x32
    StructCmpNe = 0x33
    UnicodeStringConst = 0x34
    RangeConst = 0x35
    StructMember = 0x36
    DynArrayLength = 0x37
    GlobalFunction = 0x38
    PrimitiveCast = 0x39
    DynArrayInsert = 0x40
    DynArrayRemove = 0x41
    DebugInfo = 0x42
    DelegateFunction = 0x43
    DelegateProperty = 0x44
    LetDelegate = 0x45
    PointerConst = 0x46
    EndOfScript = 0x47

    # Native function ranges
    ExtendedNative = 0x60
    FirstNative = 0x70


class UnCastType(IntEnum):
    """Primitive cast type identifiers."""

    RotatorToVector = 0x39
    ByteToInt = 0x3A
    ByteToBool = 0x3B
    ByteToFloat = 0x3C
    IntToByte = 0x3D
    IntToBool = 0x3E
    IntToFloat = 0x3F
    BoolToByte = 0x40
    BoolToInt = 0x41
    BoolToFloat = 0x42
    FloatToByte = 0x43
    FloatToInt = 0x44
    FloatToBool = 0x45
    ObjectToBool = 0x47
    NameToBool = 0x48
    StringToByte = 0x49
    StringToInt = 0x4A
    StringToBool = 0x4B
    StringToFloat = 0x4C
    StringToVector = 0x4D
    StringToRotator = 0x4E
    VectorToBool = 0x4F
    VectorToRotator = 0x50
    RotatorToBool = 0x51
    ByteToString = 0x52
    IntToString = 0x53
    BoolToString = 0x54
    FloatToString = 0x55
    ObjectToString = 0x56
    NameToString = 0x57
    VectorToString = 0x58
    RotatorToString = 0x59
    Max = 0xFF


# ===================================================================== #
#  Data structures
# ===================================================================== #


@dataclass
class UnLabelEntry:
    """A single entry in a label table: name index + int code offset."""

    name_index: int  # compact index into name table
    icode: int  # code offset for this label


# ===================================================================== #
#  In-memory type sizes (32-bit UT2004 archive format)
# ===================================================================== #

SIZE_BYTE: int = 1
SIZE_WORD: int = 2
SIZE_INT: int = 4
SIZE_FLOAT: int = 4
SIZE_OBJECT_REF: int = 4  # pointer-sized field on the original 32-bit target
SIZE_FNAME: int = 4
SIZE_LABEL_ENTRY: int = SIZE_FNAME + SIZE_INT


# ===================================================================== #
#  Base token class
# ===================================================================== #


class UnToken(ABC):
    """Abstract base class for all UnrealScript bytecode tokens."""

    token_type: UnScriptTokenType  # Set by each subclass at class level

    def __init__(self) -> None:
        """Initialize the token with a zeroed ``icode_start`` position."""
        self.icode_start: int = 0

    @abstractmethod
    def parse(self, reader: BinaryIO, icode: int, parser: "TokenStreamParser") -> int:
        """Parse token payload from the reader.

        Args:
            reader (BinaryIO): Binary stream positioned after the opcode byte.
            icode (int): Current instruction-code offset before the payload.
            parser (TokenStreamParser): Parser driving the token stream.

        Returns:
            int: The updated instruction-code offset after the payload.
        """
        ...

    @abstractmethod
    def serialize(
        self, writer: BinaryIO, icode: int, parser: "TokenStreamParser"
    ) -> int:
        """Serialize token payload to the writer.

        Args:
            writer (BinaryIO): Binary stream positioned after the opcode byte.
            icode (int): Current instruction-code offset before the payload.
            parser (TokenStreamParser): Parser driving the token stream.

        Returns:
            int: The updated instruction-code offset after the payload.
        """
        ...

    @abstractmethod
    def to_dict(self, parser: "TokenStreamParser") -> Dict[str, Any]:
        """Return a dict representation of this token.

        Args:
            parser (TokenStreamParser): Parser used to resolve references.

        Returns:
            Dict[str, Any]: The dict representation of this token.
        """
        ...

    @abstractmethod
    def from_dict(self, data: Dict[str, Any], parser: "TokenStreamParser") -> None:
        """Populate this token from a dict representation.

        Args:
            data (Dict[str, Any]): Dict representation to read from.
            parser (TokenStreamParser): Parser used to link references.
        """
        ...

    def __repr__(self) -> str:
        """Return a debug string with the class name and icode position.

        Returns:
            str: The formatted representation.
        """
        return f"{self.__class__.__name__}(icode=0x{self.icode_start:04X})"


# ===================================================================== #
#  7.1 — No-data tokens
# ===================================================================== #


class _UnTokenNoData(UnToken):
    """Mixin for tokens that carry no payload beyond the opcode byte."""

    def parse(self, reader: BinaryIO, icode: int, parser: "TokenStreamParser") -> int:
        """Parse the (empty) payload; the icode is unchanged.

        Args:
            reader (BinaryIO): Binary stream positioned after the opcode byte.
            icode (int): Current instruction-code offset.
            parser (TokenStreamParser): Parser driving the token stream.

        Returns:
            int: The unchanged instruction-code offset.
        """
        return icode

    def serialize(
        self, writer: BinaryIO, icode: int, parser: "TokenStreamParser"
    ) -> int:
        """Serialize the (empty) payload; the icode is unchanged.

        Args:
            writer (BinaryIO): Binary stream positioned after the opcode byte.
            icode (int): Current instruction-code offset.
            parser (TokenStreamParser): Parser driving the token stream.

        Returns:
            int: The unchanged instruction-code offset.
        """
        return icode

    def to_dict(self, parser: "TokenStreamParser") -> Dict[str, Any]:
        """Return a dict holding only the token type name.

        Args:
            parser (TokenStreamParser): Parser used to resolve references.

        Returns:
            Dict[str, Any]: The dict representation of this token.
        """
        return {"Type": self.token_type.name}

    def from_dict(self, data: Dict[str, Any], parser: "TokenStreamParser") -> None:
        """Populate the token from a dict; there is no payload to read.

        Args:
            data (Dict[str, Any]): Dict representation to read from.
            parser (TokenStreamParser): Parser used to link references.
        """
        pass


class UnTokenBoolVariable(UnToken):
    """Boolean variable read: a single wrapped sub-expression.

    ``EX_BoolVariable`` is a marker emitted when a bool property is used
    as a standalone boolean value; the actual variable expression follows
    inline.  Parsing/serialising the wrapped expression keeps the byte
    stream identical while giving the decompiler a proper expression tree.
    """

    token_type = UnScriptTokenType.BoolVariable

    def __init__(self) -> None:
        """Initialize the token with an empty wrapped expression."""
        super().__init__()
        self.expression: Optional[UnToken] = None

    def parse(self, reader: BinaryIO, icode: int, parser: "TokenStreamParser") -> int:
        """Parse the wrapped variable sub-expression.

        Args:
            reader (BinaryIO): Binary stream positioned after the opcode byte.
            icode (int): Current instruction-code offset.
            parser (TokenStreamParser): Parser driving the token stream.

        Returns:
            int: The updated instruction-code offset.
        """
        self.expression, icode = parser.parse_expr(reader, icode)
        return icode

    def serialize(
        self, writer: BinaryIO, icode: int, parser: "TokenStreamParser"
    ) -> int:
        """Serialize the wrapped variable sub-expression.

        Args:
            writer (BinaryIO): Binary stream positioned after the opcode byte.
            icode (int): Current instruction-code offset.
            parser (TokenStreamParser): Parser driving the token stream.

        Returns:
            int: The updated instruction-code offset.
        """
        return parser.serialize_expr(writer, self.expression, icode)

    def to_dict(self, parser: "TokenStreamParser") -> Dict[str, Any]:
        """Return a dict with the token type and wrapped expression.

        Args:
            parser (TokenStreamParser): Parser used to resolve references.

        Returns:
            Dict[str, Any]: The dict representation of this token.
        """
        return {
            "Type": self.token_type.name,
            "Expression": parser.token_to_dict(self.expression),
        }

    def from_dict(self, data: Dict[str, Any], parser: "TokenStreamParser") -> None:
        """Populate the token from a dict, restoring the wrapped expression.

        Args:
            data (Dict[str, Any]): Dict representation to read from.
            parser (TokenStreamParser): Parser used to link references.
        """
        self.expression = parser.token_from_dict(data.get("Expression", {}))


class UnTokenNothing(_UnTokenNoData):
    """No-op token (``EX_Nothing``); emits no payload."""

    token_type = UnScriptTokenType.Nothing


class UnTokenEndOfScript(_UnTokenNoData):
    """End-of-script marker token; emits no payload."""

    token_type = UnScriptTokenType.EndOfScript


class UnTokenEndFunctionParms(_UnTokenNoData):
    """End-of-function-parameters marker token; emits no payload."""

    token_type = UnScriptTokenType.EndFunctionParms


class UnTokenIntZero(_UnTokenNoData):
    """Integer literal zero token; emits no payload."""

    token_type = UnScriptTokenType.IntZero


class UnTokenIntOne(_UnTokenNoData):
    """Integer literal one token; emits no payload."""

    token_type = UnScriptTokenType.IntOne


class UnTokenTrue(_UnTokenNoData):
    """Boolean literal true token; emits no payload."""

    token_type = UnScriptTokenType.TrueToken


class UnTokenFalse(_UnTokenNoData):
    """Boolean literal false token; emits no payload."""

    token_type = UnScriptTokenType.FalseToken


class UnTokenNoObject(_UnTokenNoData):
    """None object reference token; emits no payload."""

    token_type = UnScriptTokenType.NoObject


class UnTokenSelf(_UnTokenNoData):
    """Self object reference token; emits no payload."""

    token_type = UnScriptTokenType.Self


class UnTokenIteratorPop(_UnTokenNoData):
    """Iterator pop token; emits no payload."""

    token_type = UnScriptTokenType.IteratorPop


class UnTokenStop(_UnTokenNoData):
    """State-code stop token; emits no payload."""

    token_type = UnScriptTokenType.Stop


class UnTokenIteratorNext(_UnTokenNoData):
    """Iterator next token; emits no payload."""

    token_type = UnScriptTokenType.IteratorNext


# ===================================================================== #
#  7.2 — Object reference tokens (compact index → icode += SIZE_OBJECT_REF)
# ===================================================================== #


class _UnTokenObjectRef(UnToken):
    """Token that stores a single compact-index object reference."""

    def __init__(self) -> None:
        """Initialize the token with a zeroed object reference."""
        super().__init__()
        self.object_ref: int = 0

    def parse(self, reader: BinaryIO, icode: int, parser: "TokenStreamParser") -> int:
        """Parse the compact-index object reference.

        Args:
            reader (BinaryIO): Binary stream positioned after the opcode byte.
            icode (int): Current instruction-code offset.
            parser (TokenStreamParser): Parser driving the token stream.

        Returns:
            int: The updated instruction-code offset.
        """
        self.object_ref = read_index(reader)
        return icode + SIZE_OBJECT_REF

    def serialize(
        self, writer: BinaryIO, icode: int, parser: "TokenStreamParser"
    ) -> int:
        """Serialize the compact-index object reference.

        Args:
            writer (BinaryIO): Binary stream positioned after the opcode byte.
            icode (int): Current instruction-code offset.
            parser (TokenStreamParser): Parser driving the token stream.

        Returns:
            int: The updated instruction-code offset.
        """
        write_index(writer, self.object_ref)
        return icode + SIZE_OBJECT_REF

    def to_dict(self, parser: "TokenStreamParser") -> Dict[str, Any]:
        """Return a dict with the token type and resolved object reference.

        Args:
            parser (TokenStreamParser): Parser used to resolve references.

        Returns:
            Dict[str, Any]: The dict representation of this token.
        """
        return {
            "Type": self.token_type.name,
            "ObjectRef": parser.resolve_object_ref(self.object_ref),
        }

    def from_dict(self, data: Dict[str, Any], parser: "TokenStreamParser") -> None:
        """Populate the token from a dict, linking the object reference.

        Args:
            data (Dict[str, Any]): Dict representation to read from.
            parser (TokenStreamParser): Parser used to link references.
        """
        self.object_ref = parser.link_object_ref(data.get("ObjectRef", ""))


class UnTokenLocalVariable(_UnTokenObjectRef):
    """Local variable reference token (object ref to the local property)."""

    token_type = UnScriptTokenType.LocalVariable


class UnTokenInstanceVariable(_UnTokenObjectRef):
    """Instance variable reference token (object ref to the property)."""

    token_type = UnScriptTokenType.InstanceVariable


class UnTokenDefaultVariable(_UnTokenObjectRef):
    """Default (default properties) variable reference token."""

    token_type = UnScriptTokenType.DefaultVariable


class UnTokenNativeParm(_UnTokenObjectRef):
    """Native parameter reference token (object ref to the property)."""

    token_type = UnScriptTokenType.NativeParm


# ===================================================================== #
#  7.3 — Constant value tokens
# ===================================================================== #


class UnTokenIntConst(UnToken):
    """Integer constant token holding a 4-byte little-endian int value."""

    token_type = UnScriptTokenType.IntConst

    def __init__(self) -> None:
        """Initialize the token with a zero value."""
        super().__init__()
        self.value: int = 0

    def parse(self, reader: BinaryIO, icode: int, parser: "TokenStreamParser") -> int:
        """Parse the 4-byte integer value.

        Args:
            reader (BinaryIO): Binary stream positioned after the opcode byte.
            icode (int): Current instruction-code offset.
            parser (TokenStreamParser): Parser driving the token stream.

        Returns:
            int: The updated instruction-code offset.
        """
        self.value = read_int(reader)
        return icode + SIZE_INT

    def serialize(
        self, writer: BinaryIO, icode: int, parser: "TokenStreamParser"
    ) -> int:
        """Serialize the 4-byte integer value.

        Args:
            writer (BinaryIO): Binary stream positioned after the opcode byte.
            icode (int): Current instruction-code offset.
            parser (TokenStreamParser): Parser driving the token stream.

        Returns:
            int: The updated instruction-code offset.
        """
        write_int(writer, self.value)
        return icode + SIZE_INT

    def to_dict(self, parser: "TokenStreamParser") -> Dict[str, Any]:
        """Return a dict with the token type and integer value.

        Args:
            parser (TokenStreamParser): Parser used to resolve references.

        Returns:
            Dict[str, Any]: The dict representation of this token.
        """
        return {"Type": "IntConst", "Value": self.value}

    def from_dict(self, data: Dict[str, Any], parser: "TokenStreamParser") -> None:
        """Populate the token from a dict, reading the integer value.

        Args:
            data (Dict[str, Any]): Dict representation to read from.
            parser (TokenStreamParser): Parser used to link references.
        """
        self.value = int(data.get("Value", 0))


class UnTokenFloatConst(UnToken):
    """Float constant token holding a 4-byte little-endian float value."""

    token_type = UnScriptTokenType.FloatConst

    def __init__(self) -> None:
        """Initialize the token with a zero value."""
        super().__init__()
        self.value: float = 0.0

    def parse(self, reader: BinaryIO, icode: int, parser: "TokenStreamParser") -> int:
        """Parse the 4-byte float value.

        Args:
            reader (BinaryIO): Binary stream positioned after the opcode byte.
            icode (int): Current instruction-code offset.
            parser (TokenStreamParser): Parser driving the token stream.

        Returns:
            int: The updated instruction-code offset.
        """
        self.value = read_float(reader)
        return icode + SIZE_FLOAT

    def serialize(
        self, writer: BinaryIO, icode: int, parser: "TokenStreamParser"
    ) -> int:
        """Serialize the 4-byte float value.

        Args:
            writer (BinaryIO): Binary stream positioned after the opcode byte.
            icode (int): Current instruction-code offset.
            parser (TokenStreamParser): Parser driving the token stream.

        Returns:
            int: The updated instruction-code offset.
        """
        write_float(writer, self.value)
        return icode + SIZE_FLOAT

    def to_dict(self, parser: "TokenStreamParser") -> Dict[str, Any]:
        """Return a dict with the token type and float value.

        Args:
            parser (TokenStreamParser): Parser used to resolve references.

        Returns:
            Dict[str, Any]: The dict representation of this token.
        """
        return {"Type": "FloatConst", "Value": self.value}

    def from_dict(self, data: Dict[str, Any], parser: "TokenStreamParser") -> None:
        """Populate the token from a dict, reading the float value.

        Args:
            data (Dict[str, Any]): Dict representation to read from.
            parser (TokenStreamParser): Parser used to link references.
        """
        self.value = float(data.get("Value", 0.0))


class UnTokenByteConst(UnToken):
    """Byte constant token holding a single 1-byte value."""

    token_type = UnScriptTokenType.ByteConst

    def __init__(self) -> None:
        """Initialize the token with a zero value."""
        super().__init__()
        self.value: int = 0

    def parse(self, reader: BinaryIO, icode: int, parser: "TokenStreamParser") -> int:
        """Parse the single byte value.

        Args:
            reader (BinaryIO): Binary stream positioned after the opcode byte.
            icode (int): Current instruction-code offset.
            parser (TokenStreamParser): Parser driving the token stream.

        Returns:
            int: The updated instruction-code offset.
        """
        self.value = read_byte(reader)
        return icode + SIZE_BYTE

    def serialize(
        self, writer: BinaryIO, icode: int, parser: "TokenStreamParser"
    ) -> int:
        """Serialize the single byte value.

        Args:
            writer (BinaryIO): Binary stream positioned after the opcode byte.
            icode (int): Current instruction-code offset.
            parser (TokenStreamParser): Parser driving the token stream.

        Returns:
            int: The updated instruction-code offset.
        """
        write_byte(writer, self.value)
        return icode + SIZE_BYTE

    def to_dict(self, parser: "TokenStreamParser") -> Dict[str, Any]:
        """Return a dict with the token type and byte value.

        Args:
            parser (TokenStreamParser): Parser used to resolve references.

        Returns:
            Dict[str, Any]: The dict representation of this token.
        """
        return {"Type": "ByteConst", "Value": self.value}

    def from_dict(self, data: Dict[str, Any], parser: "TokenStreamParser") -> None:
        """Populate the token from a dict, reading the byte value.

        Args:
            data (Dict[str, Any]): Dict representation to read from.
            parser (TokenStreamParser): Parser used to link references.
        """
        self.value = int(data.get("Value", 0))


class UnTokenIntConstByte(UnToken):
    """Integer-from-byte constant token holding a single 1-byte value."""

    token_type = UnScriptTokenType.IntConstByte

    def __init__(self) -> None:
        """Initialize the token with a zero value."""
        super().__init__()
        self.value: int = 0

    def parse(self, reader: BinaryIO, icode: int, parser: "TokenStreamParser") -> int:
        """Parse the single byte value.

        Args:
            reader (BinaryIO): Binary stream positioned after the opcode byte.
            icode (int): Current instruction-code offset.
            parser (TokenStreamParser): Parser driving the token stream.

        Returns:
            int: The updated instruction-code offset.
        """
        self.value = read_byte(reader)
        return icode + SIZE_BYTE

    def serialize(
        self, writer: BinaryIO, icode: int, parser: "TokenStreamParser"
    ) -> int:
        """Serialize the single byte value.

        Args:
            writer (BinaryIO): Binary stream positioned after the opcode byte.
            icode (int): Current instruction-code offset.
            parser (TokenStreamParser): Parser driving the token stream.

        Returns:
            int: The updated instruction-code offset.
        """
        write_byte(writer, self.value)
        return icode + SIZE_BYTE

    def to_dict(self, parser: "TokenStreamParser") -> Dict[str, Any]:
        """Return a dict with the token type and byte value.

        Args:
            parser (TokenStreamParser): Parser used to resolve references.

        Returns:
            Dict[str, Any]: The dict representation of this token.
        """
        return {"Type": "IntConstByte", "Value": self.value}

    def from_dict(self, data: Dict[str, Any], parser: "TokenStreamParser") -> None:
        """Populate the token from a dict, reading the byte value.

        Args:
            data (Dict[str, Any]): Dict representation to read from.
            parser (TokenStreamParser): Parser used to link references.
        """
        self.value = int(data.get("Value", 0))


class UnTokenObjectConst(UnToken):
    """Object constant token holding a compact-index object reference."""

    token_type = UnScriptTokenType.ObjectConst

    def __init__(self) -> None:
        """Initialize the token with a zeroed object reference."""
        super().__init__()
        self.object_ref: int = 0

    def parse(self, reader: BinaryIO, icode: int, parser: "TokenStreamParser") -> int:
        """Parse the compact-index object reference.

        Args:
            reader (BinaryIO): Binary stream positioned after the opcode byte.
            icode (int): Current instruction-code offset.
            parser (TokenStreamParser): Parser driving the token stream.

        Returns:
            int: The updated instruction-code offset.
        """
        self.object_ref = read_index(reader)
        return icode + SIZE_OBJECT_REF

    def serialize(
        self, writer: BinaryIO, icode: int, parser: "TokenStreamParser"
    ) -> int:
        """Serialize the compact-index object reference.

        Args:
            writer (BinaryIO): Binary stream positioned after the opcode byte.
            icode (int): Current instruction-code offset.
            parser (TokenStreamParser): Parser driving the token stream.

        Returns:
            int: The updated instruction-code offset.
        """
        write_index(writer, self.object_ref)
        return icode + SIZE_OBJECT_REF

    def to_dict(self, parser: "TokenStreamParser") -> Dict[str, Any]:
        """Return a dict with the token type and resolved object reference.

        Args:
            parser (TokenStreamParser): Parser used to resolve references.

        Returns:
            Dict[str, Any]: The dict representation of this token.
        """
        return {
            "Type": "ObjectConst",
            "ObjectRef": parser.resolve_object_ref(self.object_ref),
        }

    def from_dict(self, data: Dict[str, Any], parser: "TokenStreamParser") -> None:
        """Populate the token from a dict, linking the object reference.

        Args:
            data (Dict[str, Any]): Dict representation to read from.
            parser (TokenStreamParser): Parser used to link references.
        """
        self.object_ref = parser.link_object_ref(data.get("ObjectRef", ""))


class UnTokenNameConst(UnToken):
    """Name constant token holding a compact-index name-table reference."""

    token_type = UnScriptTokenType.NameConst

    def __init__(self) -> None:
        """Initialize the token with a zeroed name index."""
        super().__init__()
        self.name_index: int = 0

    def parse(self, reader: BinaryIO, icode: int, parser: "TokenStreamParser") -> int:
        """Parse the compact-index name reference.

        Args:
            reader (BinaryIO): Binary stream positioned after the opcode byte.
            icode (int): Current instruction-code offset.
            parser (TokenStreamParser): Parser driving the token stream.

        Returns:
            int: The updated instruction-code offset.
        """
        self.name_index = read_index(reader)
        return icode + SIZE_FNAME

    def serialize(
        self, writer: BinaryIO, icode: int, parser: "TokenStreamParser"
    ) -> int:
        """Serialize the compact-index name reference.

        Args:
            writer (BinaryIO): Binary stream positioned after the opcode byte.
            icode (int): Current instruction-code offset.
            parser (TokenStreamParser): Parser driving the token stream.

        Returns:
            int: The updated instruction-code offset.
        """
        write_index(writer, self.name_index)
        return icode + SIZE_FNAME

    def to_dict(self, parser: "TokenStreamParser") -> Dict[str, Any]:
        """Return a dict with the token type and resolved name reference.

        Args:
            parser (TokenStreamParser): Parser used to resolve references.

        Returns:
            Dict[str, Any]: The dict representation of this token.
        """
        return {
            "Type": "NameConst",
            "NameRef": parser.resolve_name_ref(self.name_index),
        }

    def from_dict(self, data: Dict[str, Any], parser: "TokenStreamParser") -> None:
        """Populate the token from a dict, linking the name reference.

        Args:
            data (Dict[str, Any]): Dict representation to read from.
            parser (TokenStreamParser): Parser used to link references.
        """
        self.name_index = parser.link_name_ref(data.get("NameRef", ""))


class UnTokenRotationConst(UnToken):
    """Rotation constant token holding pitch, yaw and roll integers."""

    token_type = UnScriptTokenType.RotationConst

    def __init__(self) -> None:
        """Initialize the token with zeroed pitch, yaw and roll."""
        super().__init__()
        self.pitch: int = 0
        self.yaw: int = 0
        self.roll: int = 0

    def parse(self, reader: BinaryIO, icode: int, parser: "TokenStreamParser") -> int:
        """Parse the pitch, yaw and roll integers.

        Args:
            reader (BinaryIO): Binary stream positioned after the opcode byte.
            icode (int): Current instruction-code offset.
            parser (TokenStreamParser): Parser driving the token stream.

        Returns:
            int: The updated instruction-code offset.
        """
        self.pitch = read_int(reader)
        self.yaw = read_int(reader)
        self.roll = read_int(reader)
        return icode + SIZE_INT * 3

    def serialize(
        self, writer: BinaryIO, icode: int, parser: "TokenStreamParser"
    ) -> int:
        """Serialize the pitch, yaw and roll integers.

        Args:
            writer (BinaryIO): Binary stream positioned after the opcode byte.
            icode (int): Current instruction-code offset.
            parser (TokenStreamParser): Parser driving the token stream.

        Returns:
            int: The updated instruction-code offset.
        """
        write_int(writer, self.pitch)
        write_int(writer, self.yaw)
        write_int(writer, self.roll)
        return icode + SIZE_INT * 3

    def to_dict(self, parser: "TokenStreamParser") -> Dict[str, Any]:
        """Return a dict with the token type and rotation components.

        Args:
            parser (TokenStreamParser): Parser used to resolve references.

        Returns:
            Dict[str, Any]: The dict representation of this token.
        """
        return {
            "Type": "RotationConst",
            "Pitch": self.pitch,
            "Yaw": self.yaw,
            "Roll": self.roll,
        }

    def from_dict(self, data: Dict[str, Any], parser: "TokenStreamParser") -> None:
        """Populate the token from a dict, reading the rotation components.

        Args:
            data (Dict[str, Any]): Dict representation to read from.
            parser (TokenStreamParser): Parser used to link references.
        """
        self.pitch = int(data.get("Pitch", 0))
        self.yaw = int(data.get("Yaw", 0))
        self.roll = int(data.get("Roll", 0))


class UnTokenVectorConst(UnToken):
    """Vector constant token holding x, y and z float components."""

    token_type = UnScriptTokenType.VectorConst

    def __init__(self) -> None:
        """Initialize the token with zeroed x, y and z components."""
        super().__init__()
        self.x: float = 0.0
        self.y: float = 0.0
        self.z: float = 0.0

    def parse(self, reader: BinaryIO, icode: int, parser: "TokenStreamParser") -> int:
        """Parse the x, y and z float components.

        Args:
            reader (BinaryIO): Binary stream positioned after the opcode byte.
            icode (int): Current instruction-code offset.
            parser (TokenStreamParser): Parser driving the token stream.

        Returns:
            int: The updated instruction-code offset.
        """
        self.x = read_float(reader)
        self.y = read_float(reader)
        self.z = read_float(reader)
        return icode + SIZE_FLOAT * 3

    def serialize(
        self, writer: BinaryIO, icode: int, parser: "TokenStreamParser"
    ) -> int:
        """Serialize the x, y and z float components.

        Args:
            writer (BinaryIO): Binary stream positioned after the opcode byte.
            icode (int): Current instruction-code offset.
            parser (TokenStreamParser): Parser driving the token stream.

        Returns:
            int: The updated instruction-code offset.
        """
        write_float(writer, self.x)
        write_float(writer, self.y)
        write_float(writer, self.z)
        return icode + SIZE_FLOAT * 3

    def to_dict(self, parser: "TokenStreamParser") -> Dict[str, Any]:
        """Return a dict with the token type and vector components.

        Args:
            parser (TokenStreamParser): Parser used to resolve references.

        Returns:
            Dict[str, Any]: The dict representation of this token.
        """
        return {
            "Type": "VectorConst",
            "X": self.x,
            "Y": self.y,
            "Z": self.z,
        }

    def from_dict(self, data: Dict[str, Any], parser: "TokenStreamParser") -> None:
        """Populate the token from a dict, reading the vector components.

        Args:
            data (Dict[str, Any]): Dict representation to read from.
            parser (TokenStreamParser): Parser used to link references.
        """
        self.x = float(data.get("X", 0.0))
        self.y = float(data.get("Y", 0.0))
        self.z = float(data.get("Z", 0.0))


class UnTokenStringConst(UnToken):
    """Null-terminated ANSI string constant.

    Read/written one byte at a time until a zero byte is reached.
    """

    token_type = UnScriptTokenType.StringConst

    def __init__(self) -> None:
        """Initialize the token with an empty string value."""
        super().__init__()
        self.value: str = ""

    def parse(self, reader: BinaryIO, icode: int, parser: "TokenStreamParser") -> int:
        """Parse the null-terminated ANSI string, one byte at a time.

        Args:
            reader (BinaryIO): Binary stream positioned after the opcode byte.
            icode (int): Current instruction-code offset.
            parser (TokenStreamParser): Parser driving the token stream.

        Returns:
            int: The updated instruction-code offset.
        """
        raw = bytearray()
        while True:
            b = read_byte(reader)
            icode += SIZE_BYTE
            raw.append(b)
            if b == 0:
                break
        self.value = raw[:-1].decode("latin-1")
        return icode

    def serialize(
        self, writer: BinaryIO, icode: int, parser: "TokenStreamParser"
    ) -> int:
        """Serialize the string followed by a null terminator byte.

        Args:
            writer (BinaryIO): Binary stream positioned after the opcode byte.
            icode (int): Current instruction-code offset.
            parser (TokenStreamParser): Parser driving the token stream.

        Returns:
            int: The updated instruction-code offset.
        """
        encoded = self.value.encode("latin-1")
        for b in encoded:
            write_byte(writer, b)
            icode += SIZE_BYTE
        write_byte(writer, 0)
        icode += SIZE_BYTE
        return icode

    def to_dict(self, parser: "TokenStreamParser") -> Dict[str, Any]:
        """Return a dict with the token type and string value.

        Args:
            parser (TokenStreamParser): Parser used to resolve references.

        Returns:
            Dict[str, Any]: The dict representation of this token.
        """
        return {"Type": "StringConst", "Value": self.value}

    def from_dict(self, data: Dict[str, Any], parser: "TokenStreamParser") -> None:
        """Populate the token from a dict, reading the string value.

        Args:
            data (Dict[str, Any]): Dict representation to read from.
            parser (TokenStreamParser): Parser used to link references.
        """
        self.value = data.get("Value", "")


class UnTokenUnicodeStringConst(UnToken):
    """Null-terminated Unicode (UTF-16LE) string constant.

    Read/written one 16-bit word at a time until a zero word is reached.
    """

    token_type = UnScriptTokenType.UnicodeStringConst

    def __init__(self) -> None:
        """Initialize the token with an empty string value."""
        super().__init__()
        self.value: str = ""

    def parse(self, reader: BinaryIO, icode: int, parser: "TokenStreamParser") -> int:
        """Parse the null-terminated UTF-16LE string, one word at a time.

        Args:
            reader (BinaryIO): Binary stream positioned after the opcode byte.
            icode (int): Current instruction-code offset.
            parser (TokenStreamParser): Parser driving the token stream.

        Returns:
            int: The updated instruction-code offset.
        """
        chars: List[int] = []
        while True:
            w = read_word(reader)
            icode += SIZE_WORD
            chars.append(w)
            if w == 0:
                break
        # Decode UTF-16 code units (excluding null terminator)
        self.value = "".join(chr(c) for c in chars[:-1])
        return icode

    def serialize(
        self, writer: BinaryIO, icode: int, parser: "TokenStreamParser"
    ) -> int:
        """Serialize the string as words followed by a null terminator word.

        Args:
            writer (BinaryIO): Binary stream positioned after the opcode byte.
            icode (int): Current instruction-code offset.
            parser (TokenStreamParser): Parser driving the token stream.

        Returns:
            int: The updated instruction-code offset.
        """
        for ch in self.value:
            write_word(writer, ord(ch))
            icode += SIZE_WORD
        write_word(writer, 0)
        icode += SIZE_WORD
        return icode

    def to_dict(self, parser: "TokenStreamParser") -> Dict[str, Any]:
        """Return a dict with the token type and string value.

        Args:
            parser (TokenStreamParser): Parser used to resolve references.

        Returns:
            Dict[str, Any]: The dict representation of this token.
        """
        return {"Type": "UnicodeStringConst", "Value": self.value}

    def from_dict(self, data: Dict[str, Any], parser: "TokenStreamParser") -> None:
        """Populate the token from a dict, reading the string value.

        Args:
            data (Dict[str, Any]): Dict representation to read from.
            parser (TokenStreamParser): Parser used to link references.
        """
        self.value = data.get("Value", "")


# ===================================================================== #
#  7.4 — Assignment tokens (two sub-expressions)
# ===================================================================== #


class _UnTokenAssignment(UnToken):
    """Token with variable + assignment sub-expressions."""

    def __init__(self) -> None:
        """Initialize the token with empty variable and assignment expressions."""
        super().__init__()
        self.variable: Optional[UnToken] = None
        self.assignment: Optional[UnToken] = None

    def parse(self, reader: BinaryIO, icode: int, parser: "TokenStreamParser") -> int:
        """Parse the variable and assignment sub-expressions.

        Args:
            reader (BinaryIO): Binary stream positioned after the opcode byte.
            icode (int): Current instruction-code offset.
            parser (TokenStreamParser): Parser driving the token stream.

        Returns:
            int: The updated instruction-code offset.
        """
        self.variable, icode = parser.parse_expr(reader, icode)
        self.assignment, icode = parser.parse_expr(reader, icode)
        return icode

    def serialize(
        self, writer: BinaryIO, icode: int, parser: "TokenStreamParser"
    ) -> int:
        """Serialize the variable and assignment sub-expressions.

        Args:
            writer (BinaryIO): Binary stream positioned after the opcode byte.
            icode (int): Current instruction-code offset.
            parser (TokenStreamParser): Parser driving the token stream.

        Returns:
            int: The updated instruction-code offset.
        """
        icode = parser.serialize_expr(writer, self.variable, icode)
        icode = parser.serialize_expr(writer, self.assignment, icode)
        return icode

    def to_dict(self, parser: "TokenStreamParser") -> Dict[str, Any]:
        """Return a dict with the token type and both sub-expressions.

        Args:
            parser (TokenStreamParser): Parser used to resolve references.

        Returns:
            Dict[str, Any]: The dict representation of this token.
        """
        return {
            "Type": self.token_type.name,
            "Variable": parser.token_to_dict(self.variable),
            "Assignment": parser.token_to_dict(self.assignment),
        }

    def from_dict(self, data: Dict[str, Any], parser: "TokenStreamParser") -> None:
        """Populate the token from a dict, restoring both sub-expressions.

        Args:
            data (Dict[str, Any]): Dict representation to read from.
            parser (TokenStreamParser): Parser used to link references.
        """
        self.variable = parser.token_from_dict(data.get("Variable", {}))
        self.assignment = parser.token_from_dict(data.get("Assignment", {}))


class UnTokenLet(_UnTokenAssignment):
    """Generic value assignment token (``variable = assignment``)."""

    token_type = UnScriptTokenType.Let


class UnTokenLetBool(_UnTokenAssignment):
    """Boolean assignment token (``variable = assignment``)."""

    token_type = UnScriptTokenType.LetBool


class UnTokenLetDelegate(_UnTokenAssignment):
    """Delegate assignment token (``variable = assignment``)."""

    token_type = UnScriptTokenType.LetDelegate


# ===================================================================== #
#  7.5 — Jump / branch tokens
# ===================================================================== #


class UnTokenJump(UnToken):
    """Unconditional jump token holding a 16-bit code offset."""

    token_type = UnScriptTokenType.Jump

    def __init__(self) -> None:
        """Initialize the token with a zero jump offset."""
        super().__init__()
        self.offset: int = 0

    def parse(self, reader: BinaryIO, icode: int, parser: "TokenStreamParser") -> int:
        """Parse the 16-bit jump offset.

        Args:
            reader (BinaryIO): Binary stream positioned after the opcode byte.
            icode (int): Current instruction-code offset.
            parser (TokenStreamParser): Parser driving the token stream.

        Returns:
            int: The updated instruction-code offset.
        """
        self.offset = read_word(reader)
        return icode + SIZE_WORD

    def serialize(
        self, writer: BinaryIO, icode: int, parser: "TokenStreamParser"
    ) -> int:
        """Serialize the 16-bit jump offset.

        Args:
            writer (BinaryIO): Binary stream positioned after the opcode byte.
            icode (int): Current instruction-code offset.
            parser (TokenStreamParser): Parser driving the token stream.

        Returns:
            int: The updated instruction-code offset.
        """
        write_word(writer, self.offset)
        return icode + SIZE_WORD

    def to_dict(self, parser: "TokenStreamParser") -> Dict[str, Any]:
        """Return a dict with the token type and symbolic jump target.

        Args:
            parser (TokenStreamParser): Parser used to resolve references.

        Returns:
            Dict[str, Any]: The dict representation of this token.
        """
        return {"Type": "Jump", "JumpTo": parser.icode_to_label(self.offset)}

    def from_dict(self, data: Dict[str, Any], parser: "TokenStreamParser") -> None:
        """Populate the token from a dict, accepting a symbolic or raw offset.

        Args:
            data (Dict[str, Any]): Dict representation to read from.
            parser (TokenStreamParser): Parser used to link references.
        """
        # Accept either symbolic JumpTo (preferred) or legacy Offset.
        if "JumpTo" in data:
            parser.register_pending_target(self, "offset", str(data["JumpTo"]))
        else:
            self.offset = int(data.get("Offset", 0))


class UnTokenJumpIfNot(UnToken):
    """Conditional jump token: 16-bit offset plus a condition expression."""

    token_type = UnScriptTokenType.JumpIfNot

    def __init__(self) -> None:
        """Initialize the token with a zero offset and empty condition."""
        super().__init__()
        self.offset: int = 0
        self.condition: Optional[UnToken] = None

    def parse(self, reader: BinaryIO, icode: int, parser: "TokenStreamParser") -> int:
        """Parse the 16-bit offset and the condition sub-expression.

        Args:
            reader (BinaryIO): Binary stream positioned after the opcode byte.
            icode (int): Current instruction-code offset.
            parser (TokenStreamParser): Parser driving the token stream.

        Returns:
            int: The updated instruction-code offset.
        """
        self.offset = read_word(reader)
        icode += SIZE_WORD
        self.condition, icode = parser.parse_expr(reader, icode)
        return icode

    def serialize(
        self, writer: BinaryIO, icode: int, parser: "TokenStreamParser"
    ) -> int:
        """Serialize the 16-bit offset and the condition sub-expression.

        Args:
            writer (BinaryIO): Binary stream positioned after the opcode byte.
            icode (int): Current instruction-code offset.
            parser (TokenStreamParser): Parser driving the token stream.

        Returns:
            int: The updated instruction-code offset.
        """
        write_word(writer, self.offset)
        icode += SIZE_WORD
        icode = parser.serialize_expr(writer, self.condition, icode)
        return icode

    def to_dict(self, parser: "TokenStreamParser") -> Dict[str, Any]:
        """Return a dict with the token type, jump target and condition.

        Args:
            parser (TokenStreamParser): Parser used to resolve references.

        Returns:
            Dict[str, Any]: The dict representation of this token.
        """
        return {
            "Type": "JumpIfNot",
            "JumpTo": parser.icode_to_label(self.offset),
            "Condition": parser.token_to_dict(self.condition),
        }

    def from_dict(self, data: Dict[str, Any], parser: "TokenStreamParser") -> None:
        """Populate the token from a dict, restoring offset and condition.

        Args:
            data (Dict[str, Any]): Dict representation to read from.
            parser (TokenStreamParser): Parser used to link references.
        """
        if "JumpTo" in data:
            parser.register_pending_target(self, "offset", str(data["JumpTo"]))
        else:
            self.offset = int(data.get("Offset", 0))
        self.condition = parser.token_from_dict(data.get("Condition", {}))


class UnTokenSwitch(UnToken):
    """Switch token: 8-bit value size plus the switched expression."""

    token_type = UnScriptTokenType.Switch

    def __init__(self) -> None:
        """Initialize the token with a zero value size and empty expression."""
        super().__init__()
        self.value_size: int = 0
        self.expression: Optional[UnToken] = None

    def parse(self, reader: BinaryIO, icode: int, parser: "TokenStreamParser") -> int:
        """Parse the value size byte and the switched sub-expression.

        Args:
            reader (BinaryIO): Binary stream positioned after the opcode byte.
            icode (int): Current instruction-code offset.
            parser (TokenStreamParser): Parser driving the token stream.

        Returns:
            int: The updated instruction-code offset.
        """
        self.value_size = read_byte(reader)
        icode += SIZE_BYTE
        self.expression, icode = parser.parse_expr(reader, icode)
        return icode

    def serialize(
        self, writer: BinaryIO, icode: int, parser: "TokenStreamParser"
    ) -> int:
        """Serialize the value size byte and the switched sub-expression.

        Args:
            writer (BinaryIO): Binary stream positioned after the opcode byte.
            icode (int): Current instruction-code offset.
            parser (TokenStreamParser): Parser driving the token stream.

        Returns:
            int: The updated instruction-code offset.
        """
        write_byte(writer, self.value_size)
        icode += SIZE_BYTE
        icode = parser.serialize_expr(writer, self.expression, icode)
        return icode

    def to_dict(self, parser: "TokenStreamParser") -> Dict[str, Any]:
        """Return a dict with the token type, value size and expression.

        Args:
            parser (TokenStreamParser): Parser used to resolve references.

        Returns:
            Dict[str, Any]: The dict representation of this token.
        """
        return {
            "Type": "Switch",
            "ValueSize": self.value_size,
            "Expression": parser.token_to_dict(self.expression),
        }

    def from_dict(self, data: Dict[str, Any], parser: "TokenStreamParser") -> None:
        """Populate the token from a dict, restoring value size and expression.

        Args:
            data (Dict[str, Any]): Dict representation to read from.
            parser (TokenStreamParser): Parser used to link references.
        """
        self.value_size = int(data.get("ValueSize", 0))
        self.expression = parser.token_from_dict(data.get("Expression", {}))


class UnTokenCase(UnToken):
    """Case statement.

    Reads a 16-bit offset.  If offset != 0xFFFF, also reads a case expression.
    """

    token_type = UnScriptTokenType.Case

    # Sentinel offset meaning "default case" (no expression follows).
    DEFAULT_OFFSET: int = 0xFFFF

    def __init__(self) -> None:
        """Initialize the token with a zero offset and empty expression."""
        super().__init__()
        self.offset: int = 0
        self.expression: Optional[UnToken] = None

    def parse(self, reader: BinaryIO, icode: int, parser: "TokenStreamParser") -> int:
        """Parse the 16-bit offset and, unless default, the case expression.

        Args:
            reader (BinaryIO): Binary stream positioned after the opcode byte.
            icode (int): Current instruction-code offset.
            parser (TokenStreamParser): Parser driving the token stream.

        Returns:
            int: The updated instruction-code offset.
        """
        self.offset = read_word(reader)
        icode += SIZE_WORD
        if self.offset != self.DEFAULT_OFFSET:
            self.expression, icode = parser.parse_expr(reader, icode)
        else:
            self.expression = None
        return icode

    def serialize(
        self, writer: BinaryIO, icode: int, parser: "TokenStreamParser"
    ) -> int:
        """Serialize the 16-bit offset and, unless default, the expression.

        Args:
            writer (BinaryIO): Binary stream positioned after the opcode byte.
            icode (int): Current instruction-code offset.
            parser (TokenStreamParser): Parser driving the token stream.

        Returns:
            int: The updated instruction-code offset.
        """
        write_word(writer, self.offset)
        icode += SIZE_WORD
        if self.offset != self.DEFAULT_OFFSET and self.expression is not None:
            icode = parser.serialize_expr(writer, self.expression, icode)
        return icode

    def to_dict(self, parser: "TokenStreamParser") -> Dict[str, Any]:
        """Return a dict with the token type, jump target and expression.

        Args:
            parser (TokenStreamParser): Parser used to resolve references.

        Returns:
            Dict[str, Any]: The dict representation of this token.
        """
        d: Dict[str, Any] = {"Type": "Case"}
        if self.offset == self.DEFAULT_OFFSET:
            d["JumpTo"] = "default"
        else:
            d["JumpTo"] = parser.icode_to_label(self.offset)
        if self.expression is not None:
            d["Expression"] = parser.token_to_dict(self.expression)
        return d

    def from_dict(self, data: Dict[str, Any], parser: "TokenStreamParser") -> None:
        """Populate the token from a dict, restoring offset and expression.

        Args:
            data (Dict[str, Any]): Dict representation to read from.
            parser (TokenStreamParser): Parser used to link references.
        """
        if "JumpTo" in data:
            jump_to = str(data["JumpTo"])
            if jump_to == "default":
                self.offset = self.DEFAULT_OFFSET
            else:
                parser.register_pending_target(self, "offset", jump_to)
        else:
            self.offset = int(data.get("Offset", 0))
        expr_data = data.get("Expression")
        if expr_data is not None and isinstance(expr_data, dict) and expr_data:
            self.expression = parser.token_from_dict(expr_data)
        else:
            self.expression = None


class UnTokenAssert(UnToken):
    """Assert token: 16-bit source line number plus a condition expression."""

    token_type = UnScriptTokenType.Assert

    def __init__(self) -> None:
        """Initialize the token with a zero line number and empty expression."""
        super().__init__()
        self.line_number: int = 0
        self.expression: Optional[UnToken] = None

    def parse(self, reader: BinaryIO, icode: int, parser: "TokenStreamParser") -> int:
        """Parse the 16-bit line number and the assert sub-expression.

        Args:
            reader (BinaryIO): Binary stream positioned after the opcode byte.
            icode (int): Current instruction-code offset.
            parser (TokenStreamParser): Parser driving the token stream.

        Returns:
            int: The updated instruction-code offset.
        """
        self.line_number = read_word(reader)
        icode += SIZE_WORD
        self.expression, icode = parser.parse_expr(reader, icode)
        return icode

    def serialize(
        self, writer: BinaryIO, icode: int, parser: "TokenStreamParser"
    ) -> int:
        """Serialize the 16-bit line number and the assert sub-expression.

        Args:
            writer (BinaryIO): Binary stream positioned after the opcode byte.
            icode (int): Current instruction-code offset.
            parser (TokenStreamParser): Parser driving the token stream.

        Returns:
            int: The updated instruction-code offset.
        """
        write_word(writer, self.line_number)
        icode += SIZE_WORD
        icode = parser.serialize_expr(writer, self.expression, icode)
        return icode

    def to_dict(self, parser: "TokenStreamParser") -> Dict[str, Any]:
        """Return a dict with the token type, line number and expression.

        Args:
            parser (TokenStreamParser): Parser used to resolve references.

        Returns:
            Dict[str, Any]: The dict representation of this token.
        """
        return {
            "Type": "Assert",
            "LineNumber": self.line_number,
            "Expression": parser.token_to_dict(self.expression),
        }

    def from_dict(self, data: Dict[str, Any], parser: "TokenStreamParser") -> None:
        """Populate the token from a dict, restoring line number and expression.

        Args:
            data (Dict[str, Any]): Dict representation to read from.
            parser (TokenStreamParser): Parser used to link references.
        """
        self.line_number = int(data.get("LineNumber", 0))
        self.expression = parser.token_from_dict(data.get("Expression", {}))


class UnTokenIterator(UnToken):
    """Iterator (foreach) token: iterator expression plus a 16-bit end offset."""

    token_type = UnScriptTokenType.Iterator

    def __init__(self) -> None:
        """Initialize the token with an empty iterator expr and zero end offset."""
        super().__init__()
        self.iterator_expr: Optional[UnToken] = None
        self.end_offset: int = 0

    def parse(self, reader: BinaryIO, icode: int, parser: "TokenStreamParser") -> int:
        """Parse the iterator sub-expression and the 16-bit end offset.

        Args:
            reader (BinaryIO): Binary stream positioned after the opcode byte.
            icode (int): Current instruction-code offset.
            parser (TokenStreamParser): Parser driving the token stream.

        Returns:
            int: The updated instruction-code offset.
        """
        self.iterator_expr, icode = parser.parse_expr(reader, icode)
        self.end_offset = read_word(reader)
        icode += SIZE_WORD
        return icode

    def serialize(
        self, writer: BinaryIO, icode: int, parser: "TokenStreamParser"
    ) -> int:
        """Serialize the iterator sub-expression and the 16-bit end offset.

        Args:
            writer (BinaryIO): Binary stream positioned after the opcode byte.
            icode (int): Current instruction-code offset.
            parser (TokenStreamParser): Parser driving the token stream.

        Returns:
            int: The updated instruction-code offset.
        """
        icode = parser.serialize_expr(writer, self.iterator_expr, icode)
        write_word(writer, self.end_offset)
        icode += SIZE_WORD
        return icode

    def to_dict(self, parser: "TokenStreamParser") -> Dict[str, Any]:
        """Return a dict with the token type, iterator expr and end label.

        Args:
            parser (TokenStreamParser): Parser used to resolve references.

        Returns:
            Dict[str, Any]: The dict representation of this token.
        """
        return {
            "Type": "Iterator",
            "IteratorExpr": parser.token_to_dict(self.iterator_expr),
            "EndLabel": parser.icode_to_label(self.end_offset),
        }

    def from_dict(self, data: Dict[str, Any], parser: "TokenStreamParser") -> None:
        """Populate the token from a dict, restoring iterator expr and offset.

        Args:
            data (Dict[str, Any]): Dict representation to read from.
            parser (TokenStreamParser): Parser used to link references.
        """
        self.iterator_expr = parser.token_from_dict(data.get("IteratorExpr", {}))
        if "EndLabel" in data:
            parser.register_pending_target(self, "end_offset", str(data["EndLabel"]))
        else:
            self.end_offset = int(data.get("EndOffset", 0))


# ===================================================================== #
#  7.6 — Function call tokens
# ===================================================================== #


class UnTokenFinalFunction(UnToken):
    """Call to a final (prebound) function.

    Reads the object ref followed by the parameter expressions until an
    ``EndFunctionParms`` token is reached.
    """

    token_type = UnScriptTokenType.FinalFunction

    def __init__(self) -> None:
        """Initialize with a zeroed function ref, empty params and debug info."""
        super().__init__()
        self.function_ref: int = 0
        self.params: List[UnToken] = []
        self.debug_info: Optional[UnToken] = None

    def parse(self, reader: BinaryIO, icode: int, parser: "TokenStreamParser") -> int:
        """Parse the function ref, parameters and optional debug info.

        Args:
            reader (BinaryIO): Binary stream positioned after the opcode byte.
            icode (int): Current instruction-code offset.
            parser (TokenStreamParser): Parser driving the token stream.

        Returns:
            int: The updated instruction-code offset.
        """
        self.function_ref = read_index(reader)
        icode += SIZE_OBJECT_REF
        self.params = []
        while True:
            param, icode = parser.parse_expr(reader, icode)
            self.params.append(param)
            if isinstance(param, UnTokenEndFunctionParms):
                break
        icode = parser.handle_optional_debug_info(reader, icode, self)
        return icode

    def serialize(
        self, writer: BinaryIO, icode: int, parser: "TokenStreamParser"
    ) -> int:
        """Serialize the function ref, parameters and optional debug info.

        Args:
            writer (BinaryIO): Binary stream positioned after the opcode byte.
            icode (int): Current instruction-code offset.
            parser (TokenStreamParser): Parser driving the token stream.

        Returns:
            int: The updated instruction-code offset.
        """
        write_index(writer, self.function_ref)
        icode += SIZE_OBJECT_REF
        for param in self.params:
            icode = parser.serialize_expr(writer, param, icode)
        if self.debug_info is not None:
            icode = parser.serialize_expr(writer, self.debug_info, icode)
        return icode

    def to_dict(self, parser: "TokenStreamParser") -> Dict[str, Any]:
        """Return a dict with the resolved function ref, params and debug info.

        Args:
            parser (TokenStreamParser): Parser used to resolve references.

        Returns:
            Dict[str, Any]: The dict representation of this token.
        """
        d: Dict[str, Any] = {
            "Type": "FinalFunction",
            "FunctionRef": parser.resolve_object_ref(self.function_ref),
            "Params": [parser.token_to_dict(p) for p in self.params],
        }
        if self.debug_info is not None:
            d["DebugInfo"] = parser.token_to_dict(self.debug_info)
        return d

    def from_dict(self, data: Dict[str, Any], parser: "TokenStreamParser") -> None:
        """Populate the token from a dict, linking ref, params and debug info.

        Args:
            data (Dict[str, Any]): Dict representation to read from.
            parser (TokenStreamParser): Parser used to link references.
        """
        self.function_ref = parser.link_object_ref(data.get("FunctionRef", ""))
        self.params = [parser.token_from_dict(p) for p in data.get("Params", [])]
        debug_data = data.get("DebugInfo")
        if debug_data is not None and isinstance(debug_data, dict) and debug_data:
            self.debug_info = parser.token_from_dict(debug_data)
        else:
            self.debug_info = None


class UnTokenVirtualFunction(UnToken):
    """Call to a virtual function by name.

    Reads the name index followed by the parameter expressions until an
    ``EndFunctionParms`` token is reached.
    """

    token_type = UnScriptTokenType.VirtualFunction

    def __init__(self) -> None:
        """Initialize with a zeroed function name, empty params and debug info."""
        super().__init__()
        self.function_name: int = 0  # name table index
        self.params: List[UnToken] = []
        self.debug_info: Optional[UnToken] = None

    def parse(self, reader: BinaryIO, icode: int, parser: "TokenStreamParser") -> int:
        """Parse the function name, parameters and optional debug info.

        Args:
            reader (BinaryIO): Binary stream positioned after the opcode byte.
            icode (int): Current instruction-code offset.
            parser (TokenStreamParser): Parser driving the token stream.

        Returns:
            int: The updated instruction-code offset.
        """
        self.function_name = read_index(reader)
        icode += SIZE_FNAME
        self.params = []
        while True:
            param, icode = parser.parse_expr(reader, icode)
            self.params.append(param)
            if isinstance(param, UnTokenEndFunctionParms):
                break
        icode = parser.handle_optional_debug_info(reader, icode, self)
        return icode

    def serialize(
        self, writer: BinaryIO, icode: int, parser: "TokenStreamParser"
    ) -> int:
        """Serialize the function name, parameters and optional debug info.

        Args:
            writer (BinaryIO): Binary stream positioned after the opcode byte.
            icode (int): Current instruction-code offset.
            parser (TokenStreamParser): Parser driving the token stream.

        Returns:
            int: The updated instruction-code offset.
        """
        write_index(writer, self.function_name)
        icode += SIZE_FNAME
        for param in self.params:
            icode = parser.serialize_expr(writer, param, icode)
        if self.debug_info is not None:
            icode = parser.serialize_expr(writer, self.debug_info, icode)
        return icode

    def to_dict(self, parser: "TokenStreamParser") -> Dict[str, Any]:
        """Return a dict with the resolved function name, params and debug info.

        Args:
            parser (TokenStreamParser): Parser used to resolve references.

        Returns:
            Dict[str, Any]: The dict representation of this token.
        """
        d: Dict[str, Any] = {
            "Type": "VirtualFunction",
            "FunctionName": parser.resolve_name_ref(self.function_name),
            "Params": [parser.token_to_dict(p) for p in self.params],
        }
        if self.debug_info is not None:
            d["DebugInfo"] = parser.token_to_dict(self.debug_info)
        return d

    def from_dict(self, data: Dict[str, Any], parser: "TokenStreamParser") -> None:
        """Populate the token from a dict, linking name, params and debug info.

        Args:
            data (Dict[str, Any]): Dict representation to read from.
            parser (TokenStreamParser): Parser used to link references.
        """
        self.function_name = parser.link_name_ref(data.get("FunctionName", ""))
        self.params = [parser.token_from_dict(p) for p in data.get("Params", [])]
        debug_data = data.get("DebugInfo")
        if debug_data is not None and isinstance(debug_data, dict) and debug_data:
            self.debug_info = parser.token_from_dict(debug_data)
        else:
            self.debug_info = None


class UnTokenGlobalFunction(UnToken):
    """Call to the non-state version of a function: name index + params."""

    token_type = UnScriptTokenType.GlobalFunction

    def __init__(self) -> None:
        """Initialize with a zeroed function name, empty params and debug info."""
        super().__init__()
        self.function_name: int = 0
        self.params: List[UnToken] = []
        self.debug_info: Optional[UnToken] = None

    def parse(self, reader: BinaryIO, icode: int, parser: "TokenStreamParser") -> int:
        """Parse the function name, parameters and optional debug info.

        Args:
            reader (BinaryIO): Binary stream positioned after the opcode byte.
            icode (int): Current instruction-code offset.
            parser (TokenStreamParser): Parser driving the token stream.

        Returns:
            int: The updated instruction-code offset.
        """
        self.function_name = read_index(reader)
        icode += SIZE_FNAME
        self.params = []
        while True:
            param, icode = parser.parse_expr(reader, icode)
            self.params.append(param)
            if isinstance(param, UnTokenEndFunctionParms):
                break
        icode = parser.handle_optional_debug_info(reader, icode, self)
        return icode

    def serialize(
        self, writer: BinaryIO, icode: int, parser: "TokenStreamParser"
    ) -> int:
        """Serialize the function name, parameters and optional debug info.

        Args:
            writer (BinaryIO): Binary stream positioned after the opcode byte.
            icode (int): Current instruction-code offset.
            parser (TokenStreamParser): Parser driving the token stream.

        Returns:
            int: The updated instruction-code offset.
        """
        write_index(writer, self.function_name)
        icode += SIZE_FNAME
        for param in self.params:
            icode = parser.serialize_expr(writer, param, icode)
        if self.debug_info is not None:
            icode = parser.serialize_expr(writer, self.debug_info, icode)
        return icode

    def to_dict(self, parser: "TokenStreamParser") -> Dict[str, Any]:
        """Return a dict with the resolved function name, params and debug info.

        Args:
            parser (TokenStreamParser): Parser used to resolve references.

        Returns:
            Dict[str, Any]: The dict representation of this token.
        """
        d: Dict[str, Any] = {
            "Type": "GlobalFunction",
            "FunctionName": parser.resolve_name_ref(self.function_name),
            "Params": [parser.token_to_dict(p) for p in self.params],
        }
        if self.debug_info is not None:
            d["DebugInfo"] = parser.token_to_dict(self.debug_info)
        return d

    def from_dict(self, data: Dict[str, Any], parser: "TokenStreamParser") -> None:
        """Populate the token from a dict, linking name, params and debug info.

        Args:
            data (Dict[str, Any]): Dict representation to read from.
            parser (TokenStreamParser): Parser used to link references.
        """
        self.function_name = parser.link_name_ref(data.get("FunctionName", ""))
        self.params = [parser.token_from_dict(p) for p in data.get("Params", [])]
        debug_data = data.get("DebugInfo")
        if debug_data is not None and isinstance(debug_data, dict) and debug_data:
            self.debug_info = parser.token_from_dict(debug_data)
        else:
            self.debug_info = None


class UnTokenDelegateFunction(UnToken):
    """Delegate function call: delegate property ref + name + params.

    The call carries the delegate property reference, a fallback function
    name, then the argument expressions terminated by ``EndFunctionParms``
    (optionally followed by a trailing ``DebugInfo`` token).
    """

    token_type = UnScriptTokenType.DelegateFunction

    def __init__(self) -> None:
        """Initialize with a zeroed delegate ref, name, params and debug info."""
        super().__init__()
        self.delegate_ref: int = 0
        self.function_name: int = 0
        self.params: List[UnToken] = []
        self.debug_info: Optional[UnToken] = None

    def parse(self, reader: BinaryIO, icode: int, parser: "TokenStreamParser") -> int:
        """Parse the delegate ref, function name, params and optional debug info.

        Args:
            reader (BinaryIO): Binary stream positioned after the opcode byte.
            icode (int): Current instruction-code offset.
            parser (TokenStreamParser): Parser driving the token stream.

        Returns:
            int: The updated instruction-code offset.
        """
        self.delegate_ref = read_index(reader)
        icode += SIZE_OBJECT_REF
        self.function_name = read_index(reader)
        icode += SIZE_FNAME
        self.params = []
        while True:
            param, icode = parser.parse_expr(reader, icode)
            self.params.append(param)
            if isinstance(param, UnTokenEndFunctionParms):
                break
        icode = parser.handle_optional_debug_info(reader, icode, self)
        return icode

    def serialize(
        self, writer: BinaryIO, icode: int, parser: "TokenStreamParser"
    ) -> int:
        """Serialize the delegate ref, name, params and optional debug info.

        Args:
            writer (BinaryIO): Binary stream positioned after the opcode byte.
            icode (int): Current instruction-code offset.
            parser (TokenStreamParser): Parser driving the token stream.

        Returns:
            int: The updated instruction-code offset.
        """
        write_index(writer, self.delegate_ref)
        icode += SIZE_OBJECT_REF
        write_index(writer, self.function_name)
        icode += SIZE_FNAME
        for param in self.params:
            icode = parser.serialize_expr(writer, param, icode)
        if self.debug_info is not None:
            icode = parser.serialize_expr(writer, self.debug_info, icode)
        return icode

    def to_dict(self, parser: "TokenStreamParser") -> Dict[str, Any]:
        """Return a dict with the resolved delegate ref, name, params, debug.

        Args:
            parser (TokenStreamParser): Parser used to resolve references.

        Returns:
            Dict[str, Any]: The dict representation of this token.
        """
        d: Dict[str, Any] = {
            "Type": "DelegateFunction",
            "DelegateRef": parser.resolve_object_ref(self.delegate_ref),
            "FunctionName": parser.resolve_name_ref(self.function_name),
            "Params": [parser.token_to_dict(p) for p in self.params],
        }
        if self.debug_info is not None:
            d["DebugInfo"] = parser.token_to_dict(self.debug_info)
        return d

    def from_dict(self, data: Dict[str, Any], parser: "TokenStreamParser") -> None:
        """Populate the token from a dict, linking ref, name, params and debug.

        Args:
            data (Dict[str, Any]): Dict representation to read from.
            parser (TokenStreamParser): Parser used to link references.
        """
        self.delegate_ref = parser.link_object_ref(data.get("DelegateRef", ""))
        self.function_name = parser.link_name_ref(data.get("FunctionName", ""))
        self.params = [parser.token_from_dict(p) for p in data.get("Params", [])]
        debug_data = data.get("DebugInfo")
        if debug_data is not None and isinstance(debug_data, dict) and debug_data:
            self.debug_info = parser.token_from_dict(debug_data)
        else:
            self.debug_info = None


class UnTokenNativeFunction(UnToken):
    """Native function call (opcode >= 0x70).

    The opcode byte IS the native index.  Not registered in
    ``TOKEN_REGISTRY``; handled directly by ``TokenStreamParser``.
    """

    # No token_type — handled specially by parser

    def __init__(self) -> None:
        """Initialize with a zeroed native index, empty params and debug info."""
        super().__init__()
        self.native_index: int = 0
        self.params: List[UnToken] = []
        self.debug_info: Optional[UnToken] = None

    def parse(self, reader: BinaryIO, icode: int, parser: "TokenStreamParser") -> int:
        """Reject direct parsing; handled by :class:`TokenStreamParser`.

        Args:
            reader (BinaryIO): Binary stream positioned after the opcode byte.
            icode (int): Current instruction-code offset.
            parser (TokenStreamParser): Parser driving the token stream.

        Returns:
            int: The updated instruction-code offset (never reached).

        Raises:
            NotImplementedError: Always; parsing is done by the parser.
        """
        # Params parsed by TokenStreamParser directly
        raise NotImplementedError(
            "UnTokenNativeFunction is parsed by TokenStreamParser"
        )

    def serialize(
        self, writer: BinaryIO, icode: int, parser: "TokenStreamParser"
    ) -> int:
        """Reject direct serialization; handled by :class:`TokenStreamParser`.

        Args:
            writer (BinaryIO): Binary stream positioned after the opcode byte.
            icode (int): Current instruction-code offset.
            parser (TokenStreamParser): Parser driving the token stream.

        Returns:
            int: The updated instruction-code offset (never reached).

        Raises:
            NotImplementedError: Always; serialization is done by the parser.
        """
        # Serialized by TokenStreamParser directly
        raise NotImplementedError(
            "UnTokenNativeFunction is serialized by TokenStreamParser"
        )

    def to_dict(self, parser: "TokenStreamParser") -> Dict[str, Any]:
        """Return a dict with the native index, params and optional debug info.

        Args:
            parser (TokenStreamParser): Parser used to resolve references.

        Returns:
            Dict[str, Any]: The dict representation of this token.
        """
        d: Dict[str, Any] = {
            "Type": "NativeFunction",
            "NativeIndex": self.native_index,
            "Params": [parser.token_to_dict(p) for p in self.params],
        }
        if self.debug_info is not None:
            d["DebugInfo"] = parser.token_to_dict(self.debug_info)
        return d

    def from_dict(self, data: Dict[str, Any], parser: "TokenStreamParser") -> None:
        """Populate the token from a dict, restoring index, params and debug.

        Args:
            data (Dict[str, Any]): Dict representation to read from.
            parser (TokenStreamParser): Parser used to link references.
        """
        self.native_index = int(data.get("NativeIndex", 0))
        self.params = [parser.token_from_dict(p) for p in data.get("Params", [])]
        debug_data = data.get("DebugInfo")
        if debug_data is not None and isinstance(debug_data, dict) and debug_data:
            self.debug_info = parser.token_from_dict(debug_data)
        else:
            self.debug_info = None

    def __repr__(self) -> str:
        """Return a debug string with the native index and icode position.

        Returns:
            str: The formatted representation.
        """
        return (
            f"UnTokenNativeFunction(native={self.native_index}, "
            f"icode=0x{self.icode_start:04X})"
        )


class UnTokenExtendedNativeFunction(UnToken):
    """Extended native function call (opcode 0x60-0x6F + second byte).

    Not registered in TOKEN_REGISTRY; handled directly by ``TokenStreamParser``.
    """

    # No token_type — handled specially by parser

    def __init__(self) -> None:
        """Initialize with a zeroed native index, empty params and debug info."""
        super().__init__()
        self.native_index: int = 0
        self.params: List[UnToken] = []
        self.debug_info: Optional[UnToken] = None

    def parse(self, reader: BinaryIO, icode: int, parser: "TokenStreamParser") -> int:
        """Reject direct parsing; handled by :class:`TokenStreamParser`.

        Args:
            reader (BinaryIO): Binary stream positioned after the opcode byte.
            icode (int): Current instruction-code offset.
            parser (TokenStreamParser): Parser driving the token stream.

        Returns:
            int: The updated instruction-code offset (never reached).

        Raises:
            NotImplementedError: Always; parsing is done by the parser.
        """
        raise NotImplementedError(
            "UnTokenExtendedNativeFunction is parsed by TokenStreamParser"
        )

    def serialize(
        self, writer: BinaryIO, icode: int, parser: "TokenStreamParser"
    ) -> int:
        """Reject direct serialization; handled by :class:`TokenStreamParser`.

        Args:
            writer (BinaryIO): Binary stream positioned after the opcode byte.
            icode (int): Current instruction-code offset.
            parser (TokenStreamParser): Parser driving the token stream.

        Returns:
            int: The updated instruction-code offset (never reached).

        Raises:
            NotImplementedError: Always; serialization is done by the parser.
        """
        raise NotImplementedError(
            "UnTokenExtendedNativeFunction is serialized by TokenStreamParser"
        )

    def to_dict(self, parser: "TokenStreamParser") -> Dict[str, Any]:
        """Return a dict with the native index, params and optional debug info.

        Args:
            parser (TokenStreamParser): Parser used to resolve references.

        Returns:
            Dict[str, Any]: The dict representation of this token.
        """
        d: Dict[str, Any] = {
            "Type": "ExtendedNativeFunction",
            "NativeIndex": self.native_index,
            "Params": [parser.token_to_dict(p) for p in self.params],
        }
        if self.debug_info is not None:
            d["DebugInfo"] = parser.token_to_dict(self.debug_info)
        return d

    def from_dict(self, data: Dict[str, Any], parser: "TokenStreamParser") -> None:
        """Populate the token from a dict, restoring index, params and debug.

        Args:
            data (Dict[str, Any]): Dict representation to read from.
            parser (TokenStreamParser): Parser used to link references.
        """
        self.native_index = int(data.get("NativeIndex", 0))
        self.params = [parser.token_from_dict(p) for p in data.get("Params", [])]
        debug_data = data.get("DebugInfo")
        if debug_data is not None and isinstance(debug_data, dict) and debug_data:
            self.debug_info = parser.token_from_dict(debug_data)
        else:
            self.debug_info = None

    def __repr__(self) -> str:
        """Return a debug string with the native index and icode position.

        Returns:
            str: The formatted representation.
        """
        return (
            f"UnTokenExtendedNativeFunction(native={self.native_index}, "
            f"icode=0x{self.icode_start:04X})"
        )


# ===================================================================== #
#  7.7 — Context tokens
# ===================================================================== #


class _UnTokenContext(UnToken):
    """Context expression: object, 16-bit null offset, 8-bit skip size, context expr.

    *null_offset* is the byte length to skip when the object is None, and
    *skip_size* is the size of the property value type (used by the VM
    for stack handling).  Neither is derivable from the token stream
    alone (the size depends on the resolved property class), so both
    are stored verbatim in the XML.
    """

    def __init__(self) -> None:
        """Initialize with empty expressions and zeroed offset and skip size."""
        super().__init__()
        self.object_expr: Optional[UnToken] = None
        self.null_offset: int = 0
        self.skip_size: int = 0
        self.context_expr: Optional[UnToken] = None

    def parse(self, reader: BinaryIO, icode: int, parser: "TokenStreamParser") -> int:
        """Parse the object expr, null offset, skip size and context expr.

        Args:
            reader (BinaryIO): Binary stream positioned after the opcode byte.
            icode (int): Current instruction-code offset.
            parser (TokenStreamParser): Parser driving the token stream.

        Returns:
            int: The updated instruction-code offset.
        """
        self.object_expr, icode = parser.parse_expr(reader, icode)
        self.null_offset = read_word(reader)
        icode += SIZE_WORD
        self.skip_size = read_byte(reader)
        icode += SIZE_BYTE
        self.context_expr, icode = parser.parse_expr(reader, icode)
        return icode

    def serialize(
        self, writer: BinaryIO, icode: int, parser: "TokenStreamParser"
    ) -> int:
        """Serialize the object expr, null offset, skip size and context expr.

        Args:
            writer (BinaryIO): Binary stream positioned after the opcode byte.
            icode (int): Current instruction-code offset.
            parser (TokenStreamParser): Parser driving the token stream.

        Returns:
            int: The updated instruction-code offset.
        """
        icode = parser.serialize_expr(writer, self.object_expr, icode)
        write_word(writer, self.null_offset)
        icode += SIZE_WORD
        write_byte(writer, self.skip_size)
        icode += SIZE_BYTE
        icode = parser.serialize_expr(writer, self.context_expr, icode)
        return icode

    def to_dict(self, parser: "TokenStreamParser") -> Dict[str, Any]:
        """Return a dict with the object/context exprs, offset and skip size.

        Args:
            parser (TokenStreamParser): Parser used to resolve references.

        Returns:
            Dict[str, Any]: The dict representation of this token.
        """
        return {
            "Type": self.token_type.name,
            "ObjectExpr": parser.token_to_dict(self.object_expr),
            "NullOffset": self.null_offset,
            "SkipSize": self.skip_size,
            "ContextExpr": parser.token_to_dict(self.context_expr),
        }

    def from_dict(self, data: Dict[str, Any], parser: "TokenStreamParser") -> None:
        """Populate the token from a dict, restoring exprs, offset and size.

        Args:
            data (Dict[str, Any]): Dict representation to read from.
            parser (TokenStreamParser): Parser used to link references.
        """
        self.object_expr = parser.token_from_dict(data.get("ObjectExpr", {}))
        self.null_offset = int(data.get("NullOffset", 0))
        self.skip_size = int(data.get("SkipSize", 0))
        self.context_expr = parser.token_from_dict(data.get("ContextExpr", {}))


class UnTokenContext(_UnTokenContext):
    """Instance context access token (``object.member``)."""

    token_type = UnScriptTokenType.Context


class UnTokenClassContext(_UnTokenContext):
    """Class context access token (``class'X'.static.member``)."""

    token_type = UnScriptTokenType.ClassContext


# ===================================================================== #
#  7.8 — Array tokens
# ===================================================================== #


class _UnTokenDualExpr(UnToken):
    """Token with two sub-expressions: index + base."""

    def __init__(self) -> None:
        """Initialize the token with empty index and base expressions."""
        super().__init__()
        self.index_expr: Optional[UnToken] = None
        self.base_expr: Optional[UnToken] = None

    def parse(self, reader: BinaryIO, icode: int, parser: "TokenStreamParser") -> int:
        """Parse the index and base sub-expressions.

        Args:
            reader (BinaryIO): Binary stream positioned after the opcode byte.
            icode (int): Current instruction-code offset.
            parser (TokenStreamParser): Parser driving the token stream.

        Returns:
            int: The updated instruction-code offset.
        """
        self.index_expr, icode = parser.parse_expr(reader, icode)
        self.base_expr, icode = parser.parse_expr(reader, icode)
        return icode

    def serialize(
        self, writer: BinaryIO, icode: int, parser: "TokenStreamParser"
    ) -> int:
        """Serialize the index and base sub-expressions.

        Args:
            writer (BinaryIO): Binary stream positioned after the opcode byte.
            icode (int): Current instruction-code offset.
            parser (TokenStreamParser): Parser driving the token stream.

        Returns:
            int: The updated instruction-code offset.
        """
        icode = parser.serialize_expr(writer, self.index_expr, icode)
        icode = parser.serialize_expr(writer, self.base_expr, icode)
        return icode

    def to_dict(self, parser: "TokenStreamParser") -> Dict[str, Any]:
        """Return a dict with the token type and index and base expressions.

        Args:
            parser (TokenStreamParser): Parser used to resolve references.

        Returns:
            Dict[str, Any]: The dict representation of this token.
        """
        return {
            "Type": self.token_type.name,
            "IndexExpr": parser.token_to_dict(self.index_expr),
            "BaseExpr": parser.token_to_dict(self.base_expr),
        }

    def from_dict(self, data: Dict[str, Any], parser: "TokenStreamParser") -> None:
        """Populate the token from a dict, restoring index and base expressions.

        Args:
            data (Dict[str, Any]): Dict representation to read from.
            parser (TokenStreamParser): Parser used to link references.
        """
        self.index_expr = parser.token_from_dict(data.get("IndexExpr", {}))
        self.base_expr = parser.token_from_dict(data.get("BaseExpr", {}))


class UnTokenArrayElement(_UnTokenDualExpr):
    """Static array element access token (index + base expression)."""

    token_type = UnScriptTokenType.ArrayElement


class UnTokenDynArrayElement(_UnTokenDualExpr):
    """Dynamic array element access token (index + base expression)."""

    token_type = UnScriptTokenType.DynArrayElement


class UnTokenDynArrayLength(UnToken):
    """Dynamic array length token (single base expression)."""

    token_type = UnScriptTokenType.DynArrayLength

    def __init__(self) -> None:
        """Initialize the token with an empty base expression."""
        super().__init__()
        self.base_expr: Optional[UnToken] = None

    def parse(self, reader: BinaryIO, icode: int, parser: "TokenStreamParser") -> int:
        """Parse the base sub-expression.

        Args:
            reader (BinaryIO): Binary stream positioned after the opcode byte.
            icode (int): Current instruction-code offset.
            parser (TokenStreamParser): Parser driving the token stream.

        Returns:
            int: The updated instruction-code offset.
        """
        self.base_expr, icode = parser.parse_expr(reader, icode)
        return icode

    def serialize(
        self, writer: BinaryIO, icode: int, parser: "TokenStreamParser"
    ) -> int:
        """Serialize the base sub-expression.

        Args:
            writer (BinaryIO): Binary stream positioned after the opcode byte.
            icode (int): Current instruction-code offset.
            parser (TokenStreamParser): Parser driving the token stream.

        Returns:
            int: The updated instruction-code offset.
        """
        icode = parser.serialize_expr(writer, self.base_expr, icode)
        return icode

    def to_dict(self, parser: "TokenStreamParser") -> Dict[str, Any]:
        """Return a dict with the token type and base expression.

        Args:
            parser (TokenStreamParser): Parser used to resolve references.

        Returns:
            Dict[str, Any]: The dict representation of this token.
        """
        return {
            "Type": "DynArrayLength",
            "BaseExpr": parser.token_to_dict(self.base_expr),
        }

    def from_dict(self, data: Dict[str, Any], parser: "TokenStreamParser") -> None:
        """Populate the token from a dict, restoring the base expression.

        Args:
            data (Dict[str, Any]): Dict representation to read from.
            parser (TokenStreamParser): Parser used to link references.
        """
        self.base_expr = parser.token_from_dict(data.get("BaseExpr", {}))


class _UnTokenDynArrayTriple(UnToken):
    """DynArrayInsert / DynArrayRemove: base + index + count."""

    def __init__(self) -> None:
        """Initialize the token with empty base, index and count expressions."""
        super().__init__()
        self.base_expr: Optional[UnToken] = None
        self.index_expr: Optional[UnToken] = None
        self.count_expr: Optional[UnToken] = None

    def parse(self, reader: BinaryIO, icode: int, parser: "TokenStreamParser") -> int:
        """Parse the base, index and count sub-expressions.

        Args:
            reader (BinaryIO): Binary stream positioned after the opcode byte.
            icode (int): Current instruction-code offset.
            parser (TokenStreamParser): Parser driving the token stream.

        Returns:
            int: The updated instruction-code offset.
        """
        self.base_expr, icode = parser.parse_expr(reader, icode)
        self.index_expr, icode = parser.parse_expr(reader, icode)
        self.count_expr, icode = parser.parse_expr(reader, icode)
        return icode

    def serialize(
        self, writer: BinaryIO, icode: int, parser: "TokenStreamParser"
    ) -> int:
        """Serialize the base, index and count sub-expressions.

        Args:
            writer (BinaryIO): Binary stream positioned after the opcode byte.
            icode (int): Current instruction-code offset.
            parser (TokenStreamParser): Parser driving the token stream.

        Returns:
            int: The updated instruction-code offset.
        """
        icode = parser.serialize_expr(writer, self.base_expr, icode)
        icode = parser.serialize_expr(writer, self.index_expr, icode)
        icode = parser.serialize_expr(writer, self.count_expr, icode)
        return icode

    def to_dict(self, parser: "TokenStreamParser") -> Dict[str, Any]:
        """Return a dict with the token type and base, index and count exprs.

        Args:
            parser (TokenStreamParser): Parser used to resolve references.

        Returns:
            Dict[str, Any]: The dict representation of this token.
        """
        return {
            "Type": self.token_type.name,
            "BaseExpr": parser.token_to_dict(self.base_expr),
            "IndexExpr": parser.token_to_dict(self.index_expr),
            "CountExpr": parser.token_to_dict(self.count_expr),
        }

    def from_dict(self, data: Dict[str, Any], parser: "TokenStreamParser") -> None:
        """Populate the token from a dict, restoring base, index and count.

        Args:
            data (Dict[str, Any]): Dict representation to read from.
            parser (TokenStreamParser): Parser used to link references.
        """
        self.base_expr = parser.token_from_dict(data.get("BaseExpr", {}))
        self.index_expr = parser.token_from_dict(data.get("IndexExpr", {}))
        self.count_expr = parser.token_from_dict(data.get("CountExpr", {}))


class UnTokenDynArrayInsert(_UnTokenDynArrayTriple):
    """Dynamic array insert token (base + index + count expressions)."""

    token_type = UnScriptTokenType.DynArrayInsert


class UnTokenDynArrayRemove(_UnTokenDynArrayTriple):
    """Dynamic array remove token (base + index + count expressions)."""

    token_type = UnScriptTokenType.DynArrayRemove


# ===================================================================== #
#  7.9 — Cast tokens
# ===================================================================== #


class UnTokenPrimitiveCast(UnToken):
    """Primitive type cast: 8-bit cast_type + expression."""

    token_type = UnScriptTokenType.PrimitiveCast

    def __init__(self) -> None:
        """Initialize the token with a zero cast type and empty expression."""
        super().__init__()
        self.cast_type: int = 0  # UnCastType value
        self.expression: Optional[UnToken] = None

    def parse(self, reader: BinaryIO, icode: int, parser: "TokenStreamParser") -> int:
        """Parse the cast type byte and the cast sub-expression.

        Args:
            reader (BinaryIO): Binary stream positioned after the opcode byte.
            icode (int): Current instruction-code offset.
            parser (TokenStreamParser): Parser driving the token stream.

        Returns:
            int: The updated instruction-code offset.
        """
        self.cast_type = read_byte(reader)
        icode += SIZE_BYTE
        self.expression, icode = parser.parse_expr(reader, icode)
        return icode

    def serialize(
        self, writer: BinaryIO, icode: int, parser: "TokenStreamParser"
    ) -> int:
        """Serialize the cast type byte and the cast sub-expression.

        Args:
            writer (BinaryIO): Binary stream positioned after the opcode byte.
            icode (int): Current instruction-code offset.
            parser (TokenStreamParser): Parser driving the token stream.

        Returns:
            int: The updated instruction-code offset.
        """
        write_byte(writer, self.cast_type)
        icode += SIZE_BYTE
        icode = parser.serialize_expr(writer, self.expression, icode)
        return icode

    def to_dict(self, parser: "TokenStreamParser") -> Dict[str, Any]:
        """Return a dict with the resolved cast type and cast expression.

        Args:
            parser (TokenStreamParser): Parser used to resolve references.

        Returns:
            Dict[str, Any]: The dict representation of this token.
        """
        # Try to resolve cast type name
        try:
            cast_name = UnCastType(self.cast_type).name
        except ValueError:
            cast_name = str(self.cast_type)
        return {
            "Type": "PrimitiveCast",
            "CastType": cast_name,
            "Expression": parser.token_to_dict(self.expression),
        }

    def from_dict(self, data: Dict[str, Any], parser: "TokenStreamParser") -> None:
        """Populate the token from a dict, restoring cast type and expression.

        Args:
            data (Dict[str, Any]): Dict representation to read from.
            parser (TokenStreamParser): Parser used to link references.
        """
        cast_str = data.get("CastType", "0")
        # Try to resolve by name first, then as int
        try:
            self.cast_type = UnCastType[cast_str].value
        except (KeyError, ValueError):
            self.cast_type = int(cast_str)
        self.expression = parser.token_from_dict(data.get("Expression", {}))


class UnTokenMetaCast(UnToken):
    """Metaclass cast: class object ref + expression."""

    token_type = UnScriptTokenType.MetaCast

    def __init__(self) -> None:
        """Initialize the token with a zero class ref and empty expression."""
        super().__init__()
        self.class_ref: int = 0
        self.expression: Optional[UnToken] = None

    def parse(self, reader: BinaryIO, icode: int, parser: "TokenStreamParser") -> int:
        """Parse the class object reference and the cast sub-expression.

        Args:
            reader (BinaryIO): Binary stream positioned after the opcode byte.
            icode (int): Current instruction-code offset.
            parser (TokenStreamParser): Parser driving the token stream.

        Returns:
            int: The updated instruction-code offset.
        """
        self.class_ref = read_index(reader)
        icode += SIZE_OBJECT_REF
        self.expression, icode = parser.parse_expr(reader, icode)
        return icode

    def serialize(
        self, writer: BinaryIO, icode: int, parser: "TokenStreamParser"
    ) -> int:
        """Serialize the class object reference and the cast sub-expression.

        Args:
            writer (BinaryIO): Binary stream positioned after the opcode byte.
            icode (int): Current instruction-code offset.
            parser (TokenStreamParser): Parser driving the token stream.

        Returns:
            int: The updated instruction-code offset.
        """
        write_index(writer, self.class_ref)
        icode += SIZE_OBJECT_REF
        icode = parser.serialize_expr(writer, self.expression, icode)
        return icode

    def to_dict(self, parser: "TokenStreamParser") -> Dict[str, Any]:
        """Return a dict with the resolved class ref and cast expression.

        Args:
            parser (TokenStreamParser): Parser used to resolve references.

        Returns:
            Dict[str, Any]: The dict representation of this token.
        """
        return {
            "Type": "MetaCast",
            "ClassRef": parser.resolve_object_ref(self.class_ref),
            "Expression": parser.token_to_dict(self.expression),
        }

    def from_dict(self, data: Dict[str, Any], parser: "TokenStreamParser") -> None:
        """Populate the token from a dict, linking class ref and expression.

        Args:
            data (Dict[str, Any]): Dict representation to read from.
            parser (TokenStreamParser): Parser used to link references.
        """
        self.class_ref = parser.link_object_ref(data.get("ClassRef", ""))
        self.expression = parser.token_from_dict(data.get("Expression", {}))


class UnTokenDynamicCast(UnToken):
    """Dynamic class cast: class object ref + expression."""

    token_type = UnScriptTokenType.DynamicCast

    def __init__(self) -> None:
        """Initialize the token with a zero class ref and empty expression."""
        super().__init__()
        self.class_ref: int = 0
        self.expression: Optional[UnToken] = None

    def parse(self, reader: BinaryIO, icode: int, parser: "TokenStreamParser") -> int:
        """Parse the class object reference and the cast sub-expression.

        Args:
            reader (BinaryIO): Binary stream positioned after the opcode byte.
            icode (int): Current instruction-code offset.
            parser (TokenStreamParser): Parser driving the token stream.

        Returns:
            int: The updated instruction-code offset.
        """
        self.class_ref = read_index(reader)
        icode += SIZE_OBJECT_REF
        self.expression, icode = parser.parse_expr(reader, icode)
        return icode

    def serialize(
        self, writer: BinaryIO, icode: int, parser: "TokenStreamParser"
    ) -> int:
        """Serialize the class object reference and the cast sub-expression.

        Args:
            writer (BinaryIO): Binary stream positioned after the opcode byte.
            icode (int): Current instruction-code offset.
            parser (TokenStreamParser): Parser driving the token stream.

        Returns:
            int: The updated instruction-code offset.
        """
        write_index(writer, self.class_ref)
        icode += SIZE_OBJECT_REF
        icode = parser.serialize_expr(writer, self.expression, icode)
        return icode

    def to_dict(self, parser: "TokenStreamParser") -> Dict[str, Any]:
        """Return a dict with the resolved class ref and cast expression.

        Args:
            parser (TokenStreamParser): Parser used to resolve references.

        Returns:
            Dict[str, Any]: The dict representation of this token.
        """
        return {
            "Type": "DynamicCast",
            "ClassRef": parser.resolve_object_ref(self.class_ref),
            "Expression": parser.token_to_dict(self.expression),
        }

    def from_dict(self, data: Dict[str, Any], parser: "TokenStreamParser") -> None:
        """Populate the token from a dict, linking class ref and expression.

        Args:
            data (Dict[str, Any]): Dict representation to read from.
            parser (TokenStreamParser): Parser used to link references.
        """
        self.class_ref = parser.link_object_ref(data.get("ClassRef", ""))
        self.expression = parser.token_from_dict(data.get("Expression", {}))


# ===================================================================== #
#  7.10 — Struct tokens
# ===================================================================== #


class _UnTokenStructCmp(UnToken):
    """Struct comparison: struct object ref + left + right expressions."""

    def __init__(self) -> None:
        """Initialize the token with a zero struct ref and empty expressions."""
        super().__init__()
        self.struct_ref: int = 0
        self.left_expr: Optional[UnToken] = None
        self.right_expr: Optional[UnToken] = None

    def parse(self, reader: BinaryIO, icode: int, parser: "TokenStreamParser") -> int:
        """Parse the struct ref and the left and right sub-expressions.

        Args:
            reader (BinaryIO): Binary stream positioned after the opcode byte.
            icode (int): Current instruction-code offset.
            parser (TokenStreamParser): Parser driving the token stream.

        Returns:
            int: The updated instruction-code offset.
        """
        self.struct_ref = read_index(reader)
        icode += SIZE_OBJECT_REF
        self.left_expr, icode = parser.parse_expr(reader, icode)
        self.right_expr, icode = parser.parse_expr(reader, icode)
        return icode

    def serialize(
        self, writer: BinaryIO, icode: int, parser: "TokenStreamParser"
    ) -> int:
        """Serialize the struct ref and the left and right sub-expressions.

        Args:
            writer (BinaryIO): Binary stream positioned after the opcode byte.
            icode (int): Current instruction-code offset.
            parser (TokenStreamParser): Parser driving the token stream.

        Returns:
            int: The updated instruction-code offset.
        """
        write_index(writer, self.struct_ref)
        icode += SIZE_OBJECT_REF
        icode = parser.serialize_expr(writer, self.left_expr, icode)
        icode = parser.serialize_expr(writer, self.right_expr, icode)
        return icode

    def to_dict(self, parser: "TokenStreamParser") -> Dict[str, Any]:
        """Return a dict with the resolved struct ref and both expressions.

        Args:
            parser (TokenStreamParser): Parser used to resolve references.

        Returns:
            Dict[str, Any]: The dict representation of this token.
        """
        return {
            "Type": self.token_type.name,
            "StructRef": parser.resolve_object_ref(self.struct_ref),
            "LeftExpr": parser.token_to_dict(self.left_expr),
            "RightExpr": parser.token_to_dict(self.right_expr),
        }

    def from_dict(self, data: Dict[str, Any], parser: "TokenStreamParser") -> None:
        """Populate the token from a dict, linking struct ref and expressions.

        Args:
            data (Dict[str, Any]): Dict representation to read from.
            parser (TokenStreamParser): Parser used to link references.
        """
        self.struct_ref = parser.link_object_ref(data.get("StructRef", ""))
        self.left_expr = parser.token_from_dict(data.get("LeftExpr", {}))
        self.right_expr = parser.token_from_dict(data.get("RightExpr", {}))


class UnTokenStructCmpEq(_UnTokenStructCmp):
    """Struct equality comparison token (struct ref + left + right)."""

    token_type = UnScriptTokenType.StructCmpEq


class UnTokenStructCmpNe(_UnTokenStructCmp):
    """Struct inequality comparison token (struct ref + left + right)."""

    token_type = UnScriptTokenType.StructCmpNe


class UnTokenStructMember(UnToken):
    """Struct member access: property object ref + inner expression."""

    token_type = UnScriptTokenType.StructMember

    def __init__(self) -> None:
        """Initialize the token with a zero property ref and empty inner expr."""
        super().__init__()
        self.property_ref: int = 0
        self.inner_expr: Optional[UnToken] = None

    def parse(self, reader: BinaryIO, icode: int, parser: "TokenStreamParser") -> int:
        """Parse the property object reference and the inner sub-expression.

        Args:
            reader (BinaryIO): Binary stream positioned after the opcode byte.
            icode (int): Current instruction-code offset.
            parser (TokenStreamParser): Parser driving the token stream.

        Returns:
            int: The updated instruction-code offset.
        """
        self.property_ref = read_index(reader)
        icode += SIZE_OBJECT_REF
        self.inner_expr, icode = parser.parse_expr(reader, icode)
        return icode

    def serialize(
        self, writer: BinaryIO, icode: int, parser: "TokenStreamParser"
    ) -> int:
        """Serialize the property object reference and the inner sub-expression.

        Args:
            writer (BinaryIO): Binary stream positioned after the opcode byte.
            icode (int): Current instruction-code offset.
            parser (TokenStreamParser): Parser driving the token stream.

        Returns:
            int: The updated instruction-code offset.
        """
        write_index(writer, self.property_ref)
        icode += SIZE_OBJECT_REF
        icode = parser.serialize_expr(writer, self.inner_expr, icode)
        return icode

    def to_dict(self, parser: "TokenStreamParser") -> Dict[str, Any]:
        """Return a dict with the resolved property ref and inner expression.

        Args:
            parser (TokenStreamParser): Parser used to resolve references.

        Returns:
            Dict[str, Any]: The dict representation of this token.
        """
        return {
            "Type": "StructMember",
            "PropertyRef": parser.resolve_object_ref(self.property_ref),
            "InnerExpr": parser.token_to_dict(self.inner_expr),
        }

    def from_dict(self, data: Dict[str, Any], parser: "TokenStreamParser") -> None:
        """Populate the token from a dict, linking property ref and inner expr.

        Args:
            data (Dict[str, Any]): Dict representation to read from.
            parser (TokenStreamParser): Parser used to link references.
        """
        self.property_ref = parser.link_object_ref(data.get("PropertyRef", ""))
        self.inner_expr = parser.token_from_dict(data.get("InnerExpr", {}))


# ===================================================================== #
#  7.11 — Miscellaneous tokens
# ===================================================================== #


class UnTokenReturn(UnToken):
    """Return token wrapping the returned expression."""

    token_type = UnScriptTokenType.Return

    def __init__(self) -> None:
        """Initialize the token with an empty return expression."""
        super().__init__()
        self.expression: Optional[UnToken] = None

    def parse(self, reader: BinaryIO, icode: int, parser: "TokenStreamParser") -> int:
        """Parse the returned sub-expression.

        Args:
            reader (BinaryIO): Binary stream positioned after the opcode byte.
            icode (int): Current instruction-code offset.
            parser (TokenStreamParser): Parser driving the token stream.

        Returns:
            int: The updated instruction-code offset.
        """
        self.expression, icode = parser.parse_expr(reader, icode)
        return icode

    def serialize(
        self, writer: BinaryIO, icode: int, parser: "TokenStreamParser"
    ) -> int:
        """Serialize the returned sub-expression.

        Args:
            writer (BinaryIO): Binary stream positioned after the opcode byte.
            icode (int): Current instruction-code offset.
            parser (TokenStreamParser): Parser driving the token stream.

        Returns:
            int: The updated instruction-code offset.
        """
        icode = parser.serialize_expr(writer, self.expression, icode)
        return icode

    def to_dict(self, parser: "TokenStreamParser") -> Dict[str, Any]:
        """Return a dict with the token type and returned expression.

        Args:
            parser (TokenStreamParser): Parser used to resolve references.

        Returns:
            Dict[str, Any]: The dict representation of this token.
        """
        return {
            "Type": "Return",
            "Expression": parser.token_to_dict(self.expression),
        }

    def from_dict(self, data: Dict[str, Any], parser: "TokenStreamParser") -> None:
        """Populate the token from a dict, restoring the returned expression.

        Args:
            data (Dict[str, Any]): Dict representation to read from.
            parser (TokenStreamParser): Parser used to link references.
        """
        self.expression = parser.token_from_dict(data.get("Expression", {}))


class UnTokenEatString(UnToken):
    """Eat-string token wrapping a discarded string sub-expression."""

    token_type = UnScriptTokenType.EatString

    def __init__(self) -> None:
        """Initialize the token with an empty wrapped expression."""
        super().__init__()
        self.expression: Optional[UnToken] = None

    def parse(self, reader: BinaryIO, icode: int, parser: "TokenStreamParser") -> int:
        """Parse the wrapped sub-expression.

        Args:
            reader (BinaryIO): Binary stream positioned after the opcode byte.
            icode (int): Current instruction-code offset.
            parser (TokenStreamParser): Parser driving the token stream.

        Returns:
            int: The updated instruction-code offset.
        """
        self.expression, icode = parser.parse_expr(reader, icode)
        return icode

    def serialize(
        self, writer: BinaryIO, icode: int, parser: "TokenStreamParser"
    ) -> int:
        """Serialize the wrapped sub-expression.

        Args:
            writer (BinaryIO): Binary stream positioned after the opcode byte.
            icode (int): Current instruction-code offset.
            parser (TokenStreamParser): Parser driving the token stream.

        Returns:
            int: The updated instruction-code offset.
        """
        icode = parser.serialize_expr(writer, self.expression, icode)
        return icode

    def to_dict(self, parser: "TokenStreamParser") -> Dict[str, Any]:
        """Return a dict with the token type and wrapped expression.

        Args:
            parser (TokenStreamParser): Parser used to resolve references.

        Returns:
            Dict[str, Any]: The dict representation of this token.
        """
        return {
            "Type": "EatString",
            "Expression": parser.token_to_dict(self.expression),
        }

    def from_dict(self, data: Dict[str, Any], parser: "TokenStreamParser") -> None:
        """Populate the token from a dict, restoring the wrapped expression.

        Args:
            data (Dict[str, Any]): Dict representation to read from.
            parser (TokenStreamParser): Parser used to link references.
        """
        self.expression = parser.token_from_dict(data.get("Expression", {}))


class UnTokenNew(UnToken):
    """New object allocation: 4 sub-expressions (parent, name, flags, class)."""

    token_type = UnScriptTokenType.New

    def __init__(self) -> None:
        """Initialize the token with empty parent, name, flags and class exprs."""
        super().__init__()
        self.parent_expr: Optional[UnToken] = None
        self.name_expr: Optional[UnToken] = None
        self.flags_expr: Optional[UnToken] = None
        self.class_expr: Optional[UnToken] = None

    def parse(self, reader: BinaryIO, icode: int, parser: "TokenStreamParser") -> int:
        """Parse the parent, name, flags and class sub-expressions.

        Args:
            reader (BinaryIO): Binary stream positioned after the opcode byte.
            icode (int): Current instruction-code offset.
            parser (TokenStreamParser): Parser driving the token stream.

        Returns:
            int: The updated instruction-code offset.
        """
        self.parent_expr, icode = parser.parse_expr(reader, icode)
        self.name_expr, icode = parser.parse_expr(reader, icode)
        self.flags_expr, icode = parser.parse_expr(reader, icode)
        self.class_expr, icode = parser.parse_expr(reader, icode)
        return icode

    def serialize(
        self, writer: BinaryIO, icode: int, parser: "TokenStreamParser"
    ) -> int:
        """Serialize the parent, name, flags and class sub-expressions.

        Args:
            writer (BinaryIO): Binary stream positioned after the opcode byte.
            icode (int): Current instruction-code offset.
            parser (TokenStreamParser): Parser driving the token stream.

        Returns:
            int: The updated instruction-code offset.
        """
        icode = parser.serialize_expr(writer, self.parent_expr, icode)
        icode = parser.serialize_expr(writer, self.name_expr, icode)
        icode = parser.serialize_expr(writer, self.flags_expr, icode)
        icode = parser.serialize_expr(writer, self.class_expr, icode)
        return icode

    def to_dict(self, parser: "TokenStreamParser") -> Dict[str, Any]:
        """Return a dict with the parent, name, flags and class expressions.

        Args:
            parser (TokenStreamParser): Parser used to resolve references.

        Returns:
            Dict[str, Any]: The dict representation of this token.
        """
        return {
            "Type": "New",
            "ParentExpr": parser.token_to_dict(self.parent_expr),
            "NameExpr": parser.token_to_dict(self.name_expr),
            "FlagsExpr": parser.token_to_dict(self.flags_expr),
            "ClassExpr": parser.token_to_dict(self.class_expr),
        }

    def from_dict(self, data: Dict[str, Any], parser: "TokenStreamParser") -> None:
        """Populate the token from a dict, restoring the four sub-expressions.

        Args:
            data (Dict[str, Any]): Dict representation to read from.
            parser (TokenStreamParser): Parser used to link references.
        """
        self.parent_expr = parser.token_from_dict(data.get("ParentExpr", {}))
        self.name_expr = parser.token_from_dict(data.get("NameExpr", {}))
        self.flags_expr = parser.token_from_dict(data.get("FlagsExpr", {}))
        self.class_expr = parser.token_from_dict(data.get("ClassExpr", {}))


class UnTokenGotoLabel(UnToken):
    """Goto-label token wrapping the target label expression."""

    token_type = UnScriptTokenType.GotoLabel

    def __init__(self) -> None:
        """Initialize the token with an empty label expression."""
        super().__init__()
        self.label_expr: Optional[UnToken] = None

    def parse(self, reader: BinaryIO, icode: int, parser: "TokenStreamParser") -> int:
        """Parse the label sub-expression.

        Args:
            reader (BinaryIO): Binary stream positioned after the opcode byte.
            icode (int): Current instruction-code offset.
            parser (TokenStreamParser): Parser driving the token stream.

        Returns:
            int: The updated instruction-code offset.
        """
        self.label_expr, icode = parser.parse_expr(reader, icode)
        return icode

    def serialize(
        self, writer: BinaryIO, icode: int, parser: "TokenStreamParser"
    ) -> int:
        """Serialize the label sub-expression.

        Args:
            writer (BinaryIO): Binary stream positioned after the opcode byte.
            icode (int): Current instruction-code offset.
            parser (TokenStreamParser): Parser driving the token stream.

        Returns:
            int: The updated instruction-code offset.
        """
        icode = parser.serialize_expr(writer, self.label_expr, icode)
        return icode

    def to_dict(self, parser: "TokenStreamParser") -> Dict[str, Any]:
        """Return a dict with the token type and label expression.

        Args:
            parser (TokenStreamParser): Parser used to resolve references.

        Returns:
            Dict[str, Any]: The dict representation of this token.
        """
        return {
            "Type": "GotoLabel",
            "LabelExpr": parser.token_to_dict(self.label_expr),
        }

    def from_dict(self, data: Dict[str, Any], parser: "TokenStreamParser") -> None:
        """Populate the token from a dict, restoring the label expression.

        Args:
            data (Dict[str, Any]): Dict representation to read from.
            parser (TokenStreamParser): Parser used to link references.
        """
        self.label_expr = parser.token_from_dict(data.get("LabelExpr", {}))


class UnTokenSkip(UnToken):
    """Skippable expression: 16-bit skip_size + expression.

    *skip_size* is a VM-internal byte count that does not always equal
    the serialised inner-expression length (it excludes some optional
    trailing data such as ``DebugInfo``), so the field is stored
    verbatim in the XML rather than recomputed.
    """

    token_type = UnScriptTokenType.Skip

    def __init__(self) -> None:
        """Initialize the token with a zero skip size and empty expression."""
        super().__init__()
        self.skip_size: int = 0
        self.expression: Optional[UnToken] = None

    def parse(self, reader: BinaryIO, icode: int, parser: "TokenStreamParser") -> int:
        """Parse the 16-bit skip size and the wrapped sub-expression.

        Args:
            reader (BinaryIO): Binary stream positioned after the opcode byte.
            icode (int): Current instruction-code offset.
            parser (TokenStreamParser): Parser driving the token stream.

        Returns:
            int: The updated instruction-code offset.
        """
        self.skip_size = read_word(reader)
        icode += SIZE_WORD
        self.expression, icode = parser.parse_expr(reader, icode)
        return icode

    def serialize(
        self, writer: BinaryIO, icode: int, parser: "TokenStreamParser"
    ) -> int:
        """Serialize the 16-bit skip size and the wrapped sub-expression.

        Args:
            writer (BinaryIO): Binary stream positioned after the opcode byte.
            icode (int): Current instruction-code offset.
            parser (TokenStreamParser): Parser driving the token stream.

        Returns:
            int: The updated instruction-code offset.
        """
        write_word(writer, self.skip_size)
        icode += SIZE_WORD
        icode = parser.serialize_expr(writer, self.expression, icode)
        return icode

    def to_dict(self, parser: "TokenStreamParser") -> Dict[str, Any]:
        """Return a dict with the token type, skip size and expression.

        Args:
            parser (TokenStreamParser): Parser used to resolve references.

        Returns:
            Dict[str, Any]: The dict representation of this token.
        """
        return {
            "Type": "Skip",
            "SkipSize": self.skip_size,
            "Expression": parser.token_to_dict(self.expression),
        }

    def from_dict(self, data: Dict[str, Any], parser: "TokenStreamParser") -> None:
        """Populate the token from a dict, restoring skip size and expression.

        Args:
            data (Dict[str, Any]): Dict representation to read from.
            parser (TokenStreamParser): Parser used to link references.
        """
        self.skip_size = int(data.get("SkipSize", 0))
        self.expression = parser.token_from_dict(data.get("Expression", {}))


class UnTokenDelegateProperty(UnToken):
    """Delegate property: function name index."""

    token_type = UnScriptTokenType.DelegateProperty

    def __init__(self) -> None:
        """Initialize the token with a zeroed function name index."""
        super().__init__()
        self.function_name: int = 0

    def parse(self, reader: BinaryIO, icode: int, parser: "TokenStreamParser") -> int:
        """Parse the function name index.

        Args:
            reader (BinaryIO): Binary stream positioned after the opcode byte.
            icode (int): Current instruction-code offset.
            parser (TokenStreamParser): Parser driving the token stream.

        Returns:
            int: The updated instruction-code offset.
        """
        self.function_name = read_index(reader)
        return icode + SIZE_FNAME

    def serialize(
        self, writer: BinaryIO, icode: int, parser: "TokenStreamParser"
    ) -> int:
        """Serialize the function name index.

        Args:
            writer (BinaryIO): Binary stream positioned after the opcode byte.
            icode (int): Current instruction-code offset.
            parser (TokenStreamParser): Parser driving the token stream.

        Returns:
            int: The updated instruction-code offset.
        """
        write_index(writer, self.function_name)
        return icode + SIZE_FNAME

    def to_dict(self, parser: "TokenStreamParser") -> Dict[str, Any]:
        """Return a dict with the token type and resolved function name.

        Args:
            parser (TokenStreamParser): Parser used to resolve references.

        Returns:
            Dict[str, Any]: The dict representation of this token.
        """
        return {
            "Type": "DelegateProperty",
            "FunctionName": parser.resolve_name_ref(self.function_name),
        }

    def from_dict(self, data: Dict[str, Any], parser: "TokenStreamParser") -> None:
        """Populate the token from a dict, linking the function name.

        Args:
            data (Dict[str, Any]): Dict representation to read from.
            parser (TokenStreamParser): Parser used to link references.
        """
        self.function_name = parser.link_name_ref(data.get("FunctionName", ""))


class UnTokenDebugInfo(UnToken):
    """Debug information: version (int), line (int), char_pos (int), opcode (byte)."""

    token_type = UnScriptTokenType.DebugInfo

    def __init__(self) -> None:
        """Initialize the token with zeroed version, line, char pos and opcode."""
        super().__init__()
        self.version: int = 0
        self.line: int = 0
        self.char_pos: int = 0
        self.opcode: int = 0

    def parse(self, reader: BinaryIO, icode: int, parser: "TokenStreamParser") -> int:
        """Parse the version, line, char position and opcode fields.

        Args:
            reader (BinaryIO): Binary stream positioned after the opcode byte.
            icode (int): Current instruction-code offset.
            parser (TokenStreamParser): Parser driving the token stream.

        Returns:
            int: The updated instruction-code offset.
        """
        self.version = read_int(reader)
        icode += SIZE_INT
        self.line = read_int(reader)
        icode += SIZE_INT
        self.char_pos = read_int(reader)
        icode += SIZE_INT
        self.opcode = read_byte(reader)
        icode += SIZE_BYTE
        return icode

    def serialize(
        self, writer: BinaryIO, icode: int, parser: "TokenStreamParser"
    ) -> int:
        """Serialize the version, line, char position and opcode fields.

        Args:
            writer (BinaryIO): Binary stream positioned after the opcode byte.
            icode (int): Current instruction-code offset.
            parser (TokenStreamParser): Parser driving the token stream.

        Returns:
            int: The updated instruction-code offset.
        """
        write_int(writer, self.version)
        icode += SIZE_INT
        write_int(writer, self.line)
        icode += SIZE_INT
        write_int(writer, self.char_pos)
        icode += SIZE_INT
        write_byte(writer, self.opcode)
        icode += SIZE_BYTE
        return icode

    def to_dict(self, parser: "TokenStreamParser") -> Dict[str, Any]:
        """Return a dict with the version, line, char position and opcode.

        Args:
            parser (TokenStreamParser): Parser used to resolve references.

        Returns:
            Dict[str, Any]: The dict representation of this token.
        """
        return {
            "Type": "DebugInfo",
            "Version": self.version,
            "Line": self.line,
            "CharPos": self.char_pos,
            "Opcode": self.opcode,
        }

    def from_dict(self, data: Dict[str, Any], parser: "TokenStreamParser") -> None:
        """Populate the token from a dict, reading all debug info fields.

        Args:
            data (Dict[str, Any]): Dict representation to read from.
            parser (TokenStreamParser): Parser used to link references.
        """
        self.version = int(data.get("Version", 0))
        self.line = int(data.get("Line", 0))
        self.char_pos = int(data.get("CharPos", 0))
        self.opcode = int(data.get("Opcode", 0))


class UnTokenLabelTable(UnToken):
    """Label table: repeated label entries until the name equals ``"None"``.

    Each entry is a (name index, code offset) pair.  The terminator is
    identified by the resolved name string being ``"None"``, not by the
    raw index being 0 — ``"None"`` can appear at any index in the
    package's name table.
    """

    token_type = UnScriptTokenType.LabelTable

    def __init__(self) -> None:
        """Initialize the token with an empty list of label entries."""
        super().__init__()
        self.entries: List[UnLabelEntry] = []

    def parse(self, reader: BinaryIO, icode: int, parser: "TokenStreamParser") -> int:
        """Parse label entries until the resolved name equals ``"None"``.

        Args:
            reader (BinaryIO): Binary stream positioned after the opcode byte.
            icode (int): Current instruction-code offset.
            parser (TokenStreamParser): Parser driving the token stream.

        Returns:
            int: The updated instruction-code offset.
        """
        self.entries = []
        while True:
            name_index = read_index(reader)
            entry_icode = read_int(reader)
            icode += SIZE_LABEL_ENTRY
            entry = UnLabelEntry(name_index=name_index, icode=entry_icode)
            self.entries.append(entry)
            # Terminate when the resolved name is "None".
            if parser.is_name_none(name_index):
                break
        return icode

    def serialize(
        self, writer: BinaryIO, icode: int, parser: "TokenStreamParser"
    ) -> int:
        """Serialize every label entry (name index + code offset).

        Args:
            writer (BinaryIO): Binary stream positioned after the opcode byte.
            icode (int): Current instruction-code offset.
            parser (TokenStreamParser): Parser driving the token stream.

        Returns:
            int: The updated instruction-code offset.
        """
        for entry in self.entries:
            write_index(writer, entry.name_index)
            write_int(writer, entry.icode)
            icode += SIZE_LABEL_ENTRY
        return icode

    def to_dict(self, parser: "TokenStreamParser") -> Dict[str, Any]:
        """Return a dict with the token type and resolved label entries.

        Args:
            parser (TokenStreamParser): Parser used to resolve references.

        Returns:
            Dict[str, Any]: The dict representation of this token.
        """
        entries = []
        for entry in self.entries:
            entry_dict: Dict[str, Any] = {
                "NameRef": parser.resolve_name_ref(entry.name_index),
            }
            # Last entry is the None terminator with no real icode target.
            if parser.is_name_none(entry.name_index):
                entry_dict["ICode"] = entry.icode
            else:
                entry_dict["JumpTo"] = parser.icode_to_label(entry.icode)
            entries.append(entry_dict)
        return {
            "Type": "LabelTable",
            "Entries": entries,
        }

    def from_dict(self, data: Dict[str, Any], parser: "TokenStreamParser") -> None:
        """Populate the token from a dict, restoring the label entries.

        Args:
            data (Dict[str, Any]): Dict representation to read from.
            parser (TokenStreamParser): Parser used to link references.
        """
        self.entries = []
        for entry_data in data.get("Entries", []):
            name_index = parser.link_name_ref(entry_data.get("NameRef", ""))
            entry = UnLabelEntry(name_index=name_index, icode=0)
            if "JumpTo" in entry_data:
                # Pending resolution after icodes are computed.
                parser.register_pending_label_entry(entry, str(entry_data["JumpTo"]))
            else:
                entry.icode = int(entry_data.get("ICode", 0))
            self.entries.append(entry)


# ===================================================================== #
#  Token registry
# ===================================================================== #


TOKEN_REGISTRY: Dict[UnScriptTokenType, Type[UnToken]] = {
    # No-data tokens
    UnScriptTokenType.BoolVariable: UnTokenBoolVariable,
    UnScriptTokenType.Nothing: UnTokenNothing,
    UnScriptTokenType.EndOfScript: UnTokenEndOfScript,
    UnScriptTokenType.EndFunctionParms: UnTokenEndFunctionParms,
    UnScriptTokenType.IntZero: UnTokenIntZero,
    UnScriptTokenType.IntOne: UnTokenIntOne,
    UnScriptTokenType.TrueToken: UnTokenTrue,
    UnScriptTokenType.FalseToken: UnTokenFalse,
    UnScriptTokenType.NoObject: UnTokenNoObject,
    UnScriptTokenType.Self: UnTokenSelf,
    UnScriptTokenType.IteratorPop: UnTokenIteratorPop,
    UnScriptTokenType.Stop: UnTokenStop,
    UnScriptTokenType.IteratorNext: UnTokenIteratorNext,
    # Object reference tokens
    UnScriptTokenType.LocalVariable: UnTokenLocalVariable,
    UnScriptTokenType.InstanceVariable: UnTokenInstanceVariable,
    UnScriptTokenType.DefaultVariable: UnTokenDefaultVariable,
    UnScriptTokenType.NativeParm: UnTokenNativeParm,
    # Constant value tokens
    UnScriptTokenType.IntConst: UnTokenIntConst,
    UnScriptTokenType.FloatConst: UnTokenFloatConst,
    UnScriptTokenType.ByteConst: UnTokenByteConst,
    UnScriptTokenType.IntConstByte: UnTokenIntConstByte,
    UnScriptTokenType.ObjectConst: UnTokenObjectConst,
    UnScriptTokenType.NameConst: UnTokenNameConst,
    UnScriptTokenType.RotationConst: UnTokenRotationConst,
    UnScriptTokenType.VectorConst: UnTokenVectorConst,
    UnScriptTokenType.StringConst: UnTokenStringConst,
    UnScriptTokenType.UnicodeStringConst: UnTokenUnicodeStringConst,
    # Assignment tokens
    UnScriptTokenType.Let: UnTokenLet,
    UnScriptTokenType.LetBool: UnTokenLetBool,
    UnScriptTokenType.LetDelegate: UnTokenLetDelegate,
    # Jump / branch tokens
    UnScriptTokenType.Jump: UnTokenJump,
    UnScriptTokenType.JumpIfNot: UnTokenJumpIfNot,
    UnScriptTokenType.Switch: UnTokenSwitch,
    UnScriptTokenType.Case: UnTokenCase,
    UnScriptTokenType.Assert: UnTokenAssert,
    UnScriptTokenType.Iterator: UnTokenIterator,
    # Function call tokens
    UnScriptTokenType.FinalFunction: UnTokenFinalFunction,
    UnScriptTokenType.VirtualFunction: UnTokenVirtualFunction,
    UnScriptTokenType.GlobalFunction: UnTokenGlobalFunction,
    UnScriptTokenType.DelegateFunction: UnTokenDelegateFunction,
    # Context tokens
    UnScriptTokenType.Context: UnTokenContext,
    UnScriptTokenType.ClassContext: UnTokenClassContext,
    # Array tokens
    UnScriptTokenType.ArrayElement: UnTokenArrayElement,
    UnScriptTokenType.DynArrayElement: UnTokenDynArrayElement,
    UnScriptTokenType.DynArrayLength: UnTokenDynArrayLength,
    UnScriptTokenType.DynArrayInsert: UnTokenDynArrayInsert,
    UnScriptTokenType.DynArrayRemove: UnTokenDynArrayRemove,
    # Cast tokens
    UnScriptTokenType.PrimitiveCast: UnTokenPrimitiveCast,
    UnScriptTokenType.MetaCast: UnTokenMetaCast,
    UnScriptTokenType.DynamicCast: UnTokenDynamicCast,
    # Struct tokens
    UnScriptTokenType.StructCmpEq: UnTokenStructCmpEq,
    UnScriptTokenType.StructCmpNe: UnTokenStructCmpNe,
    UnScriptTokenType.StructMember: UnTokenStructMember,
    # Miscellaneous tokens
    UnScriptTokenType.Return: UnTokenReturn,
    UnScriptTokenType.EatString: UnTokenEatString,
    UnScriptTokenType.New: UnTokenNew,
    UnScriptTokenType.GotoLabel: UnTokenGotoLabel,
    UnScriptTokenType.Skip: UnTokenSkip,
    UnScriptTokenType.DelegateProperty: UnTokenDelegateProperty,
    UnScriptTokenType.DebugInfo: UnTokenDebugInfo,
    UnScriptTokenType.LabelTable: UnTokenLabelTable,
}

# Reverse lookup: token type name string -> token class
_TOKEN_NAME_REGISTRY: Dict[str, Type[UnToken]] = {}
for _tt, _cls in TOKEN_REGISTRY.items():
    _TOKEN_NAME_REGISTRY[_tt.name] = _cls
# Also register special token types not in TOKEN_REGISTRY
_TOKEN_NAME_REGISTRY["NativeFunction"] = UnTokenNativeFunction
_TOKEN_NAME_REGISTRY["ExtendedNativeFunction"] = UnTokenExtendedNativeFunction


# ===================================================================== #
#  Token stream parser
# ===================================================================== #

# Attribute names under which a token may hold a child sub-expression.
# Used to walk a token tree generically (remapping, iteration).
_TOKEN_CHILD_ATTRS: Tuple[str, ...] = (
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
    "debug_info",
)

# Token attributes that hold a compact object reference (a signed package
# item index: positive = export, negative = import, 0 = null).  Every one of
# these is a genuine object reference (rendered through ``resolve_object_ref``
# in ``to_dict``), so a blanket ``hasattr`` sweep over them is safe.
_TOKEN_OBJ_REF_FIELDS: Tuple[str, ...] = (
    "object_ref",
    "function_ref",
    "delegate_ref",
    "class_ref",
    "struct_ref",
    "property_ref",
)


class TokenStreamParser:
    """Parses and serializes the UnrealScript bytecode token stream."""

    def __init__(self, package: "UnPackage") -> None:
        """Initialize the parser bound to a package.

        Args:
            package (UnPackage): The owning package used to resolve names and
                object references.
        """
        self.package = package
        self.tokens: List[UnToken] = []
        # Pending jump targets queued by ``register_pending_target`` during
        # ``from_dict`` (XML import).  Each entry is ``(target_token, attr_name,
        # label_string)``.  Resolved by :meth:`_resolve_labels_after_load`.
        self._pending_targets: List[Tuple[Any, str, str]] = []
        # Pending label-table entry icodes, resolved alongside targets.
        self._pending_label_entries: List[Tuple["UnLabelEntry", str]] = []

    def is_name_none(self, name_index: int) -> bool:
        """Check whether the name at a given index is ``"None"``.

        Args:
            name_index (int): Index into the package name table.

        Returns:
            bool: True if the resolved name is ``"None"``, else False.
        """
        if 0 <= name_index < len(self.package.names):
            return self.package.names[name_index].name == "None"
        return False

    # ------------------------------------------------------------------ #
    #  Label / jump-target machinery
    # ------------------------------------------------------------------ #

    @staticmethod
    def _make_label(icode: int) -> str:
        """Return the canonical label name for a given icode position.

        Args:
            icode (int): The instruction-code offset to label.

        Returns:
            str: The canonical ``L_xxxx`` label string.
        """
        return f"L_{icode:04X}"

    def icode_to_label(self, icode: int) -> str:
        """Return the symbolic label string for a raw icode offset.

        Used by ``to_dict`` of jump/branch tokens so XML carries
        ``JumpTo="L_0034"`` instead of brittle integer offsets.

        Args:
            icode (int): The instruction-code offset to label.

        Returns:
            str: The symbolic ``L_xxxx`` label string.
        """
        return self._make_label(icode)

    def register_pending_target(self, token: UnToken, attr: str, label: str) -> None:
        """Defer setting a token attribute to the icode of a label.

        Called from token ``from_dict`` for fields whose XML value is a
        symbolic ``L_xxxx`` label rather than a raw icode.  Resolved by
        :meth:`_resolve_labels_after_load` once every token has an
        ``icode_start`` assigned.

        Args:
            token (UnToken): The token whose attribute should be patched.
            attr (str): The attribute name to set on the token.
            label (str): The symbolic label string to resolve later.
        """
        self._pending_targets.append((token, attr, label))

    def register_pending_label_entry(self, entry: "UnLabelEntry", label: str) -> None:
        """Defer setting a label-entry icode from a label string.

        Args:
            entry (UnLabelEntry): The label-table entry to patch.
            label (str): The symbolic label string to resolve later.
        """
        self._pending_label_entries.append((entry, label))

    def _resolve_labels_after_load(self) -> None:
        """Patch deferred jump targets and label-table entries.

        Performs a dry-run serialize into a throw-away buffer so every
        token's ``icode_start`` is populated, then maps the recorded
        label strings (``L_xxxx``) back to absolute icode offsets and
        writes them into the queued target attributes.

        Raises:
            RuntimeError: If a pending jump label or label-table target
                cannot be resolved to an icode.
        """
        if not self._pending_targets and not self._pending_label_entries:
            return
        # Dry-run serialize populates ``icode_start`` on every token in
        # the tree (see ``serialize_expr``); throw away the bytes.
        self.serialize_stream(io.BytesIO())
        label_to_icode: Dict[str, int] = {}
        for token in self.tokens:
            for t in self._iter_tokens(token):
                label_to_icode[self._make_label(t.icode_start)] = t.icode_start
        for tgt, attr, label in self._pending_targets:
            icode = label_to_icode.get(label)
            if icode is None:
                raise RuntimeError(f"Unresolved jump label {label!r} in token stream")
            setattr(tgt, attr, icode)
        for entry, label in self._pending_label_entries:
            icode = label_to_icode.get(label)
            if icode is None:
                raise RuntimeError(f"Unresolved label table target {label!r}")
            entry.icode = icode
        self._pending_targets.clear()
        self._pending_label_entries.clear()

    @staticmethod
    def _iter_tokens(token: Optional[UnToken]) -> Iterator[UnToken]:
        """Depth-first walk yielding a token and every nested sub-token.

        Args:
            token (Optional[UnToken]): The token to walk, or None.

        Yields:
            UnToken: The token itself and each nested sub-token.
        """
        if token is None:
            return
        yield token
        for attr in (
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
            "debug_info",
        ):
            child = getattr(token, attr, None)
            if isinstance(child, UnToken):
                yield from TokenStreamParser._iter_tokens(child)
        params = getattr(token, "params", None)
        if isinstance(params, list):
            for p in params:
                if isinstance(p, UnToken):
                    yield from TokenStreamParser._iter_tokens(p)

    # ------------------------------------------------------------------ #
    #  Name index remapping
    # ------------------------------------------------------------------ #

    def remap_name_indices(self, index_map: Dict[int, int]) -> None:
        """Remap name indices in all tokens using an index map.

        Args:
            index_map (Dict[int, int]): Mapping from old to new name indices.
        """
        for token in self.tokens:
            self._remap_token(token, index_map)

    def _remap_token(self, token: UnToken, index_map: Dict[int, int]) -> None:
        """Recursively remap name indices inside a single token.

        Args:
            token (UnToken): The token to remap (including its children).
            index_map (Dict[int, int]): Mapping from old to new name indices.
        """
        # Tokens with name_index / function_name (name reference) fields
        if isinstance(token, UnTokenNameConst):
            token.name_index = index_map.get(token.name_index, token.name_index)
        elif isinstance(token, (UnTokenVirtualFunction, UnTokenGlobalFunction)):
            token.function_name = index_map.get(
                token.function_name, token.function_name
            )
        elif isinstance(token, UnTokenDelegateFunction):
            token.function_name = index_map.get(
                token.function_name, token.function_name
            )
        elif isinstance(token, UnTokenDelegateProperty):
            token.function_name = index_map.get(
                token.function_name, token.function_name
            )
        elif isinstance(token, UnTokenLabelTable):
            for entry in token.entries:
                entry.name_index = index_map.get(entry.name_index, entry.name_index)

        # Recurse into sub-expressions
        for attr in _TOKEN_CHILD_ATTRS:
            child = getattr(token, attr, None)
            if child is not None:
                self._remap_token(child, index_map)

        # Recurse into param lists
        params = getattr(token, "params", None)
        if params is not None:
            for p in params:
                self._remap_token(p, index_map)

    def iter_all_tokens(self) -> "Iterator[UnToken]":
        """Yield every token in the stream, recursively (depth-first).

        Walks each top-level token and all of its nested sub-expressions
        (see :data:`_TOKEN_CHILD_ATTRS`) and parameter lists.

        Yields:
            UnToken: Each token in the stream, depth-first.
        """

        def _walk(tok: "Optional[UnToken]") -> "Iterator[UnToken]":
            """Depth-first walk yielding a token and its nested sub-tokens.

            Args:
                tok (Optional[UnToken]): The token to walk, or None.

            Yields:
                UnToken: The token itself and each nested sub-token.
            """
            if tok is None:
                return
            yield tok
            for attr in _TOKEN_CHILD_ATTRS:
                child = getattr(tok, attr, None)
                if child is not None:
                    yield from _walk(child)
            for param in getattr(tok, "params", None) or []:
                yield from _walk(param)

        for token in self.tokens:
            yield from _walk(token)

    # ------------------------------------------------------------------ #
    #  Object-reference resolution (item-pointer backed)
    # ------------------------------------------------------------------ #

    def resolve_objects(self) -> None:
        """Resolve every token object reference to a package item pointer.

        Mirrors the structural :meth:`UnExport.resolve`: after this runs each
        object-ref field (see :data:`_TOKEN_OBJ_REF_FIELDS`) has a companion
        ``_item_<field>`` attribute holding the resolved
        :class:`UnPackageItem` (or ``None`` for a null ref).
        :meth:`link_objects` later re-derives the integer indices from those
        pointers, so the token stream survives export/import table
        renumbering (e.g. after ``remove_exports`` drops exports).
        """
        from ut2004packageutil.package.package import resolve_item

        for tok in self.iter_all_tokens():
            for field in _TOKEN_OBJ_REF_FIELDS:
                if hasattr(tok, field):
                    setattr(
                        tok,
                        "_item_" + field,
                        resolve_item(self.package, getattr(tok, field)),
                    )

    def link_objects(self) -> None:
        """Re-derive token object-reference indices from resolved item pointers.

        Idempotent: the integer field is overwritten from its stored item
        pointer regardless of the field's current value, so calling this
        repeatedly (or after the import/export tables have been renumbered)
        always yields the correct index.  Tokens that were never resolved
        (no ``_item_<field>`` companion) are left untouched.
        """
        from ut2004packageutil.package.package import link_item

        for tok in self.iter_all_tokens():
            for field in _TOKEN_OBJ_REF_FIELDS:
                item_attr = "_item_" + field
                if hasattr(tok, item_attr):
                    setattr(
                        tok, field, link_item(self.package, getattr(tok, item_attr))
                    )

    # ------------------------------------------------------------------ #
    #  Reference resolution helpers
    # ------------------------------------------------------------------ #

    def resolve_object_ref(self, ref: int) -> str:
        """Resolve a compact object reference index to a prefixed name string.

        Args:
            ref (int): The compact object reference index.

        Returns:
            str: The resolved prefixed name string.
        """
        return self.package.resolve_item_ref(ref)

    def link_object_ref(self, name: str) -> int:
        """Resolve a prefixed name string back to a compact reference index.

        Args:
            name (str): The prefixed name string.

        Returns:
            int: The compact object reference index.
        """
        return self.package.link_item_ref(name)

    def resolve_name_ref(self, name_index: int) -> str:
        """Resolve a name table index to the name string.

        Uses occurrence-based ``Name@N`` format (1-based) when the name
        appears more than once in the table.  Delegates to
        :meth:`UnPackage.resolve_name_index`.

        Args:
            name_index (int): Index into the package name table.

        Returns:
            str: The resolved name string (possibly ``Name@N``).
        """
        return self.package.resolve_name_index(name_index)

    def link_name_ref(self, name: str) -> int:
        """Resolve a ``Name`` or ``Name@N`` string back to a name table index.

        Delegates to :meth:`UnPackage.link_name_index`.

        Args:
            name (str): The name string (possibly ``Name@N``).

        Returns:
            int: The index into the package name table.
        """
        return self.package.link_name_index(name)

    # ------------------------------------------------------------------ #
    #  Dict serialization helpers
    # ------------------------------------------------------------------ #

    def token_to_dict(self, token: Optional[UnToken]) -> Dict[str, Any]:
        """Convert a single token to its dict representation.

        If the token's ``icode_start`` was collected by
        :meth:`_collect_referenced_labels` as a jump target, a ``Label``
        attribute is added in front of the type-specific fields purely
        for human readability.  ``Label`` is ignored on import — the
        symbolic ``L_xxxx`` strings used by jumpers are resolved by
        a dry-run serialize, not by reading these markers.

        Args:
            token (Optional[UnToken]): The token to convert, or None.

        Returns:
            Dict[str, Any]: The dict representation, or an empty dict for None.
        """
        if token is None:
            return {}
        d = token.to_dict(self)
        refs = getattr(self, "_referenced_labels", None)
        if refs and token.icode_start in refs:
            # Insert ``Label`` right after ``Type`` for readability.
            label = self._make_label(token.icode_start)
            new_d: Dict[str, Any] = {}
            for k, v in d.items():
                new_d[k] = v
                if k == "Type":
                    new_d["Label"] = label
            d = new_d
        return d

    def token_from_dict(self, data: Dict[str, Any]) -> Optional[UnToken]:
        """Reconstruct a token from its dict representation.

        Args:
            data (Dict[str, Any]): The dict representation to read from.

        Returns:
            Optional[UnToken]: The reconstructed token, or None if empty.

        Raises:
            RuntimeError: If the ``Type`` field names an unknown token type.
        """
        if not data:
            return None
        type_name = data.get("Type", "")
        if not type_name:
            return None
        token_class = _TOKEN_NAME_REGISTRY.get(type_name)
        if token_class is None:
            raise RuntimeError(f"Unknown token type: {type_name}")
        token = token_class()
        token.from_dict(data, self)
        return token

    def tokens_to_dict_list(self) -> List[Dict[str, Any]]:
        """Convert all top-level tokens to a list of dicts.

        Walks the token tree once first to record which icodes are
        actually referenced as jump targets, then emits a ``Label``
        attribute on those target tokens for human readability.

        Returns:
            List[Dict[str, Any]]: One dict per top-level token.
        """
        self._referenced_labels = self._collect_referenced_labels()
        return [self.token_to_dict(t) for t in self.tokens]

    def _collect_referenced_labels(self) -> set:
        """Return the set of icode positions referenced as jump targets.

        Inspects every token in the stream for fields that hold an
        absolute icode (Jump/JumpIfNot/Iterator end/Case/LabelTable
        entries) and collects the targets so the export can mark only
        actually-referenced icodes with a ``Label`` attribute.

        Returns:
            set: The set of referenced icode positions.
        """
        refs: set = set()
        for token in self.tokens:
            for t in self._iter_tokens(token):
                if isinstance(t, (UnTokenJump, UnTokenJumpIfNot)):
                    refs.add(t.offset)
                elif isinstance(t, UnTokenIterator):
                    refs.add(t.end_offset)
                elif (
                    isinstance(t, UnTokenCase)
                    and t.offset != UnTokenCase.DEFAULT_OFFSET
                ):
                    refs.add(t.offset)
                elif isinstance(t, UnTokenLabelTable):
                    for entry in t.entries:
                        if not self.is_name_none(entry.name_index):
                            refs.add(entry.icode)
        return refs

    def tokens_from_dict_list(self, data_list: List[Dict[str, Any]]) -> None:
        """Reconstruct all top-level tokens from a list of dicts.

        Also resolves any pending symbolic jump labels collected during
        the recursive ``from_dict`` walk by running a dry-run serialize
        to determine each token's final ``icode_start``.

        Args:
            data_list (List[Dict[str, Any]]): One dict per top-level token.
        """
        self.tokens = []
        for data in data_list:
            token = self.token_from_dict(data)
            if token is not None:
                self.tokens.append(token)
        self._resolve_labels_after_load()

    # ------------------------------------------------------------------ #
    #  Parsing
    # ------------------------------------------------------------------ #

    def parse_stream(self, reader: BinaryIO, script_size: int) -> None:
        """Parse the complete token stream from a reader.

        Reads tokens until ``icode`` reaches ``script_size``.

        Args:
            reader (BinaryIO): Binary stream positioned at the script start.
            script_size (int): Total serialized script size in bytes.

        Raises:
            RuntimeError: If the parsed size does not match ``script_size``.
        """
        icode = 0
        self.tokens = []
        while icode < script_size:
            token, icode = self.parse_expr(reader, icode)
            self.tokens.append(token)
        if icode != script_size:
            raise RuntimeError(
                f"Script serialization mismatch: got {icode}, expected {script_size}"
            )

    def parse_expr(self, reader: BinaryIO, icode: int) -> Tuple[UnToken, int]:
        """Parse a single expression from a reader.

        Args:
            reader (BinaryIO): Binary stream positioned at the opcode byte.
            icode (int): Current instruction-code offset.

        Returns:
            Tuple[UnToken, int]: The parsed token and the updated icode.

        Raises:
            RuntimeError: If the opcode byte is not a valid expression token.
        """
        token_byte = read_byte(reader)
        icode_start = icode
        icode += SIZE_BYTE

        # Native final function with id >= 0x70
        if token_byte >= UnScriptTokenType.FirstNative:
            token = UnTokenNativeFunction()
            token.icode_start = icode_start
            token.native_index = token_byte
            token.params = []
            while True:
                param, icode = self.parse_expr(reader, icode)
                token.params.append(param)
                if isinstance(param, UnTokenEndFunctionParms):
                    break
            icode = self.handle_optional_debug_info(reader, icode, token)
            return token, icode

        # Extended native with id 256-16383 (opcode 0x60-0x6F + second byte)
        if token_byte >= UnScriptTokenType.ExtendedNative:
            token = UnTokenExtendedNativeFunction()
            token.icode_start = icode_start
            second_byte = read_byte(reader)
            icode += SIZE_BYTE
            token.native_index = (token_byte - 0x60) * 256 + second_byte
            token.params = []
            while True:
                param, icode = self.parse_expr(reader, icode)
                token.params.append(param)
                if isinstance(param, UnTokenEndFunctionParms):
                    break
            icode = self.handle_optional_debug_info(reader, icode, token)
            return token, icode

        # Regular token — look up in registry
        try:
            token_type = UnScriptTokenType(token_byte)
        except ValueError:
            raise RuntimeError(f"Bad expr token 0x{token_byte:02X}")

        token_class = TOKEN_REGISTRY.get(token_type)
        if token_class is None:
            raise RuntimeError(f"Bad expr token 0x{token_byte:02X}")

        token = token_class()
        token.icode_start = icode_start
        icode = token.parse(reader, icode, self)
        return token, icode

    # ------------------------------------------------------------------ #
    #  Serialization
    # ------------------------------------------------------------------ #

    def serialize_stream(self, writer: BinaryIO) -> int:
        """Serialize all tokens to a writer.

        Args:
            writer (BinaryIO): Binary stream to write the token stream to.

        Returns:
            int: The final icode (equivalent to ``script_size``).
        """
        icode = 0
        for token in self.tokens:
            icode = self.serialize_expr(writer, token, icode)
        return icode

    def serialize_expr(self, writer: BinaryIO, token: UnToken, icode: int) -> int:
        """Serialize a single expression to a writer.

        As a side effect also records each visited token's ``icode_start``
        (used by :meth:`_resolve_labels_after_load` to map labels back to
        icodes on the import path).

        Args:
            writer (BinaryIO): Binary stream to write the token to.
            token (UnToken): The token to serialize.
            icode (int): Current instruction-code offset.

        Returns:
            int: The updated instruction-code offset.
        """
        token.icode_start = icode

        # Native function (>= 0x70)
        if isinstance(token, UnTokenNativeFunction):
            write_byte(writer, token.native_index)
            icode += SIZE_BYTE
            for param in token.params:
                icode = self.serialize_expr(writer, param, icode)
            if token.debug_info is not None:
                icode = self.serialize_expr(writer, token.debug_info, icode)
            return icode

        # Extended native (0x60-0x6F + second byte)
        if isinstance(token, UnTokenExtendedNativeFunction):
            first_byte = (token.native_index // 256) + 0x60
            second_byte = token.native_index % 256
            write_byte(writer, first_byte)
            icode += SIZE_BYTE
            write_byte(writer, second_byte)
            icode += SIZE_BYTE
            for param in token.params:
                icode = self.serialize_expr(writer, param, icode)
            if token.debug_info is not None:
                icode = self.serialize_expr(writer, token.debug_info, icode)
            return icode

        # Regular token
        write_byte(writer, token.token_type)
        icode += SIZE_BYTE
        icode = token.serialize(writer, icode, self)
        return icode

    # ------------------------------------------------------------------ #
    #  Optional trailing DebugInfo token
    # ------------------------------------------------------------------ #

    def handle_optional_debug_info(
        self,
        reader: BinaryIO,
        icode: int,
        parent_token: UnToken,
    ) -> int:
        """Check for and optionally parse a trailing DebugInfo token.

        Peeks ahead in the stream; if the next token is ``DebugInfo``
        with version 100, parses it and attaches to ``parent_token``.

        Args:
            reader (BinaryIO): Binary stream positioned after the parent token.
            icode (int): Current instruction-code offset.
            parent_token (UnToken): Token to attach the debug info to.

        Returns:
            int: The updated instruction-code offset.
        """
        saved_pos = reader.tell()

        # Try to peek at next byte
        peek_data = reader.read(1)
        if not peek_data:
            # End of stream
            reader.seek(saved_pos)
            return icode

        next_code = peek_data[0]
        version = -1
        if next_code == UnScriptTokenType.DebugInfo:
            # Peek at the version INT (4 bytes)
            version_data = reader.read(4)
            if len(version_data) == 4:
                version = struct.unpack("<i", version_data)[0]

        # Restore stream position
        reader.seek(saved_pos)

        if version == 100:
            debug_token, icode = self.parse_expr(reader, icode)
            parent_token.debug_info = debug_token  # type: ignore[attr-defined]

        return icode
