#import "MeltyHost.h"
#import "MeltyRenderer.h"
#import <UIKit/UIKit.h>
#include <Python/Python.h>
#include <atomic>

#if PY_MAJOR_VERSION != 3 || PY_MINOR_VERSION != 13
#error "The iOS host requires CPython 3.13"
#endif

static __weak MeltyHost *gHost;

static PyObject *requestFrame(PyObject *, PyObject *) {
    [gHost requestFrame];
    Py_RETURN_NONE;
}

static PyObject *writeLog(PyObject *, PyObject *args) {
    const char *text;
    if (!PyArg_ParseTuple(args, "s", &text)) return nullptr;
    [gHost writeLog:[NSString stringWithUTF8String:text]];
    Py_RETURN_NONE;
}

static PyObject *setKeyboardVisible(PyObject *, PyObject *args) {
    int visible;
    if (!PyArg_ParseTuple(args, "p", &visible)) return nullptr;
    [gHost setKeyboardVisible:visible];
    Py_RETURN_NONE;
}

static PyMethodDef nativeMethods[] = {
    {"request_frame", requestFrame, METH_NOARGS, "Wake display pacing."},
    {"write_log", writeLog, METH_VARARGS, "Write to the on-device host log."},
    {"set_keyboard_visible", setKeyboardVisible, METH_VARARGS, "Show or hide text input."},
    {nullptr, nullptr, 0, nullptr},
};
static PyModuleDef nativeModule = {
    PyModuleDef_HEAD_INIT, "_melty_ios", nullptr, -1, nativeMethods,
    nullptr, nullptr, nullptr, nullptr,
};
PyMODINIT_FUNC PyInit__melty_ios(void) { return PyModule_Create(&nativeModule); }

static NSString *pythonError() {
    // Fetch first: invoking traceback while an exception is set is invalid.
    PyObject *exception = PyErr_GetRaisedException();
    if (!exception) return @"Unknown Python failure";
    PyObject *traceback = PyImport_ImportModule("traceback");
    PyObject *parts = traceback ? PyObject_CallMethod(traceback, "format_exception", "O", exception) : nullptr;
    PyObject *separator = PyUnicode_FromString("");
    PyObject *joined = parts && separator ? PyUnicode_Join(separator, parts) : nullptr;
    PyObject *description = joined ? joined : PyObject_Str(exception);
    const char *utf8 = description ? PyUnicode_AsUTF8(description) : nullptr;
    NSString *message = utf8 ? [NSString stringWithUTF8String:utf8] : @"Unable to format Python failure";
    Py_XDECREF(description);
    Py_XDECREF(separator);
    Py_XDECREF(parts);
    Py_XDECREF(traceback);
    Py_DECREF(exception);
    PyErr_Clear();
    return message;
}

static const char *kindName(melty::InputKind kind) {
    switch (kind) {
        case melty::InputKind::Begin: return "touch_begin";
        case melty::InputKind::Move: return "touch_move";
        case melty::InputKind::End: return "touch_end";
        case melty::InputKind::Cancel: return "touch_cancel";
        case melty::InputKind::CancelAll: return "cancel_all";
        case melty::InputKind::Text: return "text";
        case melty::InputKind::Backspace: return "backspace";
    }
}

@interface MeltyHost () <CAMetalDisplayLinkDelegate>
@end

@implementation MeltyHost {
    CAMetalLayer *_layer;
    CAMetalDisplayLink *_displayLink;
    id<MTLDevice> _device;
    id<MTLCommandQueue> _commands;
    id<MeltyRenderer> _renderer;
    NSThread *_thread;
    dispatch_semaphore_t _gpuAvailable;
    melty::InputQueue _input;
    std::atomic<bool> _active;
    std::atomic<bool> _dirty;
    std::atomic<bool> _wakeScheduled;
    std::atomic<double> _scale;
    std::atomic<double> _width;
    std::atomic<double> _height;
    BOOL _failed;
    BOOL _initialized;
    PyObject *_bootstrap;
    void (^_status)(NSString *);
    void (^_keyboard)(BOOL);
    NSString *_logPath;
}

