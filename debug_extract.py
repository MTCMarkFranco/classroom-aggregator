"""Extract HTML context around assignment detail links."""
import re

html = open("debug_html/gc_classwork.html", encoding="utf-8").read()

# Find sections around assignment links
pattern = r'.{0,600}href="/u/0/c/[^/]+/a/[^/]+/details".{0,600}'
matches = re.findall(pattern, html, re.DOTALL)

for i, m in enumerate(matches[:3]):
    clean = re.sub(r"\s+", " ", m)
    print(f"=== Match {i} ===")
    print(clean[:500])
    print()
