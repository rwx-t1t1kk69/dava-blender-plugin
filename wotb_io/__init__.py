"""WoTB 3.10 DAVA I/O for Blender — tanks + hangars/maps in one add-on.

Imports DAVA Engine SceneFileV2 (.sc2) content from World of Tanks Blitz 3.10:

    File > Import > WoTB 3.10 Tank (.sc2)          — tank models (armor,
                                                     collision hull, crash-part
                                                     filtering)
    File > Import > WoTB 3.10 Hangar / Map (.sc2)  — hangar / battle-map scenes
                                                     (landscape, lights,
                                                     particle & sound markers,
                                                     baked animations)
    File > Export > WoTB Scene → Unity (.fbx)      — Unity-ready FBX export

This is a Blender 4.2+ extension; metadata lives in `blender_manifest.toml`.
"""


def register():
    from . import ops
    ops.register()


def unregister():
    from . import ops
    ops.unregister()


if __name__ == "__main__":
    register()