- (instancetype)initWithLayer:(CAMetalLayer *)layer
                       status:(void (^)(NSString *))status
                     keyboard:(void (^)(BOOL))keyboard {
    if ((self = [super init])) {
        _layer = layer;
        _status = [status copy];
        _keyboard = [keyboard copy];
        _device = MTLCreateSystemDefaultDevice();
        _layer.device = _device;
        _layer.pixelFormat = MTLPixelFormatBGRA8Unorm;
        _layer.framebufferOnly = YES;
        _layer.maximumDrawableCount = 2;
        _commands = [_device newCommandQueue];
        _gpuAvailable = dispatch_semaphore_create(1);
        _scale = 1;
        _width = 0;
        _height = 0;
        _active = false;
        _dirty = true;
        _wakeScheduled = false;
    }
    return self;
}

- (void)start {
    NSAssert([NSThread isMainThread], @"Start the host on UIKit's main thread");
    if (_thread) return;
    gHost = self;
    _thread = [[NSThread alloc] initWithTarget:self selector:@selector(run) object:nil];
    _thread.name = @"Melty render/Python";
    _thread.qualityOfService = NSQualityOfServiceUserInteractive;
    [_thread start];
}

- (void)fail:(NSString *)message {
    _failed = YES;
    _displayLink.paused = YES;
    [self writeLog:[message stringByAppendingString:@"\n"]];
    dispatch_async(dispatch_get_main_queue(), ^{ self->_status(message); });
}

- (NSDictionary *)configuration {
    NSFileManager *fm = NSFileManager.defaultManager;
    NSURL *documents = [fm URLsForDirectory:NSDocumentDirectory inDomains:NSUserDomainMask].firstObject;
    NSURL *support = [fm URLsForDirectory:NSApplicationSupportDirectory inDomains:NSUserDomainMask].firstObject;
    NSURL *cache = [fm URLsForDirectory:NSCachesDirectory inDomains:NSUserDomainMask].firstObject;
    NSURL *logDirectory = [support URLByAppendingPathComponent:@"melty-code-editor" isDirectory:YES];
    NSError *error = nil;
    if (![fm createDirectoryAtURL:logDirectory withIntermediateDirectories:YES attributes:nil error:&error]) {
        [self fail:error.localizedDescription];
        return nil;
    }
    _logPath = [[logDirectory URLByAppendingPathComponent:@"ios-host.log"] path];
    // Diagnostics stay private; user programs use the editor task output UI.
    [@"" writeToFile:_logPath atomically:YES encoding:NSUTF8StringEncoding error:nil];
    NSDictionary *settings = [NSDictionary dictionaryWithContentsOfURL:
        [NSBundle.mainBundle URLForResource:@"HostSettings" withExtension:@"plist"]];
    return @{
        @"app_id": @"melty-code-editor",
        @"documents": documents.path,
        @"workspace": [[documents URLByAppendingPathComponent:@"Projects"] path],
        @"application_support": support.path,
        @"cache": cache.path,
        @"renderer_available": @(_renderer != nil),
        @"entry_module": settings[@"entry_module"] ?: @"melty_ios_app",
    };
}

