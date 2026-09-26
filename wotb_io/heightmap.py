"""DAVA Engine `.heightmap` parser.

    u32  width           (grid is width x width)
    u32  bitsPerSample   (16 in every file surveyed)
    width*width samples  (little-endian uint16 for 16-bit)

Sample values are normalized: 0 = bbox min Z, 0xFFFF = bbox max Z.
The bounding box lives in the entity's Landscape RenderComponent
(`bbox` = 6 float32: minX minY minZ maxX maxY maxZ) — the grid is
tiled over [minX,maxX] x [minY,maxY] and scaled into [minZ,maxZ]."""
import os
import struct


def load_heightmap(path):
    """Return (width, samples: list[int]). None if the file is malformed."""
    if not path or not os.path.isfile(path):
        return None
    with open(path, "rb") as f:
        data = f.read()
    if len(data) < 8:
        return None
    width, bits = struct.unpack_from("<II", data, 0)
    if width <= 0 or bits not in (8, 16, 32):
        return None
    bpp = bits // 8
    expected = width * width * bpp
    if len(data) < 8 + expected:
        return None
    body = data[8:8 + expected]
    if bits == 16:
        samples = list(struct.unpack_from(f"<{width*width}H", body, 0))
    elif bits == 8:
        samples = list(body)
    else:  # 32-bit
        samples = list(struct.unpack_from(f"<{width*width}I", body, 0))
    return width, samples, bits


def bbox_from_bytes(bbox_bytes):
    """Landscape.bbox is 24 bytes = (minX minY minZ maxX maxY maxZ) float32."""
    if not bbox_bytes or len(bbox_bytes) < 24:
        return None
    return struct.unpack_from("<6f", bbox_bytes, 0)


def build_grid(width, samples, bits, bbox):
    """Return (verts, tris, uvs) for a width*width heightmap tessellation.

    verts: list of (x, y, z) covering [minX,maxX] x [minY,maxY]
    tris:  two triangles per grid cell
    uvs:   per-vertex (u, v), (0,0) at (minX,minY), (1,1) at (maxX,maxY)"""
    minX, minY, minZ, maxX, maxY, maxZ = bbox
    dx = (maxX - minX) / max(width - 1, 1)
    dy = (maxY - minY) / max(width - 1, 1)
    dz = maxZ - minZ
    scale = 1.0 / ((1 << bits) - 1)
    verts = []
    uvs = []
    for j in range(width):
        y = minY + dy * j
        v = j / max(width - 1, 1)
        row = j * width
        for i in range(width):
            x = minX + dx * i
            u = i / max(width - 1, 1)
            h = samples[row + i] * scale
            z = minZ + dz * h
            verts.append((x, y, z))
            uvs.append((u, v))
    tris = []
    for j in range(width - 1):
        base = j * width
        nxt = base + width
        for i in range(width - 1):
            a = base + i
            b = a + 1
            c = nxt + i
            d = c + 1
            tris.append((a, b, d))
            tris.append((a, d, c))
    return verts, tris, uvs
