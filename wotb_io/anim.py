"""DAVA `AnimationData` decoder + Blender F-curve application.

An `AnimationComponent` on an entity carries three fields:
    animation           uint32 — id of an `AnimationData` data node
    animationTimeScale  float
    repeatsCount        uint32 — 0xFFFFFFFF = loop forever

An `AnimationData` node is a flat KA that unrolls a keyframe track as
individually named entries:
    duration     float
    keyCount     uint32
    invPose      float32[16]
    key_<N>_time         float
    key_<N>_translation  vec3
    key_<N>_rotation     vec4 (x, y, z, w)
    key_<N>_scale        vec3
for N in 0 .. keyCount-1.
"""
import re


_KEY_RE = re.compile(r"^key_(\d+)_(time|translation|rotation|scale)$")

REPEAT_INFINITE = 0xFFFFFFFF


def index_animations(scene):
    """Return {int_id: node} for every AnimationData data node in `scene`.

    IDs match `AnimationComponent.animation` when interpreted as the
    little-endian int of the node's 8-byte `#id` field."""
    out = {}
    for n in scene.data_nodes:
        if n.get("##name") != "AnimationData":
            continue
        nid = n.get("#id")
        if isinstance(nid, (bytes, bytearray)):
            out[int.from_bytes(nid, "little")] = n
        elif nid is not None:
            out[int(nid)] = n
    return out


def decode_animation(node):
    """Turn a flat AnimationData KA into a sorted list of TRS keys.

    Returns dict:
        duration: float
        keys: list of dicts with time, T (vec3), R (quat x,y,z,w), S (vec3)
    """
    keys_by_idx = {}
    for k, v in node.items():
        if not isinstance(k, str):
            continue
        m = _KEY_RE.match(k)
        if not m:
            continue
        i = int(m.group(1))
        prop = m.group(2)
        keys_by_idx.setdefault(i, {})[prop] = v
    keys = []
    for i in sorted(keys_by_idx):
        d = keys_by_idx[i]
        t = d.get("time")
        T = d.get("translation")
        R = d.get("rotation")
        S = d.get("scale")
        if t is None or T is None or R is None or S is None:
            # Incomplete key — DAVA never emits these in the wild, but be safe.
            continue
        keys.append({"time": float(t),
                     "T": tuple(float(x) for x in T),
                     "R": tuple(float(x) for x in R),  # (x, y, z, w)
                     "S": tuple(float(x) for x in S)})
    return {"duration": float(node.get("duration", 0.0)), "keys": keys}


def apply_to_object(obj, node, *, time_scale=1.0, repeats=1,
                    fps=None, action_name=None):
    """Bake an AnimationData onto `obj` as Blender F-curves.

    Requires bpy at call time (this function is meant to run inside Blender).
    Returns (action, last_frame) — the created bpy.types.Action and the
    last keyframed frame number. Frame numbers are 1-based (Blender)."""
    import bpy
    if fps is None:
        fps = bpy.context.scene.render.fps

    data = decode_animation(node)
    keys = data["keys"]
    if not keys:
        return None, 0

    obj.rotation_mode = "QUATERNION"

    if obj.animation_data is None:
        obj.animation_data_create()
    action = bpy.data.actions.new(
        name=action_name or f"{obj.name}_anim",
    )
    obj.animation_data.action = action

    # Pre-create the 3+4+3 F-curves so we can push samples via foreach_set /
    # keyframe_points.add — simpler and faster than obj.keyframe_insert.
    def fc(path, count):
        return [action.fcurves.new(data_path=path, index=i) for i in range(count)]

    fc_loc = fc("location", 3)
    fc_rot = fc("rotation_quaternion", 4)
    fc_scl = fc("scale", 3)

    tscale = time_scale if time_scale and time_scale > 0 else 1.0
    # Blender frames are 1-indexed; time 0 → frame 1.
    def t_to_frame(t):
        return 1.0 + (t / tscale) * fps

    for fcurve in (*fc_loc, *fc_rot, *fc_scl):
        fcurve.keyframe_points.add(len(keys))

    last_frame = 1.0
    for i, k in enumerate(keys):
        f = t_to_frame(k["time"])
        last_frame = max(last_frame, f)
        T = k["T"]; R = k["R"]; S = k["S"]
        # DAVA quaternion (x, y, z, w) → Blender (w, x, y, z).
        qw = R[3]; qx = R[0]; qy = R[1]; qz = R[2]
        for j, val in enumerate(T):
            p = fc_loc[j].keyframe_points[i]
            p.co = (f, val); p.interpolation = "LINEAR"
        for j, val in enumerate((qw, qx, qy, qz)):
            p = fc_rot[j].keyframe_points[i]
            p.co = (f, val); p.interpolation = "LINEAR"
        for j, val in enumerate(S):
            p = fc_scl[j].keyframe_points[i]
            p.co = (f, val); p.interpolation = "LINEAR"

    for fcurve in (*fc_loc, *fc_rot, *fc_scl):
        fcurve.update()
        if repeats != 1:
            # Cyclic modifier gives us free wrap-around whether repeats is
            # a finite count or the DAVA 'infinite' sentinel.
            mod = fcurve.modifiers.new("CYCLES")
            mod.mode_before = "NONE"
            mod.mode_after = "REPEAT"
            if repeats != REPEAT_INFINITE:
                # Number of repeats AFTER the first play.
                mod.cycles_after = max(int(repeats) - 1, 0)

    return action, int(round(last_frame))
