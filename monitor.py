"""Read public availability only; never create or hold a reservation."""
import argparse
import base64
import http.cookiejar
import json
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from urllib.error import HTTPError
from urllib.parse import urlencode, urlparse
from urllib.request import HTTPCookieProcessor, Request, build_opener, urlopen

BASE = "https://www.tablecheck.com/ja/shops/hatsunezushi"
RESERVE = BASE + "/reserve"
JST = timezone(timedelta(hours=9))


def candidates(data, today):
    try:
        slots = data["data"]["slots"]
        if not isinstance(slots, dict):
            raise ValueError("Invalid slots")
        result = set()
        for day, values in slots.items():
            parsed = date.fromisoformat(day)
            if not isinstance(values, dict):
                raise ValueError("Invalid day slots")
            for value in values.values():
                if not isinstance(value.get("available"), bool):
                    raise ValueError("Invalid availability flag")
                if value["available"] and parsed >= today and parsed.weekday() in (5, 6):
                    if value["meal"] == "lunch":
                        seconds = value["seconds"]
                        if not isinstance(seconds, int) or not 0 <= seconds < 86400:
                            raise ValueError("Invalid slot time")
                        result.add((day, seconds))
        return result
    except (KeyError, TypeError, AttributeError) as exc:
        raise ValueError("Availability response changed") from exc


class CourseParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.depth = 0
        self.item_depth = None
        self.name_depth = None
        self.token = None
        self.courses = {}
        self.item = {}
        self.name_parts = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "meta" and attrs.get("name") == "csrf-token":
            self.token = attrs.get("content")
        if tag == "div":
            self.depth += 1
            classes = attrs.get("class", "").split()
            if "menu-item" in classes:
                self.item_depth = self.depth
                self.item = {}
            if self.item_depth and "menu-item-name" in classes:
                self.name_depth = self.depth
                self.name_parts = []
        if self.item_depth and tag == "input" and attrs.get("name", "").endswith("[menu_item_id]"):
            self.item["id"] = attrs.get("value")

    def handle_data(self, data):
        if self.name_depth:
            self.name_parts.append(data)

    def handle_endtag(self, tag):
        if tag != "div":
            return
        if self.depth == self.name_depth:
            self.item["name"] = "".join(self.name_parts).strip()
            self.name_depth = None
        if self.depth == self.item_depth:
            if self.item.get("id") and "ランチ" in self.item.get("name", ""):
                self.courses[self.item["id"]] = self.item["name"]
            self.item_depth = None
        self.depth -= 1


def parse_page(html):
    parser = CourseParser()
    parser.feed(html)
    if not parser.token or not parser.courses:
        raise ValueError("Reservation page or lunch courses changed")
    return parser.token, parser.courses


def new_slots(current, previous):
    return sorted(set(current) - set(previous))


def eligible_courses(data, courses, seconds):
    if not isinstance(data.get("menu_items"), list):
        raise ValueError("Menu response changed")
    return [item["id"] for item in data["menu_items"]
            if item["id"] in courses
            and any(step[1] == seconds for step in item["online_time_steps"])]


class TableCheck:
    def __init__(self):
        self.opener = build_opener(HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.opener.addheaders = [("User-Agent", "HatsunezushiAvailabilityMonitor/1.0"),
                                  ("Referer", RESERVE)]
        self.deadline = time.monotonic() + 95
        html = self.get(RESERVE, as_json=False)
        self.csrf, self.courses = parse_page(html)

    def get(self, url, as_json=True):
        remaining = self.deadline - time.monotonic()
        if remaining < 2:
            raise TimeoutError("Scan exceeded time limit; state retained")
        with self.opener.open(url, timeout=min(10, remaining)) as response:
            body = response.read().decode("utf-8")
        return json.loads(body) if as_json else body

    def timetable(self, day, course=None):
        params = {"authenticity_token": self.csrf,
                  "reservation[num_people_adult]": "2",
                  "reservation[start_date]": day}
        if course:
            params["reservation[orders_attributes][0][menu_item_id]"] = course
            params["reservation[orders_attributes][0][is_group_order]"] = "true"
        return self.get(BASE + "/available/timetable?" + urlencode(params))

    def scan(self, weeks):
        today = datetime.now(JST).date()
        possible = set()
        for week in range(weeks):
            day = (today + timedelta(days=7 * week)).isoformat()
            possible.update(candidates(self.timetable(day), today))
            time.sleep(0.5)
        # Check the course's actual booking times and availability before notifying.
        result = []
        by_day = {}
        for day, seconds in sorted(possible):
            by_day.setdefault(day, []).append(seconds)
        for day, seconds_list in by_day.items():
            menus = self.get(BASE + "/menu_items?" + urlencode({"num_people": 2, "date": day}))
            checked = {}
            for seconds in seconds_list:
                for course in eligible_courses(menus, self.courses, seconds):
                    if course not in checked:
                        checked[course] = candidates(self.timetable(day, course), today)
                        time.sleep(0.5)
                    if (day, seconds) in checked[course]:
                        result.append(f"{day}|{seconds}|{course}")
        return sorted(set(result))


def notify(title, message):
    server = (os.environ.get("NTFY_SERVER") or "https://ntfy.sh").rstrip("/")
    topic = os.environ.get("NTFY_TOPIC", "")
    parsed = urlparse(server)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        raise ValueError("NTFY_SERVER must be an HTTPS URL")
    if not topic or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for c in topic):
        raise ValueError("Set a valid NTFY_TOPIC secret")
    payload = {"topic": topic, "title": title, "message": message,
               "priority": 4, "click": RESERVE}
    headers = {"Content-Type": "application/json"}
    if os.environ.get("NTFY_TOKEN"):
        headers["Authorization"] = "Bearer " + os.environ["NTFY_TOKEN"]
    request = Request(server, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), headers=headers)
    with urlopen(request, timeout=10) as response:
        json.load(response)


