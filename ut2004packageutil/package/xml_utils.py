"""Standalone XML serialisation helpers for package data dicts.

These helpers convert between Python dicts (as produced by
``UnObject.to_dict()``/``UnPropertyTag.to_dict()``) and
``xml.etree.ElementTree`` elements.  They live in their own module so they
can be reused by both :mod:`ut2004packageutil.package.io` (the package
XML reader/writer) and :mod:`ut2004packageutil.package.object` (per-object
sidecar files) without creating an import cycle.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional

# Regex matching XML 1.0 illegal characters:
# 0x00-0x08, 0x0B, 0x0C, 0x0E-0x1F (but NOT 0x09, 0x0A, 0x0D)
_XML_ILLEGAL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
# Pattern to find our custom escape sequences during unsanitization
_XML_UNESCAPE_RE = re.compile(r"\{\{x([0-9A-Fa-f]{2})\}\}")

# Attributes whose values are fully determined by the enclosing struct
# definition (or carry a default) and can be omitted from ``<Field>``
# elements to keep the XML compact.  They are re-derived during import.
IMPLICIT_FIELD_ATTRS = frozenset({"type"})


def sanitize_xml_text(text: str) -> str:
    """Replace XML-illegal characters with ``{{xHH}}`` escapes.

    Args:
        text (str): The text to sanitize.

    Returns:
        str: The text with XML-illegal characters escaped.
    """

    def _escape(m: re.Match) -> str:
        """Return the ``{{xHH}}`` escape for a matched illegal character.

        Args:
            m (re.Match): The regex match for an illegal character.

        Returns:
            str: The escape sequence for the matched character.
        """
        return f"{{{{x{ord(m.group()):02X}}}}}"

    return _XML_ILLEGAL_RE.sub(_escape, text)


def unsanitize_xml_text(text: str) -> str:
    """Reverse ``{{xHH}}`` escapes back to the original characters.

    Args:
        text (str): The text containing ``{{xHH}}`` escapes.

    Returns:
        str: The text with escapes restored to their original characters.
    """

    def _unescape(m: re.Match) -> str:
        """Return the original character for a matched ``{{xHH}}`` escape.

        Args:
            m (re.Match): The regex match for an escape sequence.

        Returns:
            str: The character decoded from the escape's hex code.
        """
        return chr(int(m.group(1), 16))

    return _XML_UNESCAPE_RE.sub(_unescape, text)


def format_value(value: Any) -> str:
    """Format a Python value for XML text/attribute output.

    Booleans become ``"true"``/``"false"``; everything else uses ``str()``.

    Args:
        value (Any): The value to format.

    Returns:
        str: The string representation for XML output.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def is_flat_dict(d: Dict[str, Any]) -> bool:
    """Return True if *d* contains only scalar (non-dict, non-list) values.

    The special ``_struct_fields`` (dict) and ``_elements`` (list) keys
    are allowed; they are rendered as child elements rather than attributes.

    Args:
        d (Dict[str, Any]): The dict to inspect.

    Returns:
        bool: True if all values (aside from the special keys) are scalar.
    """
    for k, v in d.items():
        if k in ("_struct_fields", "_elements"):
            continue  # handled specially
        if isinstance(v, (dict, list)):
            return False
    return True


def write_struct_fields(parent_el: ET.Element, struct_fields: Dict[str, Any]) -> None:
    """Write a struct's fields into *parent_el*.

    For native structs (``native=True``): writes each (field_name, value)
    as a child element with the field name as tag.
    For tagged structs (``native=False``): writes each tag as a ``<Field>``
    child via :func:`write_tag_to_element` with ``implicit=True`` so
    redundant attributes (``type``) are elided; they are recovered from
    the struct definition during import.

    Args:
        parent_el (ET.Element): The element to write the fields into.
        struct_fields (Dict[str, Any]): The struct-field data to serialize.
    """
    if struct_fields.get("native"):
        for fname, fval in struct_fields.get("fields", {}).items():
            fel = ET.SubElement(parent_el, fname)
            if isinstance(fval, dict):
                # Nested native struct
                write_struct_fields(fel, fval)
            else:
                fel.text = sanitize_xml_text(format_value(fval))
    else:
        none_idx = struct_fields.get("none_index", "")
        if none_idx:
            parent_el.set("none_index", str(none_idx))
        for sf_item in struct_fields.get("tags", []):
            if isinstance(sf_item, dict):
                sf_el = ET.SubElement(parent_el, "Field")
                write_tag_to_element(sf_el, sf_item, implicit=True)


