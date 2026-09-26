"""DAVA ParticleEffectComponent emitters -> Blender lights + FX markers.

FBX (the Unity export path) cannot carry Blender particle systems or
billboards, so faking the effect in Blender only produces static cards that
never reach Unity alive. Instead we split the two things the hangar needs:

* **Lights** are real Blender lights and export via FBX as Point/Spot lights:
  - every lamp (`glow` / `big_glow` emitter) gets a **SPOT** pointing down so
    the hall is lit from its own fixtures (a point light would spill up/out);
  - the scene's real `LightComponent`s are still imported by the importer.

* **Particle effects** (`glow`, `smoke`, …) become lightweight **Empty
  markers** whose names encode the emitter type. FBX preserves the names, and
  the bundled Unity Editor script (`unity/Editor/WotbHangarFx.cs`) walks the
  imported hangar and spawns a real, animated `ParticleSystem` (rising steam,
  pulsating glow) at each marker. That is what makes the smoke actually move
  in Unity.

Markers are named `WOTBFX_<type>_<n>` so the Unity side can match them by
name regardless of how FBX re-suffixes duplicates. The remaining Blender
custom properties (yaml path, sprite, color, size) are still attached for
inspection — Unity's FBX importer usually drops them, but the names alone
are enough for the Editor script to rebuild the FX.
"""
import os
import re
import math
import bpy


# ---------------------------------------------------------------------------
# Emitter classification (by YAML basename — matches the game's own naming)
# ---------------------------------------------------------------------------

def classify_emitter(fname):
    """Return 'glow' (a lamp), 'smoke' (floor grate steam) or 'other'."""
    base = os.path.basename(fname or "").lower()
    if base.endswith(".yaml"):
        base = base[:-5]
    if "smoke" in base or "steam" in base:
        return "smoke"
    if "glow" in base:
        return "glow"
    return "other"


# ---------------------------------------------------------------------------
# Minimal DAVA particle YAML reader (flat two-level format)
# ---------------------------------------------------------------------------

_FLOAT_RE = re.compile(r"-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?")


def _floats(s):
    return [float(x) for x in _FLOAT_RE.findall(s or "")]


def parse_particle_yaml(path):
    """Return {'emitter': {...}, 'layers': [ {...}, ... ]} or None."""
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
    except OSError:
        return None
    sections, order, cur = {}, [], None
    for raw in lines:
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        if not raw[:1].isspace():
            name = raw.strip().rstrip(":").strip()
            cur = {}
            sections[name] = cur
            order.append(name)
            continue
        if cur is None or ":" not in raw:
            continue
        key, _, val = raw.strip().partition(":")
        cur[key.strip()] = val.strip()
    if "emitter" not in sections:
        return None
    return {"emitter": sections["emitter"],
            "layers": [sections[k] for k in order if k.startswith("layer")]}


def _val_str(d, key, default=None):
    v = d.get(key)
    return default if v is None else v.strip().strip('"').strip("'")


def _val_floats(d, key):
    return _floats(d.get(key, ""))


# ---------------------------------------------------------------------------
# Sprite path resolution (returns a path relative to the Data root when we can
# find one, so the Unity side can map it into Assets/).
# ---------------------------------------------------------------------------

def resolve_sprite_path(layer, yaml_path, data_root):
    rel = _val_str(layer, "sprite")
    if not rel or not yaml_path:
        return None
    sprite_dir = os.path.normpath(os.path.join(os.path.dirname(yaml_path), rel))
    png = None
    for cand in (os.path.join(sprite_dir, "frame0.png"), sprite_dir + ".png"):
        if os.path.isfile(cand):
            png = cand
            break
    if png is None and os.path.isdir(sprite_dir):
        try:
            for e in sorted(os.listdir(sprite_dir)):
                if e.lower().endswith((".png", ".dds", ".tga")):
                    png = os.path.join(sprite_dir, e)
                    break
        except OSError:
            pass
    if png is None:
        return None
    if data_root:
        try:
            r = os.path.relpath(png, data_root)
            if not r.startswith(".."):
                return r.replace("\\", "/")
        except ValueError:
            pass
    return png.replace("\\", "/")


def effect_params(yaml_data, yaml_path, data_root):
    """Pull the handful of values the Unity ParticleSystem needs from YAML."""
    layers = (yaml_data or {}).get("layers") or [{}]
    layer = layers[0]
    sz = _val_floats(layer, "size")
    size = sz[0] if sz else 1.0
    col = _val_floats(layer, "colorOverLife")
    color = [col[0] / 255.0, col[1] / 255.0, col[2] / 255.0] \
        if len(col) >= 3 else [1.0, 1.0, 1.0]
    life = _val_floats(layer, "life")
    dst = (_val_str(layer, "dstBlendFactor") or "").upper()
    b = _val_floats(layer, "blending")
    additive = ("ONE" in dst) or (bool(b) and int(b[0]) == 3)
    esize = _val_floats((yaml_data or {}).get("emitter", {}), "size")
    return {
        "sprite": resolve_sprite_path(layer, yaml_path, data_root),
        "size": round(size, 4),
        "color": [round(c, 4) for c in color],
        "life": round(life[0], 3) if life else 6.0,
        "additive": bool(additive),
        "emitter_size": [round(x, 3) for x in esize[:3]] if esize else [0.5, 0.5, 0.0],
    }


# ---------------------------------------------------------------------------
# Lamp spotlight (points straight down)
# ---------------------------------------------------------------------------

def make_lamp_light(name, energy, color, collection):
    data = bpy.data.lights.new(name=name, type="SPOT")
    data.color = color
    data.energy = energy
    data.spot_size = math.radians(130.0)
    data.spot_blend = 0.5
    try:
        data.shadow_soft_size = 0.4
        data.use_custom_distance = True
        data.cutoff_distance = 18.0
    except Exception:
        pass
    obj = bpy.data.objects.new(name, data)
    collection.objects.link(obj)
    # Blender spots shine along local -Z; identity rotation = straight down.
    obj["wotb_lamp_light"] = True
    return obj


# ---------------------------------------------------------------------------
# FX marker Empty (name encodes the type; Unity Editor script rebuilds FX)
# ---------------------------------------------------------------------------

def make_fx_marker(name, fx_type, location, parent, collection, params, yaml_rel):
    m = bpy.data.objects.new(name, None)
    m.empty_display_type = "SPHERE" if fx_type == "glow" else "CONE"
    m.empty_display_size = 0.25
    m.location = location
    m.parent = parent
    collection.objects.link(m)
    m["wotb_fx_type"] = fx_type
    if yaml_rel:
        m["wotb_fx_yaml"] = str(yaml_rel)
    if params:
        if params.get("sprite"):
            m["wotb_fx_sprite"] = params["sprite"]
        m["wotb_fx_size"] = float(params.get("size", 1.0))
        m["wotb_fx_life"] = float(params.get("life", 6.0))
        m["wotb_fx_additive"] = 1 if params.get("additive") else 0
        m["wotb_fx_color"] = list(params.get("color", [1.0, 1.0, 1.0]))
        m["wotb_fx_emitter_size"] = list(params.get("emitter_size", [0.5, 0.5, 0.0]))
    return m
