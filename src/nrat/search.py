"""Parse one OK listing. Listing numbers have no inferred legal role."""

from dataclasses import dataclass
from datetime import date
import re
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from .http import BASE_URL, HttpClient, HttpResult, ResponseValidationError, search_params


@dataclass(frozen=True)
class ListingRecord:
    doc_id: str
    listing_number: str
    source_url: str
    title: str
    description: str
    query_date: str


@dataclass(frozen=True)
class CardIssue:
    card_index: int
    category: str
    reason: str
    description: str
    record: ListingRecord | None = None


@dataclass(frozen=True)
class SearchPage:
    query_date: str
    current_page: int
    total_count: int
    card_count: int
    records: tuple[ListingRecord, ...]
    issues: tuple[CardIssue, ...]
    parsing_complete: bool
    next_page_url: str | None
    limited: bool
    count_ge_1000: bool
    warnings: tuple[str, ...]
    empty_day_confirmed: bool


def _fail(category: str, reason: str):
    raise ResponseValidationError(category, reason)


def _validate_filters(soup, query_date: date):
    controls = soup.select('form select[name="typeSearch2"]')
    if not controls or any(not c.select('option[value="ok"]') for c in controls):
        _fail("filter_mismatch", "OK search form is missing")
    selected = [o.get("value") for c in controls for o in c.select("option[selected]")]
    # Actual NRAT HTML initializes this select via a JS variable, without selected.
    declared = []
    for script in soup.select("script:not([src])"):
        declared.extend(re.findall(r"\bvar\s+typeSearch2\s*=\s*(['\"])([^'\"]*)\1\s*;", script.get_text()))
    values = selected + [value for _, value in declared]
    if not values or any(value != "ok" for value in values):
        _fail("filter_mismatch", "Response does not confirm typeSearch2=ok")
    for name in ("dateFromSearch", "dateToSearch"):
        fields = soup.select(f'form input[name="{name}"]')
        if not fields or any(f.get("value") != query_date.isoformat() for f in fields):
            _fail("filter_mismatch", f"Response does not preserve {name}")


def _source_link(anchor):
    href = anchor.get("href", "")
    try:
        p = urlsplit(href)
        match = re.fullmatch(r"/view/ok/([0-9a-fA-F]{32})/?", p.path)
        if (p.scheme != "https" or p.netloc.lower() not in {"nddkr.ukrintei.ua", "dir.ukrintei.ua"}
                or p.username or p.password or p.query or p.fragment or not match):
            return None
    except ValueError:
        return None
    return match.group(1).lower(), href, anchor.get_text(" ", strip=True)


def _pagination(soup, query_date: date, page: int):
    paginations = soup.select(".pagination")
    active = [e.get_text(strip=True) for e in soup.select(".pagination .page-item.active .page-link")]
    if paginations:
        if not active or any(v != str(page) for v in active):
            _fail("pagination_mismatch", "Active pagination page differs from requested page")
    elif page != 1:
        _fail("pagination_mismatch", "Cannot confirm current page without pagination")
    next_links = soup.select('.pagination a[rel~="next"]')
    urls = []
    for a in next_links:
        try:
            p = urlsplit(urljoin(BASE_URL, a.get("href", "")))
        except ValueError:
            _fail("pagination_mismatch", "Next link is a malformed URL")
        if p.scheme != "https" or p.netloc != "nrat.ukrintei.ua" or p.path not in {"/searchdb", "/searchdb/"}:
            _fail("pagination_mismatch", "Next link is not an NRAT search page")
        query = parse_qs(p.query, keep_blank_values=True)
        expected = search_params(query_date, page + 1)
        if any(query.get(k) != [v] for k, v in expected.items()):
            _fail("pagination_mismatch", "Next link does not preserve filters/page/sort")
        # Do not export session parameters; canonical search path avoids a 301.
        urls.append(urlunsplit(("https", "nrat.ukrintei.ua", "/searchdb/", urlencode(expected), "")))
    if len(set(urls)) > 1:
        _fail("pagination_mismatch", "Next links disagree")
    return urls[0] if urls else None


def parse_page(html: str, query_date: date, page: int) -> SearchPage:
    search_params(query_date, page)  # validate caller input before interpreting HTML
    soup = BeautifulSoup(html, "html.parser")
    _validate_filters(soup, query_date)
    counters = soup.select(".page_control .page_info")
    if not counters:
        _fail("count_missing", "Result counter is missing")
    counts = []
    for element in counters:
        match = re.match(r"\s*Знайдено документів:\s*([0-9]+(?:[ \u00a0\u202f][0-9]{3})*)\b", element.get_text(" ", strip=True))
        if not match:
            _fail("count_missing", "Result counter cannot be read")
        counts.append(int(re.sub(r"\s", "", match.group(1))))
    if len(set(counts)) != 1:
        _fail("count_mismatch", "Repeated result counters disagree")
    count = counts[0]
    cards = soup.select(".my-card")
    if (count == 0 and (cards or soup.select(".my-card-body") or soup.select(".pagination"))) or (count > 0 and not cards):
        _fail("count_mismatch", "Result counter contradicts cards/pagination")
    if len(cards) > count:
        _fail("count_mismatch", "Page contains more cards than the result counter")
    next_url = _pagination(soup, query_date, page)
    limited = bool(soup.select(".page_info .limited_search"))
    if count == 0 and limited:
        _fail("count_mismatch", "Zero-result page also claims a result limit")
    if page == 1 and count > len(cards) and not next_url and not limited:
        _fail("count_mismatch", "Result count exceeds cards but there is no next page")
    records = []
    issues = []
    seen = set()
    for index, card in enumerate(cards, 1):
        types = [e.get_text(" ", strip=True) for e in card.select(".typeBase")]
        if types != ["НДДКР ОК"]:
            _fail("unexpected_type", "Card has missing or unexpected document type")
        bodies = card.select(".my-card-body")
        description = bodies[0].get_text(" ", strip=True) if bodies else ""
        if len(bodies) != 1:
            issues.append(CardIssue(index, "card_body_invalid", "Card must contain one description block", description))
            continue
        candidates = [_source_link(a) for a in bodies[0].select("a[href]")]
        valid = list(dict.fromkeys(c for c in candidates if c is not None))
        if len(valid) != 1:
            issues.append(CardIssue(index, "card_link_invalid", "Expected one valid HTTPS /view/ok/<32-hex-id> link", description))
            continue
        doc_id, url, number = valid[0]
        if not number:
            issues.append(CardIssue(index, "listing_number_missing", "Source link has no listing number", description))
            continue
        title_tag = card.select_one('[name="title"]')
        title = title_tag.get_text(" ", strip=True) if title_tag else ""
        record = ListingRecord(doc_id, number, url, title, description, query_date.isoformat())
        if doc_id in seen:
            issues.append(CardIssue(index, "duplicate_doc_id", "Card repeats a doc_id already present on this page", description, record))
            continue
        seen.add(doc_id)
        records.append(record)
    warnings = []
    if limited:
        warnings.append("Site explicitly limits the search results")
    if count >= 1000:
        warnings.append("Result counter is 1000 or greater; completeness may be limited")
    return SearchPage(query_date.isoformat(), page, count, len(cards), tuple(records), tuple(issues),
                      not issues, next_url, limited, count >= 1000, tuple(warnings), count == 0)


def fetch_page(client: HttpClient, query_date: date, page: int, *, on_failure=None) -> HttpResult[SearchPage]:
    """Fetch exactly one requested page; never follow next_page_url/source_url."""
    return client.fetch(query_date, page, lambda html: parse_page(html, query_date, page), on_failure=on_failure)