- (BOOL)initializePython:(NSDictionary *)configuration {
    if (PyImport_AppendInittab("_melty_ios", PyInit__melty_ios) == -1) {
        [self fail:@"Unable to register the native Python host module"];
        return NO;
    }
    PyPreConfig preconfig;
    PyPreConfig_InitIsolatedConfig(&preconfig);
    preconfig.utf8_mode = 1;
    PyStatus status = Py_PreInitialize(&preconfig);
    if (PyStatus_Exception(status)) {
        [self fail:[NSString stringWithUTF8String:status.err_msg ?: "Python pre-initialization failed"]];
        return NO;
    }

    NSString *bundle = NSBundle.mainBundle.resourcePath;
    PyConfig config;
    PyConfig_InitIsolatedConfig(&config);
    config.buffered_stdio = 0;
    config.write_bytecode = 0;
    config.install_signal_handlers = 1;
    config.parse_argv = 0;
    config.site_import = 0;  // No ambient .pth/sitecustomize code during bootstrap.
    config.module_search_paths_set = 1;
    status = PyConfig_SetBytesString(&config, &config.home,
        [[bundle stringByAppendingPathComponent:@"python"] fileSystemRepresentation]);
    if (!PyStatus_Exception(status)) {
        status = PyConfig_SetBytesString(&config, &config.program_name, "melty-code-editor");
    }
    for (NSString *relative in @[@"python/lib/python3.13", @"python/lib/python3.13/lib-dynload",
                                @"host", @"app", @"app_packages"]) {
        if (PyStatus_Exception(status)) break;
        wchar_t *path = Py_DecodeLocale([[bundle stringByAppendingPathComponent:relative] fileSystemRepresentation], nullptr);
        if (!path) {
            status = PyStatus_NoMemory();
            break;
        }
        status = PyWideStringList_Append(&config.module_search_paths, path);
        PyMem_RawFree(path);
    }
    if (!PyStatus_Exception(status)) status = Py_InitializeFromConfig(&config);
    NSString *failure = PyStatus_Exception(status)
        ? [NSString stringWithUTF8String:status.err_msg ?: "Python initialization failed"] : nil;
    PyConfig_Clear(&config);
    if (failure) {
        [self fail:failure];
        return NO;
    }

    _bootstrap = PyImport_ImportModule("melty_ios_bootstrap");
    if (!_bootstrap) {
        [self fail:pythonError()];
        PyEval_SaveThread();
        return NO;
    }
    NSData *data = [NSJSONSerialization dataWithJSONObject:configuration options:0 error:nil];
    PyObject *json = PyImport_ImportModule("json");
    NSString *string = [[NSString alloc] initWithData:data encoding:NSUTF8StringEncoding];
    PyObject *argument = json ? PyObject_CallMethod(json, "loads", "s", string.UTF8String) : nullptr;
    PyObject *result = _bootstrap && argument
        ? PyObject_CallMethod(_bootstrap, "initialize", "O", argument) : nullptr;
    _initialized = result != nullptr;
    if (!result) [self fail:pythonError()];
    else dispatch_async(dispatch_get_main_queue(), ^{ self->_status(@""); });
    Py_XDECREF(result);
    Py_XDECREF(argument);
    Py_XDECREF(json);
    // User worker threads can run while the native host waits for a frame.
    PyEval_SaveThread();
    return _initialized;
}

- (void)run {
    @autoreleasepool {
        NSRunLoop *runLoop = NSRunLoop.currentRunLoop;
        [runLoop addPort:[NSMachPort port] forMode:NSDefaultRunLoopMode];
        if (!_device || !_commands) {
            [self fail:@"This device did not provide a usable Metal device/command queue"];
        } else {
            Class rendererClass = NSClassFromString(@"MeltyMetalRenderer");
            if (rendererClass && [rendererClass conformsToProtocol:@protocol(MeltyRenderer)]) {
                NSError *error = nil;
                _renderer = [(id<MeltyRenderer>)[rendererClass alloc] initWithDevice:_device error:&error];
                if (!_renderer) [self fail:error.localizedDescription ?: @"Metal adapter initialization failed"];
            }
            NSDictionary *config = [self configuration];
            if (config && !_failed) [self initializePython:config];
            _displayLink = [[CAMetalDisplayLink alloc] initWithMetalLayer:_layer];
            _displayLink.delegate = self;
            _displayLink.preferredFrameLatency = 1;
            _displayLink.preferredFrameRateRange = CAFrameRateRangeMake(30, 120, 120);
            _displayLink.paused = YES;
            [_displayLink addToRunLoop:runLoop forMode:NSRunLoopCommonModes];
        }
        // Suspension pauses frames, not this run loop: queued persistence work
        // must still execute. The embedded interpreter lives until process exit.
        for (;;) {
            @autoreleasepool {
                [runLoop runMode:NSDefaultRunLoopMode beforeDate:NSDate.distantFuture];
            }
        }
    }
}

