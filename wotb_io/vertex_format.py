"""DAVA Engine vertex format decoding.

Vertex layout is defined by a bitmask (EVF_*). Each set bit adds a fixed
component to the interleaved vertex stream, in a well-defined order.
"""
import struct

EVF_VERTEX          = 1 << 0   # position vec3
EVF_NORMAL          = 1 << 1   # normal vec3
EVF_COLOR           = 1 << 2   # color uint32 (RGBA)
EVF_TEXCOORD0       = 1 << 3   # uv0 vec2
EVF_TEXCOORD1       = 1 << 4   # uv1 vec2
EVF_TEXCOORD2       = 1 << 5   # uv2 vec2
EVF_TEXCOORD3       = 1 << 6   # uv3 vec2
EVF_TANGENT         = 1 << 7   # tangent vec3
EVF_BINORMAL        = 1 << 8   # binormal vec3
EVF_HARD_JOINTINDEX = 1 << 9   # single joint index float
EVF_PIVOT4          = 1 << 10  # pivot vec4
EVF_FLEXIBILITY     = 1 << 11  # flexibility float
EVF_ANGLE_SIN_COS   = 1 << 12  # vec2
EVF_JOINTINDEX      = 1 << 13  # vec4 joint indices
EVF_JOINTWEIGHT     = 1 << 14  # vec4 joint weights
EVF_CUBETEXCOORD0   = 1 << 15  # cube uv0 vec3

# (flag, name, element_count, element_type)
# element_type is 'f' (float32), 'u32', or 'u16' for packed indices.
_LAYOUT = [
    (EVF_VERTEX,          "position",   3, "f"),
    (EVF_NORMAL,          "normal",     3, "f"),
    (EVF_COLOR,           "color",      1, "u32"),
    (EVF_TEXCOORD0,       "uv0",        2, "f"),
    (EVF_TEXCOORD1,       "uv1",        2, "f"),
    (EVF_TEXCOORD2,       "uv2",        2, "f"),
    (EVF_TEXCOORD3,       "uv3",        2, "f"),
    (EVF_TANGENT,         "tangent",    3, "f"),
    (EVF_BINORMAL,        "binormal",   3, "f"),
    (EVF_HARD_JOINTINDEX, "hardJoint",  1, "f"),
    (EVF_PIVOT4,          "pivot",      4, "f"),
    (EVF_FLEXIBILITY,     "flex",       1, "f"),
    (EVF_ANGLE_SIN_COS,   "angleSC",    2, "f"),
    (EVF_JOINTINDEX,      "jointIdx",   4, "f"),
    (EVF_JOINTWEIGHT,     "jointW",     4, "f"),
    (EVF_CUBETEXCOORD0,   "cubeUV0",    3, "f"),
]


def _elem_size(kind, count):
    if kind == "f":
        return 4 * count
    if kind == "u32":
        return 4 * count
    if kind == "u16":
        return 2 * count
    raise ValueError(kind)


def vertex_stride(fmt):
    s = 0
    for flag, _, count, kind in _LAYOUT:
        if fmt & flag:
            s += _elem_size(kind, count)
    return s


def active_streams(fmt):
    """Return ordered list of (name, count, kind, offset_within_vertex)."""
    out = []
    off = 0
    for flag, name, count, kind in _LAYOUT:
        if fmt & flag:
            out.append((name, count, kind, off))
            off += _elem_size(kind, count)
    return out


def parse_vertices(vb: bytes, fmt: int, count: int):
    """Return dict of stream_name -> list of tuples."""
    stride = vertex_stride(fmt)
    if stride == 0:
        return {}
    expected = stride * count
    if len(vb) < expected:
        raise ValueError(f"Vertex buffer too short: {len(vb)} < {expected} (stride {stride} * {count})")
    streams = active_streams(fmt)
    out = {name: [None] * count for name, _, _, _ in streams}
    for i in range(count):
        base = i * stride
        for name, elems, kind, off in streams:
            p = base + off
            if kind == "f":
                out[name][i] = struct.unpack_from(f"<{elems}f", vb, p)
            elif kind == "u32":
                out[name][i] = struct.unpack_from(f"<{elems}I", vb, p)
            elif kind == "u16":
                out[name][i] = struct.unpack_from(f"<{elems}H", vb, p)
    return out


def parse_indices(ib: bytes, index_format: int, count: int):
    """Index format: 0 = uint16, 1 = uint32."""
    if index_format == 0:
        return list(struct.unpack_from(f"<{count}H", ib, 0))
    if index_format == 1:
        return list(struct.unpack_from(f"<{count}I", ib, 0))
    raise ValueError(f"Unknown indexFormat {index_format}")
