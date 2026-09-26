"""Blender importer for WoTB 3.10 tank scenes (.sc2)."""
import math
import os
import bpy
import bmesh
from mathutils import Matrix, Vector

from . import sc2_reader
from . import vertex_format as vf
from . import pvr
from . import armor as armor_mod


_C_INT_MIN = -(1 << 31)
_C_INT_MAX = (1 << 31) - 1

# Temporarily disabled per user request. Set back to True to restore the armor
# overlay and the physics collision hull (the operator toggles are ignored
# while this is False).
_ARMOR_COLLISION_ENABLED = False


def _set_alpha_clip(mat):
    """Configure the material for alpha-clip / dithered rendering across
    the old (< 4.2) and new EEVEE Next APIs. Silently no-ops on unknowns."""
    for attr, val in (("blend_method", "CLIP"),
                      ("shadow_method", "CLIP"),
                      ("surface_render_method", "DITHERED")):
        try:
            setattr(mat, attr, val)
        except (AttributeError, TypeError):
            pass
    if hasattr(mat, "alpha_threshold"):
        try:
            mat.alpha_threshold = 0.5
        except (AttributeError, TypeError):
            pass


def _set_int_prop(obj, key, val):
    """Blender ID-property ints are C int32. DAVA stores some fields as uint32
    (e.g. entity `flags` = 4294907926 in Type62.sc2), which overflow. Fall back
    to a string for values that don't fit."""
    if val is None:
        return
    v = int(val)
    if _C_INT_MIN <= v <= _C_INT_MAX:
        obj[key] = v
    else:
        obj[key] = str(v)


def _dava_matrix_to_blender(m16):
    """DAVA Matrix4 (row-major, translation in row 3) -> Blender Matrix.

    Blender uses column vectors (M @ v), so we transpose."""
    if not m16 or len(m16) != 16:
        return Matrix.Identity(4)
    rows = ((m16[0], m16[1], m16[2], m16[3]),
            (m16[4], m16[5], m16[6], m16[7]),
            (m16[8], m16[9], m16[10], m16[11]),
            (m16[12], m16[13], m16[14], m16[15]))
    return Matrix(rows).transposed()


_TEX_SIDECAR_EXTS = (
    ".dx11.dds", ".dx11.pvr",         # WoTB stores real data here
    ".pvr.dds", ".pvr.pvr",           # older Blitz naming
    ".dds", ".png", ".tga", ".jpg", ".jpeg", ".bmp",
)


def _resolve_texture(dava_path, sc2_dir, data_root):
    """Turn a DAVA texture path into a real file on disk.

    - `~res:/foo/bar.tex`   → resolved against `data_root` (Data/ folder).
    - `foo/bar.tex`         → resolved against `sc2_dir` (the .sc2's own folder).

    Blitz `.tex` is a 22-byte descriptor; the actual pixels live in a sibling
    file, typically `foo.dx11.dds`. Try that first, then a few fallbacks.
    Returns an absolute path or None if nothing was found."""
    if not dava_path:
        return None

    if dava_path.startswith("~res:/") or dava_path.startswith("~res:\\"):
        rel = dava_path[6:].lstrip("/\\")
        bases = [data_root] if data_root else []
    else:
        rel = dava_path.lstrip("/\\")
        bases = [b for b in (sc2_dir, data_root) if b]

    for base in bases:
        candidate = os.path.join(base, rel)
        found = _resolve_sidecar(candidate)
        if found:
            return found
    return None


def _resolve_sidecar(candidate):
    """Given a path that may end in .tex (or already be a real file), return
    the actual on-disk texture, or None."""
    if os.path.isfile(candidate) and not candidate.lower().endswith(".tex"):
        return candidate
    stem, _ = os.path.splitext(candidate)  # strip ".tex" (or any ext)
    for ext in _TEX_SIDECAR_EXTS:
        alt = stem + ext
        if os.path.isfile(alt):
            return alt
    d = os.path.dirname(stem)
    base_lower = os.path.basename(stem).lower()
    if os.path.isdir(d):
        try:
            for entry in os.listdir(d):
                low = entry.lower()
                if not low.startswith(base_lower + "."):
                    continue
                if low.endswith(".tex"):
                    continue
                full = os.path.join(d, entry)
                if os.path.isfile(full):
                    return full
        except OSError:
            pass
    return None


