"""Native offline-manual package behavior."""
from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path

from app.services.offline_manual import (
    OfflineManualError,
    inspect_offline_manual,
    read_offline_asset,
)


class OfflineManualTest(unittest.TestCase):
    def _package(self) -> Path:
        tmp = tempfile.NamedTemporaryFile(suffix=".hdx", delete=False)
        path = Path(tmp.name)
        tmp.close()
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(
                "profile.xml",
                """<?xml version='1.0' encoding='UTF-8'?>
                <profile><libName>测试手册</libName><productVersion>V1</productVersion>
                <libVersion>01</libVersion><issueDate>2026-01-01</issueDate>
                <language>zh</language><navi>resources/navi.xml</navi>
                <homePage>resources/home.html</homePage></profile>""",
            )
            archive.writestr(
                "resources/navi.xml",
                """<?xml version='1.0' encoding='UTF-8'?>
                <topics><topic txt='安装' url='install.html' id='a'>
                <topic txt='上架设备' url='rack.html' id='b'/></topic></topics>""",
            )
            archive.writestr("resources/home.html", "<h1>测试手册</h1>")
            archive.writestr("resources/install.html", "<h1>安装</h1>")
        return path

    def test_manifest_preserves_official_navigation_tree(self) -> None:
        path = self._package()
        try:
            manifest = inspect_offline_manual(path)
            self.assertEqual(manifest["title"], "测试手册")
            self.assertEqual(manifest["home_path"], "resources/home.html")
            self.assertEqual(manifest["tree"][0]["label"], "安装")
            self.assertEqual(manifest["tree"][0]["path"], "resources/install.html")
            self.assertEqual(manifest["tree"][0]["children"][0]["path"], "resources/rack.html")
        finally:
            path.unlink(missing_ok=True)

    def test_asset_reader_serves_original_html(self) -> None:
        path = self._package()
        try:
            asset = read_offline_asset(path, "resources/install.html")
            self.assertEqual(asset.content, b"<h1>\xe5\xae\x89\xe8\xa3\x85</h1>")
            self.assertEqual(asset.media_type, "text/html")
        finally:
            path.unlink(missing_ok=True)

    def test_asset_reader_rejects_traversal(self) -> None:
        path = self._package()
        try:
            with self.assertRaises(OfflineManualError):
                read_offline_asset(path, "../profile.xml")
        finally:
            path.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