- (void)wake {
    _wakeScheduled = false;
    if (_active.load() && !_failed && _initialized) _displayLink.paused = NO;
}

- (void)requestFrame {
    _dirty = true;
    if (_thread && !_wakeScheduled.exchange(true)) {
        [self performSelector:@selector(wake) onThread:_thread withObject:nil waitUntilDone:NO];
    }
}

- (void)setKeyboardVisible:(BOOL)visible {
    dispatch_async(dispatch_get_main_queue(), ^{ self->_keyboard(visible); });
}

- (void)enqueue:(melty::InputEvent)event {
    _input.push(std::move(event));
    [self requestFrame];
}

- (void)resizeTo:(CGSize)size scale:(CGFloat)scale {
    _width = size.width;
    _height = size.height;
    _scale = scale;
    _layer.drawableSize = CGSizeMake(size.width * scale, size.height * scale);
    [self requestFrame];
}

- (void)setActive:(BOOL)active {
    NSAssert([NSThread isMainThread], @"Scene lifecycle belongs to UIKit");
    _active = active;
    if (!active) _input.push({melty::InputKind::CancelAll, 0, CACurrentMediaTime()});
    // This is a finite request to finish saving, not background execution of
    // arbitrary user code. The OS may still suspend/terminate after expiration.
    __block UIBackgroundTaskIdentifier task = UIBackgroundTaskInvalid;
    if (!active) {
        task = [UIApplication.sharedApplication beginBackgroundTaskWithName:@"Save Melty session" expirationHandler:^{
            if (task != UIBackgroundTaskInvalid) {
                [UIApplication.sharedApplication endBackgroundTask:task];
                task = UIBackgroundTaskInvalid;
            }
        }];
    }
    void (^finish)(void) = ^{
        if (task != UIBackgroundTaskInvalid) {
            [UIApplication.sharedApplication endBackgroundTask:task];
            task = UIBackgroundTaskInvalid;
        }
    };
    NSDictionary *request = @{@"active": @(active), @"finish": [finish copy]};
    [self performSelector:@selector(applyLifecycle:) onThread:_thread withObject:request waitUntilDone:NO];
}

- (void)applyLifecycle:(NSDictionary *)request {
    BOOL active = [request[@"active"] boolValue];
    if (!active) _displayLink.paused = YES;
    if (_initialized) {
        PyGILState_STATE state = PyGILState_Ensure();
        PyObject *result = PyObject_CallMethod(_bootstrap, active ? "resume" : "suspend", nullptr);
        if (!result) [self fail:pythonError()];
        Py_XDECREF(result);
        PyGILState_Release(state);
    }
    if (active) [self requestFrame];
    void (^finish)(void) = request[@"finish"];
    dispatch_async(dispatch_get_main_queue(), finish);
}

- (void)close {
    _active = false;
    [self performSelector:@selector(applyClose) onThread:_thread withObject:nil waitUntilDone:NO];
}

- (void)applyClose {
    [_displayLink invalidate];
    if (_initialized) {
        PyGILState_STATE state = PyGILState_Ensure();
        PyObject *result = PyObject_CallMethod(_bootstrap, "close", nullptr);
        if (!result) [self fail:pythonError()];
        Py_XDECREF(result);
        PyGILState_Release(state);
        _initialized = NO;
    }
    // Do not finalize CPython while a user task may own an extension/thread.
    // iOS owns process termination; suspend() is the reliable save boundary.
}

