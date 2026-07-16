bl_info = {
    "name": "STEP/IGES Importer/Exporter (OCP)",
    "author": "Custom",
    "version": (0, 4, 0),
    "blender": (3, 6, 0),
    "location": "File > Import/Export > STEP/IGES",
    "description": "Import and export STEP/IGES CAD files using the OCP OpenCASCADE kernel",
    "category": "Import-Export",
}

import sys
import os
import glob
import subprocess
import time

import bpy
from bpy.props import StringProperty, FloatProperty, BoolProperty
from bpy.types import Operator, AddonPreferences
from bpy_extras.io_utils import ImportHelper, ExportHelper

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
    from OCP.STEPControl import STEPControl_Reader, STEPControl_Writer, STEPControl_AsIs
    from OCP.IGESControl import IGESControl_Reader, IGESControl_Writer
    from OCP.IFSelect import IFSelect_RetDone
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopAbs import TopAbs_FACE, TopAbs_SOLID, TopAbs_REVERSED, TopAbs_SHELL
    from OCP.TopoDS import TopoDS, TopoDS_Compound
    from OCP.BRepMesh import BRepMesh_IncrementalMesh
    from OCP.BRep import BRep_Tool, BRep_Builder
    from OCP.TopLoc import TopLoc_Location
    from OCP.Interface import Interface_Static
    from OCP.BRepBuilderAPI import (
        BRepBuilderAPI_MakePolygon, BRepBuilderAPI_MakeFace,
        BRepBuilderAPI_Sewing, BRepBuilderAPI_MakeSolid,
    )
    from OCP.gp import gp_Pnt

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


def read_and_tessellate_solid(solid, linear_deflection_m, angular_deflection_rad, manual_scale):
    """Meshes and tessellates a single solid."""
    BRepMesh_IncrementalMesh(solid, linear_deflection_m, False,
                              angular_deflection_rad, True)
    return tessellate_solid(solid, manual_scale)


# How long (in seconds) each modal tick is allowed to keep working through
# solids/objects before yielding back to Blender. Blender's timer only fires
# every `timer interval` seconds at minimum (see event_timer_add below), so
# processing exactly one item per tick would throttle total import time to
# (item count x timer interval) regardless of how fast the real work is --
# this budget lets many cheap items batch into one tick while still yielding
# roughly on schedule when items are individually slow.
TICK_TIME_BUDGET = 0.15


# ---------------------------------------------------------------------------
# Core: Blender mesh -> OCP shape (export)
# ---------------------------------------------------------------------------

# Blender's fundamental unit is meters; OCCT's B-rep coordinates are treated
# as an implicit millimeter convention regardless of what unit label the
# output file requests (verified empirically -- requesting "M" output on
# meter-scale raw coordinates double-converts and shrinks everything 1000x
# further). So we scale up to mm when *building* the geometry, and declare
# the file's unit as MM to match, rather than relying on write.step.unit to
# do any conversion for us.
EXPORT_SCALE_TO_MM = 1000.0


def extract_world_triangles(obj, depsgraph):
    """Returns (verts_world, tris) for one Blender object's evaluated mesh,
    in world space, in Blender's native meters. Triangulated via Blender's
    own loop_triangles (handles n-gons correctly). Must run on the main
    thread (bpy/depsgraph access)."""
    eval_obj = obj.evaluated_get(depsgraph)
    mesh = eval_obj.to_mesh()
    mesh.calc_loop_triangles()
    mat = obj.matrix_world
    verts_world = [tuple(mat @ v.co) for v in mesh.vertices]
    tris = [tuple(lt.vertices) for lt in mesh.loop_triangles]
    eval_obj.to_mesh_clear()
    return verts_world, tris


