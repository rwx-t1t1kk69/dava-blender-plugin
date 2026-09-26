"""Blender importer for WoTB 3.10 hangar / battle-map scenes (.sc2).

Handles what tank scenes never have: Landscape entities (heightmap-tessellated
terrain), LightComponents, ParticleEffectComponents (imported as marker
empties with per-emitter positions), and SoundComponents (imported as marker
empties). Mesh + material loading is otherwise the same shape as the tank
importer, minus the tank-only bits (armor, collision hull, crash variants).
"""
import math
import os
import bpy
from mathutils import Matrix

from . import sc2_reader
from . import vertex_format as vf
from . import pvr
from . import heightmap as hm
from . import anim as anim_mod
from . import particles as particles_mod


_C_INT_MIN = -(1 << 31)
_C_INT_MAX = (1 << 31) - 1


def _set_int_prop(obj, key, val):
    if val is None:
        return
    v = int(val)
    if _C_INT_MIN <= v <= _C_INT_MAX:
        obj[key] = v
    else:
        obj[key] = str(v)


def _dava_matrix_to_blender(m16):
    if not m16 or len(m16) != 16:
        return Matrix.Identity(4)
    rows = ((m16[0], m16[1], m16[2], m16[3]),
            (m16[4], m16[5], m16[6], m16[7]),
            (m16[8], m16[9], m16[10], m16[11]),
            (m16[12], m16[13], m16[14], m16[15]))
    return Matrix(rows).transposed()


_TEX_SIDECAR_EXTS = (
    ".dx11.dds", ".dx11.pvr",
    ".pvr.dds", ".pvr.pvr",
    ".dds", ".png", ".tga", ".jpg", ".jpeg", ".bmp",
)


def _resolve_texture(dava_path, sc2_dir, data_root):
    if not dava_path:
        return None
    if dava_path.startswith("~res:/") or dava_path.startswith("~res:\\"):
        rel = dava_path[6:].lstrip("/\\")
        bases = [data_root] if data_root else []
    else:
        rel = dava_path.lstrip("/\\")
        bases = [b for b in (sc2_dir, data_root) if b]
    for base in bases:
        found = _resolve_sidecar(os.path.join(base, rel))
        if found:
            return found
    return None


def _resolve_sidecar(candidate):
    if os.path.isfile(candidate) and not candidate.lower().endswith(".tex"):
        return candidate
    stem, _ = os.path.splitext(candidate)
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
        if (im.get("wotb_source") == path
                and im.get("wotb_pvr_ver") == pvr.DECODE_VERSION):
            return im
    return None


def _load_image(path):
    if not path:
        return None
    if path.lower().endswith(".pvr"):
        cached = _find_cached_image(path)
        if cached is not None:
            return cached
        try:
            w, h, px = pvr.decode_top_mip(path)
        except Exception as e:
            print(f"[wotb_scene_io] PVR decode failed for {path}: {e}")
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
        img["wotb_pvr_ver"] = pvr.DECODE_VERSION
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
    # cache entries for the same material (also lets a byteArray matid pass
    # through — previously a landscape `int(matid)` crashed on byteArrays).
    key = sc2_reader._int_id(matid)
    if key in mat_cache:
        return mat_cache[key]

    name = sc2_reader.collect_material_name(scene, matid)
    textures = sc2_reader.collect_textures(scene, matid)

    mat = bpy.data.materials.new(name=name)
    mat.use_nodes = True
    nt = mat.node_tree
    bsdf = nt.nodes.get("Principled BSDF")

    albedo_dava = (
        textures.get("albedo")
        or textures.get("diffuse")
        or textures.get("colormap")     # landscape tile
        or textures.get("cubemap")      # skybox
    )
    albedo_file = _resolve_texture(albedo_dava, sc2_dir, data_root)
    albedo_img = _load_image(albedo_file)
    if albedo_img is not None and bsdf is not None:
        tex = _add_tex_node(nt, albedo_img, (-500, 300))
        nt.links.new(tex.outputs["Color"], bsdf.inputs["Base Color"])

    normal_dava = textures.get("normalmap") or textures.get("normal")
    normal_file = _resolve_texture(normal_dava, sc2_dir, data_root)
    normal_img = _load_image(normal_file)
    if normal_img is not None and bsdf is not None and "Normal" in bsdf.inputs:
        n_tex = _add_tex_node(nt, normal_img, (-500, -50), non_color=True)
        nmap = nt.nodes.new("ShaderNodeNormalMap")
        nmap.location = (-200, -50)
        nt.links.new(n_tex.outputs["Color"], nmap.inputs["Color"])
        nt.links.new(nmap.outputs["Normal"], bsdf.inputs["Normal"])

    for k, v in textures.items():
        if isinstance(v, str):
            mat[f"wotb_tex_{k}"] = v

    mat_cache[key] = mat
    return mat


