import os
from pathlib import Path

desktop = Path(os.path.expanduser("~")) / "Desktop"
print(f"Desktop: {desktop}")

# Search in common locations
locations = [
    desktop,
    desktop.parent,
    Path("D:/"),
    Path("C:/"),
]

results = []
for loc in locations:
    try:
        for root, dirs, files in os.walk(loc):
            for f in files:
                if "AR6" in f or "NetEngine" in f.lower() or "product" in f.lower():
                    full = os.path.join(root, f)
                    size = os.path.getsize(full)
                    if size > 1000000:  # > 1MB
                        results.append(f"{full} ({size/1024/1024:.1f}MB)")
            if len(results) > 20:
                break
    except PermissionError:
        pass
    except OSError:
        pass

print("\n".join(results[:20]) if results else "No large NetEngine files found")
