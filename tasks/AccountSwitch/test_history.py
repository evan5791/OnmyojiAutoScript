from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tasks.AccountSwitch.history import record_event


class CalendarHistoryTests(unittest.TestCase):
    def test_daily_history_changes_at_midnight_without_waiting_for_jade_refresh(self):
        with patch('tasks.AccountSwitch.history.logger'):
            before = record_event(SimpleNamespace(), 'example', 'android', 'daily_done',
                                  occurred_at=datetime(2026, 10, 31, 23, 59, 59))
            after = record_event(SimpleNamespace(), 'example', 'android', 'awakening_progress',
                                 {'delta_wins': 1}, occurred_at=datetime(2026, 11, 1, 0, 0))
        self.assertEqual(before['day'], '2026-10-31')
        self.assertEqual(after['day'], '2026-11-01')
        self.assertEqual(after['details']['delta_wins'], 1)

    def test_utc_evidence_is_filed_under_its_hong_kong_calendar_date(self):
        with patch('tasks.AccountSwitch.history.logger'):
            event = record_event(SimpleNamespace(), 'example', 'ios', 'login',
                                 occurred_at=datetime(2026, 10, 31, 16, 1, tzinfo=timezone.utc))
        self.assertEqual(event['day'], '2026-11-01')
        self.assertEqual(event['occurred_at'], '2026-11-01T00:01:00+08:00')
