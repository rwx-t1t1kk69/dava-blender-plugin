"""Blender operators + menu entries for the unified WoTB 3.10 DAVA I/O add-on.

Three operators, all under File > Import-Export:
    * WOTB_OT_import_sc2         — tank models (armor / collision / crash)
    * WOTB_OT_import_scene_sc2   — hangar / battle-map scenes
    * WOTB_OT_export_unity_fbx   — export the scene to a Unity-ready .fbx
"""
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
            "file (e.g. `chassis_track_crash_L/R`). They're hidden by the "
            "game unless the tank is destroyed — but they overlap the normal "
            "tracks and cause the magenta / Z-fighting shards you see under "
            "the hull"
        ),
        default=True,
    )

    load_armor: BoolProperty(
        name="Load armor overlay",
        description=(
            "Also import the matching collision mesh from "
            "`CollisionMeshes/<nation>-<tank>.sc2`, painted with a "
            "green→yellow→red heatmap by plate thickness (mm). The overlay "
            "sits over the hull, is hidden from render, and each part is "
            "tagged with `wotb_armor_min_mm` / `wotb_armor_max_mm`"
        ),
        default=True,
    )

    generate_collision: BoolProperty(
        name="Generate collision hull",
        description=(
            "Build a simplified convex-hull shell of the whole tank made of "
            "large flat polygons — used for physics collisions instead of the "
            "detailed visual mesh"
        ),
        default=True,
    )

    collision_dissolve_deg: FloatProperty(
        name="Collision merge angle (°)",
        description=(
            "Merge convex-hull triangles whose normals differ by less than "
            "this angle into single flat polygons. Higher = fewer, bigger "
            "faces (rougher shell)"
        ),
        default=18.0,
        min=1.0,
        max=45.0,
    )

    collision_rigid_body: BoolProperty(
        name="Add rigid body physics",
        description=(
            "Also mark the collision shell as a passive rigid body with a "
            "convex-hull shape, so Blender physics uses it instead of the "
            "visible mesh"
        ),
        default=True,
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
        col_obj = getattr(wotb, "collision_obj", None)
        if self.generate_collision:
            if col_obj is not None:
                col_msg = f", collision '{col_obj.name}' ({len(col_obj.data.polygons)} faces)"
            else:
                col_msg = ", collision: SKIPPED (see system console)"
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
            # Unity wants Y-up / -Z forward. We deliberately do NOT
            # bake_space_transform — it can corrupt baked animations (the
            # 9May hangar planes) and normals. Unity applies its own import
            # rotation and the prefab lands upright anyway.
            axis_up="Y",
            axis_forward="-Z",
            apply_scale_options="FBX_SCALE_ALL",
            bake_space_transform=False,
            path_mode="AUTO",
        )
        try:
            bpy.ops.export_scene.fbx(**kwargs)
        except TypeError:
            safe = {k: v for k, v in kwargs.items() if k in (
                "filepath", "use_selection", "object_types",
                "use_custom_props", "bake_anim", "axis_up", "axis_forward",
            )}
            bpy.ops.export_scene.fbx(**safe)
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
