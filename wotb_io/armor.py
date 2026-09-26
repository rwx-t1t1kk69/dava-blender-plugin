"""Armor mesh from WoTB CollisionMeshes, for a Unity armor-inspection shader.

Every tank has a matching collision mesh under
`Data/3d/Tanks/CollisionMeshes/<nation>-<name>.sc2`. It carries:

* The exact hit-detection geometry (hull + turret, no tracks / gun / belly
  overshoot).
* Per-vertex armor thickness: each vertex's `EVF_COLOR` (uint32) is really the
  raw bits of a float32 in millimetres. IS-3 verts decode as 250.0, 220.0,
  122.0, 60.0 — the well-known plate values.

`load_armor()` imports that geometry in place (correct world transforms +
Z-alignment to the visual hull) and stores the nominal thickness in a UV layer
(`armor_mm`, thickness in `.x`) so a shader can colour it live — by nominal
thickness, or by *effective* thickness `T / cos(angle)` for the classic armor
inspector. A green→yellow→red vertex-colour heatmap is also baked for preview
inside Blender.

    thickness (mm)  → colour
    0               → grey (no armor / interior)
    1  .. 50        → green
    50 .. 100       → yellow
    100..200        → orange
    200+            → red
"""
import os
import struct

import bpy
from mathutils import Vector, Matrix

from . import sc2_reader
from . import vertex_format as vf


# `USSR/IS-3.sc2` → `CollisionMeshes/ussr-IS-3.sc2`. Two of Blitz's folder
# names map to different collision prefixes (`GB` → `uk`, `German` → `germany`).
_NATION_MAP = {
    "ussr": "ussr",
    "usa": "usa",
    "german": "germany",
    "germany": "germany",
    "japan": "japan",
    "china": "china",
    "france": "france",
    "gb": "uk",
    "uk": "uk",
    "britain": "uk",
    "other": "other",
}


def find_collision_file(tank_sc2_path):
    """Return the path to the matching CollisionMeshes file, or None."""
    p = os.path.abspath(tank_sc2_path)
    tanks_dir = os.path.dirname(os.path.dirname(p))  # .../Tanks/
    nation_folder = os.path.basename(os.path.dirname(p)).lower()
    nation = _NATION_MAP.get(nation_folder, nation_folder)
    base = os.path.splitext(os.path.basename(p))[0]
    candidate = os.path.join(tanks_dir, "CollisionMeshes", f"{nation}-{base}.sc2")
    if os.path.isfile(candidate):
        return candidate
    # Some tanks live under a differently-named parent folder — brute-force scan.
    coll_dir = os.path.join(tanks_dir, "CollisionMeshes")
    if os.path.isdir(coll_dir):
        want = f"-{base.lower()}.sc2"
        for entry in os.listdir(coll_dir):
            if entry.lower().endswith(want):
                return os.path.join(coll_dir, entry)
    return None


def _u32_to_f32(u):
    return struct.unpack("<f", struct.pack("<I", u & 0xFFFFFFFF))[0]


def _heatmap(thickness_mm):
    """Return (r,g,b,a) for a thickness in mm (Blender preview only)."""
    t = thickness_mm
    if t <= 0.0:
        return (0.3, 0.3, 0.3, 0.35)      # no armor / interior
    if t < 50.0:
        k = t / 50.0                       # grey → green
        return (0.2 * (1 - k) + 0.1 * k, 0.4 + 0.5 * k, 0.2 + 0.1 * k, 0.85)
    if t < 100.0:
        k = (t - 50.0) / 50.0             # green → yellow
        return (0.1 + 0.85 * k, 0.9, 0.1, 0.9)
    if t < 200.0:
        k = (t - 100.0) / 100.0           # yellow → orange/red
        return (0.95, 0.85 * (1 - k) + 0.35 * k, 0.05, 0.95)
    k = min((t - 200.0) / 150.0, 1.0)     # 200+ deep red, saturating at 350mm
    return (0.85 + 0.1 * k, 0.15 * (1 - k), 0.05, 1.0)


def _make_armor_material():
    """Preview material that reads the mesh's `armor` colour attribute.

    This is only for looking at the armor inside Blender; the real colouring
    (nominal / effective thickness, penetration) is done by the Unity shader
    reading the `armor_mm` UV channel."""
    name = "WOTB_Armor_Material"
    m = bpy.data.materials.get(name)
    if m is not None:
        return m
    m = bpy.data.materials.new(name)
    m.use_nodes = True
    nt = m.node_tree
    bsdf = nt.nodes.get("Principled BSDF")
    if bsdf is None:
        return m
    attr = nt.nodes.new("ShaderNodeAttribute")
    attr.attribute_name = "armor"
    attr.location = (-320, 200)
    nt.links.new(attr.outputs["Color"], bsdf.inputs["Base Color"])
    if "Alpha" in bsdf.inputs:
        nt.links.new(attr.outputs["Alpha"], bsdf.inputs["Alpha"])
    for attr_name, val in (("blend_method", "BLEND"),
                           ("surface_render_method", "BLENDED")):
        try:
            setattr(m, attr_name, val)
        except (AttributeError, TypeError):
            pass
    return m


