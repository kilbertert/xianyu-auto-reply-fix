import unittest
from pathlib import Path

from utils.qr_login import QRLoginManager


REPO_ROOT = Path(__file__).resolve().parents[1]


class QRLoginCookieTests(unittest.TestCase):
    def test_browser_cookies_use_playwright_url_shape(self):
        manager = QRLoginManager()

        cookies = manager._build_browser_cookies(
            "https://passport.goofish.com/iv/remote/pc/mini_login_check.htm",
            {"foo": "bar"},
        )

        self.assertEqual(cookies[0]["url"], "https://passport.goofish.com")
        self.assertNotIn("path", cookies[0])

    def test_primary_frontend_action_uses_completed_login_exchange(self):
        index_html = (REPO_ROOT / "static" / "index.html").read_text(encoding="utf-8")
        app_js = (REPO_ROOT / "static" / "js" / "app.js").read_text(encoding="utf-8")

        self.assertIn('class="btn btn-lg me-md-2 flex-fill qr-login-btn" onclick="showQRCodeLogin(\'lite\')"', index_html)
        self.assertNotIn("qr-login-lite-btn", index_html)
        self.assertIn("let qrLoginMode = 'lite';", app_js)
        self.assertIn("function showQRCodeLogin(mode = 'lite')", app_js)


if __name__ == "__main__":
    unittest.main()
