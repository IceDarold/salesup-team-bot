import tempfile
import unittest

from research_jobs import ResearchJobStore


class ResearchJobStoreTests(unittest.TestCase):
    def setUp(self):
        self.path = tempfile.mktemp(suffix=".sqlite3")
        self.store = ResearchJobStore(self.path)
        self.job = self.store.create(telegram_user_id=10, chat_id=20, request="https://example.com", progress_message_id=30)

    def test_claim_evidence_and_cancel(self):
        claimed = self.store.claim_next("test", 60)
        self.assertEqual(claimed["id"], self.job["id"])
        self.store.replace_evidence(
            self.job["id"],
            [{"url": "https://example.com", "title": "Example", "relevance": 1}],
            [{"claim": "Example fact", "url": "https://example.com", "confidence": "high"}],
        )
        self.assertEqual(len(self.store.sources(self.job["id"])), 1)
        self.assertEqual(len(self.store.claims(self.job["id"])), 1)
        self.store.add_event(self.job["id"], "tool_result", "web_search — результат", "Example source")
        self.assertEqual(self.store.events(self.job["id"])[0]["kind"], "tool_result")
        self.assertTrue(self.store.cancel(self.job["id"], 10))
        self.assertTrue(self.store.is_cancelled(self.job["id"]))

    def test_only_owner_can_refine(self):
        self.store.update(self.job["id"], status="completed")
        self.assertFalse(self.store.refine(self.job["id"], 999, "проверь рынок"))
        self.assertTrue(self.store.refine(self.job["id"], 10, "проверь рынок"))
        self.assertEqual(self.store.get(self.job["id"])["status"], "queued")

    def test_checkpoint_can_resume_without_erasing_evidence(self):
        self.store.update(self.job["id"], checkpoint={"sources": [{"url": "https://example.com"}], "draft": {"executive_summary": "draft"}})
        self.store.update(self.job["id"], status="partial", stage="Нужна финализация")
        self.assertTrue(self.store.resume(self.job["id"], 10))
        resumed = self.store.get(self.job["id"])
        self.assertEqual(resumed["status"], "queued")
        self.assertEqual(self.store.checkpoint(self.job["id"])["draft"]["executive_summary"], "draft")

    def test_standard_mode_is_bounded(self):
        self.assertEqual(self.job["mode"], "standard")
        self.assertEqual((self.job["max_iterations"], self.job["max_sources"]), (3, 12))


if __name__ == "__main__":
    unittest.main()