# ---------------------------------------------------------------------------
# Z-alignment: the collision file uses a different origin than the visual scene
# (mass-centred vs sitting on the tracks). Match hull belly heights.
# ---------------------------------------------------------------------------

def _hull_z_min(scene):
    """Return the lowest Z of the `hull` entity's mesh, or None."""
    for e in scene.entities_flat():
        if (e.get("name") or "").lower() != "hull":
            continue
        m = sc2_reader.get_transform_matrix(e)
        tz = m[14] if m and len(m) == 16 else 0.0
        z_min = None
        for b in sc2_reader.get_render_batches(e):
            pg = scene.polygroups.get(b.get("rb.datasource"))
            if not pg:
                continue
            mid = b.get("rb.nmatname")
            is_sh = False
            for mm in sc2_reader.resolve_material_chain(scene, mid):
                if "ShadowVolume" in (mm.get("fxName") or ""):
                    is_sh = True
                    break
            if is_sh:
                continue
            streams = vf.parse_vertices(
                pg["vertices"], pg["vertexFormat"], pg["vertexCount"],
            )
            for p in streams.get("position") or []:
                z = p[2] + tz
                z_min = z if z_min is None else min(z_min, z)
        return z_min
    return None


def compute_collision_alignment(tank_sc2_path, collision_scene=None):
    """Return a (dx, dy, dz) offset that aligns CollisionMeshes vertices with
    the visual scene's hull. Empirically the offset is only in Z."""
    coll_path = find_collision_file(tank_sc2_path)
    if not coll_path:
        return (0.0, 0.0, 0.0)
    try:
        visual = sc2_reader.load_sc2(tank_sc2_path)
        collision = collision_scene or sc2_reader.load_sc2(coll_path)
    except Exception as e:
        print(f"[wotb_io] alignment: load failed: {e}")
        return (0.0, 0.0, 0.0)
    vz = _hull_z_min(visual)
    cz = _hull_z_min(collision)
    if vz is None or cz is None:
        return (0.0, 0.0, 0.0)
    return (0.0, 0.0, vz - cz)


# ---------------------------------------------------------------------------
# Geometry + thickness extraction
# ---------------------------------------------------------------------------

def _entity_world_matrices(scene):
    """Return [(entity, world_matrix)] with transforms accumulated down the
    collision-scene hierarchy (a child's local matrix is relative to its
    parent — applying it alone dropped the turret in the wrong place)."""
    out = []

    def rec(e, parent_M):
        m16 = sc2_reader.get_transform_matrix(e)
        if m16 and len(m16) == 16:
            rows = ((m16[0], m16[1], m16[2], m16[3]),
                    (m16[4], m16[5], m16[6], m16[7]),
                    (m16[8], m16[9], m16[10], m16[11]),
                    (m16[12], m16[13], m16[14], m16[15]))
            local = Matrix(rows).transposed()
        else:
            local = Matrix.Identity(4)
        world = parent_M @ local
        out.append((e, world))
        for c in e.get("__children", ()):
            rec(c, world)

    for r in scene.root_entities:
        rec(r, Matrix.Identity(4))
    return out


def _entity_armor_geometry(scene, entity):
    """Return (verts, tris, thickness) for one entity, all batches concatenated.

    `thickness` is parallel to `verts` (mm; 0.0 where a batch carries no color
    channel), so it stays aligned even when batches differ."""
    verts, tris, thickness = [], [], []
    for b in sc2_reader.get_render_batches(entity):
        pg = scene.polygroups.get(b.get("rb.datasource"))
        if not pg:
            continue
        streams = vf.parse_vertices(
            pg["vertices"], pg["vertexFormat"], pg["vertexCount"],
        )
        positions = streams.get("position") or []
        colors = streams.get("color")
        base = len(verts)
        for i, p in enumerate(positions):
            verts.append((p[0], p[1], p[2]))
            t = 0.0
            if colors and i < len(colors):
                c = colors[i]
                u = c[0] if isinstance(c, tuple) else int(c)
                t = _u32_to_f32(u)
                if t < 0.0 or t > 2000.0:      # DAVA sentinels
                    t = 0.0
            thickness.append(t)
        indices = vf.parse_indices(
            pg["indices"], pg["indexFormat"], pg["indexCount"],
        )
        for i in range(0, len(indices), 3):
            tris.append((base + indices[i], base + indices[i + 1],
                         base + indices[i + 2]))
    return verts, tris, thickness


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------

def _armor_group_of(name):
    n = (name or "").lower()
    if n.startswith("hull"):
        return "hull"
    if n.startswith("turret"):
        return "turret"
    return None


def _bbox(verts):
    mn = [verts[0][0], verts[0][1], verts[0][2]]
    mx = [verts[0][0], verts[0][1], verts[0][2]]
    for x, y, z in verts:
        if x < mn[0]: mn[0] = x
        if y < mn[1]: mn[1] = y
        if z < mn[2]: mn[2] = z
        if x > mx[0]: mx[0] = x
        if y > mx[1]: mx[1] = y
        if z > mx[2]: mx[2] = z
    return mn, mx


