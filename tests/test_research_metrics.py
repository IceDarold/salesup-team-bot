import os
import unittest
from types import SimpleNamespace

import sales_agent
from research_worker import _usage_line


class ResearchMetricsTests(unittest.TestCase):
    def test_tracker_collects_cached_tokens_and_web_search_calls(self):
        token = sales_agent.start_usage_tracking()
        try:
            response = SimpleNamespace(
                usage=SimpleNamespace(
                    input_tokens=100,
                    output_tokens=20,
                    total_tokens=120,
                    input_tokens_details=SimpleNamespace(cached_tokens=40),
                ),
                output=[SimpleNamespace(type="web_search_call"), SimpleNamespace(type="message")],
            )
            sales_agent._record_usage(response)
            self.assertEqual(sales_agent.usage_snapshot(), {
                "input_tokens": 100,
                "cached_input_tokens": 40,
                "output_tokens": 20,
                "total_tokens": 120,
                "web_search_calls": 1,
                "calls": 1,
            })
        finally:
            sales_agent.stop_usage_tracking(token)

    def test_cost_uses_non_cached_cached_output_and_web_search_rates(self):
        old = dict(os.environ)
        try:
            os.environ.update({
                "RESEARCH_INPUT_USD_PER_M_TOKENS": "10",
                "RESEARCH_CACHED_INPUT_USD_PER_M_TOKENS": "2.5",
                "RESEARCH_OUTPUT_USD_PER_M_TOKENS": "40",
                "RESEARCH_WEB_SEARCH_USD_PER_CALL": "0.01",
            })
            line = _usage_line({
                "input_tokens": 1_000_000,
                "cached_input_tokens": 200_000,
                "output_tokens": 100_000,
                "web_search_calls": 3,
            }, 12)
            self.assertIn("(200,000 cached)", line)
            self.assertIn("web search: 3", line)
            self.assertIn("$12.5300", line)
        finally:
            os.environ.clear()
            os.environ.update(old)


if __name__ == "__main__":
    unittest.main()
