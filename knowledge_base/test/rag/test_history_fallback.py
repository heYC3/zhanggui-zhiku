import os
import unittest
from unittest.mock import MagicMock, patch

from app.clients import mongo_history_utils as history


class HistoryFallbackTests(unittest.TestCase):
    def test_explicit_history_mode_never_connects_to_mongo(self):
        with patch.dict(os.environ, {'CHAT_HISTORY_ENABLE':'false'}), \
             patch.object(history, 'HistoryMongoTool') as connect:
            self.assertEqual(history.get_recent_messages('synthetic'), [])
            self.assertEqual(history.save_chat_message('synthetic', 'user', '上一问'), '')
            self.assertEqual(history.update_message_item_names([], ['合成设备']), 0)
            self.assertEqual(history.clear_history('synthetic'), 0)
            connect.assert_not_called()

    def test_outage_does_not_reconnect_for_every_message(self):
        with patch.dict(os.environ, {'CHAT_HISTORY_ENABLE':'true'}), \
             patch.object(history, '_history_mongo_tool', None), \
             patch.object(history, '_history_retry_after', 0), \
             patch.object(history.time, 'monotonic', return_value=100), \
             patch.object(history, 'HistoryMongoTool', side_effect=OSError('unavailable')) as connect:
            self.assertIsNone(history.get_history_mongo_tool())
            self.assertIsNone(history.get_history_mongo_tool())
            self.assertEqual(connect.call_count, 1)

    def test_recovery_retries_after_backoff(self):
        recovered = object()
        with patch.dict(os.environ, {'CHAT_HISTORY_ENABLE':'true'}), \
             patch.object(history, '_history_mongo_tool', None), \
             patch.object(history, '_history_retry_after', 0), \
             patch.object(history.time, 'monotonic', return_value=100) as clock, \
             patch.object(history, 'HistoryMongoTool', side_effect=[OSError('unavailable'), recovered]):
            self.assertIsNone(history.get_history_mongo_tool())
            clock.return_value = 131
            self.assertIs(history.get_history_mongo_tool(), recovered)

    def test_recent_history_is_latest_messages_in_chronological_order(self):
        tool = MagicMock()
        cursor = tool.chat_message.find.return_value
        cursor.sort.return_value.limit.return_value = [{'text':'最新一问'}, {'text':'上一问'}]
        with patch.object(history, 'get_history_mongo_tool', return_value=tool):
            messages = history.get_recent_messages('synthetic', limit=2)
        self.assertEqual([row['text'] for row in messages], ['上一问', '最新一问'])
        cursor.sort.assert_called_once_with('ts', -1)


if __name__ == '__main__':
    unittest.main()
