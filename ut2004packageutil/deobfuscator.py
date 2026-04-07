"""Apply a symbol-name map to a package's name table (deobfuscation).

Deobfuscation recovers readable identifiers for an obfuscated package by mapping
each unique obfuscated token to a human-chosen name.  The map is applied to the
package's **name table** (each entry renamed in place), so every reference — code
tokens, ``defaultproperties``, name constants — follows automatically, because
references store a name *index*, not a copy of the string.

Multiple obfuscated tokens may map to the same human name (the same source name
recurs across unrelated classes).  Within a single function, however:

* a parameter or local may **not** share a name with any variable or function of
  its class or any superclass, and
* a local may **not** share a name with a parameter of the same function.

If either held, the recompiled source would let the local *shadow* (capture) the
member reference, silently changing behaviour — e.g. ``member = param`` collapses
to ``bStartup = bStartup`` and, once recompiled, the parameter captures the
left-hand side so the member is never written.  Such clashes are resolved here by
suffixing the offending parameter/local (the member keeps its name).  A clash
that cannot be resolved by renaming — two distinct *fields of the same class*
mapped to one name — is a map error and raises :class:`DeobfuscationError`.
"""

import base64
import re
from typing import Callable, Dict, List, Set, Tuple

from ut2004packageutil.package.flags import UnPropertyFlags
from ut2004packageutil.package.object import (
    UnClass,
    UnConst,
    UnEnum,
    UnField,
    UnFunction,
    UnProperty,
    UnState,
    UnStruct,
)
from ut2004packageutil.package.package import UnExport, UnName, UnPackage


class DeobfuscationError(RuntimeError):
    """Raised when a name map cannot be applied without an identifier clash.

    The only unresolvable case is two distinct fields of the *same* class
    resolving to one name; parameter/local clashes are auto-disambiguated.
    """


class DeobfuscationReport:
    """Summary of an :func:`apply_name_map` run.

    Attributes:
        applied (int): Number of name-table entries whose string changed.
        renames (List[Tuple[str, str, str, str]]): De-collision renames as
            ``(function, obfuscated_token, wanted_name, final_name)``.
    """

    def __init__(self) -> None:
        self.applied: int = 0
        self.renames: List[Tuple[str, str, str, str]] = []


_IDENT = re.compile(r"^[A-Za-z_]\w*$")

# A map file whose header carries this marker (in a ``# ...`` comment) has its
# obfuscated-token column base64-encoded.  Harder-mode symbols contain newlines
# and control bytes that cannot sit on a single map line, so ``obfuscate`` writes
# them base64-encoded and flags it here; simple/plain tokens are written verbatim.
TOKEN_ENCODING_MARKER = "token-encoding: base64"


def encode_map_token(token: str) -> str:
    """Base64-encode an obfuscated token for a map file's left-hand column.

    Args:
        token (str): The raw obfuscated symbol (may contain control bytes).

    Returns:
        str: An ASCII, single-line base64 rendering of ``token``.
    """
    return base64.b64encode(token.encode("latin-1")).decode("ascii")


def decode_map_token(token: str) -> str:
    """Reverse :func:`encode_map_token`.

    Args:
        token (str): The base64 text from a map file's left-hand column.

    Returns:
        str: The raw obfuscated symbol.

    Raises:
        DeobfuscationError: If ``token`` is not valid base64.
    """
    try:
        return base64.b64decode(token.encode("ascii"), validate=True).decode("latin-1")
    except (ValueError, UnicodeDecodeError) as ex:
        raise DeobfuscationError(f"malformed base64 token {token!r}: {ex}") from ex


