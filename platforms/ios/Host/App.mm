#import <UIKit/UIKit.h>
#import "MeltyHost.h"

@interface MeltyView : UIView <UIKeyInput>
@property(nonatomic, strong) MeltyHost *host;
@end

@implementation MeltyView {
    NSMapTable<UITouch *, NSNumber *> *_touchIDs;
    uint64_t _nextTouchID;
}
+ (Class)layerClass { return CAMetalLayer.class; }
- (instancetype)initWithFrame:(CGRect)frame {
    if ((self = [super initWithFrame:frame])) {
        self.multipleTouchEnabled = YES;
        self.backgroundColor = UIColor.blackColor;
        _touchIDs = [NSMapTable weakToStrongObjectsMapTable];
    }
    return self;
}
- (BOOL)canBecomeFirstResponder { return YES; }
- (BOOL)hasText { return YES; }
- (UIKeyboardType)keyboardType { return UIKeyboardTypeDefault; }
- (UITextAutocorrectionType)autocorrectionType { return UITextAutocorrectionTypeNo; }
- (UITextAutocapitalizationType)autocapitalizationType { return UITextAutocapitalizationTypeNone; }
- (UITextSmartQuotesType)smartQuotesType { return UITextSmartQuotesTypeNo; }
- (UITextSmartDashesType)smartDashesType { return UITextSmartDashesTypeNo; }
- (void)insertText:(NSString *)text {
    melty::InputEvent event{melty::InputKind::Text, 0, CACurrentMediaTime()};
    event.text = text.UTF8String;
    [self.host enqueue:std::move(event)];
}
- (void)deleteBackward {
    [self.host enqueue:melty::InputEvent{melty::InputKind::Backspace, 0, CACurrentMediaTime()}];
}
- (void)layoutSubviews {
    [super layoutSubviews];
    CGFloat scale = self.window.screen.scale ?: UIScreen.mainScreen.scale;
    self.contentScaleFactor = scale;
    [self.host resizeTo:self.bounds.size scale:scale];
}
- (void)sendTouches:(NSSet<UITouch *> *)touches event:(UIEvent *)event kind:(melty::InputKind)kind {
    for (UITouch *touch in touches) {
        NSNumber *identity = [_touchIDs objectForKey:touch];
        if (!identity) {
            identity = @(++_nextTouchID);
            [_touchIDs setObject:identity forKey:touch];
        }
        NSArray<UITouch *> *samples = kind == melty::InputKind::Move
            ? ([event coalescedTouchesForTouch:touch] ?: @[touch]) : @[touch];
        for (UITouch *sample in samples) {
            CGPoint point = [sample locationInView:self];
            melty::InputEvent input{kind, identity.unsignedLongLongValue, sample.timestamp};
            input.x = point.x;
            input.y = point.y;
            input.pressure = sample.maximumPossibleForce > 0 ? sample.force / sample.maximumPossibleForce : 0;
            input.pencil = sample.type == UITouchTypePencil;
            [self.host enqueue:std::move(input)];
        }
        if (kind == melty::InputKind::End || kind == melty::InputKind::Cancel) [_touchIDs removeObjectForKey:touch];
    }
}
- (void)touchesBegan:(NSSet<UITouch *> *)touches withEvent:(UIEvent *)event {
    [self sendTouches:touches event:event kind:melty::InputKind::Begin];
}
- (void)touchesMoved:(NSSet<UITouch *> *)touches withEvent:(UIEvent *)event {
    [self sendTouches:touches event:event kind:melty::InputKind::Move];
}
- (void)touchesEnded:(NSSet<UITouch *> *)touches withEvent:(UIEvent *)event {
    [self sendTouches:touches event:event kind:melty::InputKind::End];
}
- (void)touchesCancelled:(NSSet<UITouch *> *)touches withEvent:(UIEvent *)event {
    [self sendTouches:touches event:event kind:melty::InputKind::Cancel];
}
@end

@interface MeltyController : UIViewController
@property(nonatomic, strong) MeltyHost *host;
@end
@implementation MeltyController
- (void)loadView {
    MeltyView *view = [[MeltyView alloc] initWithFrame:UIScreen.mainScreen.bounds];
    self.view = view;
    UILabel *status = [[UILabel alloc] initWithFrame:CGRectZero];
    status.text = @"Starting embedded Python…";
    status.textColor = UIColor.whiteColor;
    status.font = [UIFont monospacedSystemFontOfSize:14 weight:UIFontWeightRegular];
    status.numberOfLines = 0;
    status.translatesAutoresizingMaskIntoConstraints = NO;
    status.userInteractionEnabled = NO;
    [view addSubview:status];
    [NSLayoutConstraint activateConstraints:@[
        [status.leadingAnchor constraintEqualToAnchor:view.safeAreaLayoutGuide.leadingAnchor constant:20],
        [status.trailingAnchor constraintEqualToAnchor:view.safeAreaLayoutGuide.trailingAnchor constant:-20],
        [status.topAnchor constraintEqualToAnchor:view.safeAreaLayoutGuide.topAnchor constant:20],
        [status.bottomAnchor constraintLessThanOrEqualToAnchor:view.safeAreaLayoutGuide.bottomAnchor constant:-20],
    ]];
    __weak MeltyView *weakView = view;
    self.host = [[MeltyHost alloc] initWithLayer:(CAMetalLayer *)view.layer status:^(NSString *message) {
        status.text = message;
        status.hidden = message.length == 0;
    } keyboard:^(BOOL visible) {
        if (visible) [weakView becomeFirstResponder]; else [weakView resignFirstResponder];
    }];
    view.host = self.host;
    [self.host start];
}
@end

// One interpreter for the process, including a scene disconnect/reconnection.
static MeltyController *gController;

@interface MeltySceneDelegate : UIResponder <UIWindowSceneDelegate>
@property(nonatomic, strong) UIWindow *window;
@property(nonatomic, strong) MeltyController *controller;
@end
@implementation MeltySceneDelegate
- (void)scene:(UIScene *)scene willConnectToSession:(UISceneSession *)session options:(UISceneConnectionOptions *)options {
    self.window = [[UIWindow alloc] initWithWindowScene:(UIWindowScene *)scene];
    if (!gController) gController = [MeltyController new];
    self.controller = gController;
    self.window.rootViewController = self.controller;
    [self.window makeKeyAndVisible];
}
- (void)sceneDidBecomeActive:(UIScene *)scene { [self.controller.host setActive:YES]; }
- (void)sceneWillResignActive:(UIScene *)scene { [self.controller.host setActive:NO]; }
- (void)sceneDidDisconnect:(UIScene *)scene { [self.controller.host setActive:NO]; }
@end

@interface MeltyAppDelegate : UIResponder <UIApplicationDelegate>
@end
@implementation MeltyAppDelegate
- (void)applicationWillTerminate:(UIApplication *)application { [gController.host close]; }
- (UISceneConfiguration *)application:(UIApplication *)application
    configurationForConnectingSceneSession:(UISceneSession *)session options:(UISceneConnectionOptions *)options {
    UISceneConfiguration *configuration = [[UISceneConfiguration alloc] initWithName:@"Melty" sessionRole:session.role];
    configuration.delegateClass = MeltySceneDelegate.class;
    return configuration;
}
@end

int main(int argc, char *argv[]) {
    @autoreleasepool {
        return UIApplicationMain(argc, argv, nil, NSStringFromClass(MeltyAppDelegate.class));
    }
}
