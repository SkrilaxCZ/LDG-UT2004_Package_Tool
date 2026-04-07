"""Unreal package flag enumerations, GUID class, and string conversion helpers."""

import uuid
from enum import IntEnum, IntFlag
from typing import BinaryIO


class UnGuid:
    """A 16-byte GUID used in Unreal packages."""

    def __init__(self, data: bytes = b"\x00" * 16) -> None:
        """Initialize the GUID from raw bytes.

        Args:
            data (bytes): The raw 16-byte GUID value.

        Raises:
            ValueError: If ``data`` is not exactly 16 bytes long.
        """
        if len(data) != 16:
            raise ValueError(f"GUID must be exactly 16 bytes, got {len(data)}")
        self._data = data

    @classmethod
    def from_hex(cls, hex_str: str) -> "UnGuid":
        """Create a GUID from a 32-char uppercase hex string.

        Args:
            hex_str (str): The hex representation, or empty for a zero GUID.

        Returns:
            UnGuid: The GUID parsed from the hex string.
        """
        if not hex_str:
            return cls()
        return cls(bytes.fromhex(hex_str))

    @classmethod
    def from_stream(cls, reader: BinaryIO) -> "UnGuid":
        """Read 16 bytes from a binary stream.

        Args:
            reader (BinaryIO): The stream to read the GUID bytes from.

        Returns:
            UnGuid: The GUID read from the stream.
        """
        return cls(reader.read(16))

    @classmethod
    def generate(cls) -> "UnGuid":
        """Generate a new random GUID.

        Returns:
            UnGuid: A newly generated random GUID.
        """
        return cls(uuid.uuid4().bytes)

    def to_hex(self) -> str:
        """Return the GUID as a 32-char uppercase hex string.

        Returns:
            str: The uppercase hex representation of the GUID.
        """
        return "".join(f"{b:02X}" for b in self._data)

    def to_bytes(self) -> bytes:
        """Return the raw 16-byte representation.

        Returns:
            bytes: The raw 16-byte GUID value.
        """
        return self._data

    def write(self, writer: BinaryIO) -> None:
        """Write 16 bytes to a binary stream.

        Args:
            writer (BinaryIO): The stream to write the GUID bytes to.
        """
        writer.write(self._data)

    def is_empty(self) -> bool:
        """Return True if the GUID is all zeros.

        Returns:
            bool: True if every byte of the GUID is zero.
        """
        return self._data == b"\x00" * 16

    def __eq__(self, other: object) -> bool:
        """Compare this GUID with another object for equality.

        Args:
            other (object): The object to compare against.

        Returns:
            bool: True if ``other`` is a GUID with identical bytes.
        """
        if isinstance(other, UnGuid):
            return self._data == other._data
        return NotImplemented

    def __repr__(self) -> str:
        """Return the debug representation of the GUID.

        Returns:
            str: The ``UnGuid(...)`` debug representation.
        """
        return f"UnGuid({self.to_hex()!r})"

    def __str__(self) -> str:
        """Return the GUID as a hex string.

        Returns:
            str: The uppercase hex representation of the GUID.
        """
        return self.to_hex()


# ===================================================================== #
#  Built-in name table
# ===================================================================== #


