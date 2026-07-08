# stpimport
Simplified .step/.stp importer for Blender 5, created by Claude

Version: 0.1.0
Addon file: `stpimport_v01.py`

---

## 1. What This Is

A Blender addon that adds native import support for STEP files (`.step` / `.stp`) — the standard neutral CAD exchange format (ISO 10303). Blender has no built-in support for STEP; this addon adds a **File > Import > STEP (.step/.stp) [OCP]** menu entry that reads a STEP file and builds real Blender mesh objects from it, with correct real-world scale.

It's useful anywhere the team needs to bring mechanical/engineering CAD geometry (from SolidWorks, Fusion 360, NX, CATIA, FreeCAD, etc.) into Blender for visualization, rendering, or downstream artistic work.

---

## 2. How It Works

### 2.1 The core problem

Blender's mesh system only understands triangles/polygons. STEP files describe geometry as exact mathematical surfaces — NURBS, planes, cylinders, fillets, boolean combinations — via the B-rep (boundary representation) standard. Something has to translate "exact math surface" into "triangle soup" before Blender can use it. Blender doesn't ship anything capable of that translation.

### 2.2 The solution: OpenCASCADE via OCP

- **OpenCASCADE Technology (OCCT)** is the CAD geometry kernel that actually understands B-rep math, reads/writes STEP files, and can tessellate (triangulate) any surface to a chosen precision. It's a C++ library maintained by Open Cascade SAS, and is the same kernel used by FreeCAD, KiCad's 3D viewer, and Gmsh.
- **OCP** is a thin Python binding layer for OCCT, auto-generated from OCCT's C++ headers using `pybind11`. It doesn't reimplement any geometry logic — it's a near 1:1 translation layer exposing OCCT's C++ classes to Python. OCP is maintained by the **CadQuery** project (the most widely-used Python CAD scripting library) specifically to give CadQuery pip-installable OCCT wheels.
- The addon installs the PyPI package **`cadquery-ocp-novtk`** — the same OCP wrapper, built without the optional VTK visualization dependency, since this addon never uses it (see §5).

### 2.3 The import pipeline, step by step

1. `STEPControl_Reader` opens the file and parses the STEP entities.
2. The file's declared unit (mm, inch, cm, m — whatever the original CAD tool used) is read, and OCCT is explicitly told to normalize all coordinates to **meters** — matching Blender's fundamental unit. This is applied automatically per file; no user action needed.
3. `BRepMesh_IncrementalMesh` tessellates every face of the shape into triangles, at a precision the user controls via **Linear Deflection** (finer detail vs. speed) and **Angular Deflection** (how well curved surfaces are approximated).
4. The addon walks every triangulated face, respecting face orientation so normals come out correct, and collects vertices/triangles per solid.
5. Each solid becomes a separate Blender mesh object (optional — can be disabled to merge into one object), with duplicate vertices along tessellation seams welded together.

### 2.4 Dependency installation — why a private virtual environment

Blender doesn't ship with `cadquery-ocp-novtk`, so it has to be installed once. Rather than `pip install`-ing into Blender's own bundled Python:

- On Linux distros like Arch (and any PEP 668 "externally managed environment" system), this is blocked by design, to prevent exactly this kind of unmanaged system Python pollution.
- Even where it's allowed, it adds a large CAD kernel permanently into a Python environment shared with everything else Blender does.

Instead, the addon creates a **dedicated virtual environment** at `~/.step_importer_ocp_venv`, installs the dependency there using that venv's own `pip`, and adds its `site-packages` folder to Blender's `sys.path` at startup. This:

- Never touches Blender's or the OS's Python installation.
- Works identically across Windows, macOS, and Linux (including Arch).
- Is trivially removable — deleting that one folder fully uninstalls the dependency, no trace left anywhere else.

The one-time install step requires internet access (to reach PyPI). After that, **every subsequent import runs fully offline** (see §4).

---

## 3. Licensing

| Component | License | Practical meaning |
|---|---|---|
| **OCP** (the wrapper) | Apache-2.0 | Permissive; no restrictions on commercial/internal use. |
| **OpenCASCADE (OCCT)** | LGPL-2.1, with an additional exception added in v6.7.0 (2013) | The exception specifically permits **static linking into closed-source/proprietary applications** without any obligation to disclose your own code. Free to use commercially, no royalties or fees. Only obligation: give reasonable notice that the software uses OCCT (e.g., a line in an internal wiki page or about-box — this document satisfies that). |
| **This addon's own code** | N/A (internal tool) — set per your team's policy | — |

**Bottom line for legal/compliance questions:** both upstream dependencies are standard, well-established open-source licenses explicitly designed to be safe for commercial/internal use, with no copyleft "infection" risk to any of the team's own code or files.

