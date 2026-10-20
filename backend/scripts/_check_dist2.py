fp = r"D:\zidongh\frontend\dist\assets\DocumentsPage-C8H9gKl8.js"
with open(fp, encoding="utf-8") as f:
    text = f.read()
# Look for "tocItems" to verify it's at least a recent build
print("length:", len(text))
print()
print("'is_binary' snippet:")
i = text.find("is_binary")
print(text[max(0,i-50):i+100] if i >= 0 else "not found")
print()
print("'computed(' usage:")
print(text.count("computed("))
print()
print("'tocItems' usage:")
print(text.count("tocItems"))
print()
# look at the section rendering
print("'manual-section' snippet:")
i = text.find("manual-section")
print(text[max(0,i-100):i+200] if i >= 0 else "not found")