class UnNameMap(IntEnum):
    """Built-in name indices reserved by the engine at fixed positions.

    These are the hard-coded name table entries that the engine
    reserves at fixed indices.  The integer value is the name table index.
    Entries are ordered by index value.
    """

    # Special zero value, meaning no name.
    NONE = 0

    # Class property types (1–16).
    ByteProperty = 1
    IntProperty = 2
    BoolProperty = 3
    FloatProperty = 4
    ObjectProperty = 5
    NameProperty = 6
    DelegateProperty = 7
    ClassProperty = 8
    ArrayProperty = 9
    StructProperty = 10
    VectorProperty = 11
    RotatorProperty = 12
    StrProperty = 13
    MapProperty = 14
    FixedArrayProperty = 15
    PointerProperty = 16

    # UnrealScript types (80–95).
    Byte = 80
    Int = 81
    Bool = 82
    Float = 83
    Name = 84
    String = 85
    Struct = 86
    Vector = 87
    Rotator = 88
    Color = 90
    Plane = 91
    Button = 92
    CompressedPosition = 93
    Pointer = 94
    Quat = 95

    # Keywords (100–122).
    Begin = 100
    State = 102
    Function = 103
    Self = 104
    TRUE = 105
    FALSE = 106
    Transient = 107
    Enum = 117
    Replication = 119
    Reliable = 120
    Unreliable = 121
    Always = 122

    # Object class names (150–163).
    Field = 150
    Object = 151
    TextBuffer = 152
    Linker = 153
    LinkerLoad = 154
    LinkerSave = 155
    Subsystem = 156
    Factory = 157
    TextBufferFactory = 158
    Exporter = 159
    StackNode = 160
    Property = 161
    Camera = 162
    PlayerInput = 163

    # Constants (600–607).
    Vect = 600
    Rot = 601
    ArrayCount = 605
    EnumCount = 606
    Rng = 607

    # Flow control (620–635).
    Else = 620
    If = 621
    Goto = 622
    Stop = 623
    Until = 625
    While = 626
    Do = 627
    Break = 628
    For = 629
    ForEach = 630
    Assert = 631
    Switch = 632
    Case = 633
    Default = 634
    Continue = 635

    # Variable overrides (640–665).
    Private = 640
    Const = 641
    Out = 642
    Export = 643
    EdFindable = 644
    Skip = 646
    Coerce = 647
    Optional = 648
    Input = 649
    Config = 650
    Travel = 652
    EditConst = 653
    Localized = 654
    GlobalConfig = 655
    SafeReplace = 656
    New = 657
    Protected = 658
    Public = 659
    EditInline = 660
    EditInlineUse = 661
    Deprecated = 662
    EditConstArray = 663
    EditInlineNotify = 664
    Automated = 665

    # Class overrides (671–699).
    Intrinsic = 671
    Within = 672
    Abstract = 673
    Package = 674
    Guid = 675
    Parent = 676
    Class = 677
    Extends = 678
    NoExport = 679
    Placeable = 680
    PerObjectConfig = 681
    NativeReplication = 682
    NotPlaceable = 683
    EditInlineNew = 684
    NotEditInlineNew = 685
    HideCategories = 686
    ShowCategories = 687
    CollapseCategories = 688
    DontCollapseCategories = 689

    # State overrides (690–692).
    Auto = 690
    Ignores = 691
    Instanced = 692

    # Misc class overrides (693–699).
    HideDropDown = 693
    CacheExempt = 694

    # Calling overrides (695–697).
    Global = 695
    Super = 696
    Outer = 697

    ExportStructs = 698
    DependsOn = 699

    # Function overrides (700–715).
    Operator = 700
    PreOperator = 701
    PostOperator = 702
    Final = 703
    Iterator = 704
    Latent = 705
    Return = 706
    Singular = 707
    Simulated = 708
    Exec = 709
    Event = 710
    Static = 711
    Native = 712
    Invariant = 713
    Delegate = 714
    Interface = 715

    # Variable declaration (720–723).
    Var = 720
    Local = 721
    Import = 722
    From = 723

    # Special commands (730–732).
    Spawn = 730
    Array = 731
    Map = 732

    # Misc (740–745).
    Tag = 740
    Role = 742
    RemoteRole = 743
    System = 744
    User = 745

    # Log messages (760–794).
    Log = 760
    Critical = 761
    Init = 762
    Exit = 763
    Cmd = 764
    Play = 765
    Console = 766
    Warning = 767
    ExecWarning = 768
    ScriptWarning = 769
    ScriptLog = 770
    Dev = 771
    DevNet = 772
    DevPath = 773
    DevNetTraffic = 774
    DevAudio = 775
    DevLoad = 776
    DevSave = 777
    DevGarbage = 778
    DevKill = 779
    DevReplace = 780
    DevMusic = 781
    DevSound = 782
    DevCompile = 783
    DevBind = 784
    Localization = 785
    Compatibility = 786
    NetComeGo = 787
    Title = 788
    Error = 789
    Heading = 790
    SubHeading = 791
    FriendlyError = 792
    Progress = 793
    UserPrompt = 794

    # Misc (820–842).
    KeyType = 820
    KeyEvent = 821
    Write = 822
    Message = 823
    InitialState = 824
    Texture = 825
    Sound = 826
    FireTexture = 827
    IceTexture = 828
    WaterTexture = 829
    WaveTexture = 830
    WetTexture = 831
    Main = 832
    NotifyLevelChange = 833
    SendText = 835
    SendBinary = 836
    ConnectFailure = 837
    Length = 838
    Insert = 839
    Remove = 840
    ProceduralSound = 841
    SoundGroup = 842

    # Debug / misc (843–865).
    Debug = 843
    DebugRon = 844
    MenuText = 845
    MusicPlayer = 846
    VoiceChat = 847
    ModAuthor = 848
    ParseConfig = 849
    Cache = 850
    DevKarma = 863
    DevLIPSinc = 864
    NetSecurity = 865

    Long = 910

    RecordCache = 927


