from dataclasses import dataclass
from enum import Enum
from html.parser import HTMLParser
import json
import re
import xml.etree.ElementTree as ET


class SearchOutcome(str, Enum):
    FOUND = "found"
    NO_RESULTS = "no_results"
    INVALID_REQUEST = "invalid_request"
    CHALLENGE = "challenge"
    HTTP_ERROR = "http_error"
    UNKNOWN = "unknown"


class DetailOutcome(str, Enum):
    READY = "ready"
    INVALID_REQUEST = "invalid_request"
    CHALLENGE = "challenge"
    HTTP_ERROR = "http_error"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class SearchDiagnostics:
    outcome: SearchOutcome
    card_count: int
    matching_cf: bool
    has_search_form: bool


@dataclass(frozen=True)
class DetailDiagnostics:
    outcome: DetailOutcome
    has_company_marker: bool
    has_xml_control: bool


@dataclass(frozen=True)
class WicketSearchFlow:
    checkbox_events: tuple[str, ...]
    field_events: tuple[str, ...]
    submit_events: tuple[str, ...]
    submit_method: str | None
    submit_form_id: str | None


class _VisibleSearchMarkup(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title_parts = []
        self.visible_parts = []
        self.card_parts = []
        self.card_count = 0
        self.has_search_form = False
        self.element_ids = set()
        self.has_xml_control = False
        self._ignored_depth = 0
        self._title_depth = 0
        self._card_depths = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in {"script", "style", "noscript"}:
            self._ignored_depth += 1
        if tag == "title":
            self._title_depth += 1
        if tag == "form" and "vetrinaSearchForm" in attrs.get("action", ""):
            self.has_search_form = True
        if attrs.get("id"):
            self.element_ids.add(attrs["id"])
        if tag == "a" and "downloadXmlLnk" in attrs.get("href", ""):
            self.has_xml_control = True
        if "searchCompanyCard" in attrs.get("class", "").split():
            self.card_count += 1
            self._card_depths.append(1)
            self.card_parts.append([])
        elif self._card_depths:
            self._card_depths[-1] += 1

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript"} and self._ignored_depth:
            self._ignored_depth -= 1
        if tag == "title" and self._title_depth:
            self._title_depth -= 1
        if self._card_depths:
            self._card_depths[-1] -= 1
            if self._card_depths[-1] <= 0:
                self._card_depths.pop()

    def handle_data(self, data):
        if self._title_depth:
            self.title_parts.append(data)
        if self._ignored_depth:
            return
        self.visible_parts.append(data)
        if self._card_depths:
            self.card_parts[-1].append(data)


def _parse_markup(markup: str) -> _VisibleSearchMarkup:
    parsed = _VisibleSearchMarkup()
    parsed.feed(markup or "")
    parsed.close()
    return parsed


def _cf_matches(card_text: str, target_cf: str) -> bool:
    target = re.sub(r"[^A-Z0-9]", "", target_cf.upper())
    for match in re.finditer(
        r"Codice\s*fiscale\s*([A-Z0-9]{11,16})", card_text, re.IGNORECASE
    ):
        found = re.sub(r"[^A-Z0-9]", "", match.group(1).upper())
        if found == target:
            return True
    return False


def classify_search_response(
    status: int | None,
    content_type: str,
    body: str,
    target_cf: str,
) -> SearchDiagnostics:
    """Classify a Wicket search response without treating HTTP 200 as success."""
    documents = []
    markup = _parse_markup(body)
    documents.append(markup)

    if "xml" in (content_type or "").lower() or (body or "").lstrip().startswith("<?xml"):
        try:
            root = ET.fromstring(body or "")
        except ET.ParseError:
            root = None
        if root is not None:
            for component in root.iter():
                if component.tag.rsplit("}", 1)[-1] == "component":
                    fragment = "".join(component.itertext())
                    documents.append(_parse_markup(fragment))

    card_count = max((doc.card_count for doc in documents), default=0)
    has_form = any(doc.has_search_form for doc in documents)
    visible_text = " ".join(
        " ".join(doc.title_parts + doc.visible_parts) for doc in documents
    )
    normalized_text = re.sub(r"\s+", " ", visible_text).strip().casefold()

    if re.search(
        r"captcha|access denied|verifica di sicurezza|request rejected|accesso bloccato|\bforbidden\b",
        normalized_text,
    ):
        outcome = SearchOutcome.CHALLENGE
    elif re.search(r"richiesta non valida|invalid request", normalized_text):
        outcome = SearchOutcome.INVALID_REQUEST
    elif any(
        _cf_matches(" ".join(parts), target_cf)
        for doc in documents
        for parts in doc.card_parts
    ):
        outcome = SearchOutcome.FOUND
    elif status is not None and not 200 <= status < 300:
        outcome = SearchOutcome.HTTP_ERROR
    elif re.search(
        r"nessun[ae]?\s+(?:risultat\w*|impres\w*|startup\w*|aziend\w*)|"
        r"no\s+(?:results?|companies)",
        normalized_text,
    ):
        outcome = SearchOutcome.NO_RESULTS
    else:
        outcome = SearchOutcome.UNKNOWN

    return SearchDiagnostics(
        outcome=outcome,
        card_count=card_count,
        matching_cf=outcome == SearchOutcome.FOUND,
        has_search_form=has_form,
    )


def classify_detail_response(
    status: int | None,
    body: str,
) -> DetailDiagnostics:
    """Check for usable detail markers without relying on HTTP 200 alone."""
    markup = _parse_markup(body)
    normalized_text = re.sub(
        r"\s+",
        " ",
        " ".join(markup.title_parts + markup.visible_parts),
    ).strip().casefold()

    challenge = re.search(
        r"captcha|access denied|verifica di sicurezza|request rejected|accesso bloccato|\bforbidden\b",
        normalized_text,
    )
    invalid_request = re.search(
        r"richiesta non valida|invalid request", normalized_text
    )
    has_company_marker = "companyNameForGA" in markup.element_ids
    has_xml_control = (
        "downloadPnl" in markup.element_ids or markup.has_xml_control
    )

    if challenge:
        outcome = DetailOutcome.CHALLENGE
    elif invalid_request:
        outcome = DetailOutcome.INVALID_REQUEST
    elif status is not None and not 200 <= status < 300:
        outcome = DetailOutcome.HTTP_ERROR
    elif has_company_marker or has_xml_control:
        outcome = DetailOutcome.READY
    else:
        outcome = DetailOutcome.UNKNOWN

    return DetailDiagnostics(
        outcome=outcome,
        has_company_marker=has_company_marker,
        has_xml_control=has_xml_control,
    )


def inspect_wicket_search_flow(markup: str) -> WicketSearchFlow:
    bindings = []
    for match in re.finditer(r"Wicket\.Ajax\.ajax\((\{[^)]*\})\)", markup or ""):
        try:
            bindings.append(json.loads(match.group(1)))
        except json.JSONDecodeError:
            continue

    checkbox = next(
        (binding for binding in bindings if "startupChk-chkFld" in binding.get("u", "")),
        {},
    )
    field = next(
        (binding for binding in bindings if "parolaChiaveFld" in binding.get("u", "")),
        {},
    )
    submit = next(
        (binding for binding in bindings if "searchBtn" in binding.get("u", "")),
        {},
    )

    return WicketSearchFlow(
        checkbox_events=tuple(checkbox.get("e", "").split()),
        field_events=tuple(field.get("e", "").split()),
        submit_events=tuple(submit.get("e", "").split()),
        submit_method=submit.get("m"),
        submit_form_id=submit.get("f"),
    )