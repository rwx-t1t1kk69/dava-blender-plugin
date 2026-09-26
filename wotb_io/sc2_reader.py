"""DAVA Engine SceneFileV2 (.sc2) parser.

File layout observed in WoTB 3.10 (version 21):
    Header (12 bytes):
        char[4]  signature  = "SFV2"
        uint32   version
        uint32   nodeCount   (number of root scene nodes)
    KA versionInfo           (usually empty)
    uint32 descSize          (typically 4)
    uint32 fileType          (0 = full scene)
    uint32 dataNodeCount
    dataNodeCount * KA       (NMaterial, PolygonGroup, ...)
    nodeCount * SceneNode    (recursive: KA with #childrenCount children)
"""
from . import ka


def _int_id(v):
    """Convert a #id / material-key value to an int.

    DAVA stores the node `#id` as an 8-byte little-endian byteArray, but the
    *references* to it (`rb.nmatname`, `parentMaterialKey`, ...) may come back
    as a uint32/uint64 int OR as the same raw byteArray depending on the field.
    Normalise both shapes to a plain int so dict lookups always match.
    Returns None for None (an absent reference)."""
    if v is None:
        return None
    if isinstance(v, (bytes, bytearray)):
        return int.from_bytes(v, "little")
    return int(v)


def _entity_children_count(ent):
    return int(ent.get("#childrenCount", 0))


class SC2Scene:
    def __init__(self):
        self.version = 0
        self.data_nodes = []       # list of KA dicts (raw)
        self.materials = {}        # id -> KA
        self.polygroups = {}       # id -> KA
        self.root_entities = []    # list of parsed entity trees

    def entities_flat(self):
        out = []
        def rec(e):
            out.append(e)
            for c in e.get("__children", ()):
                rec(c)
        for r in self.root_entities:
            rec(r)
        return out


def _parse_entity(data, off):
    ent, sz = ka.parse_ka_at(data, off)
    ent["__off"] = off
    ent["__size"] = sz
    ent["__children"] = []
    cur = off + sz
    for _ in range(_entity_children_count(ent)):
        child, cur = _parse_entity(data, cur)
        ent["__children"].append(child)
    return ent, cur


def parse_sc2(data: bytes) -> SC2Scene:
    r = ka.Reader(data)
    sig = r.raw(4)
    if sig != b"SFV2":
        raise ValueError(f"Not an SFV2 file: {sig!r}")
    version = r.u32()
    node_count = r.u32()

    scene = SC2Scene()
    scene.version = version

    # Version info KA (may be empty)
    _, sz = ka.parse_ka_at(data, r.off); r.off += sz

    desc_size = r.u32()
    file_type = r.u32()  # noqa: F841
    if desc_size > 4:
        # skip descriptor extras
        r.raw(desc_size - 4)

    data_node_count = r.u32()
    for _ in range(data_node_count):
        node, sz = ka.parse_ka_at(data, r.off); r.off += sz
        scene.data_nodes.append(node)
        cls = node.get("##name")
        nid = _int_id(node.get("#id", 0))
        if cls == "NMaterial":
            scene.materials[nid] = node
        elif cls == "PolygonGroup":
            scene.polygroups[nid] = node

    for _ in range(node_count):
        ent, r.off = _parse_entity(data, r.off)
        scene.root_entities.append(ent)

    return scene


def load_sc2(path: str) -> SC2Scene:
    with open(path, "rb") as f:
        return parse_sc2(f.read())


# ---------- helpers to walk parsed entities ----------

def get_transform_matrix(entity):
    """Return the local 4x4 matrix (list of 16 floats, row-major) or None."""
    comps = entity.get("components")
    if not isinstance(comps, dict):
        return None
    for k, v in comps.items():
        if not isinstance(v, dict):
            continue
        if v.get("comp.typename") == "TransformComponent":
            m = v.get("tc.localMatrix")
            if m and len(m) == 16:
                return m
    return None


def get_render_batches(entity):
    """Return list of render batch dicts (may be empty)."""
    comps = entity.get("components")
    if not isinstance(comps, dict):
        return []
    for k, v in comps.items():
        if not isinstance(v, dict):
            continue
        if v.get("comp.typename") != "RenderComponent":
            continue
        ro = v.get("rc.renderObj")
        if not isinstance(ro, dict):
            continue
        batches = ro.get("ro.batches")
        if not isinstance(batches, dict):
            continue
        out = []
        for bk, bv in batches.items():
            if not isinstance(bv, dict):
                continue
            if bv.get("##name") in ("RenderBatch", None):
                out.append(bv)
        # Attach LOD info
        for i, b in enumerate(out):
            b["__lodIndex"] = ro.get(f"rb{i}.lodIndex", 0)
            b["__switchIndex"] = ro.get(f"rb{i}.switchIndex", -1)
        return out
    return []


def get_lod_distances(entity):
    """Return the LodComponent distances dict or None."""
    comps = entity.get("components")
    if not isinstance(comps, dict):
        return None
    for k, v in comps.items():
        if not isinstance(v, dict):
            continue
        if v.get("comp.typename") == "LodComponent":
            return v.get("lc.loddist")
    return None


def resolve_material_chain(scene, matid):
    """Return list of NMaterial dicts from leaf to root along parent chain.

    `matid` and each `parentMaterialKey` are normalised through `_int_id` so a
    byteArray-encoded reference still matches the int-keyed `scene.materials`
    map (previously a byteArray key silently broke the chain and dropped all
    textures)."""
    chain = []
    seen = set()
    cur = _int_id(matid)
    while cur and cur not in seen:
        seen.add(cur)
        m = scene.materials.get(cur)
        if not m:
            break
        chain.append(m)
        pk = m.get("parentMaterialKey", 0)
        cur = _int_id(pk) if pk else None
    return chain


def collect_textures(scene, matid):
    """Merge 'textures' from the material chain (leaf overrides root)."""
    tex = {}
    for m in reversed(resolve_material_chain(scene, matid)):
        t = m.get("textures")
        if isinstance(t, dict):
            for k, v in t.items():
                if k == "__ka_meta__" or k == "__dupes__":
                    continue
                if isinstance(v, str):
                    tex[k] = v
    return tex


def collect_material_name(scene, matid):
    chain = resolve_material_chain(scene, matid)
    for m in chain:
        n = m.get("materialName")
        if n:
            return n
    return f"material_{_int_id(matid)}"