---

## 4. Security

### 4.1 The core question: does this ever transmit imported CAD files anywhere?

**No.** This was tested empirically, not just assumed:

- **Static reasoning:** the entire import pipeline (reading the file, tessellating, building the mesh) runs through in-process function calls into OCCT's local geometry math. There is no network client code anywhere in the import path.
- **Dynamic proof — OS-level syscall tracing:** the actual import + tessellation operation was traced with `strace -f -e trace=network`, which captures every socket/connect/DNS-lookup syscall made by the process **and all its children**, including calls made directly by compiled C++ code (i.e., this catches network activity even if it bypassed Python entirely). Result: **zero network syscalls** during a real STEP import.
- **Positive control:** the same tracing setup was verified to correctly capture an intentional network connection (DNS lookup + TCP handshake, 20+ syscalls logged) — confirming the "zero" result above isn't due to a broken or silently-failing trace.

### 4.2 The only network activity, ever

The one-time **"Install OCP Dependency"** button click, which runs `pip install` against PyPI to download the package into the private venv. After that one-time setup, importing STEP files — including sensitive/IP-bearing files — involves no network activity at all. This can be verified independently on any machine by disabling networking before an import and confirming it still works.

### 4.3 What wasn't done, in the interest of honesty

A full manual line-by-line audit of OpenCASCADE's (~1.5–2M lines of C++) and VTK's (~1M+ lines, and no longer even a dependency — see below) source code was **not** performed, and realistically isn't something any single review does for a dependency this size — a genuine audit at that scope is hundreds to low-thousands of person-hours of professional work. The syscall-level tracing above is a targeted, empirical test of actual runtime behavior, which is a stronger and more relevant signal for "will this leak data" than a source-reading exercise would be — but it's worth being precise that it's not a claim of an exhaustive code audit.

### 4.4 VTK — removed

An earlier version pulled in VTK (a Kitware visualization toolkit, unrelated to CAD geometry) as an unused transitive dependency of the standard `cadquery-ocp` package. The addon now installs `cadquery-ocp-novtk` instead — the identical OCCT wrapper, verified to work identically for this addon's needs, minus VTK, matplotlib, and their dependency chain (~150MB smaller, one fewer third-party project in the dependency tree).

---

## 5. Known Limitations

- **No color/material import.** OCCT can expose per-face/per-solid color data from STEP files (via its XCAF module), but this addon doesn't currently extract it — everything imports with default materials.
- **No PMI/GD&T, assembly hierarchy, or part naming.** Solids are named generically (`filename_001`, `filename_002`, ...) rather than using the names/structure from the original CAD assembly tree.
- **Units:** auto-converted to meters based on the file's own declared unit. A manual "Scale Multiplier" override exists in the import dialog for the rare case of a file with missing/incorrect unit metadata.
- **Precision/performance tradeoff:** finer tessellation (smaller Linear Deflection) means more accurate curves but more triangles and slower imports — worth tuning per use case rather than always maxing out precision.

---

## 6. Installation & Setup (Recap)

1. Blender: **Edit > Preferences > Add-ons > Install from Disk** → select `step_importer_ocp.py`. Enable it.
2. In the addon's preferences, click **Install OCP Dependency** (needs internet; downloads once into `~/.step_importer_ocp_venv`).
3. **Restart Blender** so it picks up the new dependency.
4. **File > Import > STEP (.step/.stp) [OCP]**.

To fully remove the dependency (e.g., to reinstall a new version): close Blender, delete `~/.step_importer_ocp_venv` (or the Windows equivalent path), then repeat steps 2–3.

---

## 7. Quick Reference — Anticipated Questions

| Question | Short answer |
|---|---|
| "Does this send our files anywhere?" | No — verified via OS-level network syscall tracing during actual use; zero network activity outside the one-time dependency install. |
| "Is this legal to use internally / commercially?" | Yes — both OCP (Apache-2.0) and OpenCASCADE (LGPL-2.1 + exception) are explicitly designed to permit this. |
| "What is OCP?" | Official Python bindings to OpenCASCADE, maintained by the CadQuery project — not a random/obscure wrapper. |
| "What is OpenCASCADE?" | The industry-standard open-source CAD geometry kernel also used by FreeCAD and other engineering tools. |
| "Why does it need internet access at all?" | Only once, to download the OCCT bindings from PyPI during setup. Never again afterward. |
| "Does it modify my system Python / Blender's Python?" | No — installs into a fully separate, isolated virtual environment. |
| "What if the imported model is the wrong size?" | Shouldn't happen — units are auto-converted per-file. A manual override exists for edge cases. |
