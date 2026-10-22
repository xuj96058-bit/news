import importlib.util
from io import StringIO
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from src.notifications import NotificationConfig


SPEC = importlib.util.spec_from_file_location("news_launcher_under_test", Path(__file__).resolve().parents[1] / "scripts" / "start_news.py")
launcher = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(launcher)


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.config = NotificationConfig("123456:" + "a" * 30, -1001234567890)
        self.root_patch = patch.object(launcher, "ROOT", self.root)
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)
        conversion = patch("src.notification_language.traditional_text", return_value="新聞")
        conversion.start()
        self.addCleanup(conversion.stop)

    def test_existing_healthy_web_server_still_ensures_background_worker(self):
        with patch.object(launcher.sys, "platform", "win32"), \
                patch.object(launcher, "healthy", return_value=True), \
                patch.object(launcher, "matching_server", return_value=True), \
                patch.object(launcher, "ensure_background_service", return_value=True) as worker, \
                patch.object(launcher.webbrowser, "open") as browser:
            self.assertEqual(launcher.main(["--no-browser"]), 0)
            worker.assert_called_once()
            browser.assert_not_called()

    def test_unconfigured_delivery_does_not_spawn_worker(self):
        with patch("src.notifications.load_notification_config", return_value=None), \
                patch.object(launcher.subprocess, "Popen") as process:
            self.assertTrue(launcher.ensure_background_service())
            process.assert_not_called()

    def test_running_service_with_different_destination_is_not_silently_reused(self):
        with patch("src.notifications.load_notification_config", return_value=self.config), \
                patch("src.news_service.service_running", return_value=True), \
                patch("src.news_service.read_status", return_value={"sender_bot_id": self.config.bot_id, "destination_chat_id": -1009999999999}), \
                patch.object(launcher.subprocess, "Popen") as process:
            self.assertFalse(launcher.ensure_background_service())
            process.assert_not_called()

    def test_missing_conversion_dependency_prevents_false_ready_and_shows_install_command(self):
        with patch("src.notifications.load_notification_config", return_value=self.config), \
                patch("src.notification_language.traditional_text", side_effect=ImportError), \
                patch.object(launcher.subprocess, "Popen") as process, \
                patch("sys.stdout", new_callable=StringIO) as output:
            self.assertFalse(launcher.ensure_background_service())
            process.assert_not_called()
            self.assertIn("pip install -r", output.getvalue())
            self.assertIn("requirements.txt", output.getvalue())

    def test_worker_spawn_is_hidden_and_command_contains_no_token(self):
        status = {"heartbeat_at": time.time() + 10, "sender_bot_id": self.config.bot_id,
                  "destination_chat_id": self.config.destination_chat_id}
        with patch("src.notifications.load_notification_config", return_value=self.config), \
                patch("src.news_service.service_running", side_effect=[False, True]), \
                patch("src.news_service.read_status", return_value=status), \
                patch.object(launcher.subprocess, "Popen", return_value=Mock()) as process:
            self.assertTrue(launcher.ensure_background_service())
            command = process.call_args.args[0]
            self.assertEqual(command[-3:], ["-m", "src.news_service", "--worker"])
            self.assertNotIn(self.config.token, str(process.call_args))
            self.assertEqual(process.call_args.kwargs["cwd"], self.root)
            if launcher.sys.platform == "win32":
                self.assertTrue(process.call_args.kwargs["creationflags"] & launcher.subprocess.CREATE_NO_WINDOW)


if __name__ == "__main__":
    unittest.main()
