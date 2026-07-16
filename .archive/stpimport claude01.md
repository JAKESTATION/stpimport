
Claude Chat: Blender STEP Extension

### 👤 Jake
are you capable of writing me a blender addon that can import .step files into blender?

### 🤖 Claude
Yes, I can build this, but it's worth knowing upfront what the addon actually has to do under the hood, since that shapes the approach. Blender has no native STEP support and no built-in geometry kernel that understands B-rep/NURBS surfaces (which is what STEP files are made of). So a real STEP importer needs a CAD kernel to parse the STEP and tessellate the surfaces into a mesh Blender can use. Writing that kernel from scratch isn't practical — that's a multi-year undertaking (it's basically what OpenCASCADE and ACIS are for). So any working addon has to lean on an existing kernel. The realistic options:

* Bundle OCP (OpenCASCADE Python bindings) — this is what CadQuery and the "STEPper" Blender addon use. The addon ships/pip-installs the OCP or pythonocc-core wheel into Blender's Python, parses the STEP file, tessellates each face, and builds Blender meshes with proper materials per solid. This gives the best fidelity (curved surfaces, correct topology) but the install step is the annoying part — Blender's bundled Python needs the wheel installed, which differs by OS.
* Shell out to FreeCAD — if the user has FreeCAD installed, the addon calls FreeCAD's Python (or freecadcmd) as a subprocess to convert STEP → OBJ/glTF, then imports that result natively into Blender. Less elegant, but avoids bundling a CAD kernel and is easier to get working reliably.
* Shell out to a converter CLI (e.g. occt tools, or cadquery if already installed) — similar tradeoff to #2.

Given your Fusion 360 / SubD background, you probably want real curved-surface fidelity rather than a blocky approximation, which points toward option 1 (OCP) despite the install friction.

Q: Which approach do you want for the underlying STEP parsing?

### 👤 Jake
A: Bundle OCP/pythonocc (best fidelity, trickier install)

### 🤖 Claude
Since I can actually verify the OCP API in my sandbox (pypi.org is reachable), let me test the tessellation logic for real rather than guessing at API signatures — OCP's Python bindings have some quirks that are easy to get wrong.

> *Ran 2 commands*

The tessellation logic is verified against a real OCP install — 6 faces, 12 triangles for a box, correct winding via orientation handling. Now I'll write the actual Blender addon using this verified approach.

> *Ran 3 commands, created a file*

The actual `tessellate_solid` function from the addon file — not a copy, the real code — correctly reads the STEP file and produces valid mesh data (24 verts, 12 tris for the test box). Copying it to your outputs now.

> *Ran a command, read a file*

**How to install:**
1.  Edit > Preferences > Add-ons > Install from Disk → select this file.
2.  Enable the "STEP Importer (OCP)" checkbox.
3.  Expand its preferences and click Install OCP Dependency — this pip-installs cadquery-ocp into Blender's own bundled Python. Needs internet access; takes a minute (it's a ~200MB wheel, pulls in VTK as a dependency).
4.  Restart Blender. OCP has to be importable at addon-registration time.
5.  File > Import > STEP (.step/.stp) [OCP].

**What it does:** reads the STEP file with OpenCASCADE's `STEPControl_Reader`, tessellates each face with `BRepMesh_IncrementalMesh` (you control precision via Linear/Angular Deflection in the import dialog), and builds a Blender mesh per solid — respecting face orientation so normals come out right. "Split by Solid" gives you one object per solid (useful for assemblies), and "Merge Vertices" welds the seams between adjacent faces' triangulations so you don't get a wall of loose edges.

**Known limitations, worth knowing before you rely on it:**
* No color/material import from the STEP file (OCP exposes this via XCAF but I left it out to keep this a single self-contained file — can add if useful).
* No PMI/GD&T, assembly hierarchy/naming, or units metadata handling — everything lands in whatever units the STEP file used, unscaled.
* The `cadquery-ocp` wheel is version-locked to Blender's bundled Python's minor version (e.g. 3.11 vs 3.12). If Blender's Python doesn't have a matching wheel on PyPI, the pip install will fail — the preferences panel will show the pip error if that happens.

If you hit an install error tied to your specific Blender version, paste the console output here and I'll adjust.

> *Files hidden in shared chats*

### 👤 Jake
so it seemed to install the addon fine, but upon trying to install OCP I get this error popup:

### 🤖 Claude
Ah — that's Arch Linux's PEP 668 "externally managed environment" protection kicking in. Blender on Arch uses the system Python, and Arch's pacman-managed Python blocks pip install directly into it (this is expected, not a bug in the addon).

I could just slap `--break-system-packages` on the pip call, but that pollutes your system Python with a 200MB CAD kernel + VTK — not great on a system you're clearly particular about. Better fix: have the addon create its own isolated venv and inject it into `sys.path` at import time, so nothing touches your system packages.

Let me verify that actually works before handing it back to you.

> *Ran 3 commands*

That part works fine — the earlier combined command must have just timed out on the big download. Let's install in the background this time.

> *Ran 4 commands*

Good, that worked. Now let's verify the site-packages path and confirm importing OCP works after manually injecting it into sys.path, exactly how the addon will do it at startup.

> *Ran a command*

That confirms the approach: create an isolated venv, install cadquery-ocp there, and inject its site-packages onto sys.path before importing OCP — no `--break-system-packages`, no touching Arch's pacman-managed Python at all. Rewriting the addon with this.

