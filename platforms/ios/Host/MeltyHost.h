#pragma once
#import <Foundation/Foundation.h>
#import <QuartzCore/QuartzCore.h>
#import "InputQueue.hpp"

@interface MeltyHost : NSObject
- (instancetype)initWithLayer:(CAMetalLayer *)layer
                       status:(void (^)(NSString *))status
                     keyboard:(void (^)(BOOL))keyboard;
- (void)start;
- (void)setActive:(BOOL)active;
- (void)close;
- (void)resizeTo:(CGSize)size scale:(CGFloat)scale;
- (void)enqueue:(melty::InputEvent)event;
- (void)requestFrame;
- (void)setKeyboardVisible:(BOOL)visible;
- (void)writeLog:(NSString *)text;
@end
