# iOS device host

This is the initial native host/build foundation for **melty-code-editor** on
ARM64 iPhones/iPads running iOS 17+. The agreed milestone remains: create a
project in Files, edit and run Python locally, inspect values, update a live
view, and restore after relaunch. **That milestone is not running yet.** The
Metal renderer and the editor's native-host application adapter are absent;
launch reports that failure explicitly instead of importing the GL entry point.

The host includes UIKit scenes, `CAMetalLayer`, `CAMetalDisplayLink`, an embedded
CPython 3.13 bootstrap, batched input, lifecycle callbacks, and a deterministic
Xcode project generator. No Intel or simulator slice is required. Python source
execution/hotswap uses the interpreter's `compile()`/`exec()`; there is no native
JIT entitlement, server, or attached-computer runtime dependency. Device signing
still requires a suitable Apple development/provisioning identity; an App Store
submission is outside scope.

## Build inputs and commands

Install full Xcode with an iPhoneOS SDK separately. Command Line Tools alone
cannot compile this target. A per-command `DEVELOPER_DIR` selects Xcode without
changing global `xcode-select` settings.

Provide a CPython **3.13 ARM64 iOS device** `Python.framework` and its matching
`lib/python3.13` standard library. If using an XCFramework, select its device
slice. Build every native dependency for that target, including the actual
`meltygui-imgui` binding; ARM64 macOS and simulator wheels are rejected. The
generator checks Python headers and Mach-O platform metadata; the bundle phase
checks extension binaries too. A working host project does not establish that
the editor's entire dependency graph supports iOS.

Assemble two clean input directories: `staged-app` with the editor source and
future `melty_ios_app.py` adapter, and `staged-packages` with installed runtime
packages/resources. Do not pass a desktop virtualenv or editable installation.
No packages are downloaded or resolved by these scripts. The app/package paths
are bundle import roots, never sibling checkout paths. `.pth` startup execution
is disabled; stage real importable packages. Dependent native libraries must
already be device frameworks with valid loader paths; raw vendored `.dylib`
files fail packaging and need a build recipe. Add such frameworks through
`--embed-framework`.

```sh
python3 platforms/ios/generate.py \
  --python-framework /path/to/ios-arm64/Python.framework \
  --python-lib /path/to/ios-arm64/lib \
  --app-dir /path/to/staged-app \
  --packages-dir /path/to/staged-packages \
  --team YOURTEAMID \
  --bundle-id your.reverse.dns.identifier

DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer \
  xcodebuild -project platforms/ios/build/MeltyIOS.xcodeproj \
  -target Melty -configuration Debug -sdk iphoneos \
  SYMROOT="$PWD/platforms/ios/build/Products" build
```

Alternatively open the generated project, select your development team and
physical device, then Build/Run. Provisioning must already be configured; the
command does not request account changes automatically. Generated projects and
local paths live under ignored `build/`. The bundle phase converts extension
modules to signed frameworks with CPython `.fwork`/`.origin` markers. Unsigned
builds can use `CODE_SIGNING_ALLOWED=NO` for compile checks, but cannot be
installed normally on a device. Do not strip the Python framework or extension
resources after signing.

## Application and renderer contracts

Link an Objective-C class named `MeltyMetalRenderer` conforming to
[`Host/MeltyRenderer.h`](Host/MeltyRenderer.h), using repeatable
`--renderer-source /path/to/file.mm` options. The adapter encodes the real
ImGui/custom graphics passes into the host's command buffer. It must use the
same ImGui context/binary as the Python binding. It owns draw resources until
GPU completion; the host owns commit/presentation. There is no placeholder
renderer that claims the OpenGL editor works.

Package `melty_ios_app.py` (or select `--entry-module`) implementing:

```python
def create_app(config, host):
    # Import meltygui_pro before restoring persisted classes.
    # Initialize the eventual native/Metal runtime, not meltygui.boot's GL path.
    return application

# application.frame(frame_info, events) -> bool  # True requests another frame
# application.suspend()                       # call meltygui.checkpoint()
# application.resume()                        # refresh files, request a frame
# application.close()                         # best-effort process exit cleanup
```