def _find_cached_image(path):
    """Return a previously-decoded Image for this exact source path, or None.

    Keyed on the `wotb_source` custom property (the absolute path) rather than
    the datablock name, so same-named textures from different folders don't
    collide on a single Blender Image."""
    for im in bpy.data.images:
        if im.get("wotb_source") == path:
            return im
    return None


def _load_image(path):
    """Load a texture into a Blender Image. Handles Blitz .pvr via our own
    decoder; everything else goes through Blender's native loaders."""
    if not path:
        return None
    lower = path.lower()
    if lower.endswith(".pvr"):
        cached = _find_cached_image(path)
        if cached is not None:
            return cached
        try:
            w, h, px = pvr.decode_top_mip(path)
        except Exception as e:
            print(f"[wotb_io] PVR decode failed for {path}: {e}")
            return None
        img = bpy.data.images.new(
            name=os.path.basename(path), width=w, height=h, alpha=True,
        )
        img.pixels.foreach_set(px)
        # DAVA/Blitz PVR textures pack shader data (masks, ~0.4 constants) in
        # the alpha channel — it is NOT an opacity mask. Mark the image
        # CHANNEL_PACKED so Blender does not blend the RGB against it and does
        # not render the surface see-through in Solid / EEVEE. The alpha bits
        # stay available as a separate output for anyone who needs them.
        try:
            img.alpha_mode = "CHANNEL_PACKED"
        except (AttributeError, TypeError):
            pass
        img.pack()
        img["wotb_source"] = path
        return img
    try:
        return bpy.data.images.load(path, check_existing=True)
    except Exception:
        return None


def _add_tex_node(nt, image, location, non_color=False):
    node = nt.nodes.new("ShaderNodeTexImage")
    node.image = image
    node.location = location
    if non_color and image is not None:
        try:
            image.colorspace_settings.name = "Non-Color"
        except Exception:
            pass
    return node


def _make_or_reuse_material(scene, matid, mat_cache, sc2_dir, data_root):
    # Normalise the key so a byteArray and its equivalent int don't create two
    # cache entries for the same material.
    key = sc2_reader._int_id(matid)
    if key in mat_cache:
        return mat_cache[key]

    name = sc2_reader.collect_material_name(scene, matid)
    textures = sc2_reader.collect_textures(scene, matid)

    mat = bpy.data.materials.new(name=name)
    mat.use_nodes = True
    nt = mat.node_tree
    bsdf = nt.nodes.get("Principled BSDF")

    # --- albedo ------------------------------------------------------------
    albedo_dava = textures.get("albedo") or textures.get("diffuse")
    albedo_file = _resolve_texture(albedo_dava, sc2_dir, data_root)
    albedo_img = _load_image(albedo_file)
    if albedo_img is not None and bsdf is not None:
        tex = _add_tex_node(nt, albedo_img, (-500, 300))
        nt.links.new(tex.outputs["Color"], bsdf.inputs["Base Color"])
        # NB: DAVA `.Alphatest` fx on tank materials does NOT use the alpha
        # channel as a hole mask — track textures store shader-side data
        # (~0.4 mean, essentially uniform) in it. Wiring it either directly
        # or through a threshold makes tanks disappear. Tanks are opaque:
        # leave BSDF.Alpha at its default 1.0.

    # --- normal map --------------------------------------------------------
    normal_dava = textures.get("normalmap") or textures.get("normal")
    normal_file = _resolve_texture(normal_dava, sc2_dir, data_root)
    normal_img = _load_image(normal_file)
    if normal_img is not None and bsdf is not None and "Normal" in bsdf.inputs:
        n_tex = _add_tex_node(nt, normal_img, (-500, -50), non_color=True)
        nmap = nt.nodes.new("ShaderNodeNormalMap")
        nmap.location = (-200, -50)
        nt.links.new(n_tex.outputs["Color"], nmap.inputs["Color"])
        nt.links.new(nmap.outputs["Normal"], bsdf.inputs["Normal"])

    # --- record all raw DAVA texture references for round-trip / debugging.
    for k, v in textures.items():
        if isinstance(v, str):
            mat[f"wotb_tex_{k}"] = v

    mat_cache[key] = mat
    return mat


