"""
SimData (DATA, type 0x545AC67A) reader and writer.

SimData is the binary copy of tuning that the game's UI reads. Any tuning
field EA marks as "exported to client" is looked up here, not in the XML, so
a career with no SimData loads fine in Python but the Find a Job picker,
career panel and so on have nothing to show for it.

Layout (all offsets marked "rel" are signed and relative to the position of
the offset field itself; 0x80000000 means null):

    header      'DATA', u32 version (0x101), rel table-info, u32 table count,
                rel schema table, u32 schema count, 8 reserved bytes
    table info  28 bytes each: rel name, u32 name hash, rel schema,
                u32 data type, u32 row size, rel rows, u32 row count
    rows        each table's rows back to back, 16-byte aligned
    schemas     24 bytes each: rel name, u32 name hash, u32 schema hash,
                u32 row size, rel field table (relative to +16), u32 fields
    fields      20 bytes each, sorted by name hash: rel name, u32 name hash,
                u32 data type, u32 offset in row, rel unused (null)
    names       NUL-terminated ASCII, each distinct string once: each
                schema's field names in offset order then the schema name,
                then every table name

Sims4Tools' s4pi DataResource only ever parses this format; its writer is
dead code. The layout above was confirmed by rebuilding every SimData
resource in the game's SimulationDeltaBuild0 package byte for byte (see
tools/check_simdata.py). Names hash as FNV-32 of the lowercased name.

Table placement is the one thing we do not copy from EA: EA's padding
between tables follows no rule we could recover, and the game follows the
stored offsets anyway. We align every table to 16 bytes, which every table
in every EA file also satisfies.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from enum import IntEnum

from .ids import fnv32

MAGIC = b"DATA"
VERSION = 0x101
NULL = 0x80000000
HEADER_SIZE = 32
TABLE_INFO_SIZE = 28
SCHEMA_SIZE = 24
FIELD_SIZE = 20
TABLE_ALIGN = 16


class DataType(IntEnum):
    BOOL = 0
    CHAR8 = 1
    INT8 = 2
    UINT8 = 3
    INT16 = 4
    UINT16 = 5
    INT32 = 6
    UINT32 = 7
    INT64 = 8
    UINT64 = 9
    FLOAT = 10
    STRING8 = 11
    HASHEDSTRING8 = 12
    OBJECT = 13
    VECTOR = 14
    FLOAT2 = 15
    FLOAT3 = 16
    FLOAT4 = 17
    TABLESETREFERENCE = 18
    RESOURCEKEY = 19
    LOCKEY = 20
    VARIANT = 21
    UNDEFINED = 22


# Field types whose first four bytes are a relative offset to other data.
POINTER_TYPES = {
    DataType.STRING8, DataType.HASHEDSTRING8, DataType.OBJECT,
    DataType.VECTOR, DataType.VARIANT,
}

# struct formats for the scalar types; pointer types are handled separately.
SCALAR_FORMAT = {
    DataType.BOOL: "<?", DataType.CHAR8: "<B", DataType.INT8: "<b",
    DataType.UINT8: "<B", DataType.INT16: "<h", DataType.UINT16: "<H",
    DataType.INT32: "<i", DataType.UINT32: "<I", DataType.INT64: "<q",
    DataType.UINT64: "<Q", DataType.FLOAT: "<f",
    DataType.TABLESETREFERENCE: "<Q", DataType.LOCKEY: "<I",
}


class SimDataError(Exception):
    """Raised when a SimData resource cannot be parsed or built."""


@dataclass
class Field:
    name: str
    type: DataType
    offset: int

    @property
    def name_hash(self) -> int:
        return fnv32(self.name)


@dataclass
class Schema:
    name: str
    schema_hash: int
    size: int
    fields: list[Field]

    def field(self, name: str) -> Field:
        for f in self.fields:
            if f.name == name:
                return f
        raise SimDataError(f"schema {self.name} has no field {name!r}")


@dataclass(frozen=True)
class Ref:
    """Points at a row (or, for raw CHAR8 tables, a byte) of another table."""
    table: "Table"
    row: int = 0


@dataclass(eq=False)
class Table:
    """
    One table. Rows are stored as raw bytes plus the pointer fields inside
    them, keyed by byte offset within the row, so a table can be moved
    without touching anything but the pointers.
    """
    name: str
    schema: Schema | None
    data_type: DataType
    row_size: int
    rows: list[bytearray] = field(default_factory=list)
    pointers: list[dict[int, Ref | None]] = field(default_factory=list)

    def add_row(self, data: bytes | bytearray,
                pointers: dict[int, Ref | None] | None = None) -> Ref:
        if len(data) != self.row_size:
            raise SimDataError(
                f"row for table {self.name or '<unnamed>'} is {len(data)} "
                f"bytes, expected {self.row_size}"
            )
        self.rows.append(bytearray(data))
        self.pointers.append(dict(pointers or {}))
        return Ref(self, len(self.rows) - 1)


@dataclass
class SimData:
    tables: list[Table]
    version: int = VERSION
    # Order of the schema table. Defaults to first use by `tables`.
    schema_order: list[Schema] | None = None

    @property
    def schemas(self) -> list[Schema]:
        if self.schema_order is not None:
            return self.schema_order
        seen: list[Schema] = []
        for t in self.tables:
            if t.schema is not None and not any(t.schema is s for s in seen):
                seen.append(t.schema)
        return seen


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

def _rel(buf: bytes, pos: int) -> int | None:
    raw = struct.unpack_from("<I", buf, pos)[0]
    if raw == NULL:
        return None
    return pos + struct.unpack_from("<i", buf, pos)[0]


def _cstr(buf: bytes, pos: int | None) -> str:
    if pos is None:
        return ""
    end = buf.index(b"\0", pos)
    return buf[pos:end].decode("ascii")


def _pointer_offsets(table: Table) -> list[int]:
    """Byte offsets within a row that hold relative pointers."""
    if table.schema is not None:
        return [f.offset for f in table.schema.fields if f.type in POINTER_TYPES]
    if table.data_type in POINTER_TYPES:
        return [0]
    return []


def read_simdata(buf: bytes) -> tuple[SimData, dict]:
    """
    Parse a SimData resource.

    Returns the model plus a layout dict (table and schema positions) that
    write_simdata can use to reproduce the original placement exactly.
    """
    if len(buf) < HEADER_SIZE or buf[:4] != MAGIC:
        raise SimDataError("not a SimData resource (bad magic bytes)")
    version = struct.unpack_from("<I", buf, 4)[0]
    info_pos = _rel(buf, 8)
    table_count = struct.unpack_from("<I", buf, 12)[0]
    schema_pos = _rel(buf, 16)
    schema_count = struct.unpack_from("<I", buf, 20)[0]
    if info_pos is None or schema_pos is None:
        raise SimDataError("SimData header has a null table or schema offset")

    schemas: dict[int, Schema] = {}
    for i in range(schema_count):
        p = schema_pos + i * SCHEMA_SIZE
        name = _cstr(buf, _rel(buf, p))
        schema_hash, size = struct.unpack_from("<II", buf, p + 8)
        ftab = _rel(buf, p + 16)
        count = struct.unpack_from("<I", buf, p + 20)[0]
        fields = []
        for j in range(count):
            fp = ftab + j * FIELD_SIZE
            ftype, offset = struct.unpack_from("<II", buf, fp + 8)
            fields.append(Field(_cstr(buf, _rel(buf, fp)), DataType(ftype), offset))
        fields.sort(key=lambda f: f.offset)
        schemas[p] = Schema(name, schema_hash, size, fields)

    tables: list[Table] = []
    raw_rows: list[tuple[Table, int, int]] = []
    for i in range(table_count):
        p = info_pos + i * TABLE_INFO_SIZE
        name = _cstr(buf, _rel(buf, p))
        schema_at = _rel(buf, p + 8)
        dtype, row_size = struct.unpack_from("<II", buf, p + 12)
        rows_at = _rel(buf, p + 20)
        count = struct.unpack_from("<I", buf, p + 24)[0]
        table = Table(name, schemas.get(schema_at) if schema_at is not None else None,
                      DataType(dtype), row_size)
        tables.append(table)
        raw_rows.append((table, rows_at if rows_at is not None else 0, count))

    def locate(target: int) -> Ref:
        for table, start, count in raw_rows:
            end = start + table.row_size * count
            if start <= target < end or (target == start and count == 0):
                index, rem = divmod(target - start, table.row_size)
                if rem:
                    raise SimDataError(f"pointer 0x{target:X} lands mid-row")
                return Ref(table, index)
        raise SimDataError(f"pointer 0x{target:X} does not land in any table")

    for table, start, count in raw_rows:
        offsets = _pointer_offsets(table)
        for r in range(count):
            row_pos = start + r * table.row_size
            data = bytearray(buf[row_pos:row_pos + table.row_size])
            pointers: dict[int, Ref | None] = {}
            for off in offsets:
                target = _rel(buf, row_pos + off)
                pointers[off] = None if target is None else locate(target)
                data[off:off + 4] = b"\0\0\0\0"  # re-filled on write
            table.rows.append(data)
            table.pointers.append(pointers)

    layout = {
        "tables": [start for _t, start, _c in raw_rows],
        "schemas": schema_pos,
    }
    return SimData(tables, version, schema_order=list(schemas.values())), layout


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

def _align(value: int, to: int) -> int:
    return (value + to - 1) // to * to


def write_simdata(model: SimData, layout: dict | None = None) -> bytes:
    """
    Serialise a SimData model.

    `layout` (as returned by read_simdata) pins tables and the schema table
    to given positions; it exists so tests can prove byte-exact round trips.
    Normal callers leave it out.
    """
    tables = model.tables
    schemas = model.schemas
    info_pos = HEADER_SIZE
    cursor = info_pos + TABLE_INFO_SIZE * len(tables)

    table_pos: list[int] = []
    for i, t in enumerate(tables):
        if layout:
            cursor = layout["tables"][i]
        else:
            cursor = _align(cursor, TABLE_ALIGN)
        table_pos.append(cursor)
        cursor += t.row_size * len(t.rows)

    schema_pos = layout["schemas"] if layout else _align(cursor, TABLE_ALIGN)
    field_pos: list[int] = []
    cursor = schema_pos + SCHEMA_SIZE * len(schemas)
    for s in schemas:
        field_pos.append(cursor)
        cursor += FIELD_SIZE * len(s.fields)

    # Names: per schema, field names in offset order then the schema name;
    # then every named table.
    names = bytearray()
    name_at: dict[tuple, int] = {}
    written: dict[str, int] = {}  # EA stores each distinct name once
    strings_start = cursor

    def put(key: tuple, text: str) -> None:
        if text not in written:
            written[text] = strings_start + len(names)
            names.extend(text.encode("ascii") + b"\0")
        name_at[key] = written[text]

    for si, s in enumerate(schemas):
        for f in sorted(s.fields, key=lambda f: f.offset):
            put(("field", si, f.name), f.name)
        put(("schema", si), s.name)
    for ti, t in enumerate(tables):
        if t.name:
            put(("table", ti), t.name)

    out = bytearray(strings_start + len(names))
    out[strings_start:] = names

    def rel_to(pos: int, target: int | None) -> None:
        if target is None:
            struct.pack_into("<I", out, pos, NULL)
        else:
            struct.pack_into("<i", out, pos, target - pos)

    index_of = {id(t): i for i, t in enumerate(tables)}

    def ref_pos(ref: Ref | None) -> int | None:
        if ref is None:
            return None
        if id(ref.table) not in index_of:
            raise SimDataError("pointer to a table that is not in this resource")
        return table_pos[index_of[id(ref.table)]] + ref.row * ref.table.row_size

    out[0:4] = MAGIC
    struct.pack_into("<I", out, 4, model.version)
    rel_to(8, info_pos)
    struct.pack_into("<I", out, 12, len(tables))
    rel_to(16, schema_pos)
    struct.pack_into("<I", out, 20, len(schemas))

    schema_index = {id(s): i for i, s in enumerate(schemas)}
    for ti, t in enumerate(tables):
        p = info_pos + ti * TABLE_INFO_SIZE
        rel_to(p, name_at.get(("table", ti)))
        struct.pack_into("<I", out, p + 4, fnv32(t.name))
        rel_to(p + 8, None if t.schema is None
               else schema_pos + SCHEMA_SIZE * schema_index[id(t.schema)])
        struct.pack_into("<III", out, p + 12, t.data_type, t.row_size, 0)
        rel_to(p + 20, table_pos[ti])
        struct.pack_into("<I", out, p + 24, len(t.rows))

        for r, (data, pointers) in enumerate(zip(t.rows, t.pointers)):
            row_at = table_pos[ti] + r * t.row_size
            out[row_at:row_at + t.row_size] = data
            for off, ref in pointers.items():
                rel_to(row_at + off, ref_pos(ref))

    for si, s in enumerate(schemas):
        p = schema_pos + si * SCHEMA_SIZE
        rel_to(p, name_at[("schema", si)])
        struct.pack_into("<III", out, p + 4, fnv32(s.name), s.schema_hash, s.size)
        rel_to(p + 16, field_pos[si])
        struct.pack_into("<I", out, p + 20, len(s.fields))
        for fi, f in enumerate(sorted(s.fields, key=lambda f: f.name_hash)):
            fp = field_pos[si] + fi * FIELD_SIZE
            rel_to(fp, name_at[("field", si, f.name)])
            struct.pack_into("<III", out, fp + 4, f.name_hash, f.type, f.offset)
            rel_to(fp + 16, None)

    return bytes(out)


# ---------------------------------------------------------------------------
# Building rows
# ---------------------------------------------------------------------------

class RowBuilder:
    """
    Fill one row of a schema table by field name.

    Scalars are packed in place; pointer fields record a Ref (or None) that
    write_simdata resolves once table positions are known.
    """

    def __init__(self, schema: Schema):
        self.schema = schema
        self.data = bytearray(schema.size)
        self.pointers: dict[int, Ref | None] = {
            f.offset: None for f in schema.fields if f.type in POINTER_TYPES
        }

    def set(self, name: str, value) -> "RowBuilder":
        f = self.schema.field(name)
        if f.type in SCALAR_FORMAT:
            struct.pack_into(SCALAR_FORMAT[f.type], self.data, f.offset, value)
        elif f.type == DataType.RESOURCEKEY:
            type_id, group_id, instance = value
            struct.pack_into("<QII", self.data, f.offset, instance, type_id, group_id)
        elif f.type == DataType.OBJECT:
            self.pointers[f.offset] = value
        elif f.type == DataType.VECTOR:
            ref, count = value if value else (None, 0)
            self.pointers[f.offset] = ref
            struct.pack_into("<I", self.data, f.offset + 4, count)
        elif f.type == DataType.VARIANT:
            ref, type_hash = value
            self.pointers[f.offset] = ref
            struct.pack_into("<I", self.data, f.offset + 4, type_hash)
        else:
            raise SimDataError(f"cannot set field {name!r} of type {f.type.name}")
        return self

    def add_to(self, table: Table) -> Ref:
        return table.add_row(self.data, self.pointers)


def raw_table(data_type: DataType, values: list) -> Table:
    """An unnamed schema-less table of scalars, e.g. a list of tuning ids."""
    fmt = SCALAR_FORMAT[data_type]
    size = struct.calcsize(fmt)
    table = Table("", None, data_type, size)
    for v in values:
        table.add_row(struct.pack(fmt, v))
    return table
