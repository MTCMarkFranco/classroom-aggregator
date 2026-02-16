"""Extract full HTML for the first assignment item on the classwork page."""
import re

html = open("debug_html/gc_classwork.html", encoding="utf-8").read()

# Find first data-stream-item container and extract a larger block
# Look for the wrapper div with data-stream-item-id
pattern = r'data-stream-item-id="844547120424"'
idx = html.find(pattern)
if idx == -1:
    print("Pattern not found")
else:
    # Go back to find the opening <div
    start = html.rfind("<div", 0, idx)
    # Find the next assignment block to estimate boundary
    next_item = html.find('data-stream-item-id="825485860420"', idx)
    if next_item == -1:
        next_item = idx + 5000
    end = min(next_item, idx + 5000)
    
    block = html[start:end]
    # Pretty print by adding newlines before tags
    block = re.sub(r"<", "\n<", block)
    # Remove excess whitespace
    lines = [l.strip() for l in block.split("\n") if l.strip()]
    for line in lines[:80]:
        print(line)
