import unittest

from scrapers.harvester import PivaSearchFailure, search_card_by_piva
from scrapers.piva_diagnostics import SearchOutcome


class FakeResponse:
    def __init__(self, url, body, status=200, content_type="text/html"):
        self.url = url
        self.status = status
        self.ok = 200 <= status < 300
        self.headers = {"content-type": content_type}
        self.request = type("Request", (), {"method": "POST"})()
        self._body = body

    async def text(self):
        return self._body


class FakeResponseContext:
    def __init__(self, response):
        self.value = self._resolve(response)

    async def _resolve(self, response):
        return response

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class FakeInput:
    def __init__(self):
        self.keys = []
        self.value = ""

    async def fill(self, value):
        self.value = value

    async def press(self, key):
        self.keys.append(key)


class FakeCard:
    async def inner_text(self):
        return "Codice fiscale 04055800926"


class FakeCards:
    async def count(self):
        return 1

    def nth(self, index):
        return FakeCard()


class FakeButton:
    def __init__(self):
        self.click_count = 0

    async def click(self):
        self.click_count += 1


class FakePage:
    def __init__(self, responses):
        self.responses = list(responses)
        self.expected_responses = 0
        self.cards = FakeCards()

    def expect_response(self, predicate, timeout):
        self.expected_responses += 1
        response = self.responses.pop(0)
        if not predicate(response):
            raise AssertionError(f"Unexpected response URL: {response.url}")
        return FakeResponseContext(response)

    async def wait_for_function(self, script, arg, timeout):
        return None

    def locator(self, selector):
        return self.cards


class PivaSearchFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_request_stops_after_single_native_submit(self):
        page = FakePage([
            FakeResponse("/isin/search?parolaChiaveFld", "<ajax-response />"),
            FakeResponse(
                "/isin/search?searchBtn",
                "<html><title>InfoCamere: Richiesta non valida</title>"
                "<body>La richiesta non è valida.</body></html>",
            ),
        ])
        search_input = FakeInput()
        search_button = FakeButton()

        with self.assertRaises(PivaSearchFailure) as raised:
            await search_card_by_piva(
                page, search_input, search_button, "04055800926"
            )

        self.assertEqual(raised.exception.phase, "search_response")
        self.assertEqual(raised.exception.outcome, SearchOutcome.INVALID_REQUEST)
        self.assertEqual(search_button.click_count, 1)
        self.assertEqual(search_input.keys, ["Tab"])
        self.assertEqual(page.expected_responses, 2)


if __name__ == "__main__":
    unittest.main()