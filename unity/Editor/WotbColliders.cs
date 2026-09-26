// WoTB collider setup for Unity.
//
// The Blender add-on exports tank collision shells as convex meshes named
// "<file>_collider_hull" / "<file>_collider_turret". On FBX import those come
// in as ordinary GameObjects with a MeshFilter + MeshRenderer, so they would
// render as solid blocks over the tank. This editor tool turns every such
// object into a real collider instead:
//
//   * adds a MeshCollider with Convex = true (so it collides with objects),
//   * removes the MeshRenderer so the shell is invisible,
//   * removes the MeshFilter (the MeshCollider keeps its own mesh reference).
//
// Drop this file anywhere under an "Editor" folder in your Unity project.
// Then select the imported tank(s) in the Hierarchy and run
// Tools > WoTB > Setup Colliders On Selection.

using UnityEditor;
using UnityEngine;

public static class WotbColliders
{
    // Objects whose name contains this token are treated as collider shells.
    private const string ColliderToken = "_collider";

    [MenuItem("Tools/WoTB/Setup Colliders On Selection")]
    private static void SetupOnSelection()
    {
        int converted = 0;

        foreach (GameObject root in Selection.gameObjects)
        {
            // include inactive children too
            foreach (MeshFilter mf in root.GetComponentsInChildren<MeshFilter>(true))
            {
                GameObject go = mf.gameObject;
                if (!go.name.Contains(ColliderToken))
                    continue;

                Mesh mesh = mf.sharedMesh;
                if (mesh == null)
                    continue;

                MeshCollider mc = go.GetComponent<MeshCollider>();
                if (mc == null)
                    mc = Undo.AddComponent<MeshCollider>(go);
                mc.sharedMesh = mesh;
                mc.convex = true;

                MeshRenderer mr = go.GetComponent<MeshRenderer>();
                if (mr != null)
                    Undo.DestroyObjectImmediate(mr);

                // The MeshCollider holds its own reference to the mesh, so the
                // MeshFilter is no longer needed for rendering or collision.
                Undo.DestroyObjectImmediate(mf);

                converted++;
            }
        }

        if (converted == 0)
            Debug.LogWarning(
                "[WoTB] No collider objects found. Select the imported tank " +
                "root(s); collider parts are named with \"" + ColliderToken + "\".");
        else
            Debug.Log($"[WoTB] Set up {converted} convex MeshCollider(s).");
    }

    // ------------------------------------------------------------------
    // Orientation fix.
    //
    // Blender is Z-up, Unity is Y-up. Exporting with Blender's own "Apply
    // Transform" bake fixes the orientation but shatters parented hierarchies
    // (the tank's turret / wheels fly apart), so the add-on leaves it off — the
    // model then imports with a -90° X rotation on the root and looks tipped
    // ("on its rear") in previews / when the root rotation is ignored.
    //
    // The clean fix is Unity's own "Bake Axis Conversion" on the model
    // importer: Unity bakes the axis change into the meshes WITHOUT breaking
    // the hierarchy, so the model stands upright everywhere and the root stays
    // at identity. Select the imported .fbx asset(s) in the Project window (or
    // the scene instances) and run this.
    // ------------------------------------------------------------------

    [MenuItem("Tools/WoTB/Fix Orientation (Bake Axis Conversion)")]
    private static void FixOrientation()
    {
        int fixedCount = 0;
        foreach (Object sel in Selection.objects)
        {
            string path = AssetDatabase.GetAssetPath(sel);
            if (string.IsNullOrEmpty(path) && sel is GameObject go)
            {
                Object src = PrefabUtility.GetCorrespondingObjectFromSource(go);
                if (src != null)
                    path = AssetDatabase.GetAssetPath(src);
            }
            if (string.IsNullOrEmpty(path))
                continue;

            var imp = AssetImporter.GetAtPath(path) as ModelImporter;
            if (imp == null)
                continue;

            imp.bakeAxisConversion = true;
            imp.SaveAndReimport();
            fixedCount++;
        }

        if (fixedCount == 0)
            Debug.LogWarning(
                "[WoTB] Select the imported tank .fbx asset(s) in the Project " +
                "window (or their scene instances), then run this again.");
        else
            Debug.Log($"[WoTB] Baked axis conversion on {fixedCount} model(s) — " +
                      "they should now stand upright.");
    }
}
