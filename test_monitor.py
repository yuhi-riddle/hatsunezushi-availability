import unittest
from datetime import date
from types import SimpleNamespace
from urllib.error import HTTPError, URLError
from unittest.mock import MagicMock, patch

import monitor


def response(slots):
    return {"data": {"slots": slots}}


def slot(available=True, meal="lunch", seconds=43200):
    return {"available": available, "meal": meal, "seconds": seconds}


def http_error(code, url=monitor.RESERVE, headers=None):
    return HTTPError(url, code, "error", headers or {}, None)


def ok_response(body=b"{}"):
    response = MagicMock()
    response.__enter__.return_value.read.return_value = body
    return response


def client_with(*results):
    client = monitor.TableCheck.__new__(monitor.TableCheck)
    client.opener = MagicMock()
    client.opener.open.side_effect = list(results)
    client.deadline = monitor.time.monotonic() + 60
    return client


def run_with_failure(previous):
    saved, notified = [], []
    class State:
        def load(self):
            return dict(previous)
        def save(self, state):
            saved.append(state)
    with patch.object(monitor, "GitHubState", return_value=State()), \
         patch.object(monitor, "TableCheck", side_effect=http_error(403)), \
         patch.object(monitor, "notify", side_effect=lambda *a: notified.append(a)):
        with unittest.TestCase().assertRaises(HTTPError):
            monitor.run(SimpleNamespace(test_notification=False, dry_run=False, weeks=52))
    return saved, notified


