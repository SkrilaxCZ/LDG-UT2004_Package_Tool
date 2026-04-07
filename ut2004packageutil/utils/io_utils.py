"""Low-level binary I/O helpers (compact index, ASCII strings, hex conversion)."""

from typing import BinaryIO

from ut2004packageutil.utils.struct_utils import read_byte, write_byte

# ===================================================================== #
#  Compact index I/O
# ===================================================================== #


def read_index(reader: BinaryIO) -> int:
    """Read a compact index from a binary stream.

    Args:
        reader (BinaryIO): The binary stream to read from.

    Returns:
        int: The decoded compact index value.
    """
    b = read_byte(reader)
    sign = (b & 0x80) != 0
    has_next = (b & 0x40) != 0
    index = b & 0x3F
    shift = 6
    while has_next:
        b = read_byte(reader)
        has_next = (b & 0x80) != 0
        val = b & 0x7F
        index += val << shift
        shift += 7
    if sign:
        index *= -1
    return index


def write_index(writer: BinaryIO, index: int) -> None:
    """Write a compact index to a binary stream.

    Args:
        writer (BinaryIO): The binary stream to write to.
        index (int): The index value to encode and write.
    """
    sign = index < 0
    if sign:
        index = -index
    has_next = index > 0x3F
    b = index & 0x3F
    if has_next:
        b |= 0x40
    if sign:
        b |= 0x80
    write_byte(writer, b)
    index >>= 6
    while has_next:
        has_next = index > 0x7F
        b = index & 0x7F
        if has_next:
            b |= 0x80
        write_byte(writer, b)
        index >>= 7


# ===================================================================== #
#  ASCII string I/O
# ===================================================================== #


def read_ascii(source) -> str:
    """Read a null-terminated ASCII string from a stream.

    Args:
        source: The binary stream to read from.

    Returns:
        str: The decoded string, excluding the null terminator.
    """
    stream = source
    chars = []
    while True:
        b = stream.read(1)
        if not b:
            break
        ch = b[0] if isinstance(b, (bytes, bytearray)) else ord(b)
        if ch == 0:
            break
        chars.append(chr(ch))
    return "".join(chars)


def write_ascii(writer: BinaryIO, s: str) -> None:
    """Write a null-terminated string to a binary stream.

    Args:
        writer (BinaryIO): The binary stream to write to.
        s (str): The string to encode and write.
    """
    writer.write(s.encode("latin-1"))
    writer.write(b"\x00")


# ===================================================================== #
#  Hex conversion helpers
# ===================================================================== #


def bytes_to_hex(data: bytes) -> str:
    """Convert bytes to a space-separated hex string.

    Args:
        data (bytes): The bytes to convert.

    Returns:
        str: The space-separated uppercase hex representation.
    """
    return " ".join(f"{b:02X}" for b in data)


def hex_to_bytes(hex_str: str) -> bytes:
    """Convert a space-separated hex string back to bytes.

    Args:
        hex_str (str): The space-separated hex string to convert.

    Returns:
        bytes: The decoded bytes.
    """
    return bytes(int(h, 16) for h in hex_str.split())