class UnObjectFlags(IntFlag):
    """Object flags used in Unreal packages."""

    Transactional = 0x00000001
    Unreachable = 0x00000002
    Public = 0x00000004
    TagImp = 0x00000008
    TagExp = 0x00000010
    SourceModified = 0x00000020
    TagGarbage = 0x00000040
    Private = 0x00000080
    Automated = 0x00000100
    NeedLoad = 0x00000200
    HighlightedName = 0x00000400
    InSingularFunc = 0x00000800
    Suppress = 0x00001000
    InEndState = 0x00002000
    Transient = 0x00004000
    PreLoading = 0x00008000
    LoadForClient = 0x00010000
    LoadForServer = 0x00020000
    LoadForEdit = 0x00040000
    Standalone = 0x00080000
    NotForClient = 0x00100000
    NotForServer = 0x00200000
    NotForEdit = 0x00400000
    Destroyed = 0x00800000
    NeedPostLoad = 0x01000000
    HasStack = 0x02000000
    Native = 0x04000000
    Marked = 0x08000000
    ErrorShutdown = 0x10000000
    DebugPostLoad = 0x20000000
    DebugSerialize = 0x40000000
    DebugDestroy = 0x80000000


class UnPackageFlags(IntFlag):
    """Package-level flags."""

    AllowDownload = 0x0001
    ClientOptional = 0x0002
    ServerSideOnly = 0x0004
    BrokenLinks = 0x0008
    Unsecure = 0x0010
    Official = 0x0020
    Need = 0x8000


# ===================================================================== #
#  Function flags
# ===================================================================== #


class UnFunctionFlags(IntFlag):
    """Function flags used in Unreal packages (EFunctionFlags)."""

    Final = 0x00000001
    Defined = 0x00000002
    Iterator = 0x00000004
    Latent = 0x00000008
    PreOperator = 0x00000010
    Singular = 0x00000020
    Net = 0x00000040
    NetReliable = 0x00000080
    Simulated = 0x00000100
    Exec = 0x00000200
    Native = 0x00000400
    Event = 0x00000800
    Operator = 0x00001000
    Static = 0x00002000
    NoExport = 0x00004000
    Const = 0x00008000
    Invariant = 0x00010000
    Public = 0x00020000
    Private = 0x00040000
    Protected = 0x00080000
    Delegate = 0x00100000
    NetServer = 0x00200000
    Interface = 0x00400000


# ===================================================================== #
#  State flags
# ===================================================================== #


class UnStateFlags(IntFlag):
    """State flags used in Unreal packages (EStateFlags)."""

    Editable = 0x00000001
    Auto = 0x00000002
    Simulated = 0x00000004


# ===================================================================== #
#  Class flags
# ===================================================================== #


class UnClassFlags(IntFlag):
    """Class flags used in Unreal packages (EClassFlags)."""

    Abstract = 0x00000001
    Compiled = 0x00000002
    Config = 0x00000004
    Transient = 0x00000008
    Parsed = 0x00000010
    Localized = 0x00000020
    SafeReplace = 0x00000040
    RuntimeStatic = 0x00000080
    NoExport = 0x00000100
    Placeable = 0x00000200
    PerObjectConfig = 0x00000400
    NativeReplication = 0x00000800
    EditInlineNew = 0x00001000
    CollapseCategories = 0x00002000
    ExportStructs = 0x00004000
    IsAUProperty = 0x00008000
    IsAUObjectProperty = 0x00010000
    IsAUBoolProperty = 0x00020000
    IsAUState = 0x00040000
    IsAUFunction = 0x00080000
    NeedsDefProps = 0x00100000
    AutoInstancedProps = 0x00200000
    HideDropDown = 0x00400000
    NoCacheExport = 0x00800000
    ParseConfig = 0x01000000
    Cacheable = 0x02000000