def write_tag_to_element(
    target_el: ET.Element, tag_dict: Dict[str, Any], implicit: bool = False
) -> None:
    """Write a tagged-property dict into *target_el*.

    Scalar entries become XML attributes; ``_text`` becomes element text;
    ``_struct_fields`` becomes nested children via :func:`write_struct_fields`;
    ``_elements`` becomes a list of ``<Element>`` children, each recursively
    handled by :func:`write_array_element`.

    When *implicit* is ``True`` (used for ``<Field>`` elements nested
    inside a struct), attributes listed in :data:`IMPLICIT_FIELD_ATTRS`
    are omitted from the output — they are uniquely determined by the
    parent struct's field definition.

    Args:
        target_el (ET.Element): The element to write the tag data into.
        tag_dict (Dict[str, Any]): The tagged-property dict to serialize.
        implicit (bool): If True, omit :data:`IMPLICIT_FIELD_ATTRS` attributes.
    """
    struct_fields = None
    elements_list = None
    for k, v in tag_dict.items():
        if k == "_elements":
            elements_list = v
        elif k == "_text":
            target_el.text = sanitize_xml_text(format_value(v))
        elif k == "_struct_fields":
            struct_fields = v
        else:
            if implicit and k in IMPLICIT_FIELD_ATTRS:
                continue
            target_el.set(k, sanitize_xml_text(format_value(v)))
    if struct_fields and isinstance(struct_fields, dict):
        write_struct_fields(target_el, struct_fields)
    if elements_list is not None:
        for elem_val in elements_list:
            eel = ET.SubElement(target_el, "Element")
            write_array_element(eel, elem_val)


def write_array_element(target_el: ET.Element, elem_val: Any) -> None:
    """Write one array element value into *target_el*.

    Struct elements (dict with ``native`` flag) use
    :func:`write_struct_fields`; everything else is written as text.

    Args:
        target_el (ET.Element): The element to write the value into.
        elem_val (Any): The array element value to serialize.
    """
    if isinstance(elem_val, dict) and ("native" in elem_val):
        write_struct_fields(target_el, elem_val)
    else:
        target_el.text = sanitize_xml_text(format_value(elem_val))


def dict_to_xml(parent: ET.Element, data: Dict[str, Any]) -> None:
    """Recursively convert a dict to XML sub-elements.

    Args:
        parent (ET.Element): The element to append sub-elements to.
        data (Dict[str, Any]): The dict to serialize.
    """
    for key, value in data.items():
        child = ET.SubElement(parent, key)
        if isinstance(value, dict):
            dict_to_xml(child, value)
        elif isinstance(value, list):
            # Detect token dict lists (dicts with uppercase "Type" key)
            if value and isinstance(value[0], dict) and "Type" in value[0]:
                for item in value:
                    if isinstance(item, dict):
                        token_dict_to_xml(child, item)
                    else:
                        item_el = ET.SubElement(child, "Item")
                        item_el.text = sanitize_xml_text(format_value(item))
            else:
                for item in value:
                    item_el = ET.SubElement(child, "Item")
                    if isinstance(item, dict) and is_flat_dict(item):
                        # Tagged-property dict — use recursive writer
                        write_tag_to_element(item_el, item)
                    elif isinstance(item, dict):
                        dict_to_xml(item_el, item)
                    else:
                        item_el.text = sanitize_xml_text(format_value(item))
        else:
            child.text = sanitize_xml_text(format_value(value))