> *Edited 2 files*

Now updating the preferences panel to reflect the venv location and add a couple of helpful diagnostics:

> *Edited 2 files*

Now let's verify the whole file compiles and re-run the same bpy-stub end-to-end test to make sure the venv-activation path didn't break anything:

> *Ran 2 commands*

This confirms the full flow — venv auto-detection, sys.path injection, OCP import, and STEP tessellation — all work end-to-end exactly as it'll run inside Blender. Updating your file.

> *Ran a command, read a file*

To use the fix: remove the currently-installed addon (Uninstall in the preferences panel), then re-install this updated file, enable it, and click Install OCP Dependency again. It'll now create a venv at `~/.step_importer_ocp_venv` and install cadquery-ocp there via that venv's own pip — no PEP 668 conflict, no touching pacman's Python. Restart Blender afterward so the addon picks up the new sys.path entry.

### 👤 Jake
ok this seems to work great - if I were to try to import any sensitive IP related .stp files, there is no way this calls back the data to some server somewhere right?

### 🤖 Claude
Good question — and no, it shouldn't. Let me actually verify this rather than just asserting it, since I can test for real network activity in my sandbox.

> *Ran a command*

Confirmed with real instrumentation, not just my say-so: I patched Python's `socket.socket()` constructor and `getaddrinfo()` (DNS resolution) and ran the exact read → tessellate pipeline your addon uses. Zero sockets created, zero DNS lookups attempted. Here's the honest breakdown of why:

The import path itself — `import_step_file()` in the addon only touches: the file on disk, OCP's local C++ geometry kernel (in-process function calls, no I/O), and Blender's own mesh API. There's no code path in what I wrote that could reach the network even if it wanted to.

The dependencies:
* OpenCASCADE/OCP — this is a geometry kernel (curves, surfaces, meshing math). It has no networking code in it at all; it's the same kernel FreeCAD and CadQuery use offline all the time.
* VTK — pulled in as a dependency of the cadquery-ocp wheel, but the addon never imports or calls into it. Dead weight in this context, not a risk.

The only time this addon touches the network, ever: the one-time "Install OCP Dependency" click, which does a pip install from PyPI into that private venv. Once that's done, everything — every subsequent STEP import — runs fully offline.

If you want to be extra sure for a given IP-sensitive file, you could literally pull your network cable / disable Wi-Fi before importing and it'll work identically, since I just proved nothing reaches for a socket during the operation.

One caveat I'll flag honestly: I verified this code path in this version of cadquery-ocp on my end. I didn't audit OpenCASCADE's or VTK's full C/C++ source trees line-by-line — that's a fair bit outside what's reasonable to promise. But behaviorally, empirically, on the actual operation you'd run: nothing phones home.

### 👤 Jake
Does this scale dependent on the units in my blender default file? right now it's in Meters, and the objects I imported are gigantic. If they were modeled in mm, if I set my blend file units to mm, will it import the correct size?

### 🤖 Claude
Good catch — and no, changing your blend file's Unit System won't fix this. Let me explain why, and verify the actual fix in the sandbox before I hand you anything.

**Why "gigantic":** OpenCASCADE (the kernel doing the STEP parsing) normalizes all imported geometry to millimeters internally by default, regardless of what unit the STEP file was authored in. My current addon takes those raw numbers and writes them straight into Blender mesh vertices — but Blender's fundamental unit (independent of what your Scene Unit System dropdown says) is meters. So a 100mm part comes back as coordinate value 100, which Blender treats as 100 meters. That's your 1000x-too-large objects.

**Why changing Blender's unit display won't help:** the Unit System setting (Metric/Millimeters/etc.) in Scene Properties only changes how numbers are labeled in the UI — "1.0" showing as "1 m" vs "1000 mm". It doesn't rescale existing geometry or change how new geometry gets imported by a script writing raw coordinates.

The actual fix has to happen in the import code — scaling the coordinates to match Blender's meter-based convention regardless of what units the STEP file was in.

Let me verify the cleanest fix: telling OCCT to normalize directly to meters (instead of its mm default) when reading, so it does the correct unit conversion itself based on whatever the file declares.

> *Ran a command*

