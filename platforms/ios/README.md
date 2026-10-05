# iOS device host and Metal renderer

This target embeds CPython 3.13 in a UIKit app for ARM64 iPhones/iPads on iOS
17+. It now renders Melty's editor with its actual ImGui draw buffers, retained
tile cache, dynamic backgrounds, grayscale text, overlay occlusion, depth masks,
shadow/specular/glow filters and linear HDR scene. Python and native rendering
share the binding's draw streams; the Metal encoder links no additional ImGui.

The complete Python dependency bundle and real editor have been installed and
run on an iPhone. Device checks pass for native dependency APIs, editor frames
and tile caching, autocomplete, and local Python file execution. **The full
editor milestone still needs complete text input and reliable Files/lifecycle
behavior.** Input latency and physical-device EDR behavior remain unmeasured.

Python code runs locally through `compile()`/`exec()` and Melty's hotswap system.
It needs no native JIT entitlement, server or attached computer after installation.
There are no Intel or simulator targets. App Store publication is outside scope;
installing on a device still needs development signing/provisioning.

## Build

Use full Xcode with the iPhoneOS SDK and MetalToolchain. Set `DEVELOPER_DIR` per
command; changing global `xcode-select` is unnecessary. Supply an ARM64 device
`Python.framework` and matching `lib/python3.13` (select only the device slice
if the provider ships an XCFramework). Runtime headers and native Mach-O platform
metadata are checked. ARM64 macOS/simulator extensions are rejected.

Build Melty's pinned ImGui binding with `build_imgui.py` using a host Python
with Cython 3.2.4 installed. The recipe verifies the upstream source archive,
regenerates its Cython wrappers for CPython 3.13, builds both extensions and
checks their 20-byte vertex/32-bit index ABI. It emits a device wheel and a
`build/binding/packages` directory:

```sh
DEVELOPER_DIR=/Applications/Xcode-beta.app/Contents/Developer \
  python3 platforms/ios/build_imgui.py \
  --python-framework /path/to/device/Python.framework
```

The remaining dependency recipes default to `build/runtime/device` and
`build/dependencies`. Use the isolated CPython 3.13 host build environment at
`build/dependencies/tools/bin/python` (build, hatchling 1.32.0, setuptools,
setuptools-scm, wheel, Cython 3.2.4, packaging and CMake). The Rust recipes use
the isolated Cargo/Rustup directories under `build/dependencies`; their
`aarch64-apple-ios` target must be installed. These builds used Rust 1.99.0 and
Maturin 1.15.0. Run from the editor checkout:

```sh
MELTY_IOS_PYTHON=platforms/ios/build/dependencies/tools/bin/python
"$MELTY_IOS_PYTHON" platforms/ios/download_numeric_wheels.py
"$MELTY_IOS_PYTHON" platforms/ios/build_platform_deps.py --jobs 2 \
  --cmake platforms/ios/build/dependencies/tools/bin/cmake
"$MELTY_IOS_PYTHON" platforms/ios/build_rust_deps.py --jobs 2
"$MELTY_IOS_PYTHON" platforms/ios/build_crypto.py --jobs 2
"$MELTY_IOS_PYTHON" platforms/ios/stage_dependencies.py
```

`stage_dependencies.py` builds ordinary MeltyGUI/Pro wheels, installs all 56
distributions and verifies their complete dependency graph using iOS/Python 3.13
markers, including requested transitive extras. It emits clean
`build/editor-bundle/{app,packages,manifest.json}`. Downloads are pinned by hash
in `dependencies.json` and native recipes; manifests record exact artifacts.
No desktop virtualenv, editable install, `.pth` file or nested build output ships.

The native packages are ImGui, NumPy, Pillow, CFFI, LibCST, Pydantic Core, jiter,
rpds-py and cryptography. FreeType and libspatialindex have separate signed
frameworks. Cryptography statically includes OpenSSL; NumPy uses Accelerate.
Watchdog uses its portable polling observer and PyYAML-ft uses its supported
Python implementation. Desktop GL/GLFW, the optional psutil profiler, Torch and
the external Claude Code executable are outside this iOS editor bundle.

```sh
DEVELOPER_DIR=/Applications/Xcode-beta.app/Contents/Developer \
  python3 platforms/ios/generate.py \
  --python-framework /path/to/device/Python.framework \
  --python-lib /path/to/device/lib \
  --app-dir platforms/ios/build/editor-bundle/app \
  --packages-dir platforms/ios/build/editor-bundle/packages \
  --embed-framework platforms/ios/build/dependencies/platform/frameworks/freetype.framework \
  --embed-framework platforms/ios/build/dependencies/platform/frameworks/spatialindex_c.framework \
  --toolkit-dir /path/to/meltygui \
  --team YOURTEAMID --bundle-id your.reverse.dns.identifier

DEVELOPER_DIR=/Applications/Xcode-beta.app/Contents/Developer \
  xcodebuild -project platforms/ios/build/MeltyIOS.xcodeproj \
  -target Melty -configuration Debug -sdk iphoneos -jobs 2 \
  SYMROOT="$PWD/platforms/ios/build/Products" build
```