`config` supplies `app_id='melty-code-editor'`, the Foundation URLs `documents`,
`workspace` (`Documents/Projects`), `application_support` and `cache`, plus
`renderer_available` and `entry_module`. Support/cache values are container base
directories; the shared runtime path helpers own their internal organization.
The application must handle `checkpoint()` returning false or raising, rather
than reporting a saved session. Move persisted absolute file references to
workspace-relative references before relying on restoration after a changed
container path; choosing sandbox directories alone does not perform migration.

`host.request_frame()` wakes an idle display link and is safe from worker
threads; `host.set_keyboard_visible(bool)` marshals to UIKit. Frame dictionaries
contain `now`, `deadline`, `presentation_time` (monotonic seconds), `width`,
`height` (**UIKit points**), and `scale` (pixels/point). Touch events contain
`kind`, stable `touch_id`, monotonic `timestamp`, `x`, `y` (points), normalized
`pressure`, and `pencil`. Text events carry `text`. Coalesced touch samples are
preserved; queue overflow produces `cancel_all` before subsequent input so a
stalled callback cannot leave a pointer held. Predicted touches are not wired.

UIKit does not acquire the GIL for input. One persistent native render thread
owns interpreter initialization, Python frames, Metal encoding, and lifecycle
work. The GIL is released between calls. A frame returning false sleeps until
the next input/invalidation; failed bootstrap also stops frames. GPU submission
permits one frame in flight. Suspend pauses presentation while keeping queued
persistence work runnable, with a finite iOS background-task allowance. It
cannot guarantee saving if user code holds the GIL until that allowance expires.
The interpreter survives scene reconnection and is not finalized under running
user threads. `close()` is best effort; normal saves must happen before it.

## Remaining device integration

- Implement the Metal renderer and native-host editor adapter; existing GL
  tile caches, shaders, text passes, and desktop launch code are not portable yet.
- Build/install the full CPython 3.13 native dependency set and device-test
  compile/run/debug/hotswap, task output, cancellation and session persistence.
- Implement full `UITextInput` composition/selection, hardware keys, keyboard
  geometry, safe-area layout, accessibility, pointer gestures and touch routing.
  Current `UIKeyInput` is only basic text/backspace delivery.
- Finish Files coordination (`NSFileCoordinator`/`NSFilePresenter`), external
  rename/delete conflicts, watcher refresh on resume, and persisted-path migration.
  The two Info.plist sharing keys expose the actual `Documents` folder in Files;
  they do not themselves synchronize open buffers or grant arbitrary file access.
- Measure device input-to-presentation latency, suspend/resume, memory pressure,
  and sustained behavior on 60/120 Hz devices. No latency has been measured yet.

Bootstrap errors appear in the native view and Xcode logs, with a bounded log
in `Library/Application Support/melty-code-editor/ios-host.log`.

## Checks available without an iOS SDK

```sh
.venv/bin/python -m unittest discover -s platforms/ios/tests -p 'test_*.py' -v
plutil -lint platforms/ios/Host/Info.plist
```

These verify bootstrap/lifecycle/error contracts, device-only generation,
extension signing invocation/markers, rejection of desktop/simulator binaries,
safe packaging paths, and the compiled C++ input queue under concurrent use.
They do **not** compile UIKit/Metal/CPython embedding code or test real signing.
At implementation time this machine has Command Line Tools only:
`xcrun --sdk iphoneos --show-sdk-path` fails and no full Xcode was found.

References: [CPython iOS embedding](https://docs.python.org/3.13/using/ios.html),
[Apple display-link callbacks](https://developer.apple.com/documentation/quartzcore/cametaldisplaylinkdelegate/metaldisplaylink(_:needsupdate:)),
[Files exposure keys](https://developer.apple.com/library/archive/documentation/General/Reference/InfoPlistKeyReference/Articles/LaunchServicesKeys.html).