# ===================================================================== #
#  Struct flags
# ===================================================================== #


class UnPropertyFlags(IntFlag):
    """Property flags used in Unreal packages (CPF_*)."""

    Edit = 0x00000001
    Const = 0x00000002
    Input = 0x00000004
    ExportObject = 0x00000008
    OptionalParm = 0x00000010
    Net = 0x00000020
    EditConstArray = 0x00000040
    Parm = 0x00000080
    OutParm = 0x00000100
    SkipParm = 0x00000200
    ReturnParm = 0x00000400
    CoerceParm = 0x00000800
    Native = 0x00001000
    Transient = 0x00002000
    Config = 0x00004000
    Localized = 0x00008000
    Travel = 0x00010000
    EditConst = 0x00020000
    GlobalConfig = 0x00040000
    OnDemand = 0x00100000
    New = 0x00200000
    NeedCtorLink = 0x00400000
    NoExport = 0x00800000
    Button = 0x01000000
    CommentString = 0x02000000
    EditInline = 0x04000000
    EdFindable = 0x08000000
    EditInlineUse = 0x10000000
    Deprecated = 0x20000000
    EditInlineNotify = 0x40000000
    Automated = 0x80000000


class UnStructFlags(IntFlag):
    """Struct flags used in Unreal packages."""

    Native = 0x00000001
    Export = 0x00000002
    Long = 0x00000004
    Init = 0x00000008


# ===================================================================== #
#  String ↔ flag conversion helpers
# ===================================================================== #


def _flags_to_string(flags: int, flag_map: dict) -> str:
    """Convert an integer flag value to a ``|``-separated name string.

    Args:
        flags (int): The combined flag bits to convert.
        flag_map (dict): Mapping of flag names to their integer values.

    Returns:
        str: The ``|``-separated names of the set flags.

    Raises:
        ValueError: If ``flags`` contains bits not present in ``flag_map``.
    """
    known = 0
    parts = []
    for name, val in flag_map.items():
        if flags & val:
            parts.append(name)
            known |= val
    unknown = flags & ~known
    if unknown:
        raise ValueError(f'Unknown package flags: 0x"{unknown}".')
    return "|".join(parts)


def _string_to_flags(s: str, flag_map: dict, cls: type, label: str) -> int:
    """Parse a ``|``-separated flag string into a flag value.

    Args:
        s (str): The ``|``-separated flag names, optionally with hex tokens.
        flag_map (dict): Mapping of flag names to their integer values.
        cls (type): The flag class used to build the returned value.
        label (str): Human-readable label used in error messages.

    Returns:
        int: The combined flag value parsed from the string.

    Raises:
        ValueError: If a part is neither a known name nor a hex literal.
    """
    if not s:
        return cls(0)
    flags = cls(0)
    for part in s.split("|"):
        part = part.strip()
        if not part:
            continue
        if part in flag_map:
            flags |= flag_map[part]
        elif part.startswith("0x") or part.startswith("0X"):
            flags |= int(part, 16)
        else:
            raise ValueError(f'Unknown {label} flag "{part}".')
    return flags


_PACKAGE_FLAG_MAP = {
    "AllowDownload": UnPackageFlags.AllowDownload,
    "ClientOptional": UnPackageFlags.ClientOptional,
    "ServerSideOnly": UnPackageFlags.ServerSideOnly,
    "BrokenLinks": UnPackageFlags.BrokenLinks,
    "Unsecure": UnPackageFlags.Unsecure,
    "Official": UnPackageFlags.Official,
    "Need": UnPackageFlags.Need,
}