The build compiles the finite built-in shader ports into `Melty.metallib`,
copies the matching uniform manifest and converts Python extensions into signed
frameworks with `.fwork`/`.origin` markers. Open the generated Xcode project to
select an already configured team/device. `CODE_SIGNING_ALLOWED=NO` provides a
compile/link/package check only; that product is not a runnable signed install.
Generated artifacts and machine-specific paths stay under ignored `build/`.

For physical-device validation, stage with `--device-check`, generate with
`--entry-module device_dependency_check`, build, install and launch. This opt-in
entry imports/exercises native dependencies, checks bundled CA certificates,
Jedi completion, actual editor frames/tile caching and local Python task
execution. Test projects/sessions stay under `Library/Caches`; success is logged
as `DEVICE DEPENDENCY CHECK PASSED` and written to
`Library/Caches/dependency-check/result.json`. Generate again
with the normal `melty_ios_app` entry before the final installation.

## Runtime contract

`MeltyHost` owns one persistent render thread, CPython, a `CAMetalDisplayLink`
and one GPU frame in flight. UIKit queues input without acquiring the Python GIL.
Python prepares the next frame while the GPU executes the previous one. The
host waits for the GPU slot after Python preparation, with the GIL released,
then encodes, commits and presents commands from `_melty_metal`. Three drawables
provide a render target while presentation retains the others; the display link
requests one-frame latency. Native texture mutation requires the owning render thread.
Copied vertices, indices and texture references survive Python frame completion.
The layer uses RGBA16Float with extended linear sRGB and EDR enabled; grayscale
font coverage avoids assuming a mobile panel's subpixel order.

`melty_ios_app.create_app(config, host)` returns an application with `frame`,
`suspend`, `resume`, `close`, and optional `presented` methods. The last is called
after successful native submission, so first-frame work does not start before
submission. `frame(info, events)` returns whether another frame is required.
An idle or failed application stops display pacing. Suspension pauses GPU work
while retaining queued lifecycle work; checkpoints have a finite iOS background
allowance. There is no claim that arbitrary user threads release the GIL in time.
The interpreter survives scene reconnection and is not finalized beneath workers.

Config includes `app_id='melty-code-editor'`, `Documents`, `Documents/Projects`,
Application Support and Caches container URLs. Shared Python path helpers own
internal organization. The Info.plist sharing keys expose the actual Documents
folder in Files; they do not coordinate simultaneous external writes.

Host services are `request_frame()`, `set_keyboard_visible(bool)`,
`set_safe_zone(float)`, `get_clipboard_text()` and `set_clipboard_text(str)`. Clipboard reads use a cache
refreshed on UIKit's main thread; writes dispatch asynchronously to that thread.
Frame width/height and touch x/y are **UIKit points**; `scale` converts to pixels.
Timing values are monotonic seconds. Coalesced touches retain their order and
identity, pressure and Pencil flag. Overflow inserts `cancel_all` so a stalled
frame cannot leave a pointer pressed. Predicted touches are not wired.

`UIKeyboardLayoutGuide` animates a clipping view around the keyboard. Its Metal
sublayer keeps a full-size canvas, and the render thread takes the live viewport
from Core Animation's presentation layer. The final pass copies scene pixels
at native scale at the canvas's top-left, so a keyboard transition never scales
the texture or makes layout jump to the final bounds before the animation.
Touch coordinates and hit bounds follow that same visible view. A reduced
viewport reveals the focused text caret; ordinary frames preserve manual
scrolling. The Wide and sRGB+ color-picker gradients upload signed HDR pixels
through Metal and use the existing per-view deferred resource lifetime.

`Toggles.Mobile.Safezone = 64` reserves a top strip in logical UI pixels (UIKit
points), below the Dynamic Island/status area. Changes apply live; `0` restores
edge-to-edge content. UIKit moves/resizes the canvas itself, keeping drawing,
touch coordinates and keyboard avoidance aligned. Negative values clamp to zero.
Views can branch on `Core.melty.is_touch`, which is true on the native iOS host
and false on desktop. Mobile dropdowns leave their search field unfocused until
tapped; desktop retains its automatic search focus.

The display link requests `CAFrameRateRangeMake(30, 120, 120)` and the app enables
`CADisableMinimumFrameDurationOnPhone`. Active rendering has no 60 FPS timer or
delta-time clamp: Python consumes the actual presentation timestamps. Idle
rendering pauses. The requested 120 Hz is subject to available display cadence,
system policy and CPU/GPU frame cost.
For an instrumented build, set `frame_diagnostics=true` in `HostSettings.plist`
before signing. `frame(info, events)` then includes `info['diagnostics']` with
display-callback/GPU-wait counts, previous frame/GPU/wait duration, screen maximum FPS,
Low Power Mode, thermal state and canvas/viewport/presentation geometry. Normal
builds omit this instrumentation.

## Remaining integration

- Exercise editing, live value/view updates,
  task cancellation and relaunch restoration on a physical device. Pending editor
  edit batches must be durable across suspension before checkpoint guarantees hold.
