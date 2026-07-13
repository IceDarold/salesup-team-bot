import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from bot.telegram_user import TelegramUserService


class ScheduledMessagesTests(unittest.TestCase):
    def setUp(self):
        self.path = tempfile.mktemp(suffix=".sqlite3")
        self.old_path = os.environ.get("TELEGRAM_USER_DB_PATH")
        os.environ["TELEGRAM_USER_DB_PATH"] = self.path
        self.service = TelegramUserService()

    def tearDown(self):
        self.service._db.close()
        if self.old_path is None:
            os.environ.pop("TELEGRAM_USER_DB_PATH", None)
        else:
            os.environ["TELEGRAM_USER_DB_PATH"] = self.old_path

    def test_due_message_requires_confirmation_before_send(self):
        item = self.service.create_scheduled_message(1, "@client", "Привет", datetime.now(timezone.utc) - timedelta(minutes=1))
        due = self.service.claim_due_scheduled_messages()
        self.assertEqual(due[0]["token"], item["token"])
        self.assertEqual(self.service.get_scheduled_message(item["token"])["status"], "awaiting_confirmation")
        self.assertTrue(self.service.begin_scheduled_message_send(item["token"], 1))
        self.assertFalse(self.service.begin_scheduled_message_send(item["token"], 1))
        self.assertTrue(self.service.mark_scheduled_message_sent(item["token"], 1, 42))
        self.assertEqual(self.service.get_scheduled_message(item["token"])["status"], "sent")

    def test_edit_resets_confirmation_to_scheduled(self):
        item = self.service.create_scheduled_message(1, "@client", "Привет", datetime.now(timezone.utc) - timedelta(minutes=1))
        self.service.claim_due_scheduled_messages()
        updated = self.service.update_scheduled_message(item["token"], 1, text="Новый текст")
        self.assertEqual(updated["text"], "Новый текст")
        self.assertEqual(updated["status"], "scheduled")


if __name__ == "__main__":
    unittest.main()
