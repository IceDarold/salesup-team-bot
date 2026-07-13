import unittest

from google_docs import _research_report_content
from sales_agent import _normalize_report


class ResearchRendererTests(unittest.TestCase):
    def setUp(self):
        self.sources = [{"id": "S1", "title": "Official site", "url": "https://example.com", "excerpt": "Evidence", "type": "official"}]
        self.report = _normalize_report({
            "executive_summary": "Короткий вывод.",
            "sales_brief": {"signals": ["Есть спрос"], "buyer": "COO", "value_proposition": "Убрать ручную работу", "cta": "15 минут"},
            "company_facts": [{"fact": "Компания использует CRM", "evidence": "Указано на сайте", "source_ids": ["S1"], "confidence": "high"}],
            "pains": [{"pain": "Вероятна ручная работа", "evidence": "Нужно проверить", "source_ids": [], "confidence": "high", "automation": "Inbox", "priority": "P1"}],
            "messages": [{"label": "Короткое", "text": "Здравствуйте"}],
            "risks": ["Нет API"],
        }, self.sources)

    def test_unsupported_source_downgrades_to_hypothesis(self):
        pain = self.report["pains"][0]
        self.assertEqual(pain["confidence"], "hypothesis")
        self.assertIn("подтверждения", pain["evidence"])

    def test_renderer_creates_native_sections_and_citations(self):
        content, headings, bullets, hypotheses = _research_report_content("Research — Example", self.report)
        self.assertIn("Что делать сейчас", content)
        self.assertIn("[S1]", content)
        self.assertIn("Реестр источников", content)
        self.assertTrue(headings)
        self.assertTrue(bullets)
        self.assertTrue(hypotheses)
        self.assertTrue(all(start < end for start, end, _ in headings))


if __name__ == "__main__":
    unittest.main()