def _build_mesh_from_polygroup(pg, name):
    fmt = pg["vertexFormat"]
    vc = pg["vertexCount"]
    ic = pg["indexCount"]
    if pg.get("packing", 0) != 0:
        raise ValueError(f"Unsupported vertex packing in {name}")
    streams = vf.parse_vertices(pg["vertices"], fmt, vc)
    indices = vf.parse_indices(pg["indices"], pg["indexFormat"], ic)
    if pg.get("rhi_primitiveType", 1) != 1:
        raise ValueError(f"Unsupported primitive type in {name}")

    positions = streams.get("position") or []
    if not positions:
        raise ValueError(f"PolygonGroup missing positions in {name}")

    me = bpy.data.meshes.new(name)
    verts = [(p[0], p[1], p[2]) for p in positions]
    tris = [(indices[i], indices[i + 1], indices[i + 2])
            for i in range(0, len(indices), 3)]
    me.from_pydata(verts, [], tris)

    uvs = streams.get("uv0")
    if uvs:
        uv_layer = me.uv_layers.new(name="UVMap")
        for loop in me.loops:
            u, v = uvs[loop.vertex_index]
            uv_layer.data[loop.index].uv = (u, 1.0 - v)

    normals = streams.get("normal")
    if normals:
        loop_normals = [normals[l.vertex_index] for l in me.loops]
        me.normals_split_custom_set(loop_normals)

    me.update()
    me.validate(clean_customdata=False)
    return me


def _batch_uses_shadow_volume(scene, batch):
    matid = batch.get("rb.nmatname")
    if matid is None:
        return False
    for m in sc2_reader.resolve_material_chain(scene, matid):
        fx = m.get("fxName") or ""
        if "ShadowVolume" in fx:
            return True
    return False


# ------------- component readers unique to hangar / map -------------

def _get_component(entity, typename):
    comps = entity.get("components")
    if not isinstance(comps, dict):
        return None
    for v in comps.values():
        if isinstance(v, dict) and v.get("comp.typename") == typename:
            return v
    return None


def _get_light(entity):
    lc = _get_component(entity, "LightComponent")
    return lc.get("lc.light") if isinstance(lc, dict) else None


def _get_particle_emitters(entity):
    pe = _get_component(entity, "ParticleEffectComponent")
    if not isinstance(pe, dict):
        return None
    emitters = pe.get("pe.emitters")
    if not isinstance(emitters, dict):
        return None
    out = []
    for _, e in emitters.items():
        if not isinstance(e, dict):
            continue
        pos = e.get("emitter.position") or (0.0, 0.0, 0.0)
        name = e.get("emitter.filename") or "emitter"
        out.append((name, pos))
    return out


def _get_sound_events(entity):
    sc = _get_component(entity, "SoundComponent")
    if not isinstance(sc, dict):
        return None
    count = int(sc.get("sc.eventCount", 0) or 0)
    return count if count > 0 else None


def _get_animation_ref(entity):
    """Return (anim_id_int, time_scale, repeats) or None."""
    ac = _get_component(entity, "AnimationComponent")
    if not isinstance(ac, dict):
        return None
    aid = ac.get("animation")
    if aid is None:
        return None
    return (int(aid),
            float(ac.get("animationTimeScale", 1.0)),
            int(ac.get("repeatsCount", 1)))