def token_dict_to_xml(parent: ET.Element, token_dict: Dict[str, Any]) -> None:
    """Convert a token dict to a ``<Token>`` element with attributes.

    Scalar values become XML attributes; nested token dicts and lists
    become child elements.  Known list tags (``Params``, ``Entries``)
    are always treated as lists during deserialization.

    Args:
        parent (ET.Element): The element to append the ``<Token>`` to.
        token_dict (Dict[str, Any]): The token dict to serialize.
    """
    el = ET.SubElement(parent, "Token")
    for key, value in token_dict.items():
        if isinstance(value, dict):
            if value:
                # Non-empty nested dict (sub-token or other)
                wrapper = ET.SubElement(el, key)
                if "Type" in value:
                    token_dict_to_xml(wrapper, value)
                else:
                    dict_to_xml(wrapper, value)
            # Empty dict = null sub-token, skip
        elif isinstance(value, list):
            wrapper = ET.SubElement(el, key)
            for item in value:
                if isinstance(item, dict) and "Type" in item:
                    token_dict_to_xml(wrapper, item)
                elif isinstance(item, dict):
                    # Flat dict → <Entry> with attributes
                    if all(not isinstance(v, (dict, list)) for v in item.values()):
                        entry_el = ET.SubElement(wrapper, "Entry")
                        for k, v in item.items():
                            entry_el.set(k, sanitize_xml_text(str(v)))
                    else:
                        item_el = ET.SubElement(wrapper, "Item")
                        dict_to_xml(item_el, item)
                else:
                    item_el = ET.SubElement(wrapper, "Item")
                    item_el.text = sanitize_xml_text(str(item))
        else:
            el.set(key, sanitize_xml_text(str(value)))


def xml_token_to_dict(element: ET.Element) -> Dict[str, Any]:
    """Convert a ``<Token>`` XML element back to a token dict.

    Args:
        element (ET.Element): The ``<Token>`` element to parse.

    Returns:
        Dict[str, Any]: The reconstructed token dict.
    """
    result: Dict[str, Any] = {}
    # Attributes → scalar values
    for key, value in element.attrib.items():
        result[key] = unsanitize_xml_text(value)
    # Child elements → nested values
    # Tags that are always treated as lists (no _list marker needed)
    _LIST_TAGS = {"Params", "Entries"}
    for child in element:
        tag = child.tag
        sub_children = list(child)
        if tag in _LIST_TAGS:
            token_children = [sc for sc in sub_children if sc.tag == "Token"]
            entry_children = [sc for sc in sub_children if sc.tag == "Entry"]
            if token_children:
                result[tag] = [xml_token_to_dict(sc) for sc in token_children]
            elif entry_children:
                result[tag] = []
                for entry_el in entry_children:
                    entry_dict: Dict[str, Any] = {}
                    for k, v in entry_el.attrib.items():
                        entry_dict[k] = unsanitize_xml_text(v)
                    result[tag].append(entry_dict)
            else:
                result[tag] = []
        elif sub_children and len(sub_children) == 1 and sub_children[0].tag == "Token":
            # Single nested token (not marked as list)
            result[tag] = xml_token_to_dict(sub_children[0])
        elif not sub_children:
            # Empty wrapper = empty dict (null sub-token)
            result[tag] = {}
        else:
            # Fallback to regular dict parsing
            result[tag] = xml_to_dict(child)
    return result


