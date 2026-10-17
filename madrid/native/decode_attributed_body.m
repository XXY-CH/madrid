// Ground-truth decoder for a real `attributedBody` blob.
//
// Ground-truth decoder for attributedBody blobs: an NSArchiver (typedstream)
// archive of an NSAttributedString. Decoding is delegated to the same
// Foundation that produced the archive.
//
//   build: clang -fobjc-arc -framework Foundation \
//            tools/decode_attributed_body.m -o /tmp/decode_attributed_body
//   use:   /tmp/decode_attributed_body <blob-file> [--with-text]
//
// SECURITY: NSUnarchiver is deprecated because it instantiates arbitrary
// archived classes. Structure only by default; the string text is emitted
// only with --with-text. Run it on your own chat.db copy, never on
// untrusted blobs.
#import <Foundation/Foundation.h>

static NSString *hexHead(NSData *d) {
  const unsigned char *b = d.bytes;
  NSMutableString *s = [NSMutableString string];
  for (NSUInteger i = 0; i < d.length; i++) [s appendFormat:@"%02x", b[i]];
  return s;
}

static NSString *jsonEscape(NSString *s) {
  NSMutableString *o = [NSMutableString string];
  for (NSUInteger i = 0; i < s.length; i++) {
    unichar c = [s characterAtIndex:i];
    switch (c) {
      case '"': [o appendString:@"\\\""]; break;
      case '\\': [o appendString:@"\\\\"]; break;
      case '\n': [o appendString:@"\\n"]; break;
      case '\r': [o appendString:@"\\r"]; break;
      case '\t': [o appendString:@"\\t"]; break;
      default:
        if (c < 0x20) [o appendFormat:@"\\u%04x", c];
        else [o appendFormat:@"%C", c];
    }
  }
  return o;
}

int main(int argc, const char **argv) {
  @autoreleasepool {
    if (argc < 2) { fprintf(stderr, "usage: %s <blob-file> [--with-text]\n", argv[0]); return 2; }
    BOOL withText = (argc > 2 && strcmp(argv[2], "--with-text") == 0);
    NSData *blob = [NSData dataWithContentsOfFile:[NSString stringWithUTF8String:argv[1]]];
    if (blob == nil) { fprintf(stderr, "cannot read %s\n", argv[1]); return 2; }

    NSMutableString *out = [NSMutableString string];
    [out appendString:@"{"];
    [out appendFormat:@"\"bytes\":%lu", (unsigned long)blob.length];
    [out appendFormat:@",\"magic\":\"%@\"",
         hexHead([blob subdataWithRange:NSMakeRange(0, MIN((NSUInteger)16, blob.length))])];

    id obj = [NSUnarchiver unarchiveObjectWithData:blob];
    if (obj == nil) {
      [out appendString:@",\"unarchived\":false}"];
      printf("%s\n", out.UTF8String);
      return 1;
    }
    [out appendFormat:@",\"unarchived\":true,\"class\":\"%@\"", NSStringFromClass([obj class])];

    if ([obj isKindOfClass:[NSAttributedString class]]) {
      NSAttributedString *as = (NSAttributedString *)obj;
      [out appendFormat:@",\"length\":%lu", (unsigned long)as.length];
      if (withText) [out appendFormat:@",\"text\":\"%@\"", jsonEscape(as.string)];

      NSMutableString *runs = [NSMutableString stringWithString:@"["];
      __block BOOL first = YES;
      [as enumerateAttributesInRange:NSMakeRange(0, as.length) options:0
                          usingBlock:^(NSDictionary *attrs, NSRange range, BOOL *stop) {
        for (NSString *key in [attrs.allKeys sortedArrayUsingSelector:@selector(compare:)]) {
          id value = attrs[key];
          if (!first) [runs appendString:@","];
          first = NO;
          NSMutableString *v = [NSMutableString string];
          if ([value isKindOfClass:[NSNumber class]]) {
            [v appendFormat:@"%@", value];
          } else if ([value isKindOfClass:[NSString class]]) {
            [v appendFormat:@"\"%@\"", jsonEscape((NSString *)value)];
          } else if ([value isKindOfClass:[NSURL class]]) {
            [v appendFormat:@"\"%@\"", jsonEscape([(NSURL *)value absoluteString])];
          } else {
            [v appendFormat:@"\"<%@>\"", NSStringFromClass([value class])];
          }
          [runs appendFormat:@"{\"name\":\"%@\",\"value\":%@,\"location\":%lu,\"length\":%lu}",
               jsonEscape(key), v, (unsigned long)range.location, (unsigned long)range.length];
        }
      }];
      [runs appendString:@"]"];
      [out appendFormat:@",\"runs\":%@", runs];

      NSMutableDictionary<NSString *, NSNumber *> *counts = [NSMutableDictionary dictionary];
      [as enumerateAttributesInRange:NSMakeRange(0, as.length) options:0
                          usingBlock:^(NSDictionary *attrs, NSRange range, BOOL *stop) {
        for (NSString *key in attrs) counts[key] = @([counts[key] integerValue] + 1);
      }];
      NSMutableString *names = [NSMutableString stringWithString:@"["];
      BOOL f2 = YES;
      for (NSString *key in [counts.allKeys sortedArrayUsingSelector:@selector(compare:)]) {
        if (!f2) [names appendString:@","];
        f2 = NO;
        [names appendFormat:@"{\"name\":\"%@\",\"runs\":%@}", jsonEscape(key), counts[key]];
      }
      [names appendString:@"]"];
      [out appendFormat:@",\"attributeNames\":%@", names];
    }
    [out appendString:@"}"];
    printf("%s\n", out.UTF8String);
  }
  return 0;
}