- Implement `UITextInput` composition/selection, hardware keys,
  automatic device/rotation safe insets, accessibility and touch gestures. Current `UIKeyInput` delivers only
  basic text/backspace events; this is not full editor keyboard support.
- Add Files coordination (`NSFileCoordinator`/`NSFilePresenter`), rename/delete
  conflict handling, resume refresh and container-relative persisted references.
- Port image inspectors, 3D views and arbitrary native GLSL/shader authoring.
  The Metal ports cover the editor's built-in passes; desktop shader APIs and
  live edits to GLSL shader source do not become portable Python automatically.
- Measure device input-to-presentation latency, full-editor 120 Hz pacing, memory pressure
  and GPU pass/buffer allocation costs. Direct UIKit/Metal removes GLFW but does
  not establish a latency result.

Bootstrap failures appear in the native view and Xcode logs. Python stdout/stderr,
caught view exceptions and host failures are written immediately to
`Library/Application Support/melty-code-editor/ios-host.log`. Launches and the
4 MiB size limit rotate the log into `.1` through `.4` (newest first), preserving
diagnostics after a crash/relaunch. Each launch records a timestamp, iOS version
and app build. These files stay outside the watched project tree; retrieve them
with `devicectl device copy from` using the `appDataContainer` domain and
`local.melty.codeeditor` identifier. This is application logging; iOS termination
reports such as memory-pressure kills remain OS diagnostics.

## Validation

To check real UIKit keyboard opening, dismissal and interrupted animations
without the Python simulator bundle, boot a reserved ARM64 iPhone simulator and
run `tests/check_keyboard_animation.py --simulator <UDID>` from this directory
with the Xcode developer directory selected. This compiles the production UIKit
view with a minimal Metal test host, reports canvas scale and animation samples,
and checks that visible content accepts touches throughout the animation. It
does not exercise the embedded Python interpreter or establish device pacing.

```sh
.venv/bin/python -m unittest discover -s platforms/ios/tests -p 'test_*.py' -v
DEVELOPER_DIR=/Applications/Xcode-beta.app/Contents/Developer \
  .venv/bin/python platforms/ios/tests/build_metal_test.py
MELTY_METAL_TEST=1 .venv/bin/python -m unittest discover \
  -s platforms/ios/tests -p 'test_*metal.py' -v
```

Portable tests cover host lifecycle/errors, clipboard, packaging, ABI/platform
rejection and the compiled concurrent C++ input queue. Opt-in tests execute the
same encoder/shaders on real **macOS Metal**, including pixel readback, tile
ownership/copy/resize, text, depth/glow, cleanup, unsafe indices, thread ownership
and a full editor code-file/cache/checkpoint lifecycle with desktop GL/GLFW
imports prohibited. UI regressions cover a stationary caret during viewport
shrink, mobile dropdown focus/typing with desktop autofocus retained,
first-touch color-popover opening, all three picker tabs, dragging,
HDR gradient readback and deferred texture cleanup. Caret tests compare actual
glyph vertices with touch placement at 1x/2x/3x density; exception tests verify
that a failed cached view preserves siblings, later frames and checkpointing.
The opt-in `device_editing_check` entry exercises new-file editing and exception
recovery on the phone, with an isolated session and results under
`Library/Caches/editing-check`. These host tests use
desktop Python/native dependencies; they do not establish their iOS compatibility.

The native source, MSL and custom ImGui extensions were compiled for
`arm64-apple-ios17.0` with Xcode 27.2 beta 2 / iPhoneOS 27.2. The complete package
set now passes dependency resolution, device binary validation and signed
Xcode builds with 56 Python distributions and 106 frameworks. Signatures pass
`codesign --verify --deep --strict` after repeated incremental packaging builds.
On October 4, 2026, the complete bundle passed the device entry-point checks on
Lukas's iPhone Air (iOS 27.0, embedded CPython 3.13.14): all tested dependency
imports/APIs, parsing, font rasterization, image/numeric operations,
cryptographic signing/encryption, bundled CA certificates, Jedi completion,
real editor frames/tile caching, and local Python module execution. This does
not measure touch latency, complete keyboard behavior or EDR correctness.

The opt-in `tests/device_ui_check.py` entry also passed on that phone. Live
safe-zone settings of 64, 92 and 0 produced viewport heights of 848, 820 and 912
points. With the 64-point inset, the actual docked keyboard reduced the viewport
from 848 to 528 points and dismissal restored it; the stationary caret stayed
visible. Dropdowns opened without the keyboard, then focused and accepted text
after a search-field tap. Injected touches opened the color chip on its first
tap and edited its color by dragging. Wide, sRGB and sRGB+ rendered without a
graphics import or ImGui stack failure.

The continuous small-scene cadence probe improved from 53.4 to 117.5 frames/sec
after allowing CPU preparation to overlap the previous GPU frame. Over 2.5
seconds, its median presentation-target interval was 8.34 ms, median Python
time 3.40 ms and median GPU time 6.01 ms. This verifies the native render loop
can exceed 60 FPS; it is not a full-editor throughput or input-latency result.
Test sessions and `result.json` stay under `Library/Caches/ui-check`; the normal
editor entry is restored after the check. Floating keyboards and rotation still
need device interaction coverage.
