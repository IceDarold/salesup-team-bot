import unittest

from sales_agent import _dedupe_claims, _research_goals_met, _source_key, _usable_query


class ResearchSearchTests(unittest.TestCase):
    def test_source_key_removes_tracking_and_trailing_slash(self):
        self.assertEqual(
            _source_key({"url": "https://Example.com/case/?utm_source=x"}),
            "https://example.com/case",
        )

    def test_internal_offer_question_is_not_sent_to_web_search(self):
        self.assertFalse(_usable_query("Уточнить продукт/услугу отправителя"))
        self.assertTrue(_usable_query('site:example.com вакансия 2026'))

    def test_stop_requires_identity_stakeholder_process_and_trigger(self):
        claims = [
            {"claim": "company", "category": "company", "confidence": "high"},
            {"claim": "founder", "category": "stakeholder", "confidence": "high"},
            {"claim": "trigger", "category": "vacancy", "confidence": "medium"},
        ]
        self.assertTrue(_research_goals_met({"goals_closed": ["process"]}, [{"url": str(n)} for n in range(5)], claims))
        self.assertEqual(len(_dedupe_claims(claims + [dict(claims[0])])), 3)


if __name__ == "__main__":
    unittest.main()