def read_struct_fields(parent_el: ET.Element) -> Optional[Dict[str, Any]]:
    """Read struct fields from *parent_el* children.

    Inverse of :func:`write_struct_fields`.  Returns a dict
    ``{"native": True, "fields": {...}}`` or
    ``{"native": False, "tags": [...], "none_index": "..."}``.
    Returns ``None`` if no struct field children are present.

    Args:
        parent_el (ET.Element): The element whose children hold struct fields.

    Returns:
        Optional[Dict[str, Any]]: The parsed struct-field dict, or None if
            no struct field children are present.
    """
    children = list(parent_el)
    field_children = [c for c in children if c.tag == "Field"]
    non_field_children = [c for c in children if c.tag not in ("Field", "Element")]
    if field_children:
        tags_list: List[Dict[str, Any]] = []
        for fc in field_children:
            tags_list.append(read_tag_from_element(fc))
        return {
            "native": False,
            "tags": tags_list,
            "none_index": parent_el.get("none_index", ""),
        }
    if non_field_children:
        fields_dict: Dict[str, Any] = {}
        for nfc in non_field_children:
            # Native struct field: child element name = field name;
            # value is either text or a nested native struct (recurse).
            nested = read_struct_fields(nfc)
            if nested is not None and nested.get("native"):
                fields_dict[nfc.tag] = nested
            else:
                raw = nfc.text if nfc.text is not None else ""
                fields_dict[nfc.tag] = unsanitize_xml_text(raw)
        return {"native": True, "fields": fields_dict}
    return None


def read_tag_from_element(target_el: ET.Element) -> Dict[str, Any]:
    """Read a tagged-property dict from *target_el*.

    Inverse of :func:`write_tag_to_element`.  Reads attributes as scalar
    fields, element text as ``_text``, ``<Field>`` children as
    ``_struct_fields``, and ``<Element>`` children as ``_elements``.

    Args:
        target_el (ET.Element): The element to parse.

    Returns:
        Dict[str, Any]: The reconstructed tagged-property dict.
    """
    result: Dict[str, Any] = {}
    for k, v in target_el.attrib.items():
        if k == "none_index":
            continue  # consumed by read_struct_fields
        result[k] = unsanitize_xml_text(v)
    # Preserve text exactly when there are no child elements (leaf node);
    # otherwise treat whitespace-only text as ET.indent artefact.
    children = list(target_el)
    if target_el.text is not None:
        if not children:
            result["_text"] = unsanitize_xml_text(target_el.text)
        elif target_el.text.strip():
            result["_text"] = unsanitize_xml_text(target_el.text)
    element_children = [c for c in children if c.tag == "Element"]
    if element_children:
        elem_list: List[Any] = []
        for ec in element_children:
            elem_list.append(read_array_element(ec))
        result["_elements"] = elem_list

    struct_fields = read_struct_fields(target_el)
    if struct_fields is not None:
        result["_struct_fields"] = struct_fields
    return result


def read_array_element(target_el: ET.Element) -> Any:
    """Read one array element value from *target_el*.

    Inverse of :func:`write_array_element`.  Struct element children
    return a dict; plain text elements return a string.

    Args:
        target_el (ET.Element): The element to parse.

    Returns:
        Any: A struct-field dict for struct elements, else the element text.
    """
    struct_fields = read_struct_fields(target_el)
    if struct_fields is not None:
        return struct_fields
    return unsanitize_xml_text(target_el.text if target_el.text else "")


def xml_to_dict(element: ET.Element) -> Dict[str, Any]:
    """Convert XML element children back to a dict.

    Args:
        element (ET.Element): The element whose children are parsed.

    Returns:
        Dict[str, Any]: The reconstructed dict.
    """
    result: Dict[str, Any] = {}
    for child in element:
        tag = child.tag
        sub_children = list(child)
        if sub_children and all(sc.tag == "Token" for sc in sub_children):
            # List of token elements
            result[tag] = [xml_token_to_dict(sc) for sc in sub_children]
        elif sub_children and all(sc.tag == "Item" for sc in sub_children):
            # This is a list
            items: List[Any] = []
            for item_el in sub_children:
                item_children = list(item_el)
                if item_el.attrib:
                    # <Item> with attributes → tagged-property dict
                    items.append(read_tag_from_element(item_el))
                elif item_children:
                    items.append(xml_to_dict(item_el))
                else:
                    raw = item_el.text if item_el.text else ""
                    items.append(unsanitize_xml_text(raw))
            result[tag] = items
        elif sub_children:
            result[tag] = xml_to_dict(child)
        else:
            raw = child.text if child.text else ""
            result[tag] = unsanitize_xml_text(raw)
    return result