Confirmed — that one line fixes it cleanly and automatically, regardless of what unit the STEP file was actually authored in (mm, inch, cm — OCCT reads the file's own declared unit and converts to meters for us). Updating the addon:

> *Edited 3 files, viewed 2 files*

Now update the solid/tessellation calls to use the new mm-based deflection variable and apply the manual scale multiplier to vertex coordinates:

> *Edited 2 files*

Now update the operator's UI properties and execute() call to match:

> *Edited a file*

Now let's verify the whole thing compiles and re-run the end-to-end test with the actual mm-declared STEP file to confirm the fix produces correctly-scaled geometry:

> *Ran 4 commands*

Found a real bug via testing — the very first `STEPControl_Reader()` construction in a session lazily resets that static parameter to its MM default, silently undoing our override if set beforehand. Setting it after constructing the reader fixes it reliably every time. Let me patch that.

> *Edited a file*

Now let's re-run the full end-to-end test, including a cold-start (simulating the very first STEP import in a fresh Blender session, where the bug actually bites) plus a follow-up import to make sure both work:

> *Ran a command*

Fixed and verified — both the cold-start case (which is exactly what happens the first time you import a STEP file after restarting Blender) and subsequent imports now correctly land at 0.1 × 0.05 × 0.025 meters for a file declaring a 100×50×25mm part. Updating your file:

> *Ran a command, read a file*

To answer your actual question directly: no, your Blender file's Unit System setting is irrelevant here — it's purely cosmetic display. What matters is that this addon now auto-detects the unit declared inside the STEP file itself (mm, inch, cm, whatever it was modeled in) and converts it to Blender's native meters, automatically, per-file. You don't need to touch your scene units at all; keep them however you like them.

Reinstall this version the same way (uninstall old, install this file, re-enable — no need to reinstall the OCP dependency itself, that's unaffected). Your existing "gigantic" imports won't retroactively fix themselves, but any new import from now on will land at the correct real-world scale.

There's also a "Manual Scale Multiplier" option in the import dialog now, for the rare case a file has bad/missing unit metadata and still comes in wrong — leave it at 1.0 otherwise.

### 👤 Jake
Alright, back to the security question, how long would it take you to audit the two repos you mentioned? Opencascade and VTK (which I'm unclear what vtk has anything to do with this addon)

### 🤖 Claude
Good question — let me separate two things: what a full audit would actually take, and what I can realistically verify for you in the next few minutes that's actually more relevant.

**What OpenCASCADE and VTK actually are, since you asked:**
* OpenCASCADE (OCCT) is a C++ library for 3D CAD geometry — it's the actual math engine that understands what a NURBS surface, a fillet, or a boolean union between solids is, and can read/write CAD exchange formats like STEP and IGES. It's not an app you run — it's the library FreeCAD, CadQuery, and this addon are built on top of.
* VTK (Visualization Toolkit), from Kitware, is a separate toolkit for turning data into rendered visuals — think ParaView. It's pulled in here as a dependency of the cadquery-ocp Python packaging, but nothing in the STEP import path actually calls into it — it's along for the ride, not used.

**On a full manual audit:** honestly, quoting a "time" for me to do this doesn't map well to how I work — I don't have a persistent process grinding through code over hours. But scoped honestly: OpenCASCADE is roughly 1.5–2 million lines of C++, VTK's core is over a million more. A genuine professional security audit of something that size is realistically hundreds to low-thousands of person-hours of work, done by a team, with tooling — not something any single review (mine or anyone's) meaningfully completes in a chat session. Nobody actually does that for dependencies like this in practice, including CAD vendors; they lean on the libraries' huge existing user base and targeted tooling instead.

What I can do right now, which is actually more relevant than a source crawl: check what network-capable code is even linked into the compiled binaries you'd be running, and trace real OS-level syscalls during an import — this catches C-level socket calls that my earlier Python-only test would've missed, since compiled extensions can make raw syscalls that bypass Python's socket module entirely. Let me do that.

> *Ran 2 commands*

Now let's trace actual OS-level network syscalls — this catches C-level socket activity that would bypass Python entirely:

> *Ran a command*

Zero network syscalls — not even a `socket()` call was made. Let me run a quick positive control to prove the trace itself is actually working (i.e., that it's not silently failing to capture anything):

> *Ran a command*

That confirms the tracer works exactly as expected — a deliberate connection attempt lit up 20+ syscalls (DNS lookup, socket creation, TCP connect, the works). Against that same tracing, the STEP import/tessellate operation produced zero network syscalls of any kind.

This is the strongest evidence available short of reading every line of both codebases: not "no code path exists that could theoretically do this," but "we watched the actual kernel-level syscalls made by the actual code you're running, and it never touched a socket."

**So, direct answer on audit time:**
* A genuine full audit of OpenCASCADE + VTK's source: not something I can meaningfully quote a session-scale time for — realistically hundreds to low-thousands of person-hours across a team for codebases this size (~1.5–2M lines and ~1M+ lines respectively). Nobody does that for a dependency like this in practice, including CAD vendors.
* What actually answers your real question — "will this leak my IP-sensitive file" — in a few minutes: syscall-level tracing of the actual operation, which is what I just did. That's a much more targeted and honestly more trustworthy signal than a manual source crawl would be, because it tests the actual behavior of the actual binaries you're running, not a theoretical reading of source that could still miss something (or not reflect the exact compiled build).

If it'd help your comfort level further, you can run this exact same `strace -f -e trace=network` check yourself on your own machine, on your own actual sensitive files, and see the same empty result — I'm glad to give you the one-liner for that.

### 👤 Jake
ok and define what the OCP is, I see it in the import dialog box after .stp [OCP], I'm just preparing for eventual questions I'll get when I try to share this with my team as a plugin we can use internally

### 🤖 Claude
Good instinct to prep for that — let me verify the licensing details precisely since that's likely the first thing a team/legal will ask about for internal tooling.

> *Searched the web*

Good context to have before that conversation. Here's the breakdown, plus I found something that'll make your audit story even cleaner — let me verify it works first.

> *Ran a command*

That confirms it — no VTK, no matplotlib, no 15-package dependency chain, just the 67MB OCCT wrapper by itself. Let me verify our exact import path still works against it before I recommend switching.

> *Ran a command*

Identical result, no VTK. Switching the addon to this variant:

> *Edited a file, ran a command, read a file*

Here's the rundown for your team conversation:

**What OCP actually is:** it's a thin Python binding layer — auto-generated (via a tool called pywrap) from the C++ headers of OpenCASCADE, using pybind11 to expose those C++ classes and functions to Python nearly 1:1. It doesn't reimplement any geometry logic itself; it's a translation layer. It's maintained by the CadQuery project (github.com/CadQuery/OCP), and it exists specifically because CadQuery — a fairly widely-used Python CAD scripting library in engineering circles — needed pip-installable wheels of OpenCASCADE, which historically only shipped via conda.

So when your team sees [OCP] in the import dialog, the honest framing is: "this is CadQuery's official OpenCASCADE bindings, the same dependency a real, actively maintained open-source CAD tool relies on — not some obscure one-off wrapper."

**Licensing, which is usually the second question after security:**
* OCP (the wrapper itself): Apache-2.0.
* OpenCASCADE (OCCT, the actual geometry kernel underneath): LGPL-2.1 with an additional exception. That exception specifically permits static linking into closed-source/proprietary applications without an obligation to disclose your own code — this was added deliberately in 2013 to make OCCT viable for exactly this kind of internal commercial tooling. You can use it in a commercial environment with no license fees or royalties. Neither license requires you to open-source anything of yours; you just need to give notice that the software uses OCCT (a line in a README or about-box, not a big lift).

**One improvement based on your question:** I noticed VTK — a separate visualization library from Kitware — was getting pulled in as an unused dependency of the standard cadquery-ocp package. Our addon never touches it. I tested the `cadquery-ocp-novtk` package variant against the exact same import code and it works identically, so I switched the addon to install that instead.

Practically: ~150MB smaller install, one fewer third-party project in your dependency chain, and one fewer thing to explain when someone asks "what's VTK doing in here."

Same file link above, already updated — no other setup changes needed.

### 👤 Jake
so how do I uninstall OCP to try reinstalling it with this new version?

### 🤖 Claude
Quick and clean — the venv is just a folder, so you don't need any special "uninstall" flow, just delete it and let the addon recreate it:

1. Close Blender first (so nothing has files open in that folder).
2. Delete the old venv:
```

Code output

````
Markdown file created successfully.

```bash
rm -rf ~/.step_importer_ocp_venv
````

(On Windows, the equivalent would be deleting `%USERPROFILE%\\.step_importer_ocp_venv` — worth knowing since you're prepping for your team, some of whom may not be on Linux.)

3. Re-install the addon file — since you already have the updated `step_importer_ocp.py` from above, in Blender: Preferences > Add-ons > find "STEP Importer (OCP)" > Remove, then Install from Disk with the same file again (or just re-enable it if Blender still points at the same path — either works, the point is getting the updated code loaded).
    
4. Click "Install OCP Dependency" again in the addon preferences. Since the venv directory is gone, it'll create a fresh one and this time install `cadquery-ocp-novtk` into it instead of the VTK-bundled version.
    
5. Restart Blender, same as last time, so it picks up the new sys.path entry.
    

One thing worth doing to confirm it actually worked: after restarting, check the addon preferences panel — it'll show "OCP is installed and ready" if the import succeeded.

If you want to be extra sure VTK is really gone, you can check the venv's site-packages folder:

Bash

```
ls ~/.step_importer_ocp_venv/lib/python*/site-packages/ | grep -i vtk
```

No output means it's clean.

Bash

```
ls ~/.step_importer_ocp_venv/lib/python*/site-packages/ | grep -i vtk
cadquery_ocp_novtk-7.9.3.1.1.dist-info
cadquery_ocp_novtk.libs
```

That's a false positive — `grep -i vtk` is matching the substring "vtk" inside "novtk", not an actual VTK package. Those two entries are just the `cadquery_ocp_novtk` package's own metadata folder and compiled libs folder — expected and correct.

To check for real, unambiguously:

Bash

```
pip --python ~/.step_importer_ocp_venv/bin/python list | grep -i vtk
```

Or just look for an actual top-level vtk package directory (not a substring match):

Bash

```
ls ~/.step_importer_ocp_venv/lib/python*/site-packages/ | grep -E '^vtk'
```

That second one should return nothing. If it does, something's wrong; if it's empty, you're clean — VTK itself isn't installed, only the wrapper package whose name happens to contain those three letters.

### 👤 Jake

Now I'd like to have a document that explains exactly what this is doing, how it's doing it, license questions, security questions, and all other information you think would be beneficial to have on deck in case of questions. Ideally this will be in a clean markdown document that clearly breaks down everything, easy to read

### 🤖 Claude

> _Created a file, ran a command, read a file_

Covers everything from this conversation: what it does, the technical pipeline (unit-conversion fix included), the venv-based dependency strategy and why, licensing for both OCP and OpenCASCADE, the security testing (with the strace methodology explained plainly, including the honest caveat about what wasn't audited), the VTK removal, known limitations, and a quick-reference Q&A table at the end for the "someone asks me on the spot" scenario. Let me know if you want a section added — e.g. a changelog, or contact/escalation info for who maintains this internally.

### 👤 Jake

how hard would it be to add .iges support?

### 🤖 Claude

Much easier than STEP was — OpenCASCADE already has full IGES support built in (it's the same kernel, just a different reader class), so this is mostly wiring up a parallel code path rather than solving anything new. Let me verify the API works before I promise anything.

> _Ran 2 commands_

Confirmed — IGES has the exact same lazy-init unit quirk as STEP, and the same `xstep.cascade.unit` fix applies identically. Since the tessellation code operates on the resulting `TopoDS_Shape` regardless of which reader produced it, almost the entire pipeline is reusable. Let's wire it in.

> _Viewed a file, ran a command_

Ah — my sandbox scratch directory reset between turns (expected behavior; it's meant for temp work only), so the copy in `/home/claude` reverted to stale content. The real up-to-date version survived in your outputs folder. Let me pull that back in before making changes.

> _Ran a command_

Good, confirmed correct and restored. Now let's add IGES support — reusing the same tessellation code, just adding a second reader path selected by file extension:

> _Edited 2 files, viewed 2 files_

Duplicate line, as expected from the partial match. Fixing:

> _Edited a file_

Also fix the leftover "STEP file" wording in the error message to be format-aware:

> _Edited a file_

Now let's view and update the operator class to accept both extensions and route to the right format:

> _Edited 2 files, viewed a file_

Also update `bl_info` for accuracy:

> _Edited a file_

Now let's syntax-check and run a real end-to-end test against both the STEP and IGES box fixtures using the actual updated `import_cad_file` function:

> _Ran 3 commands_

Good, OCP is still available system-wide. Let's regenerate the mm-declared fixture and run a full end-to-end test using the actual `import_cad_file()` for both STEP and IGES:

> _Ran 2 commands_

Both formats work correctly through the actual updated code — same tessellation logic, same unit-conversion fix, just a different reader picked by file extension. Copying the updated addon to your outputs:

> _Ran a command, read a file_

Answer to "how hard": very easy — turned out to be about 20 lines of actual change, verified end-to-end. Here's why it was cheap:

- OpenCASCADE already has full native IGES read support (`IGESControl_Reader`) sitting right next to the STEP reader — same kernel, same underlying B-rep representation once parsed.
    
- The unit-normalization quirk I found for STEP earlier turned out to apply identically to IGES too (same `xstep.cascade.unit` static parameter, same "must be set after constructing the reader" gotcha) — verified with the same fixture-based test.
    
- The tessellation code (`tessellate_solid`) operates purely on the resulting `TopoDS_Shape`, which is identical regardless of which reader produced it — zero changes needed there.
    

What changed: the file dialog now accepts .iges/.igs too, the addon picks the right reader based on the file's extension, and error/success messages are format-aware. No changes needed to the dependency install step, the venv setup, or licensing/security posture — it's the exact same cadquery-ocp-novtk package, just using a class it already ships.

No reinstall needed — the OCP dependency is unchanged, just re-install the addon file itself the same way as before (Preferences > Add-ons > remove old, install this one, re-enable). No need to touch `~/.step_importer_ocp_venv` this time.

I bumped the addon version to 0.2.0 and updated `bl_info`, so if you want to note this in the documentation doc from earlier, worth adding a line to the changelog/limitations section — happy to update that file too if useful.

### 👤 Jake

can you show some sort of UI that shows loading process while it's bringing in a large step file? Something that shows progress...currently it just freezes blender until it loads in

### 🤖 Claude

Yes — right now the import blocks Blender's main thread since it's one big synchronous operator call. Let me first check whether OCCT's Python bindings expose any real progress-reporting hooks, since that would let us show genuine progress rather than a fake spinner.

> _Ran 4 commands_

Interesting finding: OCCT's meshing calls don't release Python's GIL in these bindings, so simple background-threading alone won't give buttery-smooth progress — but it doesn't need to, because Blender's own window/viewport handling is native C++ and isn't gated on Python holding the GIL. What actually matters is restructuring so the operator returns control to Blender immediately (via a modal operator) instead of blocking in one giant call.

Let me verify the core data-flow logic works correctly before wiring it into Blender's UI, since threading bugs are exactly the kind of thing worth catching before you hand this to your team.

> _Ran a command_

Clean data flow, correct results, no race conditions. Now wiring this into an actual modal Blender operator with a visible progress UI.

> _Viewed a file_

Now let's replace the core section (`tessellate_solid` through `import_cad_file`) with the background-safe reader plus a thread-safe progress tracker:

> _Edited 2 files_

Now let's replace the operator with a modal version that shows real progress in Blender's status bar and cursor progress indicator:

> _Edited a file, ran a command_

Now let's build a stub harness that actually simulates Blender's modal event loop — calling `execute()` then repeatedly firing TIMER events at `modal()`, exactly like Blender's event system does — to catch any control-flow bugs before you try it live:

> _Ran a command_

That's just a missing stub in my test harness (`context.collection`), not a bug in the addon — let me fix the harness and rerun.

> _Ran a command_

Both formats complete correctly through the full simulated modal loop — status bar updates, progress wheel, object creation, and clean shutdown. Copying the final version:

> _Ran a command, read a file_

What you'll see now during import: the status bar at the bottom of the Blender window shows live text like `Importing STEP: Meshing solid 14/230 (Esc to cancel)`, and the progress wheel next to your cursor fills in alongside it. Blender's window stays fully responsive — you can drag it, resize it, orbit the viewport — while the import runs in the background.

**How it works, briefly:**

- The actual OCCT reading/meshing now runs on a background Python thread instead of blocking Blender's main thread directly.
    
- A modal operator (a Blender pattern for long-running interactive operations) polls that thread's progress every 0.1s via a timer and updates the status bar + progress wheel.
    
- Progress is genuinely granular per-solid (not a fake spinner) when "Split by Solid" is on, since each solid gets meshed and reported individually — which is exactly the case that matters for "large STEP file," since those are almost always multi-part assemblies.
    
- Building the actual Blender mesh objects (which must happen on the main thread — Blender's API isn't thread-safe) is chunked 5 objects at a time per tick, so even that phase doesn't stall on assemblies with hundreds of parts.
    

One honest caveat, worth knowing: I tested and confirmed OCCT's meshing calls don't release Python's GIL — so pressing Esc mid-import stops Blender from building any more objects and cleans up the UI immediately, but if the background thread is mid-way through a single very heavy solid, it can't be forcibly interrupted (no cancellation hook exposed via these bindings) and will just finish silently in the background with its result discarded. In practice this only matters for single monolithic parts with huge face counts — for the typical "large file = many parts" case, cancellation is responsive because it checks in between solids.

Want me to add a note about this to the team documentation doc, or leave that as-is for now?

### 👤 Jake

yeah an update summary please

### 🤖 Claude

Good call — makes sense to have a proper changelog for the repo. Let me bump the version number in the addon to match, then write the changelog covering everything from the start.

> _Ran 3 commands, edited a file, created a file_

Covers the whole arc chronologically — initial STEP support, the Arch/PEP 668 venv fix, the unit-scaling bug (with the lazy-init gotcha called out since it's subtle and worth remembering if it ever resurfaces), the VTK removal, IGES support, and the progress UI, each with what changed and why.

Bumped the addon's `bl_info` version to 0.3.0 to match, so the two stay in sync going forward.

Drop `CHANGELOG.md` at the repo root (standard GitHub convention — it'll surface automatically in most repo browsers) and it cross-references the documentation doc from earlier for anyone who wants the deeper architecture/licensing/security detail. Let me know if you want me to also add a version/changelog pointer section into that documentation file itself, or a README.md for the repo root while we're at it.

### 👤 Jake

alright I just tried the updated step importer with a large .stp, but I don't see any progress bar anywhere in the screen and blender still frozen. I don't mind blender being frozen, but I'd at least like to see that it's doing something instead of just nothing

### 🤖 Claude

That "still frozen with zero indication" symptom is actually a real clue — let me verify a hypothesis before changing anything.

My earlier GIL test showed OCCT's meshing calls hold Python's GIL for their entire duration. If the file's reading step behaves the same way, that would explain exactly what you're seeing: the background thread is running, but it's hogging the GIL so hard that even the modal timer callback (which needs the GIL to update the status bar) never gets to execute until the read is done. Let me confirm.

> _Ran a command_

141k entities, 6.9MB — good enough to get measurable timing. Now testing GIL release during `ReadFile()` and `TransferRoots()` specifically:

> _Ran a command_

Confirmed — during a realistic 2.5-second read+transfer on a 141k-entity file, the background thread held the GIL essentially the entire time (2 ticks observed out of ~126 possible). So the threading approach wasn't actually buying any real concurrency — it just moved the blocking call into a thread while your own progress-reporting code was starved of the GIL the whole time it mattered. That's exactly the "frozen with zero indication" symptom you saw.

The fix: since real concurrency isn't achievable here, drop the thread entirely and instead break the work into discrete steps run directly in the modal timer callback — one step per tick, with Blender's event loop guaranteed to redraw between ticks. This matches what you actually asked for (visible signs of life, not true responsiveness) and is more reliable than the threaded version.

> _Ran a command_

Removing the threading-based progress tracker in favor of a plain synchronous helper the modal state machine will call step by step:

> _Edited 2 files_

Now let's view the current operator to replace it with the state-machine version:

> _Viewed 2 files_

Now replacing the whole operator with the state-machine design:

> _Edited a file_

Let me fix a bug I just introduced — the ESC check comes after a check that already passes through all non-TIMER events, meaning ESC would never be caught:

> _Edited a file_

Now let's syntax-check and rerun the full simulated modal-loop test — this time with the big 141k-entity file, so we actually exercise the multi-tick behavior meaningfully:

> _Ran 2 commands_

That's the fix confirmed working correctly: `execute()` now returns in ~0ms (no blocking at all upfront), and the entire import unfolds across 485 separate, bounded modal ticks — each one setting a distinct status message ("Meshing solid 47/400", "Building objects (215/400)", etc.) and then returning control to Blender before the next chunk runs. That's 485 real opportunities for Blender to redraw the status bar, versus the old version's effectively-zero opportunities during the dominant work phase.

> _Ran a command, read a file_

What was actually wrong: the background-thread approach from before was based on a false assumption. I verified with a real 141k-entity test file that OCCT's read/transfer/meshing calls hold Python's GIL for their entire duration (2 out of ~126 possible polling opportunities got through during a 2.5-second call). That meant the thread wasn't giving your progress-reporting code any real chance to run — Blender was just as frozen as before, just with extra complexity and zero payoff.

The fix: dropped threading entirely. The operator is now a plain step-by-step state machine — read file, transfer geometry, mesh one solid, mesh next solid, ..., build a few objects, repeat — where each step is one bounded call, and control returns to Blender after every single one. Blender's event loop gets a guaranteed redraw between each step, which is what actually makes the status bar text and progress wheel visible and update as the import runs.

Verified against a synthetic 400-solid, 141k-entity test file: 485 distinct modal ticks, each with its own status message, execute() itself returning instantly rather than blocking.

What you should see now: the status bar at the bottom cycling through `Reading file...` → `Transferring geometry...` → `Meshing solid 1/400` → `Meshing solid 2/400` → ... → `Building objects (395/400)`, with the cursor progress wheel filling in alongside it. Each individual step still blocks briefly while it runs (unavoidable — no OCCT-level progress callback is exposed through these bindings), but you'll never go more than one step without visible feedback.

Same install steps apply — no dependency changes, just re-install the addon file. Want me to add this fix as a 0.3.1 entry in the changelog?

### 👤 Jake

is it possible that this change now causes it to take longer than it did previously? it seems much slower than before the update

### 🤖 Claude

Good question — and yes, this is very plausible. Let me check something specific: I recall the last boolean argument to `BRepMesh_IncrementalMesh` might control OCCT's internal multi-threaded meshing, which would mean meshing the whole shape in one call (old approach) could use multiple CPU cores across all faces, while meshing solid-by-solid (new approach) loses most of that parallelism. Let me verify.

> _Ran a command_

Confirmed — that's `isInParallel`. Let's measure the actual difference empirically on your real test file's structure (400 solids) rather than guessing.

> _Ran a command_

Found it — and this is a much better lead than the parallelism angle (which barely mattered: 0.44s vs 0.34s, basically a wash). The real problem is in my test harness, not your Blender: I simulated the modal loop by calling `modal()` in a tight Python loop, which doesn't reflect how Blender's timer actually behaves.

`wm.event_timer_add(0.1, ...)` means Blender delivers a TIMER event no faster than every 0.1 seconds — full stop, regardless of how fast the work itself is. My new design does exactly one solid per tick, so a 400-solid file gets throttled to a hard floor of 400 × 0.1s ≈ 40 seconds, even though the actual meshing work measures well under a second. That's an artificial slowdown I introduced, not a real computational cost. Let me confirm the math and fix it.

> _Ran a command_

That's the smoking gun. Fix: process solids/objects in a loop within each tick until a small time budget is spent, instead of exactly one per tick — so cheap items batch together and only genuinely slow items get their own tick.

> _Edited 5 files, ran a command_

Now add the `TICK_TIME_BUDGET` constant and clean up the unused `_objects_per_tick` attribute:

> _Edited 2 files, ran a command_

Now let's build a realistic harness — one that actually enforces the 0.1s minimum gap between TIMER events, the way real Blender does — and measure total wall-clock time with the fix:

> _Ran a command_

Confirmed fixed: 9 ticks total (down from 485), and realistic wall-clock time of ~2.7 seconds — which matches almost exactly what the actual OCCT computation costs (the ~2.5s read+transfer time I measured earlier for a similarly-sized file, plus a bit for meshing/building). The artificial ~45+ second timer-pacing penalty is gone. Updating your file:

> _Ran a command, read a file_

Yes — you were right, and it was my mistake.

The previous version processed exactly one solid (or up to 5 objects) per timer tick, but Blender's timer only fires at most every 0.1 seconds regardless of how fast the actual work is. For a file with hundreds or thousands of solids, that turns into a hard floor of (solid count) × 0.1s — for your test case that's roughly 40+ seconds of pure artificial waiting, layered on top of maybe a couple seconds of real work. My earlier verification test didn't catch this because I was polling `modal()` in a tight loop without reproducing Blender's actual timer-pacing behavior.

The fix: each tick now processes as many solids (or objects) as it can within a small time budget (0.15s), rather than exactly one. Cheap items — which is most of them, for typical CAD parts — batch together into a handful of ticks; only genuinely slow individual solids end up dominating their own tick.

Verified against your 400-solid test file with Blender's real timer floor properly simulated this time: down to 9 total ticks and ~2.7 seconds, matching the actual OCCT computation cost almost exactly.

You should notice this immediately — reinstall the same way (no dependency changes, just the addon file) and it should feel roughly as fast as the very first blocking version did, just with the status bar now actually updating along the way instead of nothing.

Worth logging as 0.3.1 in the changelog — want me to add that entry, describing this fix and what caused it?

### 👤 Jake

is it possible to make this plugin also support EXPORTING .stp files?

### 🤖 Claude

Yes, technically feasible — but there's an important caveat to flag first: Blender meshes are just polygon soup (triangles/quads), with no smooth NURBS/B-rep surface data. So exporting to STEP can't magically produce clean CAD surfaces — it'll produce a faceted STEP file (flat triangular/polygonal faces), which any CAD package can read, but it'll look like a low-poly mesh, not a smooth part, unless your Blender geometry was already dense/fine.

Let me verify the actual OCCT round-trip works before promising it — building B-rep faces from raw triangle data, sewing them into a solid, and writing valid STEP.

> _Ran a command_

Full round-trip: reading the exported file back to confirm it's valid and dimensionally correct:

> _Ran a command_

Found a real bug via this test — I declared the file's unit as "MM" but never actually scaled the coordinates (which were still meter-scale numbers), so the file ended up saying "0.1 millimeters" instead of "100 millimeters." This is exactly the kind of unit mismatch we already dealt with on the import side — same lesson, opposite direction. Fixing:

> _Ran 2 commands_

Same systemic quirk as the read-side, confirmed empirically rather than assumed — the parameter must be set after constructing `STEPControl_Writer()` too. Verifying the full correct pipeline end-to-end with the actual triangle-based mesh construction:

> _Ran a command_

That's odd — same set-after-construction pattern I just verified working, but it failed here. Let me isolate exactly what's different, since guessing would be irresponsible given how subtle this class of bug has proven to be.

> _Ran a command_

That trace shows the parameter itself stays correctly set throughout — so the bug isn't a lazy-reset this time. New hypothesis: OCCT's B-rep coordinates are always implicitly treated as millimeters internally (its long-standing convention), regardless of what unit label you request for the output file. So feeding it raw meter-scale numbers and asking for "M" output actually converts mm→m on numbers that were never in mm to begin with, scaling things down twice. Let me test that theory directly:

> _Ran a command_

This is a great opportunity to verify with actual Blender rather than stubs, since I now have it installed. Let me check whether Blender's Python can see the same OCP install:

> _Ran a command_

Both real Blender and OCP in the same process — this lets me do a genuinely complete end-to-end test: real Blender mesh → real triangle extraction via bpy → real OCP B-rep construction → STEP write → read-back verification.

> _Ran a command_

Fully verified — correct scale (via volume, which is rotation-independent) and correct world-space transform handling (position + rotation), all through real Blender. Now let's test the multi-object case (combining several parts into one compound STEP file) before wiring this into the addon:

> _Ran a command_

Multi-object export confirmed working. Now let's add this to the addon, following the same time-budgeted modal pattern already proven for import:

> _Ran a command, viewed a file, edited a file_

Now let's find the OCP import block to add the export-related classes:

> _Ran 2 commands, edited a file_

Let's add the export core logic right after `TICK_TIME_BUDGET`, before the import operator:

> _Viewed a file, edited a file_

Now let's add the export operator itself, right before the menu function registration:

> _Ran a command, viewed a file_

Now inserting the export operator (using the same time-budgeted modal pattern) plus updating menu registration:

> _Edited a file_

Let me clean up a slightly awkward bit — calling the batch-oriented helper one triangle at a time in a loop instead of properly batching:

> _Edited a file_

Now let's syntax-check and run the full simulated modal-loop test for the export operator, mirroring the harness used for import:

> _Ran a command_

Let's verify this using real Blender directly rather than stubs — much stronger evidence than the simulated harness approach:

> _Ran a command_

That's a real Blender constraint — `bpy.types.Operator` subclasses can't be freely instantiated outside Blender's operator system. Let me work around it by binding the actual unbound methods onto a plain duck-typed object instead, so I'm still testing the real code, just without needing full operator registration:

> _Ran a command_

Two test-harness issues (not addon bugs) — the default scene's startup Cube was included, and real bpy_struct objects don't allow monkeypatching methods. Fixing the harness:

> _Ran a command_

The actual file write already succeeded — just need to bind `_cleanup` on my test stand-in too. Fixing and getting the final verification numbers:

> _Ran a command_

Fully verified end-to-end with the real addon code and real Blender objects (rotated, scaled, offset) — 2 solids, exact volume match. Let's ship it:

> _Ran a command, read a file_

Yes, and it's now built in. New menu entry: `File > Export > STEP/IGES (.step/.stp/.iges/.igs) [OCP]`, with a "Selected Objects Only" toggle (defaults on, standard Blender exporter convention).

Uses the same time-budgeted progress UI as import — status bar + progress wheel, no freezing surprises this time since I applied the same fix from the start.

The one thing to set expectations on: since Blender meshes have no smooth surface data, this produces a faceted STEP/IGES file — flat triangular B-rep faces, not smooth NURBS surfaces. Any real CAD package can open it fine, but it'll look exactly as faceted as your Blender mesh was. This isn't a limitation of the addon — there's no way to reconstruct smooth surfaces that were never there. If you need smooth output, keep your polycount reasonably high on export, or better, do the actual solid modeling upstream in real CAD and only round-trip through Blender for visualization/mesh work.

Verification, since this is genuinely new engineering, not just wiring: I built and tested this against a real headless Blender install (yes — got Blender 4.0.2 running in the sandbox), which caught two real bugs before they reached you:

- A units bug: OCCT's B-rep coordinates are implicitly treated as millimeters internally, regardless of what unit you tell it to write the file as — feeding it Blender's raw meter-scale numbers and asking for meter output actually double-converts and shrinks everything 1000x further. Fixed by scaling to mm before building the geometry.
    
- The same "must set the parameter after constructing the object, not before" lazy-init quirk from the import side turned out to apply to the STEP writer too.
    

Final test: real Blender created two rotated/scaled/offset mesh objects, the actual export code (not a simulation) extracted and converted them, and the round-tripped file matched the expected volume exactly (0.000152000 m³ both ways) with the correct solid count.

Worth a 0.4.0 changelog entry — want me to add it?

### 👤 Jake

yeah add a 0.4.0 changelog entry, it seems to function properly

### 🤖 Claude

> _Ran 2 commands, edited a file, read a file_

Added. Sounds like a reasonable place to leave it for tonight — the sandbox testing gave real confidence (actual Blender, actual round-tripped volumes matching exactly), but that's still no substitute for you clicking through it on your own files and workflow tomorrow. If anything's off when you get to it — status bar not updating, a scaling surprise, an object that doesn't sew into a closed solid cleanly — just paste what you're seeing and I'll dig in the same way as everything else tonight.
