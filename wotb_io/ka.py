"""DAVA Engine KeyedArchive binary format parser.

A KeyedArchive is a dict-like serialized structure used throughout DAVA Engine.
Layout:
    'KA' magic (2 bytes) + version (uint16) + count (uint32)
    count * (key_variant, value_variant)

VariantType is a tagged union: 1-byte type tag + payload.
"""
import struct


# VariantType enum from DAVA Engine (dava.framework, KeyedArchive.h)
VT_NONE = 0
VT_BOOLEAN = 1
VT_INT32 = 2
VT_FLOAT = 3
VT_STRING = 4
VT_WIDE_STRING = 5
VT_BYTE_ARRAY = 6
VT_UINT32 = 7
VT_KEYED_ARCHIVE = 8
VT_INT64 = 9
VT_UINT64 = 10
VT_VECTOR2 = 11
VT_VECTOR3 = 12
VT_VECTOR4 = 13
VT_MATRIX2 = 14
VT_MATRIX3 = 15
VT_MATRIX4 = 16
VT_COLOR = 17
VT_FASTNAME = 18
VT_AABBOX3 = 19
VT_FILEPATH = 20
VT_FLOAT64 = 21
VT_INT8 = 22
VT_UINT8 = 23
VT_INT16 = 24
VT_UINT16 = 25


class Reader:
    __slots__ = ("data", "off")

    def __init__(self, data, off=0):
        self.data = data
        self.off = off

    def u8(self):
        v = self.data[self.off]; self.off += 1; return v

    def i8(self):
        v = struct.unpack_from("<b", self.data, self.off)[0]; self.off += 1; return v

    def u16(self):
        v = struct.unpack_from("<H", self.data, self.off)[0]; self.off += 2; return v

    def i16(self):
        v = struct.unpack_from("<h", self.data, self.off)[0]; self.off += 2; return v

    def u32(self):
        v = struct.unpack_from("<I", self.data, self.off)[0]; self.off += 4; return v

    def i32(self):
        v = struct.unpack_from("<i", self.data, self.off)[0]; self.off += 4; return v

    def u64(self):
        v = struct.unpack_from("<Q", self.data, self.off)[0]; self.off += 8; return v

    def i64(self):
        v = struct.unpack_from("<q", self.data, self.off)[0]; self.off += 8; return v

    def f32(self):
        v = struct.unpack_from("<f", self.data, self.off)[0]; self.off += 4; return v

    def f64(self):
        v = struct.unpack_from("<d", self.data, self.off)[0]; self.off += 8; return v

    def raw(self, n):
        v = self.data[self.off:self.off + n]; self.off += n; return v

    def bstr(self):
        n = self.u32()
        return self.raw(n).decode("utf-8", errors="replace")


def read_variant(r):
    t = r.u8()
    if t == VT_NONE:
        return None
    if t == VT_BOOLEAN:
        return bool(r.u8())
    if t == VT_INT32:
        return r.i32()
    if t == VT_FLOAT:
        return r.f32()
    if t == VT_STRING:
        return r.bstr()
    if t == VT_WIDE_STRING:
        n = r.u32(); return r.raw(n * 4).decode("utf-32-le", "replace")
    if t == VT_BYTE_ARRAY:
        n = r.u32(); return r.raw(n)
    if t == VT_UINT32:
        return r.u32()
    if t == VT_KEYED_ARCHIVE:
        n = r.u32()
        inner = r.raw(n)
        return parse_ka_bytes(inner)
    if t == VT_INT64:
        return r.i64()
    if t == VT_UINT64:
        return r.u64()
    if t == VT_VECTOR2:
        return (r.f32(), r.f32())
    if t == VT_VECTOR3:
        return (r.f32(), r.f32(), r.f32())
    if t == VT_VECTOR4:
        return (r.f32(), r.f32(), r.f32(), r.f32())
    if t == VT_MATRIX2:
        return [r.f32() for _ in range(4)]
    if t == VT_MATRIX3:
        return [r.f32() for _ in range(9)]
    if t == VT_MATRIX4:
        return [r.f32() for _ in range(16)]
    if t == VT_COLOR:
        return (r.f32(), r.f32(), r.f32(), r.f32())
    if t == VT_FASTNAME:
        return r.bstr()
    if t == VT_AABBOX3:
        return [r.f32() for _ in range(6)]
    if t == VT_FILEPATH:
        return r.bstr()
    if t == VT_FLOAT64:
        return r.f64()
    if t == VT_INT8:
        return r.i8()
    if t == VT_UINT8:
        return r.u8()
    if t == VT_INT16:
        return r.i16()
    if t == VT_UINT16:
        return r.u16()
    raise ValueError(f"Unknown VariantType {t} at offset {r.off - 1}")


def parse_ka_at(data, off):
    """Parse a KeyedArchive starting at `off` in `data`.
    Returns (dict, bytes_consumed)."""
    r = Reader(data, off)
    magic = r.raw(2)
    if magic != b"KA":
        raise ValueError(f"Not a KeyedArchive at offset {off}: {magic!r}")
    version = r.u16()  # noqa: F841
    count = r.u32()
    out = {}
    dupes = None
    for _ in range(count):
        key = read_variant(r)
        val = read_variant(r)
        if key in out:
            if dupes is None:
                dupes = []
            dupes.append((key, val))
        else:
            out[key] = val
    if dupes is not None:
        out["__dupes__"] = dupes
    return out, r.off - off


def parse_ka_bytes(buf):
    d, _ = parse_ka_at(buf, 0)
    return d
