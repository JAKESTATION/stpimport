bl_info = {
    "name": "STEP Importer (OCP)",
    "author": "Custom",
    "version": (0, 1, 0),
    "blender": (3, 6, 0),
    "location": "File > Import > STEP (.step/.stp)",
    "description": "Import STEP CAD files (AP203/AP214/AP242) using the OCP OpenCASCADE kernel",
    "category": "Import-Export",
}

import sys
import os
import glob
import subprocess

import bpy
from bpy.props import StringProperty, FloatProperty, BoolProperty
from bpy.types import Operator, AddonPreferences
from bpy_extras.io_utils import ImportHelper

# ---------------------------------------------------------------------------
# Dependency management
#
# We deliberately do NOT pip-install into Blender's own Python. On distros
# like Arch (PEP 668 "externally managed environment"), that's blocked by
# default, and even where it's allowed it pollutes a system/shared Python
# with a ~200MB CAD kernel + VTK. Instead we create a private venv next to
# the addon's config and splice its site-packages onto sys.path at runtime.
# ---------------------------------------------------------------------------

def get_blender_python():
    # From Blender 2.92+ sys.executable points at Blender's bundled/system Python.
    return sys.executable


def get_venv_dir():
    return os.path.join(os.path.expanduser("~"), ".step_importer_ocp_venv")


def get_venv_python(venv_dir):
    if sys.platform == "win32":
        return os.path.join(venv_dir, "Scripts", "python.exe")
    return os.path.join(venv_dir, "bin", "python")


def get_venv_site_packages(venv_dir):
    if sys.platform == "win32":
        candidate = os.path.join(venv_dir, "Lib", "site-packages")
        return candidate if os.path.isdir(candidate) else None
    matches = glob.glob(os.path.join(venv_dir, "lib", "python*", "site-packages"))
    return matches[0] if matches else None


def activate_venv_if_present():
    """If a previously-installed venv exists, add it to sys.path."""
    venv_dir = get_venv_dir()
    site_packages = get_venv_site_packages(venv_dir)
    if site_packages and site_packages not in sys.path:
        sys.path.insert(0, site_packages)
    return site_packages is not None


OCP_AVAILABLE = False
OCP_IMPORT_ERROR = ""
activate_venv_if_present()
try:
    from OCP.STEPControl import STEPControl_Reader
    from OCP.IFSelect import IFSelect_RetDone
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopAbs import TopAbs_FACE, TopAbs_SOLID, TopAbs_REVERSED
    from OCP.TopoDS import TopoDS
    from OCP.BRepMesh import BRepMesh_IncrementalMesh
    from OCP.BRep import BRep_Tool
    from OCP.TopLoc import TopLoc_Location
    from OCP.Interface import Interface_Static

    OCP_AVAILABLE = True
except ImportError as e:
    OCP_IMPORT_ERROR = str(e)


class STEPIMPORT_OT_install_ocp(Operator):
    bl_idname = "step_import_ocp.install_dependency"
    bl_label = "Install OCP Dependency"
    bl_description = (
        "Creates a private virtual environment and installs 'cadquery-ocp' "
        "(OpenCASCADE bindings) into it, without touching Blender's or the "
        "system's Python. Requires internet access and a restart of Blender "
        "afterwards"
    )

    def execute(self, context):
        py = get_blender_python()
        venv_dir = get_venv_dir()
        try:
            if not os.path.isdir(venv_dir):
                subprocess.run([py, "-m", "venv", venv_dir],
                                check=True, capture_output=True, text=True)

            venv_python = get_venv_python(venv_dir)
            if not os.path.exists(venv_python):
                raise RuntimeError(
                    f"venv created but no Python found at {venv_python}"
                )

            subprocess.run([venv_python, "-m", "pip", "install", "--upgrade", "pip"],
                            check=True, capture_output=True, text=True)
            # cadquery-ocp-novtk is the same OCCT wrapper, minus VTK/matplotlib and
            # their dependency chain -- this addon never uses VTK, so this trims a
            # ~150MB unused dependency and shrinks the attack/audit surface.
            subprocess.run([venv_python, "-m", "pip", "install", "cadquery-ocp-novtk"],
                            check=True, capture_output=True, text=True)
        except subprocess.CalledProcessError as e:
            msg = e.stderr[-800:] if e.stderr else str(e)
            self.report({'ERROR'}, f"Install failed: {msg}")
            return {'CANCELLED'}
        except Exception as e:
            self.report({'ERROR'}, f"Install failed: {e}")
            return {'CANCELLED'}

        self.report({'INFO'},
                     f"OCP installed into {venv_dir}. Please restart Blender to load it.")
        return {'FINISHED'}