def _get_landscape_render(entity):
    """Returns dict {hmap, matid, bbox_bytes} or None."""
    rc = _get_component(entity, "RenderComponent")
    if not isinstance(rc, dict):
        return None
    ro = rc.get("rc.renderObj")
    if not isinstance(ro, dict) or ro.get("##name") != "Landscape":
        return None
    return {
        "hmap": ro.get("hmap"),
        "matid": ro.get("matname"),
        "bbox": ro.get("bbox"),
    }


def _get_render_batches_from_mesh(entity):
    """Only returns batches when the RenderObject is a plain Mesh (not
    Landscape / SkyBox / Particle etc.)."""
    rc = _get_component(entity, "RenderComponent")
    if not isinstance(rc, dict):
        return []
    ro = rc.get("rc.renderObj")
    if not isinstance(ro, dict):
        return []
    if ro.get("##name") not in (None, "Mesh"):
        return []
    batches = ro.get("ro.batches")
    if not isinstance(batches, dict):
        return []
    out = []
    for _, bv in batches.items():
        if not isinstance(bv, dict):
            continue
        if bv.get("##name") in ("RenderBatch", None):
            out.append(bv)
    for i, b in enumerate(out):
        b["__lodIndex"] = ro.get(f"rb{i}.lodIndex", 0)
        b["__switchIndex"] = ro.get(f"rb{i}.switchIndex", -1)
    return out


# DAVA LightComponent light type ids (from LightComponent::Light::eType).
# Verified in Blitz assets: hangar's ambient sky uses type 2.
_LIGHT_TYPES = {
    0: "SUN",       # directional (LT_DIRECTIONAL)
    1: "POINT",     # point       (LT_POINT)
    2: "SUN",       # sky / ambient — closest Blender fit is a Sun light
    3: "SPOT",      # spot        (LT_SPOT)
}


