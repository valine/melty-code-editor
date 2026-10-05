# melty_code_editor

The sibling of `melty_text_editor`, built on **MeltyGUI Pro’s Code Editor**
instead of the bare text view: `editor.py` draws `draw_code_editor` where the
text editor draws `draw_text`.

The app hosts a persisted tile workspace below the menu bar. Drag a tile corner
inward to split it, then use its dropdown to select `draw_main_editor` or
`draw_placeholder`. Editor tiles share the open-file model while keeping separate
tab selection, scrolling, comparison and project state. Open and Run commands
target the last active editor. `app_model.py` owns the layout; `tile_views.py`
contains the registered render functions. The existing `melty-code-editor`
session and saved tab list remain in use.

    ./melty-code-editor FILE [FILE ...]      # each file is a tab; the first is selected
    melty-code-editor.cmd FILE [FILE ...]    # the same on Windows (.venv\Scripts)

What you get is the studio's editor window as an OS window: a tab per file
along the bottom (close buttons, drag to reorder), the editor above it with
Python highlighting, folds, search, autocomplete and analysis, the **Compare
With** dropdown (git HEAD, the file on disk, any recent commit, rendered as
an editable side-by-side diff) and the nav back / forward buttons.
The editor window leads with the file browser's **shortcuts column**
(`draw_code_editor(show_shortcuts=True)`: home, the XDG folders, the root,
then the projects), the divider next to it draggable and remembered; a
click selects that project without opening a dialog or changing tabs.
`draw_code_editor` injects an `EditorProjectState` per view (or accepts one
as `project_state=`). It persists `selected_project` and the Compare With
choice per repository. The selected project supplies the git changed-file
list and commit dropdown; clicking a changed file opens that project's file.

Symbol navigation and colours follow the **file's owning project**. The nearest
marked folder or project marker wins, including a nested project inside a parent.
Ctrl+B resolves definitions and finds usages within that project, including
unsaved buffers. Source imports use the root and its `src` directory; dependency
resolution also uses its `.venv` (or `venv`). Static completions use that same
owner's environment. Selecting a different project for git comparison does not
change a file's symbol context.

The bottom of the project column shows the selected project's venv and its status.
**Change…** selects an environment folder (including hidden `.venv` folders);
**Auto** clears the override. Overrides are shared project metadata and are used
by symbol analysis too. Environments inside the project are stored as relative
paths. Invalid folder selections leave the current association intact.

New selections default to HEAD, and non-git folders show no git changes. **Projects** are folders marked in melty's shared file-meta store
(`meltygui_pro.mark_project`; the flag rides `~/.melty/file_meta.pkl` beside the
tints, so the studio and every other melty app see it too). The Projects
menu: **Add Folder…** (the file browser as a folder picker), **Mark ▸** an
open tab's project (its git root / project marker, not marked yet), and
**Unmark ▸** one; right-clicking a folder in the Open dialog offers the
same. **Search → Search…** (Ctrl+Shift+F) is melty's global search, Code
tab only: files, classes, defs and their call sites across the marked
projects, a hit opening in the editor at the definition. Unmarking a project
removes it from search even while its tabs remain open. Spaces stay part of
one query rather than searching multiple symbols independently. `meltygui_pro.global_search(categories=('Code',),
roots=project_roots, open_files=open_files)` in `editor.py` is the whole of
it; the query and the pick counts persist with the session. Edits go
to melty's file hosts (the studio's deferred-save model: queued in memory,
written to disk when the window closes); there is no Ctrl+S.

## Task sessions and Debug