def parse_map_file(path: str) -> Dict[str, str]:
    """Parse a name map into ``{obfuscated_token: resolved_name}``.

    Each mapping line is ``<ObfuscatedToken> = <ResolvedName>`` with an optional
    trailing ``# ...`` provenance comment.  Lines that are blank or start with
    ``#`` are ignored, except that a ``#`` header line carrying
    :data:`TOKEN_ENCODING_MARKER` switches the token column into base64 (each
    token is decoded back to its raw bytes before use).  A token whose resolved
    name equals itself (still unresolved) is skipped.  Because ``#`` cannot appear
    in an UnrealScript identifier, everything from the first ``#`` in the value is
    the comment.

    The obfuscated token is taken verbatim (after any base64 decode) and its
    format is **not** validated — the parser simply renames whatever symbol it
    names.  Only the resolved name is checked to be a bare identifier.

    Args:
        path (str): Path to the map file.

    Returns:
        Dict[str, str]: Obfuscated token -> resolved identifier.

    Raises:
        DeobfuscationError: If a resolved name is not a bare identifier (a
        leaked comment or stray text would otherwise corrupt the rename), or a
        base64 token cannot be decoded.
    """
    mapping: Dict[str, str] = {}
    base64_tokens = False
    with open(path, "r", encoding="latin-1") as handle:
        for raw in handle:
            line = raw.rstrip("\r\n")
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith("#"):
                if TOKEN_ENCODING_MARKER in stripped.lower():
                    base64_tokens = True
                continue
            if " = " not in line:
                continue
            token, value = line.split(" = ", 1)
            token = token.strip()
            name = value.split("#", 1)[0].strip()
            if not token or not name:
                continue
            if base64_tokens:
                token = decode_map_token(token)
            if name == token:
                continue
            if not _IDENT.match(name):
                raise DeobfuscationError(
                    f"malformed mapped name {name!r} parsed from: {line!r}"
                )
            mapping[token] = name
    return mapping


def _iter_children(struct_obj: UnStruct) -> List["UnExport"]:
    """Return a struct's child field items in stored order.

    Args:
        struct_obj (UnStruct): The struct (class/state/function) to walk.

    Returns:
        List[UnExport]: The child field export items.
    """
    out: List["UnExport"] = []
    child = struct_obj.children
    while child is not None:
        out.append(child)
        obj = child.object
        child = obj.next_item if isinstance(obj, UnField) else None
    return out


def _enclosing_class(item: "UnExport") -> "UnClass":
    """Return the ``UnClass`` object that owns a function export, or None.

    Walks the ``group_item`` (outer) chain, so a function declared inside a
    state resolves to the state's owning class.

    Args:
        item (UnExport): A function export.

    Returns:
        UnClass: The owning class object, or None if not found.
    """
    cur = item.group_item
    while cur is not None:
        if isinstance(cur.object, UnClass):
            return cur.object
        cur = cur.group_item
    return None


def _hierarchy_field_names(
    class_obj: UnClass, resolved: Callable[[str], str]
) -> Set[str]:
    """Collect the (resolved, lowercased) names of every var/function in scope.

    Walks the class and its full superclass chain — across packages, since
    ``super_item.object`` resolves an imported engine class to its definition in
    a loaded dependency — gathering the names a parameter/local must not shadow.

    Args:
        class_obj (UnClass): The class whose hierarchy is scanned.
        resolved (Callable[[str], str]): Maps an obfuscated token to its final
            name (identity for names not in the map).

    Returns:
        Set[str]: Lowercased resolved names of all variables and functions of
        the class and its ancestors.
    """
    names: Set[str] = set()
    seen: Set[int] = set()
    cur = class_obj
    while isinstance(cur, UnStruct) and id(cur) not in seen:
        seen.add(id(cur))
        for child in _iter_children(cur):
            obj = child.object
            if isinstance(obj, (UnProperty, UnFunction)):
                names.add(resolved(child.object_name.name).lower())
        sup = cur.super_item
        cur = sup.object if sup is not None else None
    return names


def _decollide(base: str, taken: Set[str]) -> str:
    """Return a variant of ``base`` whose lowercase form is not in ``taken``.

    Tries a ``P`` (parameter) suffix first, then numeric suffixes.

    Args:
        base (str): The wanted (colliding) name.
        taken (Set[str]): Lowercased names already used in scope.

    Returns:
        str: A non-colliding identifier.
    """
    cand = base + "P"
    if cand.lower() not in taken:
        return cand
    n = 2
    while f"{base}{n}".lower() in taken:
        n += 1
    return f"{base}{n}"


