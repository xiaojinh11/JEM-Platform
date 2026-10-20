"""Inspect a Huawei CHM product documentation file.

CHM (Microsoft Compiled HTML Help) is actually an ITSS-compressed archive
containing .html, .hhc (table of contents), .hhk (index), and other assets.
We don't have a CHM library installed, but we can do a quick structural
analysis: read the magic bytes, attempt to decompress the ITSS payload
using Python's `chm` would require a lib. Instead, look at strings for hints
and the overall size / entropy to confirm it's a real CHM (not a ZIP).
"""
from pathlib import Path

p = Path(r'C:\Users\l\Desktop\NetEngine AR600, AR6100, AR6200, AR6300 V300R024 产品文档.chm')
print('size:', p.stat().st_size / 1024 / 1024, 'MB')

with p.open('rb') as f:
    magic = f.read(16)
print('magic:', magic[:4])
# ITSS / CHM starts with ITSF signature: 49 54 53 46 (ITSF)
# It's not a ZIP file despite .chm extension sometimes being used.