class MonitorTests(unittest.TestCase):
    def test_only_future_weekend_lunch_with_explicit_availability(self):
        data = response({
            "2026-10-10": {"a": slot(), "b": slot(False), "c": slot(meal="dinner")},
            "2026-10-11": {"d": slot(seconds=46800)},
            "2026-10-12": {"e": slot()},
            "2026-10-03": {"f": slot()},
        })
        self.assertEqual(monitor.candidates(data, date(2026, 10, 7)),
                         {("2026-10-10", 43200), ("2026-10-11", 46800)})

    def test_malformed_response_is_not_treated_as_full(self):
        for data in ({}, {"data": {}}, response([]), response({"bad": {}})):
            with self.subTest(data=data), self.assertRaises(ValueError):
                monitor.candidates(data, date(2026, 10, 7))

    def test_available_flag_must_be_boolean(self):
        data = response({"2026-10-10": {"a": slot("false")}})
        with self.assertRaises(ValueError):
            monitor.candidates(data, date(2026, 10, 7))

    def test_course_parser_tracks_lunch_ids_and_csrf(self):
        html = '''<meta name="csrf-token" content="sample-token">
        <div class="menu-item show-more-expander">
          <input name="reservation[orders_attributes][1][menu_item_id]" value="lunch-id">
          <div><div class="menu-item-name">ランチコース</div></div>
        </div>
        <div class="menu-item"><input name="reservation[orders_attributes][2][menu_item_id]" value="dinner-id">
          <div class="menu-item-name">ディナーコース</div></div>
        <div class="menu-item"><input name="reservation[orders_attributes][3][menu_item_id]" value="new-lunch">
          <div class="menu-item-name">ランチ、ディナーコース</div></div>'''
        token, courses = monitor.parse_page(html)
        self.assertEqual(token, "sample-token")
        self.assertEqual(courses, {"lunch-id": "ランチコース", "new-lunch": "ランチ、ディナーコース"})

    def test_course_missing_raises_instead_of_silent_monitoring(self):
        with self.assertRaises(ValueError):
            monitor.parse_page('<meta name="csrf-token" content="sample-token">')

    def test_duplicate_slots_are_suppressed_but_reopening_is_not(self):
        self.assertEqual(monitor.new_slots(["A", "B"], ["A"]), ["B"])
        self.assertEqual(monitor.new_slots(["A"], []), ["A"])

    def test_times_must_match_menu_order_times(self):
        menus = {"menu_items": [{"id": "lunch-id", "online_time_steps": [[1, 43200]]},
                                {"id": "dinner-id", "online_time_steps": [[2, 46800]]}]}
        self.assertEqual(monitor.eligible_courses(menus, {"lunch-id": "ランチコース"}, 43200), ["lunch-id"])
        self.assertEqual(monitor.eligible_courses(menus, {"lunch-id": "ランチコース"}, 45000), [])

    def test_partial_scan_failure_preserves_known_availability(self):
        previous = {"available": ["2026-10-10|43200|lunch-id"], "error": None}
        saved = []
        class State:
            def load(self):
                return dict(previous)
            def save(self, state):
                saved.append(state)
        with patch.object(monitor, "GitHubState", return_value=State()), \
             patch.object(monitor, "TableCheck", side_effect=TimeoutError), \
             patch.object(monitor, "notify"):
            with self.assertRaises(TimeoutError):
                monitor.run(SimpleNamespace(test_notification=False, dry_run=False, weeks=52))
        self.assertEqual(saved, [{"available": ["2026-10-10|43200|lunch-id"],
                                  "error": "TimeoutError", "failures": 1}])

    def test_transient_errors_are_retried(self):
        for error in (http_error(429), http_error(503), URLError("reset")):
            with self.subTest(error=error), patch.object(monitor.time, "sleep"):
                client = client_with(error, ok_response(b'{"ok": true}'))
                self.assertEqual(client.get("https://example.test"), {"ok": True})
                self.assertEqual(client.opener.open.call_count, 2)

    def test_retry_honors_short_retry_after(self):
        with patch.object(monitor.time, "sleep") as sleep:
            client_with(http_error(429, headers={"Retry-After": "5"}), ok_response()).get("https://example.test")
        sleep.assert_called_once_with(5)

    def test_forbidden_is_not_retried(self):
        client = client_with(http_error(403), ok_response())
        with self.assertRaises(HTTPError):
            client.get("https://example.test")
        self.assertEqual(client.opener.open.call_count, 1)

    def test_retries_stop_after_three_attempts(self):
        client = client_with(http_error(503), http_error(503), http_error(503), ok_response())
        with patch.object(monitor.time, "sleep"), self.assertRaises(HTTPError):
            client.get("https://example.test")
        self.assertEqual(client.opener.open.call_count, 3)

    def test_error_label_shows_status_and_stage_without_url(self):
        self.assertEqual(monitor.describe_error(http_error(403)), "HTTPError 403 tablecheck")
        self.assertEqual(monitor.describe_error(http_error(409, "https://api.github.com/repos/x/y/contents/state.json")),
                         "HTTPError 409 github")
        self.assertEqual(monitor.describe_error(http_error(500, "https://private.example/secret-topic")),
                         "HTTPError 500 ntfy")
        self.assertEqual(monitor.describe_error(TimeoutError()), "TimeoutError")

    def test_error_is_notified_only_on_third_consecutive_failure(self):
        saved, notified = run_with_failure({"available": [], "error": None, "failures": 0})
        self.assertEqual(notified, [])
        self.assertEqual(saved[0]["failures"], 1)
        self.assertEqual(saved[0]["error"], "HTTPError 403 tablecheck")

        saved, notified = run_with_failure({"available": [], "error": "HTTPError 403 tablecheck", "failures": 2})
        self.assertEqual(len(notified), 1)
        self.assertEqual(saved[0]["failures"], 3)

        saved, notified = run_with_failure({"available": [], "error": "HTTPError 403 tablecheck", "failures": 3})
        self.assertEqual((saved, notified), ([], []))

    def test_old_state_without_failure_count_starts_from_zero(self):
        saved, notified = run_with_failure({"available": [], "error": "HTTPError"})
        self.assertEqual(notified, [])
        self.assertEqual(saved[0]["failures"], 1)

    def test_success_resets_failure_count(self):
        saved = []
        class State:
            def load(self):
                return {"available": [], "error": "HTTPError 403 tablecheck", "failures": 2, "checked_week": "2026-W41"}
            def save(self, state):
                saved.append(state)
        class Client:
            def scan(self, weeks):
                return []
        with patch.object(monitor, "GitHubState", return_value=State()), \
             patch.object(monitor, "TableCheck", return_value=Client()), \
             patch.object(monitor, "datetime") as clock:
            clock.now.return_value.strftime.return_value = "2026-W41"
            monitor.run(SimpleNamespace(test_notification=False, dry_run=False, weeks=52))
        self.assertEqual(saved, [{"available": [], "error": None, "failures": 0, "checked_week": "2026-W41"}])

    def test_ntfy_topic_cannot_change_request_path(self):
        with patch.dict(monitor.os.environ, {"NTFY_TOPIC": "topic/other", "NTFY_SERVER": "https://ntfy.sh"}):
            with self.assertRaises(ValueError):
                monitor.notify("test", "test")

    def test_unchanged_availability_refreshes_activity_in_a_new_week(self):
        saved = []
        class State:
            def load(self):
                return {"available": [], "error": None, "checked_week": "2026-W40"}
            def save(self, state):
                saved.append(state)
        class Client:
            def scan(self, weeks):
                return []
        with patch.object(monitor, "GitHubState", return_value=State()), \
             patch.object(monitor, "TableCheck", return_value=Client()), \
             patch.object(monitor, "datetime") as clock:
            clock.now.return_value.strftime.return_value = "2026-W41"
            monitor.run(SimpleNamespace(test_notification=False, dry_run=False, weeks=52))
        self.assertEqual(saved, [{"available": [], "error": None, "failures": 0, "checked_week": "2026-W41"}])


if __name__ == "__main__":
    unittest.main()
