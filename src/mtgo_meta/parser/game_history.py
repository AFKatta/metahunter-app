"""Parser for MTGO's ``mtgo_game_history`` file.

This file is a .NET BinaryFormatter (MS-NRBF) serialization of a
``List<HistoricalMatch>``. We use it for one purpose: figuring out
the actual MTGO format each match was played in (Legacy / Vintage /
Modern / …), since the Match_GameLog files themselves don't store that.

We don't implement a full NRBF reader. We just walk every record in
the stream, capture string objects by ObjectId, and watch for
HistoricalMatch class instances — pulling out their ``Id`` (int) and
``TournamentStructureCode`` (string) fields.

References
----------
* [MS-NRBF] .NET Remoting: Binary Format Data Structure
  https://docs.microsoft.com/en-us/openspecs/windows_protocols/ms-nrbf/
"""

from __future__ import annotations

import struct
from pathlib import Path

# ---------------------------------------------------------------------------
# NRBF record types
# ---------------------------------------------------------------------------
_HEADER = 0
_CLASS_WITH_ID = 1
_SYSTEM_CLASS_WITH_MEMBERS = 2
_CLASS_WITH_MEMBERS = 3
_SYSTEM_CLASS_WITH_MEMBERS_AND_TYPES = 4
_CLASS_WITH_MEMBERS_AND_TYPES = 5
_BINARY_OBJECT_STRING = 6
_BINARY_ARRAY = 7
_MEMBER_PRIMITIVE_TYPED = 8
_MEMBER_REFERENCE = 9
_OBJECT_NULL = 10
_MESSAGE_END = 11
_BINARY_LIBRARY = 12
_OBJECT_NULL_MULTIPLE_256 = 13
_OBJECT_NULL_MULTIPLE = 14
_ARRAY_SINGLE_PRIMITIVE = 15
_ARRAY_SINGLE_OBJECT = 16
_ARRAY_SINGLE_STRING = 17

# BinaryTypeEnum (used inside ClassWithMembersAndTypes' MemberTypeInfo)
_BT_PRIMITIVE = 0
_BT_STRING = 1
_BT_OBJECT = 2
_BT_SYSTEM_CLASS = 3
_BT_CLASS = 4
_BT_OBJECT_ARRAY = 5
_BT_STRING_ARRAY = 6
_BT_PRIMITIVE_ARRAY = 7

# PrimitiveTypeEnum: fixed-size types we know how to skip.
_PRIMITIVE_SIZES = {
    1: 1,   # Boolean
    2: 1,   # Byte
    3: 1,   # Char (treat as 1 byte; MTGO data is ASCII)
    6: 8,   # Double
    7: 2,   # Int16
    8: 4,   # Int32
    9: 8,   # Int64
    10: 1,  # SByte
    11: 4,  # Single
    12: 8,  # TimeSpan
    13: 8,  # DateTime
    14: 2,  # UInt16
    15: 4,  # UInt32
    16: 8,  # UInt64
}


class _Bail(Exception):
    """Raised when we hit something the parser can't safely decode."""


class _ClassInfo:
    """Captured class layout for use by ClassWithId instances later on."""

    __slots__ = ("name", "member_names", "binary_types", "primitive_types")

    def __init__(
        self,
        name: str,
        member_names: list[str],
        binary_types: list[int],
        primitive_types: dict[int, int],
    ) -> None:
        self.name = name
        self.member_names = member_names
        self.binary_types = binary_types
        self.primitive_types = primitive_types  # idx -> PrimitiveTypeEnum