_OBJECT_FLAG_MAP = {
    "Automated": UnObjectFlags.Automated,
    "DebugDestroy": UnObjectFlags.DebugDestroy,
    "DebugPostLoad": UnObjectFlags.DebugPostLoad,
    "DebugSerialize": UnObjectFlags.DebugSerialize,
    "Destroyed": UnObjectFlags.Destroyed,
    "ErrorShutdown": UnObjectFlags.ErrorShutdown,
    "HasStack": UnObjectFlags.HasStack,
    "HighlightedName": UnObjectFlags.HighlightedName,
    "InEndState": UnObjectFlags.InEndState,
    "InSingularFunc": UnObjectFlags.InSingularFunc,
    "LoadForClient": UnObjectFlags.LoadForClient,
    "LoadForEdit": UnObjectFlags.LoadForEdit,
    "LoadForServer": UnObjectFlags.LoadForServer,
    "Marked": UnObjectFlags.Marked,
    "Native": UnObjectFlags.Native,
    "NeedLoad": UnObjectFlags.NeedLoad,
    "NeedPostLoad": UnObjectFlags.NeedPostLoad,
    "NotForClient": UnObjectFlags.NotForClient,
    "NotForEdit": UnObjectFlags.NotForEdit,
    "NotForServer": UnObjectFlags.NotForServer,
    "PreLoading": UnObjectFlags.PreLoading,
    "Private": UnObjectFlags.Private,
    "Public": UnObjectFlags.Public,
    "SourceModified": UnObjectFlags.SourceModified,
    "Standalone": UnObjectFlags.Standalone,
    "Suppress": UnObjectFlags.Suppress,
    "TagExp": UnObjectFlags.TagExp,
    "TagGarbage": UnObjectFlags.TagGarbage,
    "TagImp": UnObjectFlags.TagImp,
    "Transactional": UnObjectFlags.Transactional,
    "Transient": UnObjectFlags.Transient,
    "Unreachable": UnObjectFlags.Unreachable,
}


_FUNCTION_FLAG_MAP = {m.name: m for m in UnFunctionFlags}

_STATE_FLAG_MAP = {m.name: m for m in UnStateFlags}

_PROPERTY_FLAG_MAP = {m.name: m for m in UnPropertyFlags}

_STRUCT_FLAG_MAP = {m.name: m for m in UnStructFlags}

_CLASS_FLAG_MAP = {m.name: m for m in UnClassFlags}


def package_flags_to_string(flags: "UnPackageFlags") -> str:
    """Convert package flags to a ``|``-separated name string.

    Args:
        flags (UnPackageFlags): The package flags to convert.

    Returns:
        str: The ``|``-separated names of the set flags.
    """
    return _flags_to_string(flags, _PACKAGE_FLAG_MAP)


def string_to_package_flags(s: str) -> "UnPackageFlags":
    """Parse a string into package flags.

    Args:
        s (str): The ``|``-separated package flag names.

    Returns:
        UnPackageFlags: The parsed package flags.
    """
    return _string_to_flags(s, _PACKAGE_FLAG_MAP, UnPackageFlags, "package")


def object_flags_to_string(flags: "UnObjectFlags") -> str:
    """Convert object flags to a ``|``-separated name string.

    Args:
        flags (UnObjectFlags): The object flags to convert.

    Returns:
        str: The ``|``-separated names of the set flags.
    """
    return _flags_to_string(flags, _OBJECT_FLAG_MAP)


def string_to_object_flags(s: str) -> "UnObjectFlags":
    """Parse a string into object flags.

    Args:
        s (str): The ``|``-separated object flag names.

    Returns:
        UnObjectFlags: The parsed object flags.
    """
    return _string_to_flags(s, _OBJECT_FLAG_MAP, UnObjectFlags, "object")


def function_flags_to_string(flags: "UnFunctionFlags") -> str:
    """Convert function flags to a ``|``-separated name string.

    Args:
        flags (UnFunctionFlags): The function flags to convert.

    Returns:
        str: The ``|``-separated names of the set flags.
    """
    return _flags_to_string(flags, _FUNCTION_FLAG_MAP)


def string_to_function_flags(s: str) -> "UnFunctionFlags":
    """Parse a string into function flags.

    Args:
        s (str): The ``|``-separated function flag names.

    Returns:
        UnFunctionFlags: The parsed function flags.
    """
    return _string_to_flags(s, _FUNCTION_FLAG_MAP, UnFunctionFlags, "function")


