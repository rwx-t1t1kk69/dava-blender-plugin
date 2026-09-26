# dava-blender-plugin

Blender 4.2+ add-on for importing **World of Tanks Blitz 3.10** content stored
in the DAVA Engine `SceneFileV2` (`.sc2`) format — **tanks and hangars/maps in
a single plugin** — plus a Unity-ready FBX export path.

Both former add-ons (`wotb_io` for tanks, `wotb_scene_io` for hangars/maps)
have been merged into one extension in [`wotb_io/`](wotb_io/), sharing a common
DAVA parsing core instead of duplicating it.

## Features

| Menu | What it does |
|------|--------------|
| `File > Import > WoTB 3.10 Tank (.sc2)` | Tank models: meshes, materials, hardpoints, all LODs split into per-LOD collections (`<file>_LOD0`, `_LOD1`, … — LOD1+ hidden by default), a single convex collider wrapping hull + turret for Unity, crash-part filtering. Armor overlay exists but is temporarily disabled in code. |
| `File > Import > WoTB 3.10 Hangar / Map (.sc2)` | Scenes: meshes, materials, heightmap landscape, lights, lamp/smoke/particle & sound marker empties, baked animations |
| `File > Export > WoTB Scene → Unity (.fbx)` | Y-up / -Z-forward FBX carrying `wotb_*` custom props and `WOTBFX_*` markers for the Unity editor script |

### Supported formats

- **Scene:** `SFV2` version 21 (WoTB 3.10)
- **Textures:** Blitz `.pvr` (PVR3 uncompressed RGBA4444 / RGB565); `.dds`,
  `.png`, `.tga`, etc. via Blender's native loaders
- **Heightmaps:** DAVA `.heightmap` (8/16/32-bit)

## Layout

```
wotb_io/
├── __init__.py            add-on entry (register/unregister)
├── blender_manifest.toml  extension metadata
├── ops.py                 operators + File menu entries (2 import, 1 export)
│
├── ka.py                  DAVA KeyedArchive binary parser          ┐
├── sc2_reader.py          SceneFileV2 (.sc2) tree parser           │ shared
├── vertex_format.py       EVF_* interleaved vertex decoding        │ DAVA
├── pvr.py                 PVR3 texture decoder                     │ core
├── heightmap.py           .heightmap tessellation                  │
├── anim.py                AnimationData → Blender F-curves          │
├── particles.py           ParticleEffectComponent → lights/markers ┘
│
├── importer_tank.py       tank importer (WOTBImporter)
├── armor.py               armor overlay + collision from CollisionMeshes
└── importer_scene.py      hangar/map importer (WOTBSceneImporter)
```

### Unity helpers (`unity/Editor/`)

- **`WotbColliders.cs`** — after importing a tank FBX, select it in the
  Hierarchy and run `Tools > WoTB > Setup Colliders On Selection`. It turns
  every `*_collider` object into a convex `MeshCollider` and strips its
  renderer, so the tank collides with the world without drawing the collision
  shells.
- **`WotbHangarFx.cs`** *(not in this repo yet)* — the hangar/map round-trip
  relies on a companion script that rebuilds animated ParticleSystems and
  lights from the exported `WOTBFX_*` markers.

## Install

1. Zip the `wotb_io/` folder (the one containing `blender_manifest.toml`), or
   point Blender at it directly.
2. Blender → `Edit > Preferences > Get Extensions > Install from Disk…` → pick
   the zip.
3. Enable the add-on. The three menu entries appear under `File > Import` /
   `File > Export`.

**Textures:** keep the extracted game `Data/` tree intact. The importer walks
up from the `.sc2` to find the `Data` folder so `~res:/...` paths resolve; you
can turn this off and rely on the file's own folder.

## Notes & limitations

- Targets WoTB **3.10** specifically. Newer client versions change the `.sc2`
  version, may pack vertices, and ship compressed textures (PVRTC/ETC2/ASTC)
  that this decoder does not handle.
- PVR decoding and heightmap tessellation run in pure Python — large battle
  maps can be slow to import.
- The particle YAML reader is intentionally minimal.

## License

MIT — see [`LICENSE`](LICENSE).