class _Parser:
    """Sequential NRBF parser that pulls HistoricalMatch summary records.

    For each match we capture the Description text (which contains the
    MTGO format keyword) and the StartTime (Unix seconds). We correlate
    these with Match_GameLog files via their mtime — same match-start
    time on either side of MTGO's storage.
    """

    def __init__(self, buf: bytes) -> None:
        self.buf = buf
        self.pos = 0
        self.strings: dict[int, str] = {}
        self.classes: dict[int, _ClassInfo] = {}
        # List of (start_time_unix, format_name) tuples — one per
        # HistoricalMatch with a successful Description extraction.
        # Sorted by start_time for binary-search correlation later.
        self.matches: list[tuple[float, str]] = []
        # In-flight per-match data, finalised on instance-end.
        self._cur_match_id: int | None = None
        self._cur_desc_value: str | None = None
        self._cur_desc_ref: int | None = None
        self._cur_start: float | None = None
        # Forward-resolved on parse completion.
        self._pending: list[tuple[float | None, int]] = []  # (start, desc_ref)

    # ----- low-level cursor helpers ---------------------------------------
    def _i8(self) -> int:
        v = self.buf[self.pos]
        self.pos += 1
        return v

    def _i32(self) -> int:
        v = struct.unpack_from("<i", self.buf, self.pos)[0]
        self.pos += 4
        return v

    def _i16(self) -> int:
        v = struct.unpack_from("<h", self.buf, self.pos)[0]
        self.pos += 2
        return v

    def _i64(self) -> int:
        v = struct.unpack_from("<q", self.buf, self.pos)[0]
        self.pos += 8
        return v

    def _f32(self) -> float:
        v = struct.unpack_from("<f", self.buf, self.pos)[0]
        self.pos += 4
        return v

    def _f64(self) -> float:
        v = struct.unpack_from("<d", self.buf, self.pos)[0]
        self.pos += 8
        return v

    def _read(self, n: int) -> bytes:
        b = self.buf[self.pos:self.pos + n]
        self.pos += n
        return b

    def _varlen_string(self) -> str:
        length = 0
        shift = 0
        while True:
            b = self.buf[self.pos]
            self.pos += 1
            length |= (b & 0x7F) << shift
            if (b & 0x80) == 0:
                break
            shift += 7
        s = self.buf[self.pos:self.pos + length].decode("utf-8", errors="replace")
        self.pos += length
        return s

    # ----- entry point ----------------------------------------------------
    def parse(self) -> None:
        while self.pos < len(self.buf):
            try:
                rt = self._i8()
            except IndexError:
                break
            if rt == _MESSAGE_END:
                break
            self._consume_record(rt)
        # Forward-resolve descriptions that were referenced before their
        # BinaryObjectString record actually appeared in the stream.
        for start, ref in self._pending:
            s = self.strings.get(ref)
            if s is not None:
                self.matches.append((start, s))
        self.matches.sort(key=lambda x: x[0])

    # ----- record dispatch ------------------------------------------------
    # Each handler is responsible for consuming the ENTIRE serialized
    # representation of its record, including nested content (array
    # elements, class member values, …). Calls into _consume_record for
    # nested records.
    def _consume_record(self, rt: int) -> object:
        if rt == _HEADER:
            self._read(16)  # RootId, HeaderId, MajorVersion, MinorVersion
            return None
        if rt == _BINARY_LIBRARY:
            self._i32()
            self._varlen_string()
            return None
        if rt == _BINARY_OBJECT_STRING:
            oid = self._i32()
            s = self._varlen_string()
            self.strings[oid] = s
            return ("string", oid, s)
        if rt == _CLASS_WITH_MEMBERS_AND_TYPES:
            return self._read_class_definition(system=False)
        if rt == _SYSTEM_CLASS_WITH_MEMBERS_AND_TYPES:
            return self._read_class_definition(system=True)
        if rt == _CLASS_WITH_ID:
            obj_id = self._i32()
            metadata_id = self._i32()
            cls = self.classes.get(metadata_id)
            if cls is None:
                raise _Bail()
            return self._read_class_instance(obj_id, cls)
        if rt == _MEMBER_REFERENCE:
            return ("ref", self._i32())
        if rt == _OBJECT_NULL:
            return ("null", None)
        if rt == _OBJECT_NULL_MULTIPLE_256:
            return ("nulls", self._i8())
        if rt == _OBJECT_NULL_MULTIPLE:
            return ("nulls", self._i32())
        if rt == _MEMBER_PRIMITIVE_TYPED:
            pt = self._i8()
            return ("prim", self._read_primitive(pt))
        if rt == _ARRAY_SINGLE_PRIMITIVE:
            self._i32()              # ObjectId
            length = self._i32()
            pt = self._i8()
            size = _PRIMITIVE_SIZES.get(pt)
            if size is None:
                raise _Bail()
            self._read(size * length)
            return None
        if rt == _ARRAY_SINGLE_STRING:
            self._i32()
            length = self._i32()
            self._read_n_inline_objects(length)
            return None
        if rt == _ARRAY_SINGLE_OBJECT:
            self._i32()
            length = self._i32()
            self._read_n_inline_objects(length)
            return None
        if rt == _BINARY_ARRAY:
            return self._read_binary_array()
        if rt in (_CLASS_WITH_MEMBERS, _SYSTEM_CLASS_WITH_MEMBERS):
            # Untyped variants — can't decode safely.
            raise _Bail()
        raise _Bail()

    def _read_n_inline_objects(self, n: int) -> None:
        consumed = 0
        while consumed < n:
            rt = self._i8()
            res = self._consume_record(rt)
            if isinstance(res, tuple) and res[0] == "nulls":
                consumed += res[1]
            else:
                consumed += 1

    def _read_binary_array(self) -> None:
        self._i32()  # ObjectId
        binary_array_type = self._i8()
        rank = self._i32()
        lengths = [self._i32() for _ in range(rank)]
        if binary_array_type in (3, 4, 5):  # SingleOffset / Jagged / Rectangular
            for _ in range(rank):
                self._i32()  # LowerBounds
        # MemberTypeInfo: one BinaryTypeEnum + optional AdditionalInfo
        elem_bt = self._i8()
        elem_pt = None
        if elem_bt == _BT_PRIMITIVE or elem_bt == _BT_PRIMITIVE_ARRAY:
            elem_pt = self._i8()
        elif elem_bt == _BT_SYSTEM_CLASS:
            self._varlen_string()
        elif elem_bt == _BT_CLASS:
            self._varlen_string()
            self._i32()
        total = 1
        for L in lengths:
            total *= L
        if elem_bt == _BT_PRIMITIVE and elem_pt is not None:
            size = _PRIMITIVE_SIZES.get(elem_pt)
            if size is None:
                raise _Bail()
            self._read(size * total)
        else:
            self._read_n_inline_objects(total)
        return None

    # ----- class definition + instance ------------------------------------
    def _read_class_definition(self, *, system: bool) -> object:
        obj_id = self._i32()
        name = self._varlen_string()
        member_count = self._i32()
        member_names = [self._varlen_string() for _ in range(member_count)]
        binary_types = [self._i8() for _ in range(member_count)]
        primitive_types: dict[int, int] = {}
        for i, bt in enumerate(binary_types):
            if bt == _BT_PRIMITIVE or bt == _BT_PRIMITIVE_ARRAY:
                primitive_types[i] = self._i8()
            elif bt == _BT_SYSTEM_CLASS:
                self._varlen_string()
            elif bt == _BT_CLASS:
                self._varlen_string()
                self._i32()
        if not system:
            self._i32()  # LibraryId
        cls = _ClassInfo(name, member_names, binary_types, primitive_types)
        self.classes[obj_id] = cls
        # Definitions are immediately followed by the first instance's values.
        return self._read_class_instance(obj_id, cls)

    def _read_class_instance(self, obj_id: int, cls: _ClassInfo) -> object:
        is_match = cls.name.endswith(".HistoricalMatch")
        desc_value: str | None = None
        desc_ref: int | None = None
        start_time: float | None = None
        for i, bt in enumerate(cls.binary_types):
            name = cls.member_names[i]
            if bt == _BT_PRIMITIVE:
                pt = cls.primitive_types.get(i, 0)
                # StartTime is DateTime (pt=13). Decode bits-0..61 as
                # ticks-since-AD-0001 → Unix seconds.
                if is_match and name == "StartTime" and pt == 13:
                    raw = struct.unpack_from("<q", self.buf, self.pos)[0]
                    self.pos += 8
                    ticks = raw & 0x3FFFFFFFFFFFFFFF
                    # .NET ticks per second = 1e7. Epoch offset to 1970:
                    # 0001-01-01 -> 1970-01-01 in ticks = 621355968000000000.
                    start_time = (ticks - 621355968000000000) / 1e7
                else:
                    self._read_primitive(pt)
            else:
                rt = self._i8()
                res = self._consume_record(rt)
                if is_match and name == "Description":
                    if isinstance(res, tuple):
                        if res[0] == "string":
                            desc_value = res[2]
                        elif res[0] == "ref":
                            desc_ref = res[1]
        if is_match and start_time is not None:
            if desc_value is not None:
                self.matches.append((start_time, desc_value))
            elif desc_ref is not None:
                self._pending.append((start_time, desc_ref))
        return ("inst", obj_id)

    # ----- primitive readers ---------------------------------------------
    def _read_primitive(self, pt: int) -> object:
        if pt == 1:
            return bool(self._i8())
        if pt == 2 or pt == 10:
            return self._i8()
        if pt == 7:
            return self._i16()
        if pt == 8:
            return self._i32()
        if pt == 9:
            return self._i64()
        if pt == 6:
            return self._f64()
        if pt == 11:
            return self._f32()
        if pt == 13 or pt == 12:
            self._read(8)
            return None
        if pt == 14:
            v = struct.unpack_from("<H", self.buf, self.pos)[0]
            self.pos += 2
            return v
        if pt == 15:
            v = struct.unpack_from("<I", self.buf, self.pos)[0]
            self.pos += 4
            return v
        if pt == 16:
            v = struct.unpack_from("<Q", self.buf, self.pos)[0]
            self.pos += 8
            return v
        if pt == 18:
            return self._varlen_string()
        raise _Bail()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

