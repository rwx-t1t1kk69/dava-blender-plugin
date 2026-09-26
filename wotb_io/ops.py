"""Blender operators + menu entries for the unified WoTB 3.10 DAVA I/O add-on.

Three operators, all under File > Import-Export:
    * WOTB_OT_import_sc2         — tank models (armor / collision / crash)
    * WOTB_OT_import_scene_sc2   — hangar / battle-map scenes
    * WOTB_OT_export_unity_fbx   — export the scene to a Unity-ready .fbx
"""
import math
import os
import bpy
from bpy.props import StringProperty, EnumProperty, BoolProperty, FloatProperty
from bpy_extras.io_utils import ImportHelper, ExportHelper


def _guess_tex_root(path):
    """Walk up from the .sc2 file to find the game's `Data` folder, so
    `~res:/...` texture paths resolve. Falls back to the file's own folder."""
    p = os.path.abspath(path)
    for _ in range(8):
        p = os.path.dirname(p)
        if not p or len(p) < 3:
            break
        if os.path.basename(p).lower() == "data" and os.path.isdir(p):
            return p
    return os.path.dirname(os.path.abspath(path))


# ---------------------------------------------------------------------------
# Export-time "upright" bake (the Blender→Unity rotate/apply trick).
#
# Blender is Z-up, Unity Y-up. Rather than bake_space_transform (which shatters
# parented hierarchies) we rotate the whole model -90° X, apply the rotation
# into the mesh data, then rotate +90° X back — the well-known trick that makes
# Unity import the model upright with an identity root while keeping the
# hierarchy. The whole scene is snapshotted (vertex positions + object
# transforms) before and restored after export, so nothing is left changed.
# ---------------------------------------------------------------------------

def _find_view3d_ctx(context):
    for window in context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == "VIEW_3D":
                region = next((r for r in area.regions if r.type == "WINDOW"), None)
                if region is not None:
                    return {"window": window, "area": area, "region": region}
    return None


def _snapshot(objs):
    basis = {o: o.matrix_basis.copy() for o in objs}
    pinv = {o: o.matrix_parent_inverse.copy() for o in objs}
    meshes = {}
    for o in objs:
        me = o.data
        if o.type == "MESH" and me is not None and me not in meshes:
            arr = [0.0] * (len(me.vertices) * 3)
            me.vertices.foreach_get("co", arr)
            meshes[me] = arr
    return basis, pinv, meshes


def _restore_snapshot(basis, pinv, meshes):
    for me, arr in meshes.items():
        me.vertices.foreach_set("co", arr)
        me.update()
    for o, m in pinv.items():
        o.matrix_parent_inverse = m
    for o, m in basis.items():
        o.matrix_basis = m


def _bake_upright(context, objs):
    """Rotate/apply so the export is upright in Unity. Returns a restore
    callback, or None if it could not run (export proceeds un-baked)."""
    ov = _find_view3d_ctx(context)
    if ov is None:
        print("[wotb_io] upright bake: no 3D viewport found, exporting as-is")
        return None

    objs = [o for o in objs if o.type in {"MESH", "EMPTY"}]
    if not objs:
        return None

    ts = context.scene.tool_settings
    vl = context.view_layer
    basis, pinv, meshes = _snapshot(objs)
    obj_hide = [(o, o.hide_get(), o.hide_select, o.hide_viewport) for o in objs]
    coll_hide = [(c, c.hide_viewport) for c in bpy.data.collections]
    prev_pivot = ts.transform_pivot_point
    prev_active = vl.objects.active
    prev_sel = [o for o in vl.objects if o.select_get()]

    def restore():
        _restore_snapshot(basis, pinv, meshes)
        try:
            ts.transform_pivot_point = prev_pivot
        except Exception:
            pass
        for c, hv in coll_hide:
            c.hide_viewport = hv
        for o, hg, hs, hv in obj_hide:
            o.hide_viewport = hv
            o.hide_select = hs
            try:
                o.hide_set(hg)
            except Exception:
                pass
        for o in vl.objects:
            try:
                o.select_set(o in prev_sel)
            except Exception:
                pass
        vl.objects.active = prev_active
        context.view_layer.update()

    try:
        for c, _ in coll_hide:
            c.hide_viewport = False
        for o, *_ in obj_hide:
            o.hide_viewport = False
            o.hide_select = False
            try:
                o.hide_set(False)
            except Exception:
                pass
        context.view_layer.update()

        with context.temp_override(**ov):
            if context.object and context.object.mode != "OBJECT":
                bpy.ops.object.mode_set(mode="OBJECT")
            bpy.ops.object.select_all(action="DESELECT")
            for o in objs:
                o.select_set(True)
            vl.objects.active = objs[0]
            ts.transform_pivot_point = "INDIVIDUAL_ORIGINS"
            bpy.ops.transform.rotate(value=math.radians(90),
                                     orient_axis="X", orient_type="GLOBAL")
            bpy.ops.object.transform_apply(rotation=True, location=False,
                                           scale=False)
            bpy.ops.transform.rotate(value=math.radians(-90),
                                     orient_axis="X", orient_type="GLOBAL")
        context.view_layer.update()
        return restore
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"[wotb_io] upright bake failed, exporting as-is: {e}")
        try:
            restore()
        except Exception:
            pass
        return None