class STEPIMPORT_AddonPreferences(AddonPreferences):
    bl_idname = __name__

    def draw(self, context):
        layout = self.layout
        venv_dir = get_venv_dir()
        if OCP_AVAILABLE:
            layout.label(text="OCP is installed and ready.", icon='CHECKMARK')
            layout.label(text=f"Venv location: {venv_dir}")
        else:
            box = layout.box()
            box.label(text="OCP is not installed — STEP import is disabled.", icon='ERROR')
            if OCP_IMPORT_ERROR:
                box.label(text=f"Import error: {OCP_IMPORT_ERROR}")
            box.operator(STEPIMPORT_OT_install_ocp.bl_idname, icon='IMPORT')
            col = box.column()
            col.label(text="Notes:")
            col.label(text="- Installs into a private venv (not Blender's or the system Python).")
            col.label(text=f"- Venv location: {venv_dir}")
            col.label(text="- Needs internet access to reach PyPI.")
            col.label(text="- The venv's Python version matches Blender's, so wheels always match.")
            col.label(text="- If install fails, check Window > Toggle System Console for the pip log.")
            if os.path.isdir(venv_dir):
                col.label(text="- A venv dir already exists but OCP failed to import from it —")
                col.label(text="  it may be partially installed or Blender needs a restart.")


# ---------------------------------------------------------------------------
# Core: OCP shape -> Blender mesh
# ---------------------------------------------------------------------------

def tessellate_solid(solid_shape, manual_scale=1.0):
    """Walk the faces of a solid's triangulation and return (verts, tris)."""
    verts = []
    tris = []

    exp_face = TopExp_Explorer(solid_shape, TopAbs_FACE)
    while exp_face.More():
        face = TopoDS.Face_s(exp_face.Current())
        loc = TopLoc_Location()
        tri_data = BRep_Tool.Triangulation_s(face, loc)
        if tri_data is None:
            exp_face.Next()
            continue

        trsf = loc.Transformation()
        nb_nodes = tri_data.NbNodes()
        base_index = len(verts)
        for i in range(1, nb_nodes + 1):
            p = tri_data.Node(i).Transformed(trsf)
            verts.append((p.X() * manual_scale, p.Y() * manual_scale, p.Z() * manual_scale))

        reversed_face = face.Orientation() == TopAbs_REVERSED
        nb_tri = tri_data.NbTriangles()
        for i in range(1, nb_tri + 1):
            n1, n2, n3 = tri_data.Triangle(i).Get()
            n1 += base_index - 1
            n2 += base_index - 1
            n3 += base_index - 1
            if reversed_face:
                tris.append((n1, n3, n2))
            else:
                tris.append((n1, n2, n3))

        exp_face.Next()

    return verts, tris


def build_blender_object(name, verts, tris, merge_vertices):
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata(verts, [], tris)
    mesh.update()

    if merge_vertices:
        import bmesh
        bm = bmesh.new()
        bm.from_mesh(mesh)
        bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=1e-6)
        bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
        bm.to_mesh(mesh)
        bm.free()
    else:
        mesh.calc_normals if hasattr(mesh, "calc_normals") else None

    obj = bpy.data.objects.new(name, mesh)
    bpy.context.collection.objects.link(obj)
    return obj