def struct_flags_to_string(flags: "UnStructFlags") -> str:
    """Convert struct flags to a ``|``-separated name string.

    Args:
        flags (UnStructFlags): The struct flags to convert.

    Returns:
        str: The ``|``-separated names of the set flags.
    """
    return _flags_to_string(flags, _STRUCT_FLAG_MAP)


def string_to_struct_flags(s: str) -> "UnStructFlags":
    """Parse a string into struct flags.

    Args:
        s (str): The ``|``-separated struct flag names.

    Returns:
        UnStructFlags: The parsed struct flags.
    """
    return _string_to_flags(s, _STRUCT_FLAG_MAP, UnStructFlags, "struct")


def state_flags_to_string(flags: "UnStateFlags") -> str:
    """Convert state flags to a ``|``-separated name string.

    Args:
        flags (UnStateFlags): The state flags to convert.

    Returns:
        str: The ``|``-separated names of the set flags.
    """
    return _flags_to_string(flags, _STATE_FLAG_MAP)


def string_to_state_flags(s: str) -> "UnStateFlags":
    """Parse a string into state flags.

    Args:
        s (str): The ``|``-separated state flag names.

    Returns:
        UnStateFlags: The parsed state flags.
    """
    return _string_to_flags(s, _STATE_FLAG_MAP, UnStateFlags, "state")


def property_flags_to_string(flags: "UnPropertyFlags") -> str:
    """Convert property flags to a ``|``-separated name string.

    Args:
        flags (UnPropertyFlags): The property flags to convert.

    Returns:
        str: The ``|``-separated names of the set flags.
    """
    return _flags_to_string(flags, _PROPERTY_FLAG_MAP)


def string_to_property_flags(s: str) -> "UnPropertyFlags":
    """Parse a string into property flags.

    Args:
        s (str): The ``|``-separated property flag names.

    Returns:
        UnPropertyFlags: The parsed property flags.
    """
    return _string_to_flags(s, _PROPERTY_FLAG_MAP, UnPropertyFlags, "property")


def class_flags_to_string(flags: "UnClassFlags") -> str:
    """Convert class flags to a ``|``-separated name string.

    Args:
        flags (UnClassFlags): The class flags to convert.

    Returns:
        str: The ``|``-separated names of the set flags.
    """
    return _flags_to_string(flags, _CLASS_FLAG_MAP)


def string_to_class_flags(s: str) -> "UnClassFlags":
    """Parse a string into class flags.

    Args:
        s (str): The ``|``-separated class flag names.

    Returns:
        UnClassFlags: The parsed class flags.
    """
    return _string_to_flags(s, _CLASS_FLAG_MAP, UnClassFlags, "class")


# ===================================================================== #
#  Probe mask names (bit 0 = name index 300, … bit 63 = name index 363)
# ===================================================================== #

# Ordered list: index in this list == bit position in the 64-bit mask.
# Unused slots are named Probe<bit>.
PROBE_NAMES: list = [
    # 300-309
    "Probe0",  # 300 (unused)
    "Destroyed",  # 301
    "GainedChild",  # 302
    "LostChild",  # 303
    "Created",  # 304
    "Probe5",  # 305 (unused)
    "Trigger",  # 306
    "UnTrigger",  # 307
    "Timer",  # 308
    "HitWall",  # 309
    # 310-319
    "Falling",  # 310
    "Landed",  # 311
    "PhysicsVolumeChange",  # 312
    "Touch",  # 313
    "UnTouch",  # 314
    "Bump",  # 315
    "BeginState",  # 316
    "EndState",  # 317
    "BaseChange",  # 318
    "Attach",  # 319
    # 320-329
    "Detach",  # 320
    "ActorEntered",  # 321
    "ActorLeaving",  # 322
    "ZoneChange",  # 323
    "AnimEnd",  # 324
    "EndedRotation",  # 325
    "InterpolateEnd",  # 326
    "EncroachingOn",  # 327
    "EncroachedBy",  # 328
    "NotifyTurningInPlace",  # 329
    # 330-339
    "HeadVolumeChange",  # 330
    "PostTouch",  # 331
    "PawnEnteredVolume",  # 332
    "MayFall",  # 333
    "CheckDirectionChange",  # 334
    "PawnLeavingVolume",  # 335
    "Tick",  # 336
    "PlayerTick",  # 337
    "ModifyVelocity",  # 338
    "CheckMovementTransition",  # 339
    # 340-349
    "SeePlayer",  # 340
    "EnemyNotVisible",  # 341
    "HearNoise",  # 342
    "UpdateEyeHeight",  # 343
    "SeeMonster",  # 344
    "SeeFriend",  # 345
    "SpecialHandling",  # 346
    "BotDesireability",  # 347
    "NotifyBump",  # 348
    "NotifyPhysicsVolumeChange",  # 349
    # 350-359
    "AIHearSound",  # 350
    "NotifyHeadVolumeChange",  # 351
    "NotifyLanded",  # 352
    "NotifyHitWall",  # 353
    "PostNetReceive",  # 354
    "PreBeginPlay",  # 355
    "BeginPlay",  # 356
    "PostBeginPlay",  # 357
    "Probe58",  # 358 (unused)
    "PhysicsChangedFor",  # 359
    # 360-363
    "ActorEnteredVolume",  # 360
    "ActorLeavingVolume",  # 361
    "PrepareForMove",  # 362
    "All",  # 363
]