# Format keywords searched inside HistoricalMatch.Description, which is
# free-form text like "Play up to 5 rounds with your Legacy deck — on
# your schedule!" or "Vintage Challenge 32". Order matters — Premodern
# must match before "Modern" so we don't tag Premodern as Modern.
_FORMAT_KEYWORDS = (
    ("Premodern",  "Premodern"),
    ("Pre-modern", "Premodern"),
    ("Pre-Modern", "Premodern"),
    ("Vintage",    "Vintage"),
    ("Legacy",     "Legacy"),
    ("Modern",     "Modern"),
    ("Pauper",     "Pauper"),
    ("Pioneer",    "Pioneer"),
    ("Standard",   "Standard"),
    ("Commander",  "Commander"),
    ("Cube",       "Cube"),
)


def format_from_code(description: str) -> str:
    """Translate a HistoricalMatch.Description into a friendly format name.

    The Description is the league/event blurb MTGO shows in its UI —
    it always contains the format keyword. We search for keywords in a
    deterministic order so "Premodern" doesn't get tagged "Modern".
    Returns ``"Unknown"`` if no keyword matches; callers should fall
    back to card-based detection in that case.
    """
    if not description:
        return "Unknown"
    for kw, label in _FORMAT_KEYWORDS:
        if kw in description:
            return label
    return "Unknown"


