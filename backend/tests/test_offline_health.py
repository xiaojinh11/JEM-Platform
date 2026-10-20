"""Offline-manual health scanner tests."""
from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path

from app.services.offline_health import (
    _looks_like_vendor_pseudo_query,
    inspect_offline_health,
)


class OfflineHealthTest(unittest.TestCase):
    def _package(
        self,
        members: dict[str, str] | None = None,
        *,
        include_profile: bool = True,
    ) -> Path:
        members = members or {}
        tmp = tempfile.NamedTemporaryFile(suffix=".hdx", delete=False)
        path = Path(tmp.name)
        tmp.close()
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            if include_profile:
                archive.writestr(
                    "profile.xml",
                    "<?xml version='1.0'?><profile>"
                    "<libName>t</libName><navi>resources/navi.xml</navi>"
                    "<homePage>resources/home.html</homePage></profile>",
                )
                archive.writestr(
                    "resources/navi.xml",
                    "<?xml version='1.0'?><topics></topics>",
                )
            for name, content in members.items():
                archive.writestr(name, content)
        return path

    def test_clean_package_scores_one_hundred(self) -> None:
        path = self._package(
            {
                "resources/home.html": "<html><body>hi</body></html>",
                "resources/install.html": '<html><a href="install.html">go</a></html>',
            }
        )
        try:
            health = inspect_offline_health(path)
            self.assertEqual(health.score, 100)
            self.assertEqual(health.grade, "ok")
            self.assertEqual(health.references_missing, 0)
            self.assertEqual(health.references_case_only, 0)
        finally:
            path.unlink(missing_ok=True)

    def test_case_only_mismatch_is_flagged(self) -> None:
        """HTML writes ``mediaQuery.css`` but the archive only has the lowercase form."""
        path = self._package(
            {
                "resources/home.html": '<html><link href="mediaQuery.css" rel="stylesheet"></html>',
                "resources/mediaquery.css": "body{}",
            }
        )
        try:
            health = inspect_offline_health(path)
            self.assertEqual(health.references_missing, 0)
            self.assertGreaterEqual(health.references_case_only, 1)
            case_targets = health.to_dict()["case_only_targets"]
            self.assertEqual(case_targets[0][0].lower(), "resources/mediaquery.css")
        finally:
            path.unlink(missing_ok=True)

    def test_truly_missing_member_counts(self) -> None:
        path = self._package(
            {
                "resources/home.html": '<html><a href="chapter1.html">c1</a><a href="ghost.html">g</a></html>',
                "resources/chapter1.html": "<h1>c1</h1>",
            }
        )
        try:
            health = inspect_offline_health(path)
            self.assertEqual(health.references_missing, 1)
            missing = health.to_dict()["missing_targets"]
            self.assertEqual(missing[0][0], "resources/ghost.html")
        finally:
            path.unlink(missing_ok=True)

    def test_vendor_pseudo_query_is_skipped(self) -> None:
        """Vendor ``cmdqueryname=system-view`` style links must not flood the report."""
        path = self._package(
            {
                "resources/home.html": (
                    '<html>'
                    '<a href="cmdqueryname=quit">q</a>'
                    '<a href="cmdqueryname=ftp+1.1.1.1">f</a>'
                    '</html>'
                ),
            }
        )
        try:
            health = inspect_offline_health(path)
            self.assertEqual(health.references_missing, 0)
            self.assertEqual(health.references_case_only, 0)
            self.assertEqual(health.references_total, 0)
        finally:
            path.unlink(missing_ok=True)

    def test_external_and_anchor_links_are_skipped(self) -> None:
        path = self._package(
            {
                "resources/home.html": (
                    '<html>'
                    '<a href="#section">a</a>'
                    '<a href="https://example.com">b</a>'
                    '<a href="mailto:hi@x">c</a>'
                    '<a href="javascript:void(0)">d</a>'
                    '</html>'
                ),
            }
        )
        try:
            health = inspect_offline_health(path)
            self.assertEqual(health.references_total, 0)
        finally:
            path.unlink(missing_ok=True)

    def test_corrupt_archive_returns_empty_health(self) -> None:
        tmp = tempfile.NamedTemporaryFile(suffix=".hdx", delete=False)
        path = Path(tmp.name)
        tmp.close()
        path.write_bytes(b"not a zip file")
        try:
            health = inspect_offline_health(path)
            self.assertEqual(health.total_members, 0)
            self.assertEqual(health.references_total, 0)
        finally:
            path.unlink(missing_ok=True)


class VendorPseudoQueryTest(unittest.TestCase):
    """Direct unit tests for the vendor-link heuristic."""

    def test_short_keys_with_equals_are_pseudo(self) -> None:
        self.assertTrue(_looks_like_vendor_pseudo_query("cmdqueryname=quit"))
        self.assertTrue(_looks_like_vendor_pseudo_query("system-view"))

    def test_keys_with_plus_are_pseudo(self) -> None:
        self.assertTrue(_looks_like_vendor_pseudo_query("cmdqueryname=ftp+172.16.104.110"))

    def test_real_paths_are_kept(self) -> None:
        self.assertFalse(_looks_like_vendor_pseudo_query("mediaQuery.css"))
        self.assertFalse(_looks_like_vendor_pseudo_query("resources/install.html"))
        self.assertFalse(_looks_like_vendor_pseudo_query("a/b.html"))
        self.assertFalse(_looks_like_vendor_pseudo_query("#anchor"))


if __name__ == "__main__":
    unittest.main()
