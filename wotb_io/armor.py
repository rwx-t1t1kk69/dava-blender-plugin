"""Armor overlay + physics collision from WoTB CollisionMeshes.

Every tank has a matching collision mesh under
`Data/3d/Tanks/CollisionMeshes/<nation>-<name>.sc2`. Two things live in it:

* The exact geometry the game uses for hit detection. It's tight around the
  hull and turret, no tracks / no gun barrel, no belly overshoot — perfect
  source for both the physics collision envelope and armor visualisation.
* Per-vertex armor thickness: each vertex's `EVF_COLOR` (uint32) is really
  the raw bits of a float32 in millimetres. IS-3 verts decode as 250.0,
  220.0, 122.0, 60.0 — the well-known plate values.

`load_armor()` paints a heat-map onto slightly-inflated coplanar panels so
each plate reads as its own floating card over the tank body.
`build_collision_meshes()` returns the raw hull + turret vertex bags used to
form the physics envelope.

    thickness (mm)  → colour
    0               → grey (transparent-ish)
    1  .. 50        → green
    50 .. 100       → yellow
    100..200        → orange
    200+            → red
"""
import math
import os
import struct

import bpy
import bmesh
from mathutils import Vector

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
    """Return (r,g,b,a) for a thickness in mm."""
    t = thickness_mm
    if t <= 0.0:
        return (0.3, 0.3, 0.3, 0.35)      # no armor / interior
    if t < 50.0:
        # 0..50: grey → green
        k = t / 50.0
        return (0.2 * (1 - k) + 0.1 * k, 0.4 + 0.5 * k, 0.2 + 0.1 * k, 0.85)
    if t < 100.0:
        k = (t - 50.0) / 50.0             # green → yellow
        return (0.1 + 0.85 * k, 0.9, 0.1, 0.9)
    if t < 200.0:
        k = (t - 100.0) / 100.0           # yellow → orange/red
        return (0.95, 0.85 * (1 - k) + 0.35 * k, 0.05, 0.95)
    # 200+ : deep red, saturating at 350mm
    k = min((t - 200.0) / 150.0, 1.0)
    return (0.85 + 0.1 * k, 0.15 * (1 - k), 0.05, 1.0)


def _make_armor_material():
    """Shared material that reads the mesh's `armor` color attribute."""
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
    # Alpha comes from the same attribute so thin/no-armor faces fade out.
    if "Alpha" in bsdf.inputs:
        nt.links.new(attr.outputs["Alpha"], bsdf.inputs["Alpha"])
    for attr_name, val in (("blend_method", "BLEND"),
                           ("surface_render_method", "BLENDED")):
        try:
            setattr(m, attr_name, val)
        except (AttributeError, TypeError):
            pass
    return m


def _collect_entity_geometry(scene, entity):
    """Concat every batch's positions/indices/thickness for one entity."""
    verts = []
    tris = []
    thickness = []
    for b in sc2_reader.get_render_batches(entity):
        pg = scene.polygroups.get(b.get("rb.datasource"))
        if not pg:
            continue
        streams = vf.parse_vertices(pg["vertices"], pg["vertexFormat"], pg["vertexCount"])
        indices = vf.parse_indices(pg["indices"], pg["indexFormat"], pg["indexCount"])
        positions = streams.get("position") or []
        colors = streams.get("color") or []
        base = len(verts)
        for p in positions:
            verts.append((p[0], p[1], p[2]))
        for c in colors:
            u = c[0] if isinstance(c, tuple) else int(c)
            t = _u32_to_f32(u)
            if t < 0.0 or t > 2000.0:      # DAVA sentinels
                t = 0.0
            thickness.append(t)
        for i in range(0, len(indices), 3):
            tris.append((base + indices[i], base + indices[i + 1], base + indices[i + 2]))
    return verts, tris, thickness