# ---------------------------------------------------------------------------
# Tank model import
# ---------------------------------------------------------------------------

class WOTB_OT_import_sc2(bpy.types.Operator, ImportHelper):
    """Import a WoTB 3.10 tank scene (.sc2)."""
    bl_idname = "wotb.import_sc2"
    bl_label = "Import WoTB Tank (.sc2)"
    bl_options = {"REGISTER", "UNDO"}

    filename_ext = ".sc2"
    filter_glob: StringProperty(default="*.sc2", options={"HIDDEN"})

    import_lods: EnumProperty(
        name="LODs",
        description="Which levels of detail to import",
        items=[
            ("all", "All LODs", "Import every LOD, split into per-LOD "
                                "collections (<file>_LOD0, _LOD1, …); LOD1+ "
                                "are hidden by default"),
            ("lod0", "LOD 0 only", "Highest quality only"),
        ],
        default="all",
    )

    guess_tex_root: BoolProperty(
        name="Guess texture root",
        description=(
            "Walk up from the .sc2 file to find the 'Data' folder and use it "
            "as the texture root for `~res:/...` paths"
        ),
        default=True,
    )

    skip_shadow_volumes: BoolProperty(
        name="Skip shadow-volume meshes",
        description=(
            "DAVA stores extruded stencil-shadow proxy geometry inside the "
            "same file as a second RenderBatch per entity — that's the "
            "white duplicate that appears next to the textured mesh. "
            "Skip it by default"
        ),
        default=True,
    )

    skip_crash_variants: BoolProperty(
        name="Skip broken (crash) parts",
        description=(
            "DAVA ships broken-track / broken-chassis fragments in the same "
            "file (e.g. `chassis_track_crash_L/R`). By default they ARE "
            "imported, into a hidden `<file>_crash` collection you can toggle "
            "on to see the destroyed variant — so they don't overlap the intact "
            "parts. Enable this only to drop them entirely"
        ),
        default=False,
    )

    load_armor: BoolProperty(
        name="Load armor overlay",
        description=(
            "Import the matching `CollisionMeshes/<nation>-<tank>.sc2` hitbox "
            "as an armor overlay. Per-vertex plate thickness (mm) is baked into "
            "a UV channel (`armor_mm`) for a Unity armor-inspection shader, plus "
            "a green→yellow→red heatmap for Blender preview. Each part is tagged "
            "`wotb_armor` with `wotb_armor_min_mm` / `wotb_armor_max_mm`"
        ),
        default=True,
    )

    generate_collision: BoolProperty(
        name="Generate collision",
        description=(
            "Build a single convex collider that wraps the hull and the turret "
            "together, from the visual hull/turret meshes (gun, tracks and "
            "wheels excluded). Named `<file>_collider`, hidden from render, and "
            "tagged for a Unity MeshCollider (Convex)"
        ),
        default=True,
    )

    collision_dissolve_deg: FloatProperty(
        name="Collision merge angle (°)",
        description=(
            "Merge convex-hull triangles whose normals differ by less than "
            "this angle into single flat polygons. Higher = fewer, bigger "
            "faces (lighter collider)"
        ),
        default=12.0,
        min=1.0,
        max=45.0,
    )

    collision_rigid_body: BoolProperty(
        name="Add Blender rigid body",
        description=(
            "Also mark each collider piece as a passive rigid body with a "
            "convex-hull shape for Blender's own physics. Off by default — the "
            "colliders are meant for Unity"
        ),
        default=False,
    )

    def execute(self, context):
        from . import importer_tank as importer

        tex_root = _guess_tex_root(self.filepath) if self.guess_tex_root else None
        try:
            wotb = importer.WOTBImporter(
                self.filepath,
                import_lods=self.import_lods,
                skip_shadow_volumes=self.skip_shadow_volumes,
                skip_crash_variants=self.skip_crash_variants,
                generate_collision=self.generate_collision,
                collision_dissolve_deg=self.collision_dissolve_deg,
                collision_rigid_body=self.collision_rigid_body,
                load_armor=self.load_armor,
                tex_root=tex_root,
            )
            scene, root_obj = wotb.run()
        except Exception as e:
            import traceback
            traceback.print_exc()
            self.report({"ERROR"}, f"Import failed: {e}")
            return {"CANCELLED"}

        col_msg = ""
        if self.generate_collision:
            summary = getattr(wotb, "collision_summary", "") or "SKIPPED (see console)"
            col_msg = f", collision: {summary}"
        armor_msg = ""
        armor_root = getattr(wotb, "armor_root", None)
        if self.load_armor:
            if armor_root is not None:
                armor_msg = f", armor '{armor_root.name}' ({len(armor_root.children)} plates)"
            else:
                armor_msg = ", armor: NOT FOUND"
        self.report(
            {"INFO"},
            f"Imported {len(scene.polygroups)} polygroups, "
            f"{len(scene.materials)} materials{col_msg}{armor_msg}",
        )
        return {"FINISHED"}