def build_shape_from_triangles(verts_world, tris, sewing):
    """Adds one B-rep planar face per triangle to the given (shared) Sewing
    builder. Pure OCP -- no bpy -- so this is the part that can be time-
    budgeted across many triangles without touching Blender's API."""
    built = 0
    for (a, b, c) in tris:
        va, vb, vc = verts_world[a], verts_world[b], verts_world[c]
        p1 = gp_Pnt(va[0] * EXPORT_SCALE_TO_MM, va[1] * EXPORT_SCALE_TO_MM, va[2] * EXPORT_SCALE_TO_MM)
        p2 = gp_Pnt(vb[0] * EXPORT_SCALE_TO_MM, vb[1] * EXPORT_SCALE_TO_MM, vb[2] * EXPORT_SCALE_TO_MM)
        p3 = gp_Pnt(vc[0] * EXPORT_SCALE_TO_MM, vc[1] * EXPORT_SCALE_TO_MM, vc[2] * EXPORT_SCALE_TO_MM)
        poly = BRepBuilderAPI_MakePolygon(p1, p2, p3, True)
        if not poly.IsDone():
            continue  # degenerate (zero-area) triangle -- skip rather than fail the whole export
        face_maker = BRepBuilderAPI_MakeFace(poly.Wire())
        if not face_maker.IsDone():
            continue
        sewing.Add(face_maker.Face())
        built += 1
    return built


def finalize_sewn_shape(sewing):
    """Turns accumulated sewn faces into a closed Solid if possible, else
    returns the Shell/Compound as-is (e.g. for open/non-manifold meshes)."""
    sewing.Perform()
    sewn = sewing.SewedShape()
    if sewn.ShapeType() == TopAbs_SHELL:
        shell = TopoDS.Shell_s(sewn)
        solid_maker = BRepBuilderAPI_MakeSolid(shell)
        if solid_maker.IsDone():
            return solid_maker.Solid()
        return shell
    return sewn


# ---------------------------------------------------------------------------
# Operator / UI
# ---------------------------------------------------------------------------

