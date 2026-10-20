import os, time
src = r"D:\zidongh\frontend\src\pages\DocumentsPage.vue"
dst = r"D:\zidongh\frontend\dist\assets\DocumentsPage-C8H9gKl8.js"
print("src mtime:", time.ctime(os.path.getmtime(src)), "size:", os.path.getsize(src))
print("dst mtime:", time.ctime(os.path.getmtime(dst)), "size:", os.path.getsize(dst))
