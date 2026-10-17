// Render an HTML fragment (table / code block / any rich block) to a PNG
// card, for messages whose content has no native iMessage representation.
// Composer-friendly: white rounded card + light border, readable in both
// light and dark bubbles.
//
//   build: clang -fobjc-arc -framework Foundation -framework AppKit \
//            madrid/native/render_html_png.m -o /tmp/render_html_png
//   use:   /tmp/render_html_png block.html out.png [max_width]
//
// stdin carries the HTML when block.html is "-". Prints {width,height,bytes}.
#import <Foundation/Foundation.h>
#import <AppKit/AppKit.h>

static NSString *read_stdin(void) {
  NSMutableData *buf = [NSMutableData data];
  char chunk[65536];
  size_t n;
  while ((n = fread(chunk, 1, sizeof(chunk), stdin)) > 0) [buf appendBytes:chunk length:n];
  return [[NSString alloc] initWithData:buf encoding:NSUTF8StringEncoding];
}

int main(int argc, const char **argv) {
  @autoreleasepool {
    if (argc < 3) { fprintf(stderr, "usage: %s <html-file|-> <out.png> [max_width]\n", argv[0]); return 2; }
    NSString *html = strcmp(argv[1], "-") == 0
        ? read_stdin()
        : [NSString stringWithContentsOfFile:[NSString stringWithUTF8String:argv[1]]
                                  encoding:NSUTF8StringEncoding error:nil];
    NSData *data = [html dataUsingEncoding:NSUTF8StringEncoding];
    if (!data) { fprintf(stderr, "no input\n"); return 2; }
    NSInteger maxWidth = argc > 3 ? atoi(argv[3]) : 720;

    NSDictionary *opts = @{NSDocumentTypeDocumentAttribute: NSHTMLTextDocumentType};
    NSAttributedString *as = [[NSAttributedString alloc] initWithHTML:data
                                                              options:opts
                                                   documentAttributes:nil];
    if (!as.length) { fprintf(stderr, "empty render\n"); return 3; }

    CGFloat pad = 14.0;
    NSRect textRect = [as boundingRectWithSize:NSMakeSize(maxWidth - 2 * pad, CGFLOAT_MAX)
                                       options:NSStringDrawingUsesLineFragmentOrigin];
    NSSize size = NSMakeSize(MIN(maxWidth, ceil(textRect.size.width) + 2 * pad),
                             ceil(textRect.size.height) + 2 * pad);

    NSImage *image = [[NSImage alloc] initWithSize:size];
    [image lockFocus];
    [[NSColor whiteColor] setFill];
    NSBezierPath *card = [NSBezierPath bezierPathWithRoundedRect:NSInsetRect(NSMakeRect(0, 0, size.width, size.height), 1, 1)
                                                         xRadius:10 yRadius:10];
    [card fill];
    [[NSColor colorWithWhite:0.82 alpha:1.0] setStroke];
    card.lineWidth = 1;
    [card stroke];
    [as drawInRect:NSMakeRect(pad, pad, size.width - 2 * pad, size.height - 2 * pad)];
    [image unlockFocus];

    NSBitmapImageRep *rep = [NSBitmapImageRep imageRepWithData:[image TIFFRepresentation]];
    NSData *png = [rep representationUsingType:NSBitmapImageFileTypePNG properties:@{}];
    NSString *out = [NSString stringWithUTF8String:argv[2]];
    if (![png writeToFile:out atomically:NO]) { fprintf(stderr, "write failed\n"); return 4; }
    printf("{\"width\":%ld,\"height\":%ld,\"bytes\":%lu}\n",
           (long)size.width, (long)size.height, (unsigned long)png.length);
  }
  return 0;
}