# ---------------------------------------------------------------------------
# Hangar / battle-map scene import
# ---------------------------------------------------------------------------

class WOTB_OT_import_scene_sc2(bpy.types.Operator, ImportHelper):
    """Import a WoTB 3.10 hangar or battle-map scene (.sc2)."""
    bl_idname = "wotb.import_scene_sc2"
    bl_label = "Import WoTB Hangar / Map (.sc2)"
    bl_options = {"REGISTER", "UNDO"}

    filename_ext = ".sc2"
    filter_glob: StringProperty(default="*.sc2", options={"HIDDEN"})

    import_lods: EnumProperty(
        name="LODs",
        description="Which levels of detail to import",
        items=[
            ("lod0", "LOD 0 only", "Highest quality only (recommended)"),
            ("all", "All LODs", "Import every LOD; non-zero LODs are hidden"),
        ],
        default="lod0",
    )

    guess_tex_root: BoolProperty(
        name="Guess texture root",
        description=(
            "Walk up from the .sc2 file to find the 'Data' folder and use it "
            "as the texture root for `~res:/...` paths"
        ),
        default=True,
    )

    skip_shadow_volumes: BoolProperty(
        name="Skip shadow-volume meshes",
        description=(
            "Skip RenderBatches whose material chain resolves to "
            "`~res:/Materials/ShadowVolume.material` — those are the "
            "stencil-shadow proxies that read as white duplicates"
        ),
        default=True,
    )

    skip_shadow_meshes: BoolProperty(
        name="Skip *_shadow blob planes",
        description=(
            "DAVA pastes a dark ground-shadow quad under many hangar props "
            "(e.g. `g_a_shadow`). They overlap the real shadow Blender will "
            "cast and read as ugly dark cards"
        ),
        default=True,
    )

    import_landscape: BoolProperty(
        name="Import landscape",
        description=(
            "Tessellate the entity's Landscape RenderComponent from its "
            "`.heightmap` file, using the AABB from the RenderObject"
        ),
        default=True,
    )

    import_lights: BoolProperty(
        name="Import lights",
        description=(
            "Convert LightComponents into Blender lights. Intensity is "
            "copied verbatim from DAVA (unspecified units), retune if needed"
        ),
        default=True,
    )

    emit_lamp_lights: BoolProperty(
        name="Lights from lamps",
        description=(
            "The hangar lamps are just meshes + a `glow` particle effect — "
            "they carry no real LightComponent, so an imported hangar is "
            "unlit. Place a warm Blender SPOT light (pointing down) at every "
            "glow / big_glow emitter so the scene is lit by its own lamps. "
            "They export into Unity as real Point/Spot lights via FBX"
        ),
        default=True,
    )

    lamp_light_energy: FloatProperty(
        name="Lamp light energy",
        description="Watts for each synthesised lamp SPOT light",
        default=250.0,
        min=0.0,
        max=100000.0,
    )

    import_smoke: BoolProperty(
        name="Smoke markers (floor grates)",
        description=(
            "Drop an FX marker Empty (`WOTBFX_smoke_*`) at each smoke emitter "
            "carrying its sprite/size/color. FBX can't hold particles, so the "
            "bundled Unity Editor script `unity/Editor/WotbHangarFx.cs` turns "
            "these markers into real animated ParticleSystems (rising steam) "
            "after import"
        ),
        default=True,
    )

    import_particle_markers: BoolProperty(
        name="Import other particle markers",
        description=(
            "For particle effects that aren't lamps or smoke, create marker "
            "Empties (`WOTBFX_fx_*`) at the emitter position, tagged with the "
            "YAML path / sprite in custom properties"
        ),
        default=True,
    )

    import_sound_markers: BoolProperty(
        name="Import sound markers",
        description=(
            "Create arrow Empties for SoundComponents, tagged with the "
            "event count in `wotb_sound_events`"
        ),
        default=False,
    )

    import_animations: BoolProperty(
        name="Import animations",
        description=(
            "Bake DAVA `AnimationComponent` keyframes into Blender F-curves "
            "on the entity object (location / rotation_quaternion / scale). "
            "In the 9May hangar this animates the flyover planes. Playback "
            "range is extended if the clip is longer than the scene end frame"
        ),
        default=True,
    )

    light_energy_scale: FloatProperty(
        name="Light energy scale",
        description=(
            "DAVA stores light `intensity` as a raw shader multiplier "
            "(hangar sky = 300). Blender/Unity lights treat energy as "
            "physical units, so 300 blows out the scene. Multiplier applied "
            "on import — the default 0.01 turns 300 → 3.0, which reads as "
            "a normal Sun. Original value is saved in `wotb_light_intensity_raw`"
        ),
        default=0.01,
        min=0.0001,
        max=10.0,
        precision=4,
    )

    def execute(self, context):
        from . import importer_scene as importer

        tex_root = _guess_tex_root(self.filepath) if self.guess_tex_root else None
        try:
            wotb = importer.WOTBSceneImporter(
                self.filepath,
                import_lods=self.import_lods,
                skip_shadow_volumes=self.skip_shadow_volumes,
                skip_shadow_meshes=self.skip_shadow_meshes,
                import_landscape=self.import_landscape,
                import_lights=self.import_lights,
                import_particle_markers=self.import_particle_markers,
                import_sound_markers=self.import_sound_markers,
                import_animations=self.import_animations,
                light_energy_scale=self.light_energy_scale,
                emit_lamp_lights=self.emit_lamp_lights,
                lamp_light_energy=self.lamp_light_energy,
                import_smoke=self.import_smoke,
                tex_root=tex_root,
            )
            scene, root_obj = wotb.run()
        except Exception as e:
            import traceback
            traceback.print_exc()
            self.report({"ERROR"}, f"Import failed: {e}")
            return {"CANCELLED"}

        c = wotb.counts
        self.report(
            {"INFO"},
            f"Imported {c['meshes']} meshes, {c['materials']} materials, "
            f"{c['landscapes']} landscape, {c['lights']} lights, "
            f"{c['lamp_lights']} lamp lights, {c['smoke']} smoke markers, "
            f"{c['particles']} other FX markers, {c['sounds']} sound markers, "
            f"{c['animations']} animations",
        )
        return {"FINISHED"}