def apply_name_map(pkg: UnPackage, mapping: Dict[str, str]) -> DeobfuscationReport:
    """Rename a package's symbols in place from ``mapping`` (obfuscated -> name).

    Renames name-table entries so references follow automatically, and
    disambiguates parameters/locals that would shadow a class/superclass member
    once recompiled.

    Args:
        pkg (UnPackage): The loaded package (with dependencies, so superclass
            members resolve) to rename in place.
        mapping (Dict[str, str]): Obfuscated token -> resolved human name.
            Tokens absent from the map are left untouched.

    Returns:
        DeobfuscationReport: What changed, including de-collision renames.

    Raises:
        DeobfuscationError: If two distinct fields of one class resolve to the
        same name (an unresolvable map error).
    """

    def resolved(name: str) -> str:
        return mapping.get(name, name)

    report = DeobfuscationReport()

    # 1. Unresolvable clashes: two distinct fields of one class -> one name.
    for exp in pkg.exports:
        obj = exp.object
        if not isinstance(obj, UnClass):
            continue
        seen: Dict[str, "UnExport"] = {}
        for child in _iter_children(obj):
            if not isinstance(
                child.object,
                (UnProperty, UnFunction, UnState, UnEnum, UnConst, UnStruct),
            ):
                continue
            final = resolved(child.object_name.name)
            key = final.lower()
            prev = seen.get(key)
            if prev is not None and prev is not child:
                raise DeobfuscationError(
                    f"class {exp.object_name.name}: '{prev.object_name.name}' "
                    f"and '{child.object_name.name}' both resolve to '{final}'"
                )
            seen[key] = child

    # 2. Per-function param/local de-collision against the class hierarchy.
    overrides: List[Tuple["UnExport", str]] = []  # (property_export, final_name)
    forbidden_cache: Dict[int, Set[str]] = {}
    for exp in pkg.exports:
        obj = exp.object
        if not isinstance(obj, UnFunction):
            continue
        cls = _enclosing_class(exp)
        if cls is None:
            continue
        cid = id(cls)
        forbidden = forbidden_cache.get(cid)
        if forbidden is None:
            forbidden = _hierarchy_field_names(cls, resolved)
            forbidden_cache[cid] = forbidden

        # Parameters first, then locals, in declaration order, so a local is
        # disambiguated against the parameters as well as the members.
        params: List["UnExport"] = []
        locals_: List["UnExport"] = []
        for child in _iter_children(obj):
            co = child.object
            if not isinstance(co, UnProperty):
                continue
            if co.property_flags & UnPropertyFlags.Parm:
                params.append(child)
            else:
                locals_.append(child)

        assigned: Set[str] = set()
        for child in params + locals_:
            want = resolved(child.object_name.name)
            low = want.lower()
            if low in forbidden or low in assigned:
                final = _decollide(want, forbidden | assigned)
                overrides.append((child, final))
                assigned.add(final.lower())
                report.renames.append(
                    (exp.object_name.name, child.object_name.name, want, final)
                )
            else:
                assigned.add(low)

    # 3. Apply.  The plain map renames each name-table entry in place, so every
    #    reference (which stores a name *index*) follows automatically.  A
    #    de-collision instead repoints its property to a FRESH entry: the
    #    property's colliding name may be a preserved string shared with a
    #    function or a name constant (e.g. ``Kill``), and mutating that shared
    #    entry would rename those too.  A local/parameter is only ever addressed
    #    through its export, so repointing renames it alone.
    for entry in list(pkg.names):
        if entry.name in mapping:
            entry.name = mapping[entry.name]
            report.applied += 1
    for child, final in overrides:
        new_entry = UnName(final, child.object_name.flags)
        pkg.names.append(new_entry)
        child.object_name = new_entry
        report.applied += 1

    # Renaming (and appending de-collision entries) leaves duplicate name-table
    # strings — several tokens legitimately share one human name.  We do NOT
    # merge them (pkg.deduplicate_names rebuilds the table and mis-remaps raw
    # name indices inside nested struct/array defaultproperties, corrupting e.g.
    # an emitter's `SizeScale` field names); duplicates are harmless because the
    # package is only decompiled, and the decompiler renders defaultproperties by
    # string (dropping the ``Name@N`` occurrence marker it would otherwise emit
    # to disambiguate duplicates — that marker is not valid UnrealScript).
    pkg._invalidate_caches()
    return report
