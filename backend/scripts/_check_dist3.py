fp = r"D:\zidongh\frontend\dist\assets\DocumentsPage-C8H9gKl8.js"
with open(fp, encoding="utf-8") as f:
    text = f.read()
# Look for /(\.css|\.js)$/.test in the bundle - that's the isInfrastructureFile check
checks = [
    r"profile.xml",
    r"navi.xml",
    r"hedex",
    r"homepage.html",
    r"crumb-link",
    r"filterBy",
    r"meta-collapse",
    r"goToList",
    r"scrollToSection",
    r"detailScrollRef",
    r"scrollToTop",
    r"tocFilter",
    r"manual-toc",
    r"toc-folder",
    r"可见",  # unlikely but try
    r"本文目录",
    r"原始资源文件",
    r"is_active",
]
for c in checks:
    print(f"  {c:20s} : {c in text}")