def parse_history(path: Path) -> list[tuple[float, str]]:
    """Return ``[(start_time_unix, format_name), ...]`` sorted ascending.

    Each entry comes from one HistoricalMatch record in
    ``mtgo_game_history``. The caller correlates with Match_GameLog
    files via the log's mtime (which is approximately the match's
    start time). Returns an empty list on parse failure.
    """
    try:
        buf = path.read_bytes()
    except OSError:
        return []
    parser = _Parser(buf)
    try:
        parser.parse()
    except _Bail:
        for start, ref in parser._pending:
            s = parser.strings.get(ref)
            if s is not None:
                parser.matches.append((start, s))
        parser.matches.sort(key=lambda x: x[0])
    return [(start, format_from_code(desc)) for start, desc in parser.matches]


def build_format_index(
    history_paths: list[Path],
) -> list[tuple[float, str]]:
    """Merge all history files into one ascending-by-time list.

    Multiple AppFiles folders may each carry their own mtgo_game_history.
    We concatenate them and sort, so a single binary-search-style lookup
    by match mtime covers the user's full history.
    """
    merged: list[tuple[float, str]] = []
    for p in history_paths:
        merged.extend(parse_history(p))
    merged.sort(key=lambda x: x[0])
    return merged


def format_for_mtime(
    index: list[tuple[float, str]],
    mtime: float,
    tolerance_seconds: float = 7200,
) -> str | None:
    """Find the format whose StartTime is closest to ``mtime``.

    Returns ``None`` if no HistoricalMatch is within ``tolerance_seconds``
    (default 2 hours — generous because MTGO writes the log file after
    the match ends, which can be 30-60 minutes after StartTime for
    long league matches).
    """
    if not index:
        return None
    # Binary search for closest start_time.
    import bisect
    starts = [s for s, _ in index]
    i = bisect.bisect_left(starts, mtime)
    candidates: list[tuple[float, str]] = []
    if i < len(index):
        candidates.append(index[i])
    if i > 0:
        candidates.append(index[i - 1])
    best: tuple[float, str] | None = None
    best_delta = float("inf")
    for start, fmt in candidates:
        delta = abs(start - mtime)
        if delta < best_delta:
            best_delta = delta
            best = (start, fmt)
    if best is None or best_delta > tolerance_seconds:
        return None
    return best[1]