def _build_armor_mesh(scene, entity, name, collection, *,
                      panel_offset=0.04, coplanar_deg=2.0):
    """Build a floating-panel armor overlay for one collision entity.

    We take the raw hitbox, weld duplicates, merge nearly-coplanar triangles
    into big flat plates via dissolve_limit, then push every vertex outward
    along its face normal by `panel_offset` metres. The result reads as a
    stack of thin cards hovering just off the tank body, one per plate."""
    verts_raw, tris_raw, thickness = _collect_entity_geometry(scene, entity)
    if not verts_raw or not tris_raw:
        return None

    bm = bmesh.new()
    bm_verts = [bm.verts.new(co) for co in verts_raw]
    bm.verts.ensure_lookup_table()
    for a, b, c in tris_raw:
        try:
            bm.faces.new((bm_verts[a], bm_verts[b], bm_verts[c]))
        except ValueError:
            pass   # duplicate face — just skip
    bm.faces.ensure_lookup_table()

    # Attach thickness to verts via a bmesh custom layer so it survives the
    # coplanar merge / offset ops below.
    tlayer = bm.verts.layers.float.new("armor_mm")
    for v, t in zip(bm_verts, thickness):
        v[tlayer] = t

    bmesh.ops.remove_doubles(bm, verts=list(bm.verts), dist=0.001)
    bmesh.ops.recalc_face_normals(bm, faces=list(bm.faces))
    # Merge co-planar triangles into big flat plates. Small angle so we
    # only fuse what really is one plate; different plates keep their edges.
    bmesh.ops.dissolve_limit(
        bm,
        angle_limit=math.radians(coplanar_deg),
        use_dissolve_boundaries=False,
        verts=list(bm.verts),
        edges=list(bm.edges),
        delimit=set(),
    )

    # Push each vertex outward along the average of its face normals so the
    # panels sit off the surface, not clipping through the tank.
    for v in bm.verts:
        if not v.link_faces:
            continue
        n = Vector((0.0, 0.0, 0.0))
        for f in v.link_faces:
            n += f.normal
        n.normalize()
        v.co += n * panel_offset

    # Grab per-vertex thickness AFTER dissolve (surviving verts kept theirs).
    per_vert_mm = [v[tlayer] for v in bm.verts]

    me = bpy.data.meshes.new(name)
    bm.to_mesh(me)
    bm.free()
    me.update()

    attr = me.color_attributes.new(name="armor", type="FLOAT_COLOR", domain="POINT")
    flat = []
    for t in per_vert_mm:
        r, g, b, a = _heatmap(t)
        flat.extend((r, g, b, a))
    attr.data.foreach_set("color", flat)

    obj = bpy.data.objects.new(name, me)
    collection.objects.link(obj)
    obj.data.materials.append(_make_armor_material())

    obj["wotb_armor"] = True
    obj["wotb_armor_min_mm"] = float(min(per_vert_mm) if per_vert_mm else 0.0)
    obj["wotb_armor_max_mm"] = float(max(per_vert_mm) if per_vert_mm else 0.0)
    obj.show_in_front = True
    obj.hide_render = True
    return obj


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
            # Skip shadow-volume proxies in the VISUAL scene — they inflate
            # the mesh downwards and would move the offset.
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
    """Return a (dx, dy, dz) offset that, when applied to CollisionMeshes
    vertex positions, aligns them with the visual scene's hull.

    In WoTB the collision file uses a different origin (roughly centred on
    the tank's mass), while the visual file has the tank sitting on the
    tracks. Empirically the offset is only in Z and matches the difference
    between the two hulls' belly heights."""
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


def build_collision_source(tank_sc2_path):
    """Return (scene, entities_to_use, z_offset) or (None, [], 0.0)."""
    coll_path = find_collision_file(tank_sc2_path)
    if not coll_path:
        return None, [], 0.0
    scene = sc2_reader.load_sc2(coll_path)
    keep = []
    for e in scene.entities_flat():
        name = (e.get("name") or "").lower()
        if name == "hull" or name == "turret_01":
            keep.append(e)
    if not keep:
        for e in scene.entities_flat():
            name = (e.get("name") or "").lower()
            if name.startswith(("hull", "turret")):
                keep.append(e)
    dx, dy, dz = compute_collision_alignment(tank_sc2_path, scene)
    return scene, keep, dz


def _first_variant_only(entities):
    """Keep only the first turret / gun variant so panels don't stack."""
    kept = []
    seen_turret = False
    seen_gun = False
    for e in entities:
        name = (e.get("name") or "").lower()
        if name.startswith("turret"):
            if seen_turret:
                continue
            seen_turret = True
        if name.startswith("gun"):
            if seen_gun:
                continue
            seen_gun = True
        kept.append(e)
    return kept


def load_armor(tank_sc2_path, collection, parent_obj, panel_offset=0.04):
    """Import the matching armor collision mesh as floating overlay panels.

    Returns the parent Empty containing the armor sub-objects, or None if
    the file isn't found."""
    coll_path = find_collision_file(tank_sc2_path)
    if not coll_path:
        print(f"[wotb_io] armor: no collision file found for {tank_sc2_path}")
        return None

    scene = sc2_reader.load_sc2(coll_path)
    dx, dy, dz = compute_collision_alignment(tank_sc2_path, scene)

    root = bpy.data.objects.new(
        f"{os.path.splitext(os.path.basename(tank_sc2_path))[0]}_armor", None,
    )
    root.empty_display_type = "PLAIN_AXES"
    root.empty_display_size = 0.15
    collection.objects.link(root)
    root.parent = parent_obj
    root.location = (dx, dy, dz)   # align collision-frame → visual-frame
    root["wotb_armor"] = True
    root["wotb_align_z"] = float(dz)

    from mathutils import Matrix
    built = 0
    entities = _first_variant_only(list(scene.entities_flat()))
    for entity in entities:
        name = entity.get("name") or "part"
        obj = _build_armor_mesh(
            scene, entity, f"{name}_armor", collection,
            panel_offset=panel_offset,
        )
        if obj is None:
            continue
        obj.parent = root
        m16 = sc2_reader.get_transform_matrix(entity)
        if m16 and len(m16) == 16:
            rows = ((m16[0], m16[1], m16[2], m16[3]),
                    (m16[4], m16[5], m16[6], m16[7]),
                    (m16[8], m16[9], m16[10], m16[11]),
                    (m16[12], m16[13], m16[14], m16[15]))
            obj.matrix_local = Matrix(rows).transposed()
        built += 1

    print(f"[wotb_io] armor: built {built} panelised meshes from {os.path.basename(coll_path)}")
    if built == 0:
        bpy.data.objects.remove(root, do_unlink=True)
        return None
    return root
