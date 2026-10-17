// Real-send rich text bridge, stage 1: put an NSAttributedString on the
// pasteboard. Messages.app's composition field is an NSTextView — pasting
// preserves standard formatting (bold/italic/underline/strikethrough/fonts/
// colors/links), the same attributes its own Format menu writes.
//
//   build: clang -fobjc-arc -framework Foundation -framework AppKit \
//            tools/rich_pasteboard.m -o /tmp/rich_pasteboard
//   use:   /tmp/rich_pasteboard --html '<b>hi</b> <a href="https://x">l</a>'
//          /tmp/rich_pasteboard --selftest   # round-trip proof, no sending
//
// Input HTML is the bridge's own standard subset (madrid/rich.py); the
// NSAttributedString is built via NSAttributedString HTML import, so
// anything WebKit maps (incl. colors/spans) rides along.
#import <Foundation/Foundation.h>
#import <AppKit/AppKit.h>

static NSString *escapeForScript(NSString *s) {
  return [s stringByReplacingOccurrencesOfString:@"\\" withString:@"\\\\"];
}

int main(int argc, const char **argv) {
  @autoreleasepool {
    if (argc < 2) {
      fprintf(stderr, "usage: %s --html <html> | --selftest\n", argv[0]);
      return 2;
    }
    NSString *mode = [NSString stringWithUTF8String:argv[1]];

    NSDictionary *opts = @{NSDocumentTypeDocumentAttribute: NSHTMLTextDocumentType};
    NSAttributedString *as = nil;
    if ([mode isEqualToString:@"--selftest"]) {
      NSString *html = @"<b>bold</b> <i>italic</i> <u>underline</u>"
                       @"<s>strike</s> <a href=\"https://example.com/x\">link</a>";
      NSData *data = [html dataUsingEncoding:NSUTF8StringEncoding];
      as = [[NSAttributedString alloc] initWithHTML:data
                                          options:opts
                                       documentAttributes:nil];
    } else if ([mode isEqualToString:@"--html"] && argc >= 3) {
      NSString *html = [NSString stringWithUTF8String:argv[2]];
      NSData *data = [html dataUsingEncoding:NSUTF8StringEncoding];
      as = [[NSAttributedString alloc] initWithHTML:data
                                          options:opts
                                       documentAttributes:nil];
    } else {
      fprintf(stderr, "bad arguments\n");
      return 2;
    }

    // report the attribute runs so callers can verify what will be pasted
    NSMutableString *out = [NSMutableString stringWithString:@"{"];
    [out appendFormat:@"\"text\":\"%@\",\"length\":%lu,\"runs\":[",
         [as.string stringByReplacingOccurrencesOfString:@"\n" withString:@" "],
         (unsigned long)as.length];
    __block NSMutableArray *runList = [NSMutableArray array];
    [as enumerateAttributesInRange:NSMakeRange(0, as.length) options:0
                        usingBlock:^(NSDictionary *attrs, NSRange range, BOOL *stop) {
      NSMutableDictionary *j = [NSMutableDictionary dictionary];
      for (NSString *k in attrs) {
        id v = attrs[k];
        if ([v isKindOfClass:[NSFont class]]) {
          NSFont *f = v;
          NSFontManager *fm = [NSFontManager sharedFontManager];
          if ([fm traitsOfFont:f] & NSBoldFontMask) j[@"bold"] = @1;
          if ([fm traitsOfFont:f] & NSItalicFontMask) j[@"italic"] = @1;
        } else if ([v isKindOfClass:[NSNumber class]] && [k containsString:@"nderline"]) {
          j[@"underline"] = @1;
        } else if ([v isKindOfClass:[NSNumber class]] && [k containsString:@"trikethrough"]) {
          j[@"strikethrough"] = @1;
        } else if ([v isKindOfClass:[NSURL class]]) {
          j[@"link"] = [(NSURL *)v absoluteString];
        } else if ([v isKindOfClass:[NSURL class]] == NO && [k isEqualToString:NSLinkAttributeName]) {
          j[@"link"] = [v description];
        }
      }
      NSString *seg = [as.string substringWithRange:range];
      NSString *attrsJson = [NSJSONSerialization isValidJSONObject:j]
              ? [[NSString alloc] initWithData:[NSJSONSerialization dataWithJSONObject:j options:0 error:nil]
                                      encoding:NSUTF8StringEncoding] : @"{}";
      NSString *segJson = [seg stringByReplacingOccurrencesOfString:@"\"" withString:@"\\\""];
      [runList addObject:[NSString stringWithFormat:@"{\"text\":\"%@\",\"attrs\":%@}", segJson, attrsJson]];
    }];
    [out appendFormat:@"%@]}", [runList componentsJoinedByString:@","]];
    printf("%s\n", out.UTF8String);

    NSPasteboard *pb = [NSPasteboard generalPasteboard];
    [pb clearContents];
    BOOL ok = [pb writeObjects:@[as]];
    printf("{\"pasteboard\":%s}\n", ok ? "true" : "false");
  }
  return 0;
}