def _build_mesh_from_polygroup(scene, pg, name):
    fmt = pg["vertexFormat"]
    vc = pg["vertexCount"]
    ic = pg["indexCount"]
    packing = pg.get("packing", 0)
    if packing != 0:
        raise ValueError(f"Unsupported vertex packing {packing} in {name}")

    streams = vf.parse_vertices(pg["vertices"], fmt, vc)
    indices = vf.parse_indices(pg["indices"], pg["indexFormat"], ic)

    prim_type = pg.get("rhi_primitiveType", 1)  # 1 == TRIANGLELIST
    if prim_type != 1:
        raise ValueError(f"Unsupported primitive type {prim_type}")

    positions = streams.get("position") or []
    if not positions:
        raise ValueError("PolygonGroup missing positions")

    me = bpy.data.meshes.new(name)
    verts = [(p[0], p[1], p[2]) for p in positions]
    tris = [(indices[i], indices[i + 1], indices[i + 2]) for i in range(0, len(indices), 3)]
    me.from_pydata(verts, [], tris)

    # UVs (loops)
    uvs = streams.get("uv0")
    if uvs:
        uv_layer = me.uv_layers.new(name="UVMap")
        for loop in me.loops:
            u, v = uvs[loop.vertex_index]
            # DAVA UVs use top-left origin -> flip V for Blender.
            uv_layer.data[loop.index].uv = (u, 1.0 - v)

    # Normals: apply custom split normals per-vertex, propagated to loops.
    normals = streams.get("normal")
    if normals:
        # Blender expects one normal per loop.
        loop_normals = [normals[l.vertex_index] for l in me.loops]
        me.normals_split_custom_set(loop_normals)

    me.update()
    me.validate(clean_customdata=False)
    return me


def _lod_key(lod):
    """Normalise a batch LOD index to a non-negative int (-1 / None -> 0)."""
    return 0 if lod in (None, -1) else int(lod)


def _batch_uses_shadow_volume(scene, batch):
    """A RenderBatch is a shadow-volume proxy if its material chain resolves
    to `~res:/Materials/ShadowVolume.material` — those meshes are the source
    of the 'duplicate white mesh' the game generates for stencil shadows."""
    matid = batch.get("rb.nmatname")
    if matid is None:
        return False
    for m in sc2_reader.resolve_material_chain(scene, matid):
        fx = m.get("fxName") or ""
        if "ShadowVolume" in fx:
            return True
    return False


