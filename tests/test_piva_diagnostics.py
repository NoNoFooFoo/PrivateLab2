from pathlib import Path
import unittest

from scrapers.piva_diagnostics import (
    DetailOutcome,
    SearchOutcome,
    classify_detail_response,
    classify_search_response,
    inspect_wicket_search_flow,
)


ROOT = Path(__file__).resolve().parents[1]
PIVA_FIXTURE = (
    ROOT / "scrapers" / "schede_test_html" /
    "ricerca-per-partita-iva-04055800926-htmlhead.txt"
)


class SearchResponseClassificationTests(unittest.TestCase):
    def classify(self, status, content_type, body, cf="04055800926"):
        return classify_search_response(status, content_type, body, cf)

    def test_full_html_fixture_finds_exact_company(self):
        body = PIVA_FIXTURE.read_text(encoding="utf-8")

        result = self.classify(200, "text/html; charset=UTF-8", body)

        self.assertEqual(result.outcome, SearchOutcome.FOUND)
        self.assertTrue(result.matching_cf)
        self.assertTrue(result.has_search_form)

    def test_wicket_xml_component_finds_exact_company(self):
        body = """<?xml version="1.0" encoding="UTF-8"?>
        <ajax-response><component id="searchResults"><![CDATA[
          <div class="searchCompanyCard">
            Codice fiscale <span>04055800926</span>
          </div>
        ]]></component></ajax-response>"""

        result = self.classify(200, "text/xml; charset=UTF-8", body)

        self.assertEqual(result.outcome, SearchOutcome.FOUND)
        self.assertEqual(result.card_count, 1)

    def test_http_200_invalid_request_is_not_no_results(self):
        body = """<html><head><title>InfoCamere: Richiesta non valida</title>
        <script src="/TSPD/example?type=17"></script></head>
        <body><p>La richiesta non è valida.</p></body></html>"""

        result = self.classify(200, "text/html", body)

        self.assertEqual(result.outcome, SearchOutcome.INVALID_REQUEST)

    def test_tspd_script_alone_does_not_mean_challenge(self):
        body = """<html><head><title>Startup e PMI innovative</title>
        <script src="/TSPD/example?type=17"></script></head>
        <body><form action="./search?x-vetrinaSearchForm"></form></body></html>"""

        result = self.classify(200, "text/html", body)

        self.assertEqual(result.outcome, SearchOutcome.UNKNOWN)
        self.assertTrue(result.has_search_form)

    def test_explicit_challenge_takes_precedence(self):
        body = "<html><title>Access denied</title><body>Request rejected</body></html>"

        result = self.classify(403, "text/html", body)

        self.assertEqual(result.outcome, SearchOutcome.CHALLENGE)

    def test_explicit_zero_results_marker_is_distinct(self):
        body = """<html><title>Startup e PMI innovative</title><body>
        <form action="./search?x-vetrinaSearchForm"></form>
        <div id="searchResults">Nessuna impresa trovata</div></body></html>"""

        result = self.classify(200, "text/html", body)

        self.assertEqual(result.outcome, SearchOutcome.NO_RESULTS)

    def test_http_200_without_card_or_message_is_ambiguous(self):
        result = self.classify(200, "text/html", "<html><body>Loading</body></html>")

        self.assertEqual(result.outcome, SearchOutcome.UNKNOWN)

    def test_transport_http_error_is_distinct(self):
        result = self.classify(503, "text/html", "<html><body>Service unavailable</body></html>")

        self.assertEqual(result.outcome, SearchOutcome.HTTP_ERROR)


class DetailResponseClassificationTests(unittest.TestCase):
    def test_saved_detail_fixture_has_valid_markers(self):
        body = (ROOT / "scrapers" / "schede_test_html" / "htmlhead.txt").read_text(
            encoding="utf-8"
        )

        result = classify_detail_response(200, body)

        self.assertEqual(result.outcome, DetailOutcome.READY)
        self.assertTrue(result.has_company_marker or result.has_xml_control)

    def test_detail_error_page_is_not_ready(self):
        body = """<html><head><title>InfoCamere: Richiesta non valida</title></head>
        <body>La richiesta non è valida.</body></html>"""

        result = classify_detail_response(200, body)

        self.assertEqual(result.outcome, DetailOutcome.INVALID_REQUEST)

    def test_detail_challenge_is_distinct(self):
        result = classify_detail_response(
            403, "<html><title>Access denied</title></html>"
        )

        self.assertEqual(result.outcome, DetailOutcome.CHALLENGE)


class WicketFlowInspectionTests(unittest.TestCase):
    def test_fixture_declares_native_change_and_click_bindings(self):
        body = PIVA_FIXTURE.read_text(encoding="utf-8")

        flow = inspect_wicket_search_flow(body)

        self.assertEqual(flow.checkbox_events, ("change",))
        self.assertEqual(flow.field_events, ("change",))
        self.assertEqual(flow.submit_events, ("click",))
        self.assertEqual(flow.submit_method, "POST")
        self.assertTrue(flow.submit_form_id)


if __name__ == "__main__":
    unittest.main()