class WOTBSceneImporter:
    def __init__(
        self,
        path,
        *,
        import_lods="lod0",
        skip_shadow_volumes=True,
        skip_shadow_meshes=True,
        import_landscape=True,
        import_lights=True,
        import_particle_markers=True,
        import_sound_markers=True,
        import_animations=True,
        light_energy_scale=0.01,
        emit_lamp_lights=True,
        lamp_light_energy=250.0,
        lamp_light_color=(1.0, 0.85, 0.62),
        import_smoke=True,
        link_collection=None,
        tex_root=None,
    ):
        self.path = path
        self.import_lods = import_lods
        self.skip_shadow_volumes = skip_shadow_volumes
        self.skip_shadow_meshes = skip_shadow_meshes
        self.import_landscape = import_landscape
        self.import_lights = import_lights
        self.import_particle_markers = import_particle_markers
        self.import_sound_markers = import_sound_markers
        self.import_animations = import_animations
        self.light_energy_scale = light_energy_scale
        self.emit_lamp_lights = emit_lamp_lights
        self.lamp_light_energy = lamp_light_energy
        self.lamp_light_color = tuple(lamp_light_color)
        self.import_smoke = import_smoke
        self.link_collection = link_collection
        self.sc2_dir = os.path.dirname(os.path.abspath(path))
        self.data_root = tex_root
        self.mat_cache = {}
        self._anim_index = {}     # int id -> AnimationData node
        self._last_anim_frame = 1

        self.counts = {
            "meshes": 0, "materials": 0, "landscapes": 0,
            "lights": 0, "particles": 0, "sounds": 0, "empties": 0,
            "animations": 0, "lamp_lights": 0, "smoke": 0,
        }

    def run(self):
        scene = sc2_reader.load_sc2(self.path)
        collection = self._prepare_collection()
        if self.import_animations:
            self._anim_index = anim_mod.index_animations(scene)

        file_root = bpy.data.objects.new(os.path.basename(self.path), None)
        file_root.empty_display_type = "PLAIN_AXES"
        collection.objects.link(file_root)

        for root_entity in scene.root_entities:
            self._import_entity(scene, root_entity,
                                parent=file_root, collection=collection)

        self.counts["materials"] = len(self.mat_cache)
        # Extend the scene's playback range if we baked in longer animations
        # than the current end frame — user still opens on frame 1.
        if self._last_anim_frame > 1:
            sc = bpy.context.scene
            if self._last_anim_frame > sc.frame_end:
                sc.frame_end = self._last_anim_frame
        return scene, file_root

    def _prepare_collection(self):
        if self.link_collection is not None:
            return self.link_collection
        base = os.path.splitext(os.path.basename(self.path))[0]
        col = bpy.data.collections.new(base)
        bpy.context.scene.collection.children.link(col)
        return col

    def _import_entity(self, scene, entity, parent, collection):
        name = entity.get("name") or "Entity"

        # Blob-shadow planes DAVA drops under many objects (`*_shadow`).
        # They overlap the real shadow the engine casts and read as dark
        # squares under Blender's own lighting.
        if self.skip_shadow_meshes and name.lower().endswith("_shadow"):
            return

        matrix = _dava_matrix_to_blender(sc2_reader.get_transform_matrix(entity))
        obj = self._build_entity_object(scene, entity, name, collection)
        obj.matrix_local = matrix
        obj.parent = parent

        _set_int_prop(obj, "wotb_id", entity.get("id"))
        _set_int_prop(obj, "wotb_flags", entity.get("flags", 0))

        # Animation last: it overrides loc/rot/scale via F-curves. matrix_local
        # above still seeds the un-keyframed frames sensibly.
        self._maybe_apply_animation(entity, obj)

        for child in entity.get("__children", ()):
            self._import_entity(scene, child, parent=obj, collection=collection)

    def _maybe_apply_animation(self, entity, obj):
        if not self.import_animations or not self._anim_index:
            return
        ref = _get_animation_ref(entity)
        if not ref:
            return
        aid, tscale, repeats = ref
        node = self._anim_index.get(aid)
        if node is None:
            print(f"[wotb_scene_io] anim: id {aid} missing for {obj.name}")
            return
        try:
            _action, last = anim_mod.apply_to_object(
                obj, node, time_scale=tscale, repeats=repeats,
                action_name=f"{obj.name}_anim",
            )
        except Exception as e:
            print(f"[wotb_scene_io] anim: failed on {obj.name}: {e}")
            return
        if last:
            self._last_anim_frame = max(self._last_anim_frame, last)
            self.counts["animations"] += 1
        obj["wotb_anim_id"] = aid
        obj["wotb_anim_time_scale"] = tscale
        obj["wotb_anim_repeats"] = str(repeats)  # uint32 can overflow int prop

    def _build_entity_object(self, scene, entity, name, collection):
        # 1. Landscape wins over Mesh — RenderObject.##name switches on it.
        land = _get_landscape_render(entity)
        if land is not None:
            if self.import_landscape:
                landobj = self._build_landscape(scene, land, name, collection)
                if landobj is not None:
                    return landobj
            return self._make_empty(name, collection)

        # 2. Regular renderable mesh.
        batches = _get_render_batches_from_mesh(entity)
        if self.import_lods == "lod0":
            batches = [b for b in batches if b.get("__lodIndex", 0) in (0, -1)]
        if self.skip_shadow_volumes:
            batches = [b for b in batches
                       if not _batch_uses_shadow_volume(scene, b)]

        if batches:
            if len(batches) == 1:
                obj = self._build_batch_object(
                    scene, batches[0], name, collection,
                )
            else:
                group = self._make_empty(name, collection)
                for i, b in enumerate(batches):
                    sub = self._build_batch_object(
                        scene, b,
                        f"{name}_lod{b.get('__lodIndex', 0)}_b{i}",
                        collection,
                    )
                    sub.parent = group
                    if b.get("__lodIndex", 0) not in (0, -1):
                        sub.hide_set(True)
                        sub.hide_render = True
                obj = group
            self._attach_side_components(entity, obj, collection)
            return obj

        # 3. Light: only if the entity carries no visible geometry — otherwise
        # we already returned a real object above.
        light = _get_light(entity) if self.import_lights else None
        if light is not None:
            lobj = self._build_light(name, light, collection)
            self._attach_side_components(entity, lobj, collection)
            return lobj

        # 4. Empty transform node (may still host particles / sound / children).
        obj = self._make_empty(name, collection)
        self._attach_side_components(entity, obj, collection)
        return obj

    def _make_empty(self, name, collection):
        obj = bpy.data.objects.new(name, None)
        obj.empty_display_type = "PLAIN_AXES"
        obj.empty_display_size = 0.5
        collection.objects.link(obj)
        self.counts["empties"] += 1
        return obj

    def _build_batch_object(self, scene, batch, name, collection):
        pgid = batch.get("rb.datasource")
        matid = batch.get("rb.nmatname")
        pg = scene.polygroups.get(pgid)
        if pg is None:
            raise ValueError(
                f"RenderBatch references missing PolygonGroup id={pgid}"
            )
        mesh = _build_mesh_from_polygroup(pg, name)
        obj = bpy.data.objects.new(name, mesh)
        collection.objects.link(obj)

        mat = _make_or_reuse_material(
            scene, matid, self.mat_cache, self.sc2_dir, self.data_root,
        )
        if mat is not None:
            mesh.materials.append(mat)

        _set_int_prop(obj, "wotb_pgid", pgid if pgid is not None else -1)
        _set_int_prop(obj, "wotb_matid", matid if matid is not None else -1)
        _set_int_prop(obj, "wotb_lod", batch.get("__lodIndex", 0))
        self.counts["meshes"] += 1
        return obj

    # ---------------- landscape ----------------
    def _build_landscape(self, scene, land, name, collection):
        hmap_rel = land.get("hmap")
        bbox = hm.bbox_from_bytes(land.get("bbox"))
        if not hmap_rel or bbox is None:
            print(f"[wotb_scene_io] landscape: missing hmap/bbox in {name}")
            return None
        hmap_path = _resolve_hmap(hmap_rel, self.sc2_dir, self.data_root)
        loaded = hm.load_heightmap(hmap_path) if hmap_path else None
        if loaded is None:
            print(f"[wotb_scene_io] landscape: heightmap "
                  f"not found: {hmap_rel}")
            return None
        width, samples, bits = loaded
        verts, tris, uvs = hm.build_grid(width, samples, bits, bbox)

        me = bpy.data.meshes.new(name)
        me.from_pydata(verts, [], tris)
        uv_layer = me.uv_layers.new(name="UVMap")
        for loop in me.loops:
            u, v = uvs[loop.vertex_index]
            uv_layer.data[loop.index].uv = (u, 1.0 - v)
        me.update()
        me.validate(clean_customdata=False)

        matid = land.get("matid")
        if matid is not None:
            mat = _make_or_reuse_material(
                scene, matid, self.mat_cache, self.sc2_dir, self.data_root,
            )
            if mat is not None:
                me.materials.append(mat)

        obj = bpy.data.objects.new(name, me)
        collection.objects.link(obj)
        obj["wotb_landscape"] = True
        obj["wotb_hmap"] = str(hmap_rel)
        obj["wotb_bbox"] = list(bbox)
        self.counts["landscapes"] += 1
        return obj

    # ---------------- light ----------------
    def _build_light(self, name, light, collection):
        ltype = int(light.get("type", 0))
        blender_type = _LIGHT_TYPES.get(ltype, "SUN")
        data = bpy.data.lights.new(name=name, type=blender_type)
        r = float(light.get("color.r", 1.0))
        g = float(light.get("color.g", 1.0))
        b = float(light.get("color.b", 1.0))
        try:
            data.color = (r, g, b)
        except Exception:
            pass
        intensity = float(light.get("intensity", 1.0))
        # DAVA intensity is a shader multiplier — 300 for the hangar sky is
        # totally normal there, but plugged into Blender/Unity lights as-is
        # it blows the scene out (Unity's FBX importer converts light
        # intensity directly). Scale by `light_energy_scale` so 300 → 3.0
        # by default, then let the user override.
        scaled = intensity * float(self.light_energy_scale)
        try:
            data.energy = scaled
        except Exception:
            pass
        obj = bpy.data.objects.new(name, data)
        collection.objects.link(obj)
        obj["wotb_light_type"] = ltype
        obj["wotb_light_intensity_raw"] = intensity
        obj["wotb_light_energy_scale"] = float(self.light_energy_scale)
        self.counts["lights"] += 1
        return obj

    # -------- particles: lamps → lights, grates → smoke, rest → markers -----
    def _resolve_particle_yaml(self, fname):
        """DAVA `emitter.filename` is relative to the .sc2 (or ~res:/Data).
        Walk the usual spots and return an existing .yaml path or None."""
        if not fname:
            return None
        f = fname.replace("\\", "/")
        cands = []
        if f.startswith("~res:/"):
            rel = f[6:].lstrip("/")
            if self.data_root:
                cands.append(os.path.join(self.data_root, rel))
        else:
            cands.append(os.path.normpath(os.path.join(self.sc2_dir, f)))
            if self.data_root:
                cands.append(os.path.join(self.data_root, f.lstrip("./")))
        for c in cands:
            if os.path.isfile(c):
                return c
        return None

    def _fx_params(self, fname):
        """(yaml_rel, params_dict) for an emitter, or (fname, None)."""
        yml = self._resolve_particle_yaml(fname)
        data = particles_mod.parse_particle_yaml(yml) if yml else None
        params = (particles_mod.effect_params(data, yml, self.data_root)
                  if data is not None else None)
        return fname, params

    def _attach_side_components(self, entity, parent_obj, collection):
        emitters = _get_particle_emitters(entity)
        if emitters:
            for i, (fname, pos) in enumerate(emitters):
                kind = particles_mod.classify_emitter(fname)
                yaml_rel, params = self._fx_params(fname)

                # Lamp → SPOT light (down), exports to Unity via FBX as a
                # real light (the Unity editor script also rebuilds these).
                if kind == "glow" and self.emit_lamp_lights:
                    lamp = particles_mod.make_lamp_light(
                        f"{parent_obj.name}_lamp_{i:02d}",
                        self.lamp_light_energy, self.lamp_light_color,
                        collection,
                    )
                    lamp.location = pos
                    lamp.parent = parent_obj
                    lamp["wotb_particle_emitter"] = str(fname)
                    self.counts["lamp_lights"] += 1
                    # + a glow FX marker (Unity script spawns the animated glow)
                    particles_mod.make_fx_marker(
                        f"WOTBFX_glow_{self.counts['lamp_lights']:03d}",
                        "glow", pos, parent_obj, collection, params, yaml_rel)
                    continue

                # Floor grate → smoke FX marker (Unity script spawns rising steam).
                if kind == "smoke" and self.import_smoke:
                    self.counts["smoke"] += 1
                    particles_mod.make_fx_marker(
                        f"WOTBFX_smoke_{self.counts['smoke']:03d}",
                        "smoke", pos, parent_obj, collection, params, yaml_rel)
                    continue

                # Everything else → generic marker Empty.
                if self.import_particle_markers:
                    self.counts["particles"] += 1
                    particles_mod.make_fx_marker(
                        f"WOTBFX_fx_{self.counts['particles']:03d}",
                        "other", pos, parent_obj, collection, params, yaml_rel)
        if self.import_sound_markers:
            count = _get_sound_events(entity)
            if count:
                m = bpy.data.objects.new(f"{parent_obj.name}_snd", None)
                m.empty_display_type = "SINGLE_ARROW"
                m.empty_display_size = 0.3
                m.parent = parent_obj
                m["wotb_sound_events"] = int(count)
                collection.objects.link(m)
                self.counts["sounds"] += 1


def _resolve_hmap(rel, sc2_dir, data_root):
    """Heightmaps live next to the .sc2 (hangar) or under a landscape/
    subfolder (battle maps). Walk a few likely spots."""
    if not rel:
        return None
    candidates = [
        os.path.join(sc2_dir, rel),
        os.path.join(sc2_dir, "landscape", rel),
        os.path.join(sc2_dir, "landscape", os.path.basename(rel)),
        os.path.join(sc2_dir, os.path.basename(rel)),
    ]
    if data_root:
        candidates.append(os.path.join(data_root, rel))
    for c in candidates:
        if os.path.isfile(c):
            return c
    return None
