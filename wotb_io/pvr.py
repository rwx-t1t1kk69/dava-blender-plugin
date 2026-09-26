"""Minimal PVR3 decoder for the formats WoTB 3.10 actually uses.

Blitz .pvr files are PVR3 containers with uncompressed pixels — surveying the
game shows only RGBA4444 (113/116) and RGB565 (3/116) among tank textures.
That's what we handle here. Anything else is reported as unsupported.
"""
import struct


PVR3_MAGIC = 0x03525650  # 'PVR' + version 3

# Bump when the decode logic changes so importers re-decode instead of reusing
# a previously-cached (possibly wrongly-decoded) image from the .blend.
DECODE_VERSION = 2


def _decode_pixel_format(pf: bytes):
    """PVR3 pixel format is 8 bytes.

    If all four low bytes are printable ASCII, it's an uncompressed channel
    layout: bytes[0:4] = channel names (LSB first), bytes[4:8] = bits/channel.
    Otherwise it's a compressed enum id (uint64).
    """
    if all(0x20 <= b <= 0x7e or b == 0 for b in pf[:4]) and pf[0] != 0:
        # Strip trailing zero channel names
        names = pf[:4].rstrip(b"\x00").decode("ascii")
        bits = tuple(pf[4:8][:len(names)])
        return ("channels", names, bits)
    return ("compressed", struct.unpack("<Q", pf)[0])


def _mip_size(w, h, bits_per_pixel):
    stride = (w * bits_per_pixel + 7) // 8
    return stride * h


def _decode_rgba4444(buf: bytes, w: int, h: int):
    """Return list of length w*h*4 with floats in [0,1], row 0 = TOP row."""
    out = [0.0] * (w * h * 4)
    view = memoryview(buf)
    for i in range(w * h):
        px = view[i * 2] | (view[i * 2 + 1] << 8)
        r = (px & 0x000F)
        g = (px & 0x00F0) >> 4
        b = (px & 0x0F00) >> 8
        a = (px & 0xF000) >> 12
        j = i * 4
        out[j    ] = r / 15.0
        out[j + 1] = g / 15.0
        out[j + 2] = b / 15.0
        out[j + 3] = a / 15.0
    return out


def _decode_rgb565(buf: bytes, w: int, h: int):
    # Standard R5G6B5: red in the high 5 bits, blue in the low 5. Reading red
    # from the low bits (as the PVR3 channel-name order literally implies)
    # swaps red and blue and turns track textures cyan/red — DAVA stores them
    # in the conventional layout, so decode R high / B low.
    out = [0.0] * (w * h * 4)
    view = memoryview(buf)
    for i in range(w * h):
        px = view[i * 2] | (view[i * 2 + 1] << 8)
        r = (px & 0xF800) >> 11
        g = (px & 0x07E0) >> 5
        b = (px & 0x001F)
        j = i * 4
        out[j    ] = r / 31.0
        out[j + 1] = g / 63.0
        out[j + 2] = b / 31.0
        out[j + 3] = 1.0
    return out


def _flip_rows(pixels, w, h):
    """Blender's Image.pixels expects row 0 = BOTTOM. PVR stores row 0 = TOP.
    Swap in place."""
    stride = w * 4
    for y in range(h // 2):
        top = y * stride
        bot = (h - 1 - y) * stride
        pixels[top:top + stride], pixels[bot:bot + stride] = (
            pixels[bot:bot + stride],
            pixels[top:top + stride],
        )
    return pixels


def decode_top_mip(path: str):
    """Return (width, height, rgba_float_list) for the top mip level.

    The returned pixel list is oriented for Blender (row 0 = bottom row).
    Raises ValueError on unsupported files."""
    with open(path, "rb") as f:
        data = f.read()

    if len(data) < 52:
        raise ValueError(f"{path}: file too small to be PVR3")

    version = struct.unpack("<I", data[0:4])[0]
    if version != PVR3_MAGIC:
        raise ValueError(f"{path}: not a PVR3 file (magic=0x{version:08x})")

    pf = data[8:16]
    height = struct.unpack("<I", data[24:28])[0]
    width = struct.unpack("<I", data[28:32])[0]
    depth = struct.unpack("<I", data[32:36])[0]
    surfaces = struct.unpack("<I", data[36:40])[0]
    faces = struct.unpack("<I", data[40:44])[0]
    mips = struct.unpack("<I", data[44:48])[0]
    meta_size = struct.unpack("<I", data[48:52])[0]

    if depth != 1 or surfaces != 1 or faces != 1:
        raise ValueError(f"{path}: 3D/array/cube textures not supported")
    if mips < 1:
        raise ValueError(f"{path}: no mip levels")

    fmt = _decode_pixel_format(pf)
    body_off = 52 + meta_size
    print(f"[wotb_io] PVR decode: {path}  fmt={fmt}  {width}x{height}")

    if fmt[0] == "channels" and fmt[1] == "rgba" and fmt[2] == (4, 4, 4, 4):
        bpp = 16
        mip_bytes = _mip_size(width, height, bpp)
        top = data[body_off:body_off + mip_bytes]
        px = _decode_rgba4444(top, width, height)
    elif fmt[0] == "channels" and fmt[1] == "rgb" and fmt[2] == (5, 6, 5):
        bpp = 16
        mip_bytes = _mip_size(width, height, bpp)
        top = data[body_off:body_off + mip_bytes]
        px = _decode_rgb565(top, width, height)
    else:
        raise ValueError(f"{path}: unsupported PVR pixel format {fmt}")

    _flip_rows(px, width, height)
    return width, height, px