class WOTBImporter:
    def __init__(
        self,
        path,
        *,
        import_lods="lod0",
        skip_shadow_volumes=True,
        skip_crash_variants=True,
        generate_collision=True,
        collision_dissolve_deg=18.0,
        collision_rigid_body=True,
        load_armor=True,
        link_collection=None,
        tex_root=None,
    ):
        self.path = path
        self.import_lods = import_lods  # "lod0" | "all"
        self.skip_shadow_volumes = skip_shadow_volumes
        self.skip_crash_variants = skip_crash_variants
        self.generate_collision = generate_collision
        self.collision_dissolve_deg = collision_dissolve_deg
        self.collision_rigid_body = collision_rigid_body
        self.load_armor = load_armor
        self.link_collection = link_collection
        self.sc2_dir = os.path.dirname(os.path.abspath(path))
        self.data_root = tex_root  # the game's `Data/` folder, if known
        self.mat_cache = {}
        self._built_meshes = []
        self.collision_obj = None
        self.armor_root = None
        self._root_collection = None
        self._lod_collections = {}   # lod int -> bpy.types.Collection

    def run(self):
        scene = sc2_reader.load_sc2(self.path)
        collection = self._prepare_collection()
        self._root_collection = collection
        # Build a placeholder root empty for the file's top-level scene node.
        # DAVA typically has 1 root ("MaxScene"); we still make a group.
        file_root = bpy.data.objects.new(os.path.basename(self.path), None)
        file_root.empty_display_type = "PLAIN_AXES"
        collection.objects.link(file_root)

        # For LOD switching we mimic DAVA: keep only LOD 0 visible when
        # import_lods == 'lod0'; otherwise import all LODs but hide non-zero.
        for root_entity in scene.root_entities:
            self._import_entity(scene, root_entity, parent=file_root, collection=collection)

        self.collision_obj = None
        if _ARMOR_COLLISION_ENABLED and self.generate_collision:
            self.collision_obj = self._build_collision_hull(file_root, collection)

        if _ARMOR_COLLISION_ENABLED and self.load_armor:
            try:
                self.armor_root = armor_mod.load_armor(
                    self.path, collection, file_root,
                )
            except Exception as e:
                print(f"[wotb_io] armor: failed: {e}")
                self.armor_root = None

        return scene, file_root

    def _prepare_collection(self):
        if self.link_collection is not None:
            return self.link_collection
        base = os.path.splitext(os.path.basename(self.path))[0]
        col = bpy.data.collections.new(base)
        bpy.context.scene.collection.children.link(col)
        return col

    def _lod_collection(self, lod):
        """Return (creating on first use) the `<file>_LOD<n>` sub-collection
        for this LOD level. Every LOD is imported; LOD1+ collections are hidden
        by default so only LOD0 shows and the levels don't Z-fight."""
        key = _lod_key(lod)
        col = self._lod_collections.get(key)
        if col is None:
            base = os.path.splitext(os.path.basename(self.path))[0]
            col = bpy.data.collections.new(f"{base}_LOD{key}")
            parent = self._root_collection or bpy.context.scene.collection
            parent.children.link(col)
            if key != 0:
                col.hide_viewport = True
                col.hide_render = True
            self._lod_collections[key] = col
        return col

    def _import_entity(self, scene, entity, parent, collection):
        name = entity.get("name") or "Entity"
        if self.skip_crash_variants and "crash" in name.lower():
            # DAVA ships broken-tank fragments (chassis_track_crash_L/R, ...)
            # in the same scene — hidden by the game unless the tank is
            # destroyed. They otherwise overlap normal parts and cause the
            # magenta / Z-fighting shards visible under the hull.
            return
        matrix = _dava_matrix_to_blender(sc2_reader.get_transform_matrix(entity))

        obj = self._build_entity_object(scene, entity, name, collection)
        obj.matrix_local = matrix
        obj.parent = parent
        # Preserve DAVA id / flags for round-trip identification.
        _set_int_prop(obj, "wotb_id", entity.get("id"))
        _set_int_prop(obj, "wotb_flags", entity.get("flags", 0))

        for child in entity.get("__children", ()):
            self._import_entity(scene, child, parent=obj, collection=collection)

    def _build_entity_object(self, scene, entity, name, collection):
        batches = sc2_reader.get_render_batches(entity)
        if self.import_lods == "lod0":
            batches = [b for b in batches if b.get("__lodIndex", 0) == 0]
        if self.skip_shadow_volumes:
            batches = [b for b in batches if not _batch_uses_shadow_volume(scene, b)]

        if not batches:
            # Empty (transform-only) helper — hardpoints (HP_*), markers, etc.
            obj = bpy.data.objects.new(name, None)
            obj.empty_display_type = "PLAIN_AXES"
            obj.empty_display_size = 0.2
            collection.objects.link(obj)
            return obj

        if len(batches) == 1:
            return self._build_batch_object(scene, batches[0], name)

        # Multiple batches — usually the same part at several LOD levels (and
        # occasionally several materials at one LOD). Keep them under one group
        # empty for the entity; each mesh is named <part>_LOD<n> and lands in
        # the matching <file>_LOD<n> collection.
        group = bpy.data.objects.new(name, None)
        group.empty_display_type = "PLAIN_AXES"
        group.empty_display_size = 0.2
        collection.objects.link(group)

        counts = {}
        for b in batches:
            lod = _lod_key(b.get("__lodIndex", 0))
            n = counts.get(lod, 0)
            counts[lod] = n + 1
            sub_name = f"{name}_LOD{lod}" + (f"_b{n}" if n else "")
            sub = self._build_batch_object(scene, b, sub_name)
            sub.parent = group
        return group

    def _build_batch_object(self, scene, batch, name):
        pgid = batch.get("rb.datasource")
        matid = batch.get("rb.nmatname")
        pg = scene.polygroups.get(pgid)
        if pg is None:
            raise ValueError(f"RenderBatch references missing PolygonGroup id={pgid}")

        mesh = _build_mesh_from_polygroup(scene, pg, name)
        obj = bpy.data.objects.new(name, mesh)
        # Link into the per-LOD collection so all LODs import but stay separated.
        lod = _lod_key(batch.get("__lodIndex", 0))
        self._lod_collection(lod).objects.link(obj)

        mat = _make_or_reuse_material(
            scene, matid, self.mat_cache, self.sc2_dir, self.data_root,
        )
        if mat is not None:
            mesh.materials.append(mat)

        _set_int_prop(obj, "wotb_pgid", pgid if pgid is not None else -1)
        _set_int_prop(obj, "wotb_matid", matid if matid is not None else -1)
        _set_int_prop(obj, "wotb_lod", lod)
        self._built_meshes.append(obj)
        return obj

    # ---------------- collision hull ----------------
    def _build_collision_hull(self, file_root, collection):
        # Prefer the game's own hitbox geometry from `CollisionMeshes/` —
        # it's already low-poly and tight around hull+turret with no
        # tracks, no gun, and no belly overshoot. When we have it we take
        # the real triangle mesh so the shell reads as the actual game
        # hitbox (with concavities); Blender's rigid-body CONVEX_HULL
        # shape wraps it back to a convex volume for physics anyway.
        verts, tris = self._collect_collision_source_mesh()
        if verts and tris:
            source = "CollisionMeshes"
            hull_from_mesh = False
        else:
            # Fallback: convex hull of the visual body meshes.
            verts = self._collect_body_vertices()
            tris = None
            source = "visual mesh (fallback)"
            hull_from_mesh = True

        print(f"[wotb_io] collision: {len(verts)} verts, "
              f"{'raw mesh' if tris else 'convex hull'} from {source}")
        if len(verts) < 4:
            print("[wotb_io] collision: not enough vertices, skipping")
            return None

        bm = bmesh.new()
        bm_verts = [bm.verts.new(co) for co in verts]
        bm.verts.ensure_lookup_table()

        if tris is not None:
            for a, b, c in tris:
                try:
                    bm.faces.new((bm_verts[a], bm_verts[b], bm_verts[c]))
                except ValueError:
                    pass
        else:
            bmesh.ops.convex_hull(bm, input=list(bm.verts), use_existing_faces=False)
            loose = [v for v in bm.verts if not v.link_faces]
            if loose:
                bmesh.ops.delete(bm, geom=loose, context="VERTS")

        bmesh.ops.remove_doubles(bm, verts=list(bm.verts), dist=0.001)
        bmesh.ops.recalc_face_normals(bm, faces=list(bm.faces))

        # Merge nearly-coplanar tris into big flat polygons — the "rough
        # shell of large flat faces" the user asked for.
        bmesh.ops.dissolve_limit(
            bm,
            angle_limit=math.radians(self.collision_dissolve_deg),
            use_dissolve_boundaries=False,
            verts=list(bm.verts),
            edges=list(bm.edges),
            delimit=set(),
        )

        face_count = len(bm.faces)
        base = os.path.splitext(os.path.basename(self.path))[0]
        name = f"{base}_collision"
        me = bpy.data.meshes.new(name)
        bm.to_mesh(me)
        bm.free()
        me.update()

        obj = bpy.data.objects.new(name, me)
        collection.objects.link(obj)
        obj.parent = file_root
        obj.display_type = "WIRE"
        obj.show_in_front = True
        obj.hide_render = True
        obj.color = (0.1, 1.0, 0.3, 1.0)
        obj["wotb_collision"] = True

        if self.collision_rigid_body:
            self._try_add_rigid_body(obj)

        print(f"[wotb_io] collision: built '{name}' with {face_count} faces "
              f"({'game hitbox' if not hull_from_mesh else 'convex fallback'})")
        return obj

    # DAVA tanks lay out their scene as a flat set of named sibling entities:
    # `hull`, `turret_*`, `gun_*`, `chassis_*`, `HP_*`, `smoke`, ...
    # We only want the body shell — corpus + turret (all the way to the top).
    _COLLISION_INCLUDE_PREFIXES = ("hull", "turret")
    _COLLISION_EXCLUDE_PREFIXES = (
        "hp_",         # hardpoints (gunfire/exhaust markers)
        "chassis",     # tracks + wheels
        "gun",         # gun barrels
        "smoke",       # smoke launchers
        "wheel", "track",
        "crash",       # broken-tank variants
        "fire", "exhaus",
    )

    def _collect_collision_source_mesh(self):
        """Load the game's hitbox mesh from `CollisionMeshes/`. Returns
        (verts, tris) where verts are in visual-scene space (auto-aligned
        to the visual hull) and tris index into `verts`. `([], [])` if the
        collision file isn't available."""
        scene, entities, z_off = armor_mod.build_collision_source(self.path)
        if not scene or not entities:
            return [], []
        verts = []
        tris = []
        for e in entities:
            m16 = sc2_reader.get_transform_matrix(e)
            if m16 and len(m16) == 16:
                rows = ((m16[0], m16[1], m16[2], m16[3]),
                        (m16[4], m16[5], m16[6], m16[7]),
                        (m16[8], m16[9], m16[10], m16[11]),
                        (m16[12], m16[13], m16[14], m16[15]))
                M = Matrix(rows).transposed()
            else:
                M = Matrix.Identity(4)
            for b in sc2_reader.get_render_batches(e):
                pg = scene.polygroups.get(b.get("rb.datasource"))
                if not pg:
                    continue
                streams = vf.parse_vertices(
                    pg["vertices"], pg["vertexFormat"], pg["vertexCount"],
                )
                indices = vf.parse_indices(
                    pg["indices"], pg["indexFormat"], pg["indexCount"],
                )
                base = len(verts)
                for p in streams.get("position") or []:
                    co = M @ Vector((p[0], p[1], p[2]))
                    verts.append((co.x, co.y, co.z + z_off))
                for i in range(0, len(indices), 3):
                    tris.append(
                        (base + indices[i], base + indices[i + 1], base + indices[i + 2])
                    )
        return verts, tris

    def _collect_body_vertices(self):
        """Return world-space positions for the corpus + turret meshes only.

        We deliberately skip gun barrels, tracks, wheels, hardpoints and the
        `*_crash` broken variants — a good collision shell should hug the
        hull and reach up to the top of the turret, without spikes for the
        cannon or fan-out for the tracks."""
        out = []
        kept = []
        for obj in self._built_meshes:
            if obj.type != "MESH" or obj.data is None:
                continue
            if int(obj.get("wotb_lod", 0)) != 0:
                continue   # only the visible LOD0 shell feeds the fallback hull
            if not self._name_included(obj):
                continue
            kept.append(obj.name)
            M = obj.matrix_world
            for v in obj.data.vertices:
                co = M @ v.co
                out.append((co.x, co.y, co.z))
        if kept:
            print(f"[wotb_io] collision: including "
                  f"{len(kept)} meshes: {kept[:8]}"
                  f"{'…' if len(kept) > 8 else ''}")
        return out

    @classmethod
    def _name_included(cls, obj):
        """Walk obj and its ancestors — first blacklist match wins, then the
        first whitelist match. Missing → skip."""
        cur = obj
        while cur is not None:
            n = (cur.name or "").lower()
            for p in cls._COLLISION_EXCLUDE_PREFIXES:
                if n.startswith(p):
                    return False
            for p in cls._COLLISION_INCLUDE_PREFIXES:
                if n.startswith(p):
                    return True
            cur = cur.parent
        return False

    @staticmethod
    def _try_add_rigid_body(obj):
        """Add a passive rigid body with a convex-hull collision shape so
        this envelope — and not the visible mesh — is what physics uses."""
        scene = bpy.context.scene
        if getattr(scene, "rigidbody_world", None) is None:
            try:
                bpy.ops.rigidbody.world_add()
            except Exception as e:
                print(f"[wotb_io] cannot create rigid body world: {e}")
                return

        vl = bpy.context.view_layer
        old_active = vl.objects.active
        prev_selected = [o for o in vl.objects if o.select_get()]
        try:
            for o in prev_selected:
                o.select_set(False)
            obj.select_set(True)
            vl.objects.active = obj
            bpy.ops.rigidbody.object_add(type="PASSIVE")
            rb = obj.rigid_body
            if rb is not None:
                rb.collision_shape = "CONVEX_HULL"
                # Physics-only: don't move, but block dynamic objects.
                rb.kinematic = False
                rb.friction = 0.6
                rb.restitution = 0.1
        except Exception as e:
            print(f"[wotb_io] rigid body setup skipped: {e}")
        finally:
            obj.select_set(False)
            for o in prev_selected:
                try:
                    o.select_set(True)
                except Exception:
                    pass
            vl.objects.active = old_active