class GitHubState:
    def __init__(self):
        self.url = "https://api.github.com/repos/" + os.environ["GITHUB_REPOSITORY"] + "/contents/state.json"
        self.headers = {"Authorization": "Bearer " + os.environ["GH_TOKEN"],
                        "Accept": "application/vnd.github+json", "User-Agent": "hatsunezushi-monitor"}
        self.sha = None

    def load(self):
        try:
            with urlopen(Request(self.url, headers=self.headers), timeout=10) as response:
                item = json.load(response)
        except HTTPError as exc:
            if exc.code == 404:
                return {"available": [], "error": None}
            raise
        self.sha = item["sha"]
        state = json.loads(base64.b64decode(item["content"]))
        if not isinstance(state.get("available"), list):
            raise ValueError("Invalid saved state")
        return state

    def save(self, state):
        content = json.dumps(state, ensure_ascii=False, indent=2).encode("utf-8")
        identity = {"name": "Availability Monitor", "email": "monitor@users.noreply.github.com"}
        body = {"message": "Update availability state", "content": base64.b64encode(content).decode(),
                "author": identity, "committer": identity}
        if self.sha:
            body["sha"] = self.sha
        request = Request(self.url, data=json.dumps(body).encode(), method="PUT", headers=self.headers)
        with urlopen(request, timeout=10) as response:
            json.load(response)


def run(args):
    if args.test_notification:
        notify("初音鮨：通知テスト", "通知の接続を確認しました。これは空席のお知らせではありません。")
        print("Test notification delivered to ntfy server.")
        return
    if not 1 <= args.weeks <= 52:
        raise ValueError("weeks must be between 1 and 52")
    store = None if args.dry_run else GitHubState()
    previous = {"available": [], "error": None} if store is None else store.load()
    try:
        client = TableCheck()
        current = client.scan(args.weeks)
        additions = new_slots(current, previous["available"])
        if additions and not args.dry_run:
            lines = []
            for key in additions[:20]:
                day, seconds, course = key.split("|")
                seconds = int(seconds)
                lines.append(f"{day} {seconds // 3600:02}:{seconds % 3600 // 60:02} {client.courses[course]}")
            if len(additions) > 20:
                lines.append(f"ほか {len(additions) - 20} 枠")
            notify("初音鮨：2名のランチに空きが出ました", "\n".join(lines) + "\n通知をタップして最新の空席を確認してください。")
        state = {"available": current, "error": None,
                 "checked_week": datetime.now(JST).strftime("%G-W%V")}
        if store and state != previous:
            store.save(state)
        print(json.dumps({"weeks_scanned": args.weeks, "available_slots": len(current),
                          "new_slots": len(additions), "dry_run": args.dry_run}))
    except Exception as exc:
        # Never erase availability state on failed or partial scans.
        error = type(exc).__name__
        if store and previous.get("error") != error:
            notify("初音鮨：空席確認でエラー", "空席を確認できませんでした。次の定期実行で再試行します。GitHub Actionsの実行結果を確認してください。")
            previous["error"] = error
            store.save(previous)
        raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--test-notification", action="store_true")
    parser.add_argument("--weeks", type=int, default=52)
    args = parser.parse_args()
    try:
        run(args)
    except Exception as exc:
        # Do not log request URLs, headers, or notification topic names.
        print("Monitor failed: " + type(exc).__name__, file=sys.stderr)
        sys.exit(1)