Tasks owns execution, output and inspection in one `TaskState`. File Editor,
Console and Locals tiles can select that session through their Links menu
(`debugger_state` → the Tasks tile's `task_state`). Each view keeps its own
presentation state. Removing the Tasks tile or changing its renderer keeps the
session running and preserves explicit links; Stop and app exit stop execution.

The environment picker beside Run defaults to **Project venv**. Choose another
known venv to run and debug there without changing the project's analysis or
dependency environment. The choice persists per project in that Tasks session.
On macOS, paired iOS devices appear by device name in the same picker. Run builds
and launches the project's configured Melty app; see the
[iOS setup guide](../meltygui/meltygui/platforms/ios/README.md#run-from-the-editor).

File Editor prepares source keys in the background. Click its line-number gutter
to toggle a persisted breakpoint, then use its **Debug** context-menu entry
(or the Tasks tile's Debug button). Console provides Continue / Into /
Over / Out, and Locals selects captured stack frames. Inline values reuse the
normal live-value markers and `draw_any` renderers. A captured binding retains
its object after resume; other threads and GPU work may still change its contents.

Debug runs Python module tasks in the task's selected interpreter, environment
and working directory. The target needs Python 3.12+ (`sys.monitoring`), but does
not need Melty installed. Breakpoints and stepping cover project modules on the
execution thread, including imports; other threads keep running. Console captures
the process's stdout and stderr on a channel separate from debugger commands.
Shell tasks still use Run. `TaskState.run_local` remains available for explicit
in-process execution with native Python references.

Captured bindings keep their referenced objects alive. Aliases share one model,
and object fields expand asynchronously in bounded pages without evaluating
properties or user `repr`. Refresh updates those observations. A completed run
keeps its worker alive while any inspection or value view holds references.
Stop cancels cooperatively; Force stop also makes its retained values unavailable.
Removing a tile continues to retain the session through `draw_tiles`.

CUDA references use the same driver buffer adapter as Run Visualize. Rendering
maps the producer's allocation on its own GPU, matched by UUID, and does not need
torch in the editor. Explicit allocation leases cover retained slices, queued GPU
work and producer storage resizing. Only the rendered image crosses GPUs/host.
An unsupported allocation reports an error without copying the tensor. Shared
buffer sort/mean/normalization must currently be performed in the owning program;
slice, axis remap and neural-flow views operate on the original allocation.
Automatic detection of unreported mutations, child-thread debugging and network
transport remain future work. The external transport is currently local Unix IPC.

## What draw_code_editor needs that draw_text does not

`draw_text` takes a string. `draw_code_editor` takes an **`OpenFiles`**: the
tab list, one `code_file_io` RenderHost per file from the shared code-host
cache (`src/lsd/gl_gui/model/open_files.py`, a light module with no studio
imports behind it). The app builds one, opens its files into it and posts
`jump_to_path` for the first file so its tab is selected. Passed `None`, as
the studio's `@window` registration does, the editor falls back to the app
model's `Melty.vis.root.open_files`.

File tints (and icons, folder order) come from the **shared file-meta
store**, `file_meta_store()` in `src/lsd/gl_gui/model/file_meta.py`: a
`FileMetaProxy` dict backed by `~/.melty/file_meta.pkl` that the studio and
this app both read and write. Paint a file in the studio and the tab here
takes the colour within half a second (a poller thread watches the file;
writes are debounced and atomic, concurrent edits from two apps merge per
path). `MELTY_FILE_META=/path.pkl` points a process at another store.

The file hosts need nothing from the app. melty draws every registered
RenderHost once a frame (`RenderHost.draw_all`, the studio's `draw_main` host
loop, run from `Surface.frame` in a melty app) — that is what loads a file,
reparses it after an edit and queues its save — and flushes the queued saves
to disk when the loop exits (`app.run`). The studio's plain-file codec refuses
library installs (venv, site-packages, node_modules) and files the process
cannot write (`address.writable_file_refusal`); a refused tab shows the
reason, and the app refuses such a file up front with the same reason.


## Setup

The app uses two sibling checkouts, installed **editable** into its own Python
3.12 venv:

- `../meltygui`: Apache-2.0 UI, text, inspection, file browser and tensor toolkit.
- `../meltygui-pro`: proprietary code editor, projects, Git, environments and
  dependency management.

Native ImGui comes from PyPI. On Apple Silicon macOS it builds from source
using Xcode Command Line Tools when no wheel matches. A local
`../meltygui/dist/release` directory is optional; pass it with `--find-links`
only when using unpublished support wheels. See
[MeltyGUI setup](../meltygui/docs/DEVELOPMENT.md). Run these commands from this
app's directory:

    uv venv .venv --python 3.12
    uv pip install --python .venv/bin/python -r requirements.txt
    ./melty-code-editor ~/some/file.py ~/some/notes.txt

macOS uses OpenGL 4.1 and native Retina backing resolution for fonts, overlays
and cached tiles. Python tasks and debugging work with the selected project
interpreter (Python 3.12+ for debugging). CUDA visualization needs an NVIDIA
system and is unavailable on macOS.

    MELTY_BENCH=1 .venv/bin/python editor.py FILE     # smoke test: exit 0 after the first frame
    MELTY_CODE_DEBUG=1 ./melty-code-editor FILE        # melty's perf trace → /tmp/lsd_symbol_perf.log

The app retains `app_id='melty-code-editor'`, so existing sessions stay at
`~/.local/state/melty-code-editor/session.pkl`. Importing `meltygui_pro` registers
the old saved-class names before the session is loaded. No latent-descent
checkout or legacy `src` imports are needed.

In IntelliJ / PyCharm set the project interpreter to `.venv/bin/python`.
`melty-code-editor.desktop` registers it in the app menu (copy to
`~/.local/share/applications/`). First frame is ~0.4 s, the same as the
text editor (it was 1.6 s while the app model loaded).

### macOS app launcher

After setting up the venv, build and install the native launcher:

    uv pip install --python .venv/bin/python --group macos
    .venv/bin/python macos.py --install --desktop

This installs `~/Applications/Melty Code Editor.app` and a Desktop shortcut.
Double-click either, or drag the app into the Dock. Finder's **Open With** and
files dropped on the app open in the existing editor. Command-Q follows the
normal save-on-exit flow. The bundle uses the existing icon and declares native
Retina support. It registers as an alternative editor without changing your
default file associations. Logs go to
`~/Library/Logs/Melty Code Editor/launch.log` (the previous launch is kept as
`previous.log`).

The launcher uses py2app alias mode: the app's `.venv` and both editable sibling
libraries stay in place. Source edits need no rebuild. Rebuild after moving a
checkout, replacing the venv/Python, or changing the app's name, icon, version or
bundle identifier. This is a local development launcher; a distributable app
would need a separate bundled-runtime build, signing and notarization.

App metadata lives in `[tool.melty.app]` in `pyproject.toml`; `macos.py` owns
packaging and the editor's Finder file events. Build without flags to leave a
bundle in `dist/macos`; `--install` moves it into `~/Applications`. For other
Melty apps, use the same per-app metadata and move shared packaging/event
plumbing into MeltyGUI when a second app needs it. Document handling stays with
the app that owns the open-file model.

## iOS

The editor uses its ordinary `editor.py` entry point on iOS. Its
`[tool.melty.app]` metadata declares the two library dependencies and templates;
MeltyGUI owns the UIKit/Metal host, native lifecycle and packaging commands.
See the [MeltyGUI iOS build guide](../meltygui/meltygui/platforms/ios/README.md).

The Tasks tile delegates execution to Pro's `ProjectExecution`. On iOS, Python
module tasks run in the embedded interpreter; shell commands and project
subprocesses are unavailable. Device checks specific to editing and dependencies
live in `tests/ios`; the reusable host and UI checks live with MeltyGUI.

## Startup profiling

Startup profiling and the native-window import optimizations are documented in
[STARTUP_PROFILE.md](STARTUP_PROFILE.md), with a repeatable fresh-process harness.
The final paired measurements were 519 → 396 ms to the first frame and
652 → 526 ms to present the selected file's text.

## Known gaps

* Read-only files and library installs are refused (studio rule, see above).
* Started through the desktop tooling's `launch` (the seat's own Wayland
  socket) an agent seat can click and type into the app; a plain start binds
  the first `wl_seat` only.

## File menu

**File → New…** or **Ctrl+N** opens a one-line path field under the menu
bar, prefilled with `untitled.py` beside the last opened tab. Edit the path
and press Enter: the file is created (an existing path is simply opened) and
becomes the selected tab. Escape closes the field. A path that cannot be
created shows its error under the bar, the way a refused open does.

A project is any folder. The saved ones (Projects → Add Folder…, the marked
folders of the shared file-meta store) lead the **project selector**, a
dropdown: the saved projects, a divider, the filesystem shortcuts the shortcuts
column shows (home, the XDG folders, the root), a divider, Choose Folder….

The tab bar shows the open files inside the selected project
(`OpenFiles.paths_in`); `open_paths` stays the one persisted list. Selecting a
project brings back the tab it showed last; opening or jumping to a file
outside it (File → Open, search, go to definition, a new project) selects that
file's project: its saved project, else its repository root or folder.
Dragging a tab reorders it among its own project's tabs.
`draw_code_editor(project_tabs=False)` shows every tab.

The **Files** tile (`project_tree.py`) is the selector over that project's
file tree. Folder icons throughout the tree, project selector, link menus, and
file picker use the shared MeltyGUI path-icon renderer and app artwork from a directly contained `.desktop`
file's `Icon` entry when available; explicit metadata icons take precedence.
Absolute and project-relative image paths and installed theme icons are supported.
PNG/XPM icons load through Pillow; SVG icons use `gdk-pixbuf-thumbnailer` when
installed. Missing or broken artwork falls back to the folder glyph. Icons are
decoded in the background, refreshed on file changes, and remain tint controls.

A code editor draws the same selector at the top of its files
column, so either works alone; side by side they link and the editor drops its
selector: the tree's project is the editor's, in both directions (pick in the
tree, or let the editor follow a tab, a crumb or a shortcuts row). The link
icon before the tree's selector lists the editor tiles: "Nearest editor" (the
default) follows the layout, or check any set of them, so every editor can
share one tree's project, each editor can have its own tree, or an unchecked
editor keeps its own project and selector. The link is a `ProjectLink` on the
shared `OpenFiles` (`link_project` / `project_link`); files picked in the tree
open in its first linked editor.

**File → New Project…** opens the New Project window (a child OS window,
`new_project.py`): a template, a name and location, the Python interpreter
(uv's installed ones), and whether to create `.venv` and install the
project's `requirements.txt` into it. Create runs in the background; the new
folder is marked as a project and its main file becomes the selected tab. If
the environment or the install fails, the project still exists and the window
says what failed.

The templates are functions: each sub-folder of `project_templates/` defines
`create(name, ...)`, which returns the project as `{relative path: str |
bytes}`. The function's other parameters are the window's inputs for that
template (`str` a text field, `bool` a checkbox, `Literal[...]` a dropdown,
`Annotated[T, 'Label']` to name it); `name` and `python_version` come from the
window's own fields. `python_basic` is static (its `files/` folder with
`{{placeholders}}`, via `static_files`); `melty_app` is generated (a render
function, a persisted state object, and a requirements.txt that points at the
meltygui install the editor runs on, plus the selected torch version). To add
a template, add a folder with a `create` function; see the package docstring.
`.venv/bin/python -m pytest tests` covers the package.

**File → Open…** or **Ctrl+O** opens melty's file selector. Double-click a
file or select it and press Enter to open and select its tab. Choosing an
already-open file selects that tab; existing tabs and edits stay intact.
Cancel/Escape leaves the editor unchanged. Read-only files and library
installs show the same refusal as command-line opens.

**File → Quit** closes the window and uses melty's normal save-on-exit flow.

## Inline function console

The function's existing Run/Run Visualize and parameters panel also contains a
scrollable, selectable console. Each run starts a fresh transcript, capturing
Python stdout and stderr together. Functions run on a worker so `input()` and
`sys.stdin` can wait while the editor stays responsive. Type into the console's
input field and press Enter or **Send**; **End input** sends EOF. Output stays
visible after the run finishes, and errors remain dismissible in the panel.

A second manual click while the function is running does not start another
copy. Auto Execute keeps the latest requested rerun until the current run
finishes. Capture is scoped to the runner thread; other app threads, native
file-descriptor writes, and subprocess streams keep their normal destinations.


**Dependencies…** in the project information section opens a nested dependency
window. **Assemble lists** reads installed venv package metadata, root
`requirements*.txt` files (including `-r` includes) and `pyproject.toml` project
dependencies, plus static Python imports throughout the owning project. The
three tables show versions or requirement constraints. Import versions are
shown when installed metadata identifies the module; otherwise they show `—`.
Collection runs in the background only on request; it does not install packages.

Dependency tables sort alphabetically (case-insensitive) and label discrepancies:
green **Matching**, grey **Not imported**, red **Missing in venv** or **Version
mismatch**, and orange **Missing requirements**. Installed package metadata maps
module aliases such as PIL to Pillow. Standard-library and project-local imports
are labelled separately. Not imported means no static direct import was found;
tools and transitive dependencies may still be needed. Missing/incorrect installed
versions take priority over missing declarations; unused packages remain grey.

The dependency display is one aligned, three-column table: installed, requirements,
and imports. Each dependency occupies one alphabetical row, with blank cells where
it is absent. Cells contain names and versions only; colours are explained by the
legend above the table. Package/import aliases share a row.

Missing-dependency colours apply to empty cells: red in Installed in venv and
orange in Requirements. Populated matching cells stay green and unused entries
stay grey. Standard-library/local-only rows do not flag missing packages.

**Install** in a red cell runs `uv pip install --python <selected-venv-python>`
with the declared requirement. **Add** in an orange cell appends the installed
package/version to the project's `requirements.txt`, preserving comments and
unsaved edits through the normal editor save queue. **Install all** and **Add
all** perform those actions for all known packages in their columns. The table
refreshes afterwards; progress and failures appear above it. Unknown import
names use **Install…** to enter the package name; bulk install never guesses.

## Dependency collection profiling

### Inline install and import

Open an undefined-name error (for example, `torch` in `torch.rand(2)`) to get
an **Install torch + import torch** fix. Recommendations use `pyproject.toml`
first, then `requirements.txt` (including nested includes), then a curated
list of known packages and aliases such as `np` → NumPy and `PIL` → Pillow.
Declared version constraints are preserved. Unknown names are not guessed.

The fix uses the file's project environment, creates a venv if needed, and
inserts the import only after installation succeeds. Already-installed modules
offer an import-only fix. Resolution runs in the background without assembling
the dependency table; manifest edits and environment changes are checked on
lookup and again before installation. Unsupported custom sources report an
error instead of silently falling back to PyPI.

Validation: 241 targeted tests pass, including environment isolation, aliases,
manifest priority, unsaved manifest edits, installation failures, import
placement, and an offline wheel install into a new venv. The same offline
install-and-import flow was verified through the editor UI. A real Torch install,
manifest save, function run, and full-file run were also verified manually.

[Profiling results and automatic-update strategy](profiles/dependencies/README.md)
include a repeatable collector benchmark and regression coverage using the seven
downloaded PyTorch review repositories.

### Dependency onboarding and running code

Missing-package diagnostics also cover `import torch`. Diagnostics refresh when
opening a file and after changing imports. Click the error icon to open or close fixes beside the code.
Installation shows the target project/environment and live installer output;
background installation continues if the diagnostic popup is closed. **Save dependency in
project** records the installed dependency directly (including Torch's exact
build and backend source). New files receive keyboard focus immediately.

Use **Run → Run file (F5)** to execute the current buffer in the project's Python
and see output in a console. Function Run also uses the selected project venv.
Cross-environment function runs accept JSON parameters and show console output;
inline visualizations still require the editor environment.

[Fix verification and remaining UX limits](profiles/dependencies/ux-review/FIXES.md)
record the real Torch onboarding walkthrough and regression checks.