def load_armor(tank_sc2_path, collection, parent_obj, visual_bboxes=None,
               panel_offset=0.02):
    """Import the CollisionMeshes hitbox as an armor overlay with per-vertex
    thickness baked into a UV channel (`armor_mm`, mm in .x) for the Unity
    shader, plus a vertex-colour heatmap for Blender preview.

    The shell is built per group (hull, turret) using the collision file's own
    transforms for the correct relative shape, then each group is translated so
    its bounding-box centre matches the matching VISUAL part's bounding box
    (passed in `visual_bboxes`). This snaps the armor onto the rendered model
    regardless of the collision file's coordinate frame — trusting the raw
    transforms sat the turret inside the hull.

    Returns the parent Empty holding the armor pieces, or None."""
    coll_path = find_collision_file(tank_sc2_path)
    if not coll_path:
        print(f"[wotb_io] armor: no collision file found for {tank_sc2_path}")
        return None

    scene = sc2_reader.load_sc2(coll_path)
    vb = visual_bboxes or {}

    # Gather geometry per group, positioned by the collision transforms so the
    # parts keep their correct relative layout inside the group.
    groups = {}
    for entity, cworld in _entity_world_matrices(scene):
        grp = _armor_group_of(entity.get("name"))
        if grp is None:
            continue
        rv, rt, rk = _entity_armor_geometry(scene, entity)
        if not rv or not rt:
            continue
        g = groups.setdefault(grp, {"verts": [], "tris": [], "thk": []})
        offset = len(g["verts"])
        for p in rv:
            co = cworld @ Vector((p[0], p[1], p[2]))
            g["verts"].append((co.x, co.y, co.z))
        for (a, b, c) in rt:
            g["tris"].append((offset + a, offset + b, offset + c))
        g["thk"].extend(rk)

    if not groups:
        return None

    base = os.path.splitext(os.path.basename(tank_sc2_path))[0]
    root = bpy.data.objects.new(f"{base}_armor", None)
    root.empty_display_type = "PLAIN_AXES"
    root.empty_display_size = 0.15
    collection.objects.link(root)
    root.parent = parent_obj
    root["wotb_armor"] = True

    mat = _make_armor_material()
    built = 0
    tmin, tmax = 1e30, -1e30
    for grp, g in groups.items():
        verts, tris, thickness = g["verts"], g["tris"], g["thk"]
        if not verts or not tris:
            continue

        # Snap the group's bbox centre onto the visual part's bbox centre.
        if grp in vb:
            amn, amx = _bbox(verts)
            vmn, vmx = vb[grp]
            dxc = (vmn[0] + vmx[0]) * 0.5 - (amn[0] + amx[0]) * 0.5
            dyc = (vmn[1] + vmx[1]) * 0.5 - (amn[1] + amx[1]) * 0.5
            dzc = (vmn[2] + vmx[2]) * 0.5 - (amn[2] + amx[2]) * 0.5
            verts = [(x + dxc, y + dyc, z + dzc) for (x, y, z) in verts]

        name = f"{grp}_armor"
        me = bpy.data.meshes.new(name)
        me.from_pydata(verts, [], tris)

        # Thickness → UV channel (float). Only UV set, so it exports as
        # TEXCOORD0; the Unity shader reads uv.x as mm.
        uv = me.uv_layers.new(name="armor_mm")
        for loop in me.loops:
            vi = loop.vertex_index
            t = thickness[vi] if vi < len(thickness) else 0.0
            uv.data[loop.index].uv = (t, 0.0)

        # Vertex-colour heatmap for Blender preview.
        try:
            attr = me.color_attributes.new(
                name="armor", type="FLOAT_COLOR", domain="POINT")
            flat = []
            for i in range(len(me.vertices)):
                t = thickness[i] if i < len(thickness) else 0.0
                flat.extend(_heatmap(t))
            attr.data.foreach_set("color", flat)
        except Exception:
            pass

        me.update()
        me.validate(clean_customdata=False)

        if panel_offset:
            for v in me.vertices:
                v.co += v.normal * panel_offset
            me.update()

        obj = bpy.data.objects.new(name, me)
        collection.objects.link(obj)
        obj.parent = root
        obj.data.materials.append(mat)
        obj["wotb_armor"] = True
        obj.hide_render = True
        obj.show_in_front = False

        for t in thickness:
            if t > 0.0:
                tmin = min(tmin, t)
                tmax = max(tmax, t)
        built += 1

    if built == 0:
        bpy.data.objects.remove(root, do_unlink=True)
        return None

    if tmax >= tmin:
        root["wotb_armor_min_mm"] = float(tmin)
        root["wotb_armor_max_mm"] = float(tmax)
        print(f"[wotb_io] armor: built {built} meshes "
              f"({tmin:.0f}–{tmax:.0f} mm) from {os.path.basename(coll_path)}")
    else:
        print(f"[wotb_io] armor: built {built} meshes (no thickness data) "
              f"from {os.path.basename(coll_path)}")
    return root