class STEPIMPORT_OT_import(Operator, ImportHelper):
    bl_idname = "import_scene.step_ocp"
    bl_label = "Import STEP/IGES"
    bl_options = {'PRESET', 'UNDO'}

    filename_ext = ".step"
    filter_glob: StringProperty(
        default="*.step;*.stp;*.STEP;*.STP;*.iges;*.igs;*.IGES;*.IGS",
        options={'HIDDEN'},
    )

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
        description=(
            "Create one Blender object per solid instead of a single merged "
            "object. Also enables per-solid progress reporting during import"
        ),
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

    # --- internal modal state (not exposed as operator properties) ---
    #
    # IMPORTANT: OCP/OCCT's calls (ReadFile, TransferRoots, meshing) do not
    # release Python's GIL, verified empirically (a 2.5s ReadFile+TransferRoots
    # call on a 141k-entity file let a polling thread run only 2 out of ~126
    # possible ticks). That means a background thread doing this work can't
    # actually update Blender's UI concurrently -- the modal timer callback
    # needs the GIL too, and won't get it until the worker thread releases it.
    #
    # So instead of threading, this operator is a plain step-by-step state
    # machine driven by the modal timer: each tick performs ONE bounded chunk
    # of work (read the file, transfer roots, mesh one solid, build a few
    # Blender objects...) and then returns control to Blender, which gets a
    # real redraw in between ticks. Each individual blocking OCCT call still
    # blocks Blender for its own duration (there's no way around that without
    # OCCT-level progress callbacks, which aren't exposed through this
    # binding) -- but you get a status update and a redraw before and after
    # every such call, so the addon never goes silent for the full runtime.
    _timer = None
    _phase = 'idle'
    _file_format = "step"
    _reader = None
    _shape = None
    _solids = None
    _solid_index = 0
    _linear_deflection_m = 0.0
    _angular_deflection_rad = 0.0
    _base_name = ""
    _results = None
    _created_objects = None
    _build_index = 0

    def invoke(self, context, event):
        if not OCP_AVAILABLE:
            self.report(
                {'ERROR'},
                "OCP is not installed. Go to Edit > Preferences > Add-ons > "
                "STEP Importer (OCP) and click 'Install OCP Dependency'."
            )
            return {'CANCELLED'}
        return ImportHelper.invoke(self, context, event)

    def execute(self, context):
        if not OCP_AVAILABLE:
            self.report(
                {'ERROR'},
                "OCP is not installed. Go to Edit > Preferences > Add-ons > "
                "STEP Importer (OCP) and click 'Install OCP Dependency'."
            )
            return {'CANCELLED'}

        lower_path = self.filepath.lower()
        self._file_format = "iges" if lower_path.endswith((".iges", ".igs")) else "step"

        import math
        self._linear_deflection_m = self.linear_deflection / 1000.0
        self._angular_deflection_rad = math.radians(self.angular_deflection)

        base_name = self.filepath.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
        self._base_name = base_name.rsplit(".", 1)[0]

        self._reader = None
        self._shape = None
        self._solids = None
        self._solid_index = 0
        self._results = []
        self._created_objects = []
        self._build_index = 0
        self._phase = 'pending_read'

        wm = context.window_manager
        wm.progress_begin(0, 100)
        self._set_status(context, "Starting import...")

        self._timer = wm.event_timer_add(0.1, window=context.window)
        wm.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        if event.type == 'ESC':
            self._cleanup(context)
            self.report({'INFO'}, "Import cancelled.")
            return {'CANCELLED'}

        if event.type != 'TIMER':
            return {'PASS_THROUGH'}

        try:
            finished = self._step(context)
        except Exception as e:
            self._cleanup(context)
            self.report({'ERROR'}, f"{self._file_format.upper()} import failed: {e}")
            return {'CANCELLED'}

        self._tag_redraw(context)
        return {'FINISHED'} if finished else {'RUNNING_MODAL'}

    def _step(self, context):
        """Performs exactly one bounded chunk of work, updates the status/
        progress display, and returns True once the whole import is done."""

        if self._phase == 'pending_read':
            # Show the message on this tick, do the (blocking) work on the
            # next one -- guarantees Blender actually redraws the status bar
            # before the freeze, instead of setting text and blocking in the
            # same breath.
            self._set_status(context, "Reading file...")
            context.window_manager.progress_update(2)
            self._phase = 'read'
            return False

        if self._phase == 'read':
            self._reader = IGESControl_Reader() if self._file_format == "iges" else STEPControl_Reader()
            # Must be set AFTER constructing the reader -- OCCT lazily resets
            # this static parameter to its MM default the first time a
            # reader is constructed in the process, undoing an earlier
            # override if set before construction.
            Interface_Static.SetCVal_s("xstep.cascade.unit", "M")
            status = self._reader.ReadFile(self.filepath)
            if status != IFSelect_RetDone:
                raise RuntimeError(f"Failed to read {self._file_format.upper()} file (status={status})")
            self._phase = 'pending_transfer'
            return False

        if self._phase == 'pending_transfer':
            self._set_status(context, "Transferring geometry...")
            context.window_manager.progress_update(5)
            self._phase = 'transfer'
            return False

        if self._phase == 'transfer':
            self._reader.TransferRoots()
            self._shape = self._reader.OneShape()

            if self.split_solids:
                solids = []
                exp_solid = TopExp_Explorer(self._shape, TopAbs_SOLID)
                while exp_solid.More():
                    solids.append(TopoDS.Solid_s(exp_solid.Current()))
                    exp_solid.Next()
                self._solids = solids
                self._phase = 'mesh' if solids else 'mesh_whole'
            else:
                self._phase = 'mesh_whole'
            return False

        if self._phase == 'mesh':
            total = len(self._solids)
            tick_start = time.time()
            # Process as many solids as fit in a short time budget per tick,
            # rather than exactly one -- with many cheap solids (the common
            # case), one-per-tick would be throttled to Blender's minimum
            # timer interval (e.g. 0.1s) *per solid*, which for a
            # thousand-solid assembly means many seconds of pure artificial
            # pacing on top of the real (much smaller) computation time.
            while self._solid_index < total and (time.time() - tick_start) < TICK_TIME_BUDGET:
                idx = self._solid_index
                solid = self._solids[idx]
                verts, tris = read_and_tessellate_solid(
                    solid, self._linear_deflection_m, self._angular_deflection_rad, self.manual_scale
                )
                if verts:
                    self._results.append((f"{self._base_name}_{idx + 1:03d}", verts, tris))
                self._solid_index += 1

            self._set_status(context, f"Meshing solid {self._solid_index}/{total}")
            context.window_manager.progress_update(10 + 80 * (self._solid_index / max(total, 1)))

            if self._solid_index >= total:
                self._phase = 'mesh_whole' if not self._results else 'build'
            return False

        if self._phase == 'mesh_whole':
            # Either split_solids is off, or no explicit solids were found
            # (e.g. a shell-only file) -- mesh the whole shape as one call.
            self._set_status(context, "Meshing shape...")
            context.window_manager.progress_update(50)
            verts, tris = read_and_tessellate_solid(
                self._shape, self._linear_deflection_m, self._angular_deflection_rad, self.manual_scale
            )
            if verts:
                self._results.append((self._base_name, verts, tris))
            self._phase = 'build'
            return False

        if self._phase == 'build':
            if not self._results:
                raise RuntimeError(f"No triangulated geometry found in this {self._file_format.upper()} file.")

            total = len(self._results)
            tick_start = time.time()
            while self._build_index < total and (time.time() - tick_start) < TICK_TIME_BUDGET:
                name, verts, tris = self._results[self._build_index]
                obj = build_blender_object(name, verts, tris, self.merge_vertices)
                self._created_objects.append(obj)
                self._build_index += 1

            self._set_status(context, f"Building objects  ({self._build_index}/{total})")
            context.window_manager.progress_update(90 + 10 * (self._build_index / max(total, 1)))

            if self._build_index >= total:
                self._phase = 'done'
            return False

        if self._phase == 'done':
            for obj in context.selected_objects:
                obj.select_set(False)
            for obj in self._created_objects:
                obj.select_set(True)
            if self._created_objects:
                context.view_layer.objects.active = self._created_objects[0]

            count = len(self._created_objects)
            self._cleanup(context)
            self.report({'INFO'}, f"Imported {count} object(s) from {self._file_format.upper()} file.")
            return True

        return True  # unknown phase -- fail safe and stop

    def _set_status(self, context, text):
        try:
            context.workspace.status_text_set(f"Importing {self._file_format.upper()}: {text}  (Esc to cancel)")
        except Exception:
            pass  # status bar API not available in this Blender version; progress cursor still works

    def _tag_redraw(self, context):
        try:
            for area in context.screen.areas:
                area.tag_redraw()
        except Exception:
            pass

    def _cleanup(self, context):
        wm = context.window_manager
        if self._timer is not None:
            wm.event_timer_remove(self._timer)
            self._timer = None
        wm.progress_end()
        try:
            context.workspace.status_text_set(None)
        except Exception:
            pass

    def cancel(self, context):
        self._cleanup(context)