def import_step_file(filepath, linear_deflection_mm, angular_deflection_deg,
                      split_solids, merge_vertices, manual_scale=1.0):
    import math

    reader = STEPControl_Reader()

    # OCCT reads whatever length unit the STEP file declares (mm, inch, cm, m...)
    # and normalizes coordinates to a single internal working unit. It defaults
    # to millimeters, but Blender's fundamental unit is meters, so without this
    # override every import comes in 1000x too large. Setting this to "M" makes
    # OCCT do the correct per-file unit conversion for us automatically.
    # NOTE: this must be set AFTER constructing STEPControl_Reader() — OCCT
    # lazily (re)initializes this static parameter to its MM default the first
    # time a reader is constructed in the process, which silently undoes an
    # earlier override if set beforehand.
    Interface_Static.SetCVal_s("xstep.cascade.unit", "M")

    status = reader.ReadFile(filepath)
    if status != IFSelect_RetDone:
        raise RuntimeError(f"Failed to read STEP file (status={status})")

    reader.TransferRoots()
    shape = reader.OneShape()

    angular_deflection_rad = math.radians(angular_deflection_deg)
    # Geometry is now in meters, so the tessellation tolerance (which must be
    # in the same units as the shape) needs converting from the mm value the
    # user entered.
    linear_deflection_m = linear_deflection_mm / 1000.0
    BRepMesh_IncrementalMesh(shape, linear_deflection_m, False,
                              angular_deflection_rad, True)

    base_name = filepath.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    base_name = base_name.rsplit(".", 1)[0]

    created = []

    if split_solids:
        exp_solid = TopExp_Explorer(shape, TopAbs_SOLID)
        idx = 0
        while exp_solid.More():
            idx += 1
            solid = TopoDS.Solid_s(exp_solid.Current())
            verts, tris = tessellate_solid(solid, manual_scale)
            if verts:
                obj = build_blender_object(f"{base_name}_{idx:03d}", verts, tris, merge_vertices)
                created.append(obj)
            exp_solid.Next()

        if not created:
            # no explicit solids (e.g. a shell-only file) - fall back to whole shape
            verts, tris = tessellate_solid(shape, manual_scale)
            if verts:
                created.append(build_blender_object(base_name, verts, tris, merge_vertices))
    else:
        verts, tris = tessellate_solid(shape, manual_scale)
        if verts:
            created.append(build_blender_object(base_name, verts, tris, merge_vertices))

    if not created:
        raise RuntimeError("No triangulated geometry found in this STEP file.")

    return created


# ---------------------------------------------------------------------------
# Operator / UI
# ---------------------------------------------------------------------------

class STEPIMPORT_OT_import(Operator, ImportHelper):
    bl_idname = "import_scene.step_ocp"
    bl_label = "Import STEP"
    bl_options = {'PRESET', 'UNDO'}

    filename_ext = ".step"
    filter_glob: StringProperty(default="*.step;*.stp;*.STEP;*.STP", options={'HIDDEN'})

    linear_deflection: FloatProperty(
        name="Linear Deflection (mm)",
        description="Tessellation precision, in millimeters (smaller = more triangles, slower)",
        default=0.1,
        min=0.0001,
        max=1000.0,
    )
    angular_deflection: FloatProperty(
        name="Angular Deflection (deg)",
        description="Max angle between adjacent triangle normals on curved surfaces",
        default=20.0,
        min=1.0,
        max=60.0,
    )
    split_solids: BoolProperty(
        name="Split by Solid",
        description="Create one Blender object per solid instead of a single merged object",
        default=True,
    )
    merge_vertices: BoolProperty(
        name="Merge Vertices",
        description="Weld duplicate vertices along tessellation seams between faces",
        default=True,
    )
    manual_scale: FloatProperty(
        name="Manual Scale Multiplier",
        description=(
            "Geometry is auto-converted to meters based on the STEP file's own "
            "declared unit. Leave at 1.0 unless a file has missing/incorrect "
            "unit metadata and still imports at the wrong size"
        ),
        default=1.0,
        min=0.000001,
        max=1000000.0,
    )

    def execute(self, context):
        if not OCP_AVAILABLE:
            self.report(
                {'ERROR'},
                "OCP is not installed. Go to Edit > Preferences > Add-ons > "
                "STEP Importer (OCP) and click 'Install OCP Dependency'."
            )
            return {'CANCELLED'}

        try:
            objs = import_step_file(
                self.filepath,
                self.linear_deflection,
                self.angular_deflection,
                self.split_solids,
                self.merge_vertices,
                self.manual_scale,
            )
        except Exception as e:
            self.report({'ERROR'}, f"STEP import failed: {e}")
            return {'CANCELLED'}

        for obj in bpy.context.selected_objects:
            obj.select_set(False)
        for obj in objs:
            obj.select_set(True)
        if objs:
            bpy.context.view_layer.objects.active = objs[0]

        self.report({'INFO'}, f"Imported {len(objs)} object(s) from STEP file.")
        return {'FINISHED'}


def menu_func_import(self, context):
    self.layout.operator(STEPIMPORT_OT_import.bl_idname, text="STEP (.step/.stp) [OCP]")


classes = (
    STEPIMPORT_OT_install_ocp,
    STEPIMPORT_AddonPreferences,
    STEPIMPORT_OT_import,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.TOPBAR_MT_file_import.append(menu_func_import)


def unregister():
    bpy.types.TOPBAR_MT_file_import.remove(menu_func_import)
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
