import unittest

from scrapers.apify_fallback import (
    APIFY_ACTOR_ID,
    PAGE_FUNCTION,
    PORTAL_HOME,
    build_apify_input,
)


class ApifyFallbackInputTests(unittest.TestCase):
    def test_builds_single_start_url_with_normalized_cf(self):
        actor_input = build_apify_input("IT00977110949", "STARTUP")

        self.assertEqual(actor_input["startUrls"], [{
            "url": PORTAL_HOME,
            "userData": {"cf": "00977110949", "tipo": "STARTUP"},
        }])
        self.assertEqual(actor_input["maxPagesPerCrawl"], 1)
        self.assertEqual(actor_input["maxConcurrency"], 1)
        self.assertEqual(actor_input["maxRequestRetries"], 0)

    def test_actor_input_has_no_proxy_or_secret_value(self):
        actor_input = build_apify_input("00977110949")

        self.assertEqual(actor_input["proxyConfiguration"], {"useApifyProxy": False})
        self.assertIn("pageFunction", actor_input)
        self.assertNotIn("apiKey", actor_input)
        self.assertTrue(APIFY_ACTOR_ID)

    def test_page_function_uses_wicket_flow_and_stops_on_block_markers(self):
        self.assertIn("parolaChiaveFld", PAGE_FUNCTION)
        self.assertIn("searchBtn", PAGE_FUNCTION)
        self.assertIn("invalid_request", PAGE_FUNCTION)
        self.assertIn("challenge", PAGE_FUNCTION)
        self.assertNotIn("TSPD", PAGE_FUNCTION)

    def test_invalid_cf_is_rejected_before_request_creation(self):
        with self.assertRaises(ValueError):
            build_apify_input("not-a-cf")


if __name__ == "__main__":
    unittest.main()