# ---------------------------------------------------------------------------
# Unity FBX export
# ---------------------------------------------------------------------------

class WOTB_OT_export_unity_fbx(bpy.types.Operator, ExportHelper):
    """Export the current scene to a Unity-ready .fbx.

    Meshes + empty markers (`WOTBFX_*`) + baked object animations. The FX
    markers keep their names through FBX, so the Unity editor script
    `WotbHangarFx.cs` can build Spot lights and animated ParticleSystems on
    them after import. Lights are exported too (Unity may or may not use them;
    the script rebuilds the lamps regardless).
    """
    bl_idname = "wotb.export_unity_fbx"
    bl_label = "Export WoTB Scene → Unity (.fbx)"
    bl_options = {"REGISTER"}

    filename_ext = ".fbx"
    filter_glob: StringProperty(default="*.fbx", options={"HIDDEN"})

    selection_only: BoolProperty(
        name="Selected only",
        description="Export only selected objects. Off = whole scene",
        default=False,
    )

    include_lights: BoolProperty(
        name="Include lights",
        description="Also export Blender lights (Unity import of FBX lights "
                    "is unreliable — the Unity script rebuilds lamps anyway)",
        default=True,
    )

    apply_transform: BoolProperty(
        name="Upright for Unity",
        description=(
            "Bake the Z-up → Y-up orientation so the model stands upright in "
            "Unity with an identity root (rotate -90° X → apply → rotate +90° X "
            "trick). Keeps the parented hierarchy intact, unlike the FBX "
            "'Apply Transform' bake. The scene is snapshotted and restored "
            "around the export, so nothing is left changed here"
        ),
        default=True,
    )

    def execute(self, context):
        types = {"EMPTY", "MESH"}
        if self.include_lights:
            types.add("LIGHT")
        kwargs = dict(
            filepath=self.filepath,
            use_selection=self.selection_only,
            object_types=types,
            use_custom_props=True,          # carry wotb_* props as FBX user props
            bake_anim=True,
            bake_anim_use_all_bones=False,
            add_leaf_bones=False,
            mesh_smooth_type="FACE",
            # Unity wants Y-up / -Z forward. With apply_transform (Apply
            # Transform / bake_space_transform) the axis conversion is baked
            # into the vertices, so the Unity root stays at identity and asset
            # previews stand upright. It is exposed as an option because baking
            # can corrupt baked object animations, so animated hangar exports
            # should turn it off.
            axis_up="Y",
            axis_forward="-Z",
            apply_scale_options="FBX_SCALE_ALL",
            # Orientation is handled by our own snapshot-safe upright bake below
            # (bake_space_transform shatters parented hierarchies).
            bake_space_transform=False,
            path_mode="AUTO",
        )

        restore = None
        if self.apply_transform:
            if self.selection_only:
                objs = list(context.selected_objects)
            else:
                objs = list(context.scene.objects)
            restore = _bake_upright(context, objs)

        try:
            try:
                bpy.ops.export_scene.fbx(**kwargs)
            except TypeError:
                safe = {k: v for k, v in kwargs.items() if k in (
                    "filepath", "use_selection", "object_types",
                    "use_custom_props", "bake_anim", "axis_up", "axis_forward",
                )}
                bpy.ops.export_scene.fbx(**safe)
        finally:
            if restore is not None:
                restore()

        self.report({"INFO"}, f"Wrote {self.filepath}")
        return {"FINISHED"}


# ---------------------------------------------------------------------------
# Menu registration
# ---------------------------------------------------------------------------

def _menu_import(self, context):
    self.layout.operator(
        WOTB_OT_import_sc2.bl_idname,
        text="WoTB 3.10 Tank (.sc2)",
    )
    self.layout.operator(
        WOTB_OT_import_scene_sc2.bl_idname,
        text="WoTB 3.10 Hangar / Map (.sc2)",
    )


def _menu_export(self, context):
    self.layout.operator(
        WOTB_OT_export_unity_fbx.bl_idname,
        text="WoTB Scene → Unity (.fbx)",
    )


_classes = (
    WOTB_OT_import_sc2,
    WOTB_OT_import_scene_sc2,
    WOTB_OT_export_unity_fbx,
)


def register():
    for c in _classes:
        bpy.utils.register_class(c)
    bpy.types.TOPBAR_MT_file_import.append(_menu_import)
    bpy.types.TOPBAR_MT_file_export.append(_menu_export)


def unregister():
    bpy.types.TOPBAR_MT_file_export.remove(_menu_export)
    bpy.types.TOPBAR_MT_file_import.remove(_menu_import)
    for c in reversed(_classes):
        bpy.utils.unregister_class(c)