class STEPIMPORT_OT_export(Operator, ExportHelper):
    bl_idname = "export_scene.step_ocp"
    bl_label = "Export STEP/IGES"
    bl_options = {'PRESET'}

    filename_ext = ".step"
    filter_glob: StringProperty(
        default="*.step;*.stp;*.STEP;*.STP;*.iges;*.igs;*.IGES;*.IGS",
        options={'HIDDEN'},
    )

    export_format: StringProperty(
        name="Format",
        description="Internal: set automatically from the chosen file extension",
        default="step",
        options={'HIDDEN'},
    )
    selected_only: BoolProperty(
        name="Selected Objects Only",
        description="Export only selected mesh objects, instead of every mesh object in the scene",
        default=True,
    )

    # --- internal modal state ---
    #
    # NOTE ON OUTPUT QUALITY: Blender meshes are polygon soup with no smooth
    # NURBS/B-rep surface data, so this necessarily produces a *faceted*
    # STEP/IGES file (flat triangular faces) -- any CAD package can read it,
    # but it will look exactly as faceted as the source Blender mesh, not
    # smoothed. This is a fundamental limitation of exporting from mesh data,
    # not something this addon can work around.
    _timer = None
    _phase = 'idle'
    _file_format = "step"
    _objects = None
    _object_index = 0
    _depsgraph = None
    _sewing = None
    _current_verts = None
    _current_tris = None
    _triangle_index = 0
    _shapes = None  # finished per-object TopoDS shapes, combined at write time

    def invoke(self, context, event):
        if not OCP_AVAILABLE:
            self.report(
                {'ERROR'},
                "OCP is not installed. Go to Edit > Preferences > Add-ons > "
                "STEP Importer (OCP) and click 'Install OCP Dependency'."
            )
            return {'CANCELLED'}
        return ExportHelper.invoke(self, context, event)

    def execute(self, context):
        if not OCP_AVAILABLE:
            self.report(
                {'ERROR'},
                "OCP is not installed. Go to Edit > Preferences > Add-ons > "
                "STEP Importer (OCP) and click 'Install OCP Dependency'."
            )
            return {'CANCELLED'}

        lower_path = self.filepath.lower()
        self._file_format = "iges" if lower_path.endswith((".iges", ".igs")) else "step"

        if self.selected_only:
            candidates = context.selected_objects
        else:
            candidates = context.view_layer.objects[:]
        self._objects = [o for o in candidates if o.type == 'MESH']

        if not self._objects:
            self.report({'ERROR'}, "No mesh objects to export (check selection / Selected Objects Only).")
            return {'CANCELLED'}

        self._object_index = 0
        self._depsgraph = context.evaluated_depsgraph_get()
        self._sewing = None
        self._current_verts = None
        self._current_tris = None
        self._triangle_index = 0
        self._shapes = []
        self._phase = 'pending_extract'

        wm = context.window_manager
        wm.progress_begin(0, 100)
        self._set_status(context, "Starting export...")
        self._timer = wm.event_timer_add(0.1, window=context.window)
        wm.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        if event.type == 'ESC':
            self._cleanup(context)
            self.report({'INFO'}, "Export cancelled.")
            return {'CANCELLED'}

        if event.type != 'TIMER':
            return {'PASS_THROUGH'}

        try:
            finished = self._step(context)
        except Exception as e:
            self._cleanup(context)
            self.report({'ERROR'}, f"{self._file_format.upper()} export failed: {e}")
            return {'CANCELLED'}

        self._tag_redraw(context)
        return {'FINISHED'} if finished else {'RUNNING_MODAL'}

    def _step(self, context):
        total_objects = len(self._objects)

        if self._phase == 'pending_extract':
            obj = self._objects[self._object_index]
            self._set_status(context, f"Reading mesh {self._object_index + 1}/{total_objects}: {obj.name}")
            context.window_manager.progress_update(5 + 80 * (self._object_index / max(total_objects, 1)))
            self._phase = 'extract'
            return False

        if self._phase == 'extract':
            obj = self._objects[self._object_index]
            self._current_verts, self._current_tris = extract_world_triangles(obj, self._depsgraph)
            self._triangle_index = 0
            self._sewing = BRepBuilderAPI_Sewing(1e-6)
            self._phase = 'build_faces'
            return False

        if self._phase == 'build_faces':
            total_tris = len(self._current_tris)
            tick_start = time.time()
            # Time-budgeted, same reasoning as the import side: many cheap
            # triangles should batch into one tick rather than being
            # throttled to one-per-timer-interval. Process triangles in
            # small batches so the elapsed-time check doesn't itself run
            # once per single triangle.
            batch_size = 200
            while self._triangle_index < total_tris and (time.time() - tick_start) < TICK_TIME_BUDGET:
                end = min(self._triangle_index + batch_size, total_tris)
                batch = self._current_tris[self._triangle_index:end]
                build_shape_from_triangles(self._current_verts, batch, self._sewing)
                self._triangle_index = end

            obj = self._objects[self._object_index]
            self._set_status(
                context,
                f"Building {obj.name}: face {self._triangle_index}/{total_tris}",
            )
            if self._triangle_index >= total_tris:
                self._phase = 'finalize_object'
            return False

        if self._phase == 'finalize_object':
            shape = finalize_sewn_shape(self._sewing)
            self._shapes.append(shape)
            self._object_index += 1
            self._sewing = None
            self._current_verts = None
            self._current_tris = None
            self._phase = 'pending_extract' if self._object_index < total_objects else 'pending_write'
            return False

        if self._phase == 'pending_write':
            self._set_status(context, "Writing file...")
            context.window_manager.progress_update(90)
            self._phase = 'write'
            return False

        if self._phase == 'write':
            if len(self._shapes) == 1:
                final_shape = self._shapes[0]
            else:
                builder = BRep_Builder()
                compound = TopoDS_Compound()
                builder.MakeCompound(compound)
                for shape in self._shapes:
                    builder.Add(compound, shape)
                final_shape = compound

            if self._file_format == "iges":
                writer = IGESControl_Writer("MM", 1)
                writer.AddShape(final_shape)
                writer.ComputeModel()
                writer.Write(self.filepath)
            else:
                writer = STEPControl_Writer()
                # Must be set AFTER constructing the writer -- same lazy-init
                # static-parameter behavior verified on the reader side also
                # applies here.
                Interface_Static.SetCVal_s("write.step.unit", "MM")
                writer.Transfer(final_shape, STEPControl_AsIs)
                writer.Write(self.filepath)

            self._phase = 'done'
            return False

        if self._phase == 'done':
            count = len(self._shapes)
            self._cleanup(context)
            self.report({'INFO'}, f"Exported {count} object(s) to {self._file_format.upper()} file.")
            return True

        return True

    def _set_status(self, context, text):
        try:
            context.workspace.status_text_set(f"Exporting {self._file_format.upper()}: {text}  (Esc to cancel)")
        except Exception:
            pass

    def _tag_redraw(self, context):
        try:
            for area in context.screen.areas:
                area.tag_redraw()
        except Exception:
            pass

    def _cleanup(self, context):
        wm = context.window_manager
        if self._timer is not None:
            wm.event_timer_remove(self._timer)
            self._timer = None
        wm.progress_end()
        try:
            context.workspace.status_text_set(None)
        except Exception:
            pass

    def cancel(self, context):
        self._cleanup(context)


def menu_func_import(self, context):
    self.layout.operator(STEPIMPORT_OT_import.bl_idname, text="STEP/IGES (.step/.stp/.iges/.igs) [OCP]")


def menu_func_export(self, context):
    self.layout.operator(STEPIMPORT_OT_export.bl_idname, text="STEP/IGES (.step/.stp/.iges/.igs) [OCP]")


classes = (
    STEPIMPORT_OT_install_ocp,
    STEPIMPORT_AddonPreferences,
    STEPIMPORT_OT_import,
    STEPIMPORT_OT_export,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.TOPBAR_MT_file_import.append(menu_func_import)
    bpy.types.TOPBAR_MT_file_export.append(menu_func_export)


def unregister():
    bpy.types.TOPBAR_MT_file_import.remove(menu_func_import)
    bpy.types.TOPBAR_MT_file_export.remove(menu_func_export)
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