- (void)metalDisplayLink:(CAMetalDisplayLink *)link needsUpdate:(CAMetalDisplayLinkUpdate *)update {
    if (_failed || !_active.load() || !_initialized) {
        link.paused = YES;
        return;
    }
    if (!_dirty.load()) {
        link.paused = YES;
        return;
    }
    // Never queue another frame behind an unfinished GPU frame.
    if (dispatch_semaphore_wait(_gpuAvailable, DISPATCH_TIME_NOW)) return;
    _dirty = false;
    const auto events = _input.drain();
    PyGILState_STATE state = PyGILState_Ensure();
    PyObject *inputs = PyList_New((Py_ssize_t)events.size());
    for (size_t i = 0; inputs && i < events.size(); ++i) {
        const auto &event = events[i];
        PyObject *item = Py_BuildValue("{s:s,s:K,s:d,s:d,s:d,s:d,s:O,s:s}",
            "kind", kindName(event.kind), "touch_id", (unsigned long long)event.touch,
            "timestamp", event.timestamp, "x", event.x, "y", event.y,
            "pressure", event.pressure, "pencil", event.pencil ? Py_True : Py_False,
            "text", event.text.c_str());
        if (!item) { Py_CLEAR(inputs); break; }
        PyList_SET_ITEM(inputs, (Py_ssize_t)i, item);
    }
    PyObject *info = Py_BuildValue("{s:d,s:d,s:d,s:d,s:d,s:d}",
        "deadline", update.targetTimestamp, "presentation_time", update.targetPresentationTimestamp,
        "now", CACurrentMediaTime(), "width", _width.load(), "height", _height.load(), "scale", _scale.load());
    PyObject *result = inputs && info ? PyObject_CallMethod(_bootstrap, "frame", "OO", info, inputs) : nullptr;
    int again = result ? PyObject_IsTrue(result) : -1;
    if (again < 0) [self fail:pythonError()];
    Py_XDECREF(result);
    Py_XDECREF(info);
    Py_XDECREF(inputs);
    PyGILState_Release(state);
    if (_failed || !_active.load()) {
        dispatch_semaphore_signal(_gpuAvailable);
        return;
    }
    id<MTLCommandBuffer> commands = [_commands commandBuffer];
    NSError *error = nil;
    if (!commands || ![_renderer encodeFrame:commands drawable:update.drawable error:&error]) {
        [self fail:error.localizedDescription ?: @"Metal adapter could not encode a frame"];
        dispatch_semaphore_signal(_gpuAvailable);
        return;
    }
    dispatch_semaphore_t available = _gpuAvailable;
    [commands addCompletedHandler:^(id<MTLCommandBuffer> completed) {
        dispatch_semaphore_signal(available);
        if (completed.status == MTLCommandBufferStatusError) {
            NSString *message = completed.error.localizedDescription ?: @"Metal command buffer failed";
            [self performSelector:@selector(fail:) onThread:self->_thread withObject:message waitUntilDone:NO];
        }
    }];
    [commands commit];
    // CAMetalDisplayLink requires present(), not presentAtTime:.
    [update.drawable present];
    if (again) _dirty = true;
    link.paused = !_dirty.load();
}

- (void)writeLog:(NSString *)text {
    NSLog(@"%@", text);
    @synchronized(self) {
        if (!_logPath) return;
        NSFileHandle *file = [NSFileHandle fileHandleForWritingAtPath:_logPath];
        @try {
            [file seekToEndOfFile];
            if (file.offsetInFile > 4 * 1024 * 1024) {
                [file truncateFileAtOffset:0];
                [file seekToFileOffset:0];
            }
            [file writeData:[text dataUsingEncoding:NSUTF8StringEncoding]];
            [file closeFile];
        } @catch (NSException *exception) {
            NSLog(@"Could not write Python log: %@", exception.reason);
        }
    }
}
@end
