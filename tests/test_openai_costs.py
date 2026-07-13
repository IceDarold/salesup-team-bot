import os
import unittest
from unittest.mock import patch

import openai_costs


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def read(self):
        return self.payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class OpenAICostTests(unittest.TestCase):
    def test_returns_none_without_admin_key(self):
        with patch.dict(os.environ, {"OPENAI_ADMIN_API_KEY": ""}):
            self.assertIsNone(openai_costs.today_cost())

    def test_sums_cost_api_buckets(self):
        payload = b'{"data":[{"results":[{"amount":{"value":0.12,"currency":"usd"}},{"amount":{"value":0.03,"currency":"usd"}}]}]}'
        with patch.dict(os.environ, {"OPENAI_ADMIN_API_KEY": "admin-test"}), patch(
            "openai_costs.urllib.request.urlopen", return_value=_Response(payload)
        ):
            self.assertEqual(openai_costs.today_cost(), {"amount": 0.15, "currency": "usd", "period": "today_utc"})


if __name__ == "__main__":
    unittest.main()
