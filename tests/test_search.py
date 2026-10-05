from datetime import date
import unittest

from nrat.http import ResponseValidationError
from nrat.search import parse_page
from tests.examples import DAY, ID, URL, card, html_page, pagination

D = date.fromisoformat(DAY)


class SearchTests(unittest.TestCase):
    def assert_error(self, category, html, page=1):
        with self.assertRaises(ResponseValidationError) as ctx:
            parse_page(html, D, page)
        self.assertEqual(ctx.exception.category, category)

    def test_normal_page(self):
        p = parse_page(html_page(count=2, pages=pagination()), D, 1)
        self.assertEqual(p.total_count, 2)  # duplicated counters are not summed
        self.assertEqual(p.records[0].doc_id, ID)
        self.assertEqual(p.records[0].listing_number, "0210U000704")
        self.assertEqual(p.records[0].source_url, URL)
        self.assertEqual(p.records[0].query_date, DAY)
        self.assertIn("Test", p.records[0].description)
        self.assertIn("Тестова", p.records[0].title)
        self.assertTrue(p.parsing_complete)
        self.assertIn("pa=2", p.next_page_url)
        self.assertEqual(p.current_page, 1)
        self.assertFalse(p.empty_day_confirmed)

    def test_selected_option_is_supported(self):
        self.assertTrue(parse_page(html_page(selected=True), D, 1).parsing_complete)

    def test_actual_domain_preserved(self):
        source = URL.replace("nddkr.", "dir.")
        self.assertEqual(parse_page(html_page(cards=card(source)), D, 1).records[0].source_url, source)

    def test_confirmed_empty_day(self):
        p = parse_page(html_page(cards="", count=0), D, 1)
        self.assertTrue(p.empty_day_confirmed)
        self.assertTrue(p.parsing_complete)
        self.assertEqual(p.records, ())

    def test_missing_counter(self):
        self.assert_error("count_missing", html_page(count=None))

    def test_unreadable_counter(self):
        self.assert_error("count_missing", html_page(count="unknown"))

    def test_counter_conflict(self):
        self.assert_error("count_mismatch", html_page(second_count=2))

    def test_counter_card_contradictions(self):
        for h in [html_page(count=0), html_page(count=1, cards=""),
                  html_page(count=0, cards="", pages=pagination()), html_page(count=2)]:
            with self.subTest(h=h):
                self.assert_error("count_mismatch", h)

    def test_wrong_filters(self):
        for h in [html_page(day="2010-03-13"), html_page(kind="rk"),
                  html_page().replace('name="dateToSearch"', 'name="missing"'),
                  html_page().replace("var typeSearch2 = 'ok';", "")]:
            self.assert_error("filter_mismatch", h)

    def test_conflicting_type_initialization(self):
        self.assert_error("filter_mismatch", html_page(selected=True) + "<script>var typeSearch2='rk';</script>")

    def test_unexpected_card_type(self):
        self.assert_error("unexpected_type", html_page(cards=card(kind="НДДКР РК")))

    def test_invalid_or_missing_id_is_not_silently_skipped(self):
        urls = [URL[:-1], URL.replace(ID, "z" * 32), URL.replace("nddkr.ukrintei.ua", "evil.example"),
                URL.replace("/view/ok/", "/view/rk/"), URL.replace("nddkr.ukrintei.ua", "nddkr.ukrintei.ua.evil.example"),
                "/searchdoc/0210U000704/", "#", URL + "?token=ARTIFICIAL_TEST_ONLY"]
        for url in urls:
            with self.subTest(url=url):
                p = parse_page(html_page(cards=card(url)), D, 1)
                self.assertFalse(p.parsing_complete)
                self.assertEqual(p.records, ())
                self.assertEqual(p.issues[0].card_index, 1)
                self.assertEqual(p.issues[0].category, "card_link_invalid")
                self.assertTrue(p.issues[0].description)

    def test_missing_number(self):
        p = parse_page(html_page(cards=card(number="")), D, 1)
        self.assertFalse(p.parsing_complete)
        self.assertEqual(p.issues[0].category, "listing_number_missing")

    def test_partial_page_keeps_successful_cards(self):
        p = parse_page(html_page(count=2, cards=card()+card("#")), D, 1)
        self.assertEqual(len(p.records), 1)
        self.assertEqual(len(p.issues), 1)
        self.assertFalse(p.parsing_complete)

    def test_duplicate_id_is_a_problem(self):
        p = parse_page(html_page(count=2, cards=card()+card()), D, 1)
        self.assertEqual(len(p.records), 1)
        self.assertEqual(p.issues[0].category, "duplicate_doc_id")

    def test_limit_warning(self):
        p = parse_page(html_page(count=1000, limited=True), D, 1)
        self.assertTrue(p.limited)
        self.assertTrue(p.count_ge_1000)
        self.assertEqual(len(p.warnings), 2)

    def test_threshold_warning_without_explicit_limit(self):
        p = parse_page(html_page(count=1001, pages=pagination()), D, 1)
        self.assertFalse(p.limited)
        self.assertTrue(p.count_ge_1000)

    def test_next_link_must_preserve_filters(self):
        self.assert_error("pagination_mismatch", html_page(count=2, pages=pagination(kind="rk")))

    def test_current_page_must_match(self):
        self.assert_error("pagination_mismatch", html_page(pages=pagination(page=2)))

    def test_malformed_next_link(self):
        pages = pagination().replace('href="/searchdb?', 'href="https://[broken/?')
        self.assert_error("pagination_mismatch", html_page(count=2, pages=pages))

    def test_doubled_pagination(self):
        p = parse_page(html_page(count=2, pages=pagination()*2), D, 1)
        self.assertIn("pa=2", p.next_page_url)

    def test_next_link_session_data_not_exported(self):
        pages = pagination().replace('href="/searchdb?', 'href="/searchdb?_token=ARTIFICIAL_TEST_ONLY&amp;')
        self.assertNotIn("token", parse_page(html_page(count=2, pages=pages), D, 1).next_page_url)


if __name__ == "__main__":
    unittest.main()
