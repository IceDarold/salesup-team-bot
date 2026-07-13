import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from bot.telegram_user import TelegramUserService
from bot.handlers import _calendar_keyboard, _hour_keyboard, _minute_keyboard, _relative_time, _scheduled_timezone


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

    def test_picker_has_calendar_hour_pages_and_minute_steps(self):
        tomorrow = datetime.now(_scheduled_timezone()).date() + timedelta(days=1)
        calendar = _calendar_keyboard(tomorrow)
        self.assertGreaterEqual(len(calendar.inline_keyboard), 6)
        self.assertTrue(any("date:pick" in button.callback_data for row in calendar.inline_keyboard for button in row))
        hours = _hour_keyboard(tomorrow, 2)
        self.assertTrue(any(button.callback_data.endswith("hour:pick:12") for row in hours.inline_keyboard for button in row))
        minutes = _minute_keyboard(tomorrow, 12)
        self.assertEqual(sum(len(row) for row in minutes.inline_keyboard), 12)
        self.assertTrue(_relative_time(datetime.now(_scheduled_timezone()) + timedelta(minutes=75)).startswith("через"))


if __name__ == "__main__":
    unittest.main()
