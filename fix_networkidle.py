"""Quick script to replace all 'networkidle' with 'load' in the codebase."""
import os

files = ["auth.py", "google_classroom_scraper.py", "brightspace_scraper.py"]
for f in files:
    path = os.path.join(os.path.dirname(__file__), f)
    text = open(path, encoding="utf-8").read()
    count = text.count('"networkidle"')
    new_text = text.replace('"networkidle"', '"load"')
    open(path, "w", encoding="utf-8").write(new_text)
    print(f"{f}: replaced {count} occurrences")