# Reverse lookup: name → bit position
_PROBE_NAME_TO_BIT: dict = {name: bit for bit, name in enumerate(PROBE_NAMES)}


def probe_mask_to_string(mask: int) -> str:
    """Convert a 64-bit unsigned probe mask to a ``|``-separated name string.

    Set bits correspond to enabled probes. Returns ``""`` when no bits
    are set.

    Args:
        mask (int): The 64-bit unsigned probe mask.

    Returns:
        str: The ``|``-separated names of the enabled probes.
    """
    if mask == 0:
        return ""
    parts: list = []
    for bit in range(64):
        if mask & (1 << bit):
            if bit < len(PROBE_NAMES):
                parts.append(PROBE_NAMES[bit])
            else:
                parts.append(f"Probe{bit}")
    return "|".join(parts)


def string_to_probe_mask(s: str) -> int:
    """Convert a ``|``-separated probe name string back to a 64-bit mask.

    Args:
        s (str): The ``|``-separated probe names.

    Returns:
        int: The 64-bit unsigned probe mask.
    """
    if not s or not s.strip():
        return 0
    mask = 0
    for name in s.split("|"):
        name = name.strip()
        if not name:
            continue
        bit = _PROBE_NAME_TO_BIT.get(name)
        if bit is not None:
            mask |= 1 << bit
        elif name.startswith("Probe") and name[5:].isdigit():
            mask |= 1 << int(name[5:])
    return mask


def ignore_mask_to_string(mask: int) -> str:
    """Convert a 64-bit unsigned ignore mask to a ``|``-separated name string.

    The stored value has bits **set** for probes that are *not* ignored.
    This function returns the names of the *ignored* probes (clear bits).
    An all-ones mask (0xFFFFFFFFFFFFFFFF, i.e. nothing ignored) returns ``""``.

    Args:
        mask (int): The 64-bit unsigned ignore mask.

    Returns:
        str: The ``|``-separated names of the ignored probes.
    """
    # Treat as unsigned 64-bit
    mask &= 0xFFFFFFFFFFFFFFFF
    if mask == 0xFFFFFFFFFFFFFFFF:
        return ""
    inverted = ~mask & 0xFFFFFFFFFFFFFFFF
    return probe_mask_to_string(inverted)


def string_to_ignore_mask(s: str) -> int:
    """Convert a ``|``-separated ignored-probe name string back to a mask.

    The result has bits **set** for probes that are *not* ignored.
    An empty string means nothing is ignored, giving all bits set
    (0xFFFFFFFFFFFFFFFF).

    Args:
        s (str): The ``|``-separated names of the ignored probes.

    Returns:
        int: The 64-bit unsigned mask with non-ignored probe bits set.
    """
    if not s or not s.strip():
        return 0xFFFFFFFFFFFFFFFF
    ignored_bits = string_to_probe_mask(s)
    return ~ignored_bits & 0xFFFFFFFFFFFFFFFF
