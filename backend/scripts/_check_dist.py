fp = r"D:\zidongh\frontend\dist\assets\DocumentsPage-C8H9gKl8.js"
with open(fp, encoding="utf-8") as f:
    text = f.read()
# Check various identifiers
for kw in [
    "toc-folder", "manual-crumbs", "crumb-link", "manual-toolbar",
    "meta-collapse", "scroll-top-btn", "showScrollTop",
    "computed", "visibleSections", "goToList", "nodeMatchesFilter",
    "isInfrastructureFile", "filterByCategory", "filterByVendor",
    "scrollToTop", "markdown-body", "manual-layout", "manual-section",
    "manual-content", "manual-toc", "section-path",
]:
    print(f"  {kw:30s} : {kw in text}")
