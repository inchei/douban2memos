#!/usr/bin/env python3
# Copyright (C) 2026 douban2memos contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""douban2memos - 将豆瓣「带短评」的收藏标记导入 Memos。

纯文字 memo，正文含状态词、条目名、短评（可选评分）与豆瓣条目链接。
按 memo uid（douban-{条目 subject id}）幂等，重复运行不产生重复 memo；
默认增量同步（状态文件记录最新 pubDate，提前停止处理更旧条目），--full 强制全量。

数据来源（豆瓣无公开 API，且 RSS 公开无需登录）：
  1. 增量：豆瓣 RSS `https://www.douban.com/feed/people/{uid}/interests`
     （公开，最近 ~10 条兴趣，含看过/读过/听过/玩过与想*/在*；无 token）
  2. 初始/全量：本仓库附带的油猴脚本 `userscript/export.user.js`（复制自
     douban-backup）导出的 CSV，用 --import-csv 一次性导入历史收藏

两种写入方式：
  1. API 模式（--api，memos 运行中）：memos >= 0.30 用 --password 登录换取短期 token，
     < 0.30 用 --token（Access Token）；请求体带 createTime 保留豆瓣时间
  2. 直写数据库（--db）：直接插入 memo 表，保留时间

仅用 Python 标准库（urllib / tomllib / sqlite3 / xml.etree / csv），无需安装任何依赖。

用法示例：
  python3 douban2memos.py --douban-user-id MoNoMilky --api http://localhost:5230 --password '***'
  python3 douban2memos.py --import-csv db-movie.csv db-book.csv --api http://localhost:5230 --password '***'
  python3 douban2memos.py --config config.toml --dry-run
"""

import argparse
import csv
import datetime
import email.utils
import html
import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

try:
    import tomllib
except ImportError:
    sys.stderr.write("需要 Python >= 3.11（内置 tomllib）\n")
    raise


def default_ua():
    return "douban2memos (https://github.com/inchei/douban2memos)"


DEFAULT_CONFIG_PATH = "config.toml"
DEFAULT_DB = "memos.db"
DEFAULT_USER = "admin"
DEFAULT_VISIBILITY = "private"
DEFAULT_STATE = "state.json"
DEFAULT_TIMEOUT = 30

RSS_FEED_BASE = os.environ.get("DOUBAN2MEMOS_FEED_BASE", "https://www.douban.com")
RSS_FEED_URL = RSS_FEED_BASE + "/feed/people/{uid}/interests"
UID_PREFIX = "douban-"

# 油猴脚本 export.user.js 导出的 collect CSV 命名：db-{type}-{yyyymmdd}.csv（如 db-book-20260816.csv）
CSV_NAME_RE = re.compile(r"^db-(book|movie|music|game|drama)-(\d{8})\.csv$")

VIS_PRIVATE = "PRIVATE"
VIS_PROTECTED = "PROTECTED"
VIS_PUBLIC = "PUBLIC"

# 完成态前缀：只导入这些状态且带短评的条目
COMPLETED_PREFIXES = {"看过", "读过", "听过", "玩过"}
# RSS title 的全部状态前缀（含想看/在看等，用于从标题剥离状态词）
ALL_PREFIX_RE = re.compile(
    r"^(?:最近)?(看过|读过|听过|玩过|想看|想读|想听|想玩|在看|在读|在听|在玩)")

# CSV 无法仅凭 title 拿状态词，按条目链接的域名推断（镜像 bangumi 的 status_label）
CATEGORY_LABELS = {"book": "读过", "music": "听过", "game": "玩过", "movie": "看过", "drama": "看过"}

# 评分词：RSS 里是中文（推荐: 力荐），CSV 里是数字 1-5
RATING_RSS = {"很差": "很差", "较差": "较差", "还行": "还行", "推荐": "推荐", "力荐": "力荐"}
RATING_NUM = {"": None, "0": None}
RATING_ORDER = {"力荐": 5, "推荐": 4, "还行": 3, "较差": 2, "很差": 1}


def die(msg):
    print(msg, file=sys.stderr)
    sys.exit(1)


def truncate(s, n):
    if len(s) <= n:
        return s
    return s[:n] + "…"


DEFAULTS = {
    "douban_user_id": "",
    "import_csv": [],
    "api": "",
    "token": "",
    "password": "",
    "db": DEFAULT_DB,
    "user": DEFAULT_USER,
    "visibility": DEFAULT_VISIBILITY,
    "tag": "",
    "tag_in_content": True,
    "dry_run": False,
    "full": False,
    "verbose": False,
    "delete": False,
    "state": DEFAULT_STATE,
    "timeout": DEFAULT_TIMEOUT,
}


def find_config_path(args):
    for i, a in enumerate(args):
        if a in ("-config", "--config") and i + 1 < len(args):
            return args[i + 1]
        if a.startswith("-config="):
            return a[len("-config="):]
    return DEFAULT_CONFIG_PATH


def load_config(path):
    cfg = dict(DEFAULTS)
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except FileNotFoundError:
        return cfg
    except OSError as e:
        die("读取配置文件 {} 失败：{}".format(path, e))
    except tomllib.TOMLDecodeError as e:
        die("解析配置文件 {} 失败：{}".format(path, e))
    if not isinstance(data, dict):
        die("解析配置文件 {} 失败：内容不是 TOML 表".format(path))
    for key, value in data.items():
        if key in cfg:
            cfg[key] = value
    return cfg


def as_str_list(value):
    if value is None:
        return []
    if isinstance(value, str):
        return [v.strip() for v in value.split(",") if v.strip()]
    return [str(v).strip() for v in value if str(v).strip()]


def parse_str_list(s):
    return as_str_list(s)


def build_parser(cfg):
    p = argparse.ArgumentParser(
        prog="douban2memos",
        description="把豆瓣用户带短评的收藏标记导入 Memos（数据源：公开 RSS + 油猴 CSV）。",
        epilog="配置文件键名 = 选项名去掉 --（- 可写作 _），如 douban_user_id。",
    )
    p.add_argument("--config", default=DEFAULT_CONFIG_PATH, help="配置文件路径（默认 config.toml，不存在则跳过）")
    p.add_argument("--douban-user-id", dest="douban_user_id", default=cfg["douban_user_id"],
                   help="豆瓣用户 ID（主页 URL 里 /people/{id}/，RSS 抓取必填）")
    p.add_argument("--import-csv", dest="import_csv", type=parse_str_list, default=cfg["import_csv"],
                   help="初始导入：油猴脚本 export.user.js 导出的 CSV 文件，逗号分隔多个（一次性历史全量）")
    p.add_argument("--api", default=cfg["api"], help="Memos API 地址（设置则用 API 模式）")
    p.add_argument("--token", default=cfg["token"], help="Memos token（memos < 0.30 的 Access Token）")
    p.add_argument("--password", default=cfg["password"], help="Memos 密码（memos >= 0.30 用于登录换取 token）")
    p.add_argument("--db", default=cfg["db"], help="Memos sqlite 数据库路径（直写模式）")
    p.add_argument("--user", default=cfg["user"], help="Memos 用户名（直写模式必填；API 模式用于登录/过滤）")
    p.add_argument("--visibility", default=cfg["visibility"], help="memo 可见性：private/protected/public")
    p.add_argument("--tag", default=cfg["tag"], help="附加标签（默认以 #tag 追加到正文并显式传入；--no-tag-in-content 后仅显式传入标签）")
    p.add_argument("--tag-in-content", dest="tag_in_content", action=argparse.BooleanOptionalAction, default=cfg["tag_in_content"], help="是否将标签以 #tag 追加到正文（默认追加并显式传入；关闭后仅显式传入，正文不含 #tag，编辑 memo 会丢失标签）")
    p.add_argument("--dry-run", dest="dry_run", action="store_true", default=cfg["dry_run"], help="只预览不写入")
    p.add_argument("--full", action="store_true", default=cfg["full"], help="忽略状态文件，全量处理")
    p.add_argument("--verbose", action="store_true", default=cfg["verbose"], help="逐条输出创建的 memo")
    p.add_argument("--delete", action="store_true", default=cfg["delete"],
                   help="卸载已导入的豆瓣 memos（删除 uid 以 douban- 开头的 memo）")
    p.add_argument("--state", default=cfg["state"], help="增量状态文件路径")
    p.add_argument("--timeout", type=int, default=cfg["timeout"], help="HTTP 超时秒数")
    return p


def subject_id_from_link(link):
    m = re.search(r"(?:subject|game|drama)/(\d+)", link or "")
    return m.group(1) if m else ""


def category_from_link(link):
    link = link or ""
    if "book.douban.com" in link:
        return "book"
    if "music.douban.com" in link:
        return "music"
    if "/game/" in link:
        return "game"
    if "location/drama" in link:
        return "drama"
    return "movie"


def build_content(status, title, comment, rating, link):
    body = "{0}《{1}》：{2}".format(status, title, comment)
    if rating:
        body += " 〔{0}〕".format(rating)
    return "{0}\n\n{1}".format(body, link)


def rating_from_rss(text):
    text = (text or "").strip()
    return RATING_RSS.get(text)


def rating_from_num(num):
    if num is None:
        return None
    try:
        n = int(str(num).strip())
    except ValueError:
        return None
    for word, val in RATING_ORDER.items():
        if val == n:
            return word
    return None


def parse_pubdate(s):
    dt = email.utils.parsedate_to_datetime(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt


def parse_rating_date(s):
    s = (s or "").strip().replace("/", "-")
    try:
        return datetime.datetime.strptime(s, "%Y-%m-%d")
    except ValueError:
        try:
            return datetime.datetime.strptime(s, "%Y-%m")
        except ValueError:
            raise ValueError("无法解析打分日期 {!r}".format(s))


def strip_movie_useful(comment):
    return re.sub(r"\s*\(\d+ 有用\)\s*$", "", comment).rstrip()


def load_state(path):
    if not path:
        return {"last_updated_ts": 0}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or "last_updated_ts" not in data:
            return {"last_updated_ts": 0}
        return {"last_updated_ts": int(data.get("last_updated_ts", 0))}
    except FileNotFoundError:
        return {"last_updated_ts": 0}
    except (OSError, ValueError) as e:
        die("解析状态文件 {} 失败：{}".format(path, e))


def save_state(path, state):
    if not path:
        return
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def fmt_ts(ts):
    return time.strftime("%Y-%m-%d %H:%M:%S %z", time.localtime(ts))


def http_req(url, *, method="GET", data=None, headers=None, timeout=DEFAULT_TIMEOUT):
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except urllib.error.URLError as e:
        raise RuntimeError("请求 {} 失败：{}".format(url, e.reason))


def parse_rss_description(desc):
    rating = comment = ""
    for m in re.finditer(r"<p[^>]*>(.*?)</p>", desc or "", re.S):
        text = html.unescape(m.group(1)).strip()
        if text.startswith("推荐:"):
            rating = text[len("推荐:"):].strip()
        elif text.startswith("备注:"):
            comment = text[len("备注:"):].strip()
    return rating, comment


def fetch_rss_items(user_id, timeout):
    url = RSS_FEED_URL.format(uid=urllib.parse.quote(user_id))
    code, body = http_req(url, headers={
        "User-Agent": default_ua(),
        "Accept": "application/rss+xml, application/xml, text/xml",
    }, timeout=timeout)
    if code != 200:
        raise RuntimeError("豆瓣 RSS 返回 HTTP {0}：{1}".format(code, truncate(body.decode("utf-8", "replace"), 200)))
    try:
        root = ET.fromstring(body)
    except ET.ParseError as e:
        raise RuntimeError("解析豆瓣 RSS 失败：{}".format(e))
    items = []
    for item in root.iter("item"):
        def child(tag):
            el = item.find(tag)
            return el.text if el is not None else ""
        title = (child("title") or "").strip()
        link = (child("link") or "").strip()
        pub = (child("pubDate") or "").strip()
        guid = (child("guid") or "").strip()
        desc = child("description")
        if not title or not link or not pub:
            continue
        items.append({
            "title": title, "link": link, "pubDate": pub,
            "guid": guid, "description": desc,
        })
    return items


def rss_item_to_memo(item):
    title = item["title"]
    m = ALL_PREFIX_RE.match(title)
    if not m:
        return None
    prefix = m.group(1)
    if prefix not in COMPLETED_PREFIXES:
        return None
    display_title = title[m.end():].strip(" 《》")
    link = item["link"].strip()
    sid = subject_id_from_link(link)
    if not sid:
        return None
    rating, comment = parse_rss_description(item.get("description") or "")
    comment = strip_movie_useful(comment)
    if not comment.strip():
        return None
    rating_word = rating_from_rss(rating)
    dt = parse_pubdate(item["pubDate"])
    ts = int(dt.timestamp())
    return {
        "uid": UID_PREFIX + sid,
        "content": build_content(prefix, display_title, comment, rating_word, link),
        "create_time": dt.isoformat(),
        "ts": ts,
        "title": display_title,
    }


def csv_reader(path):
    try:
        f = open(path, "r", encoding="utf-8-sig", newline="")
    except OSError as e:
        die("打开 CSV {} 失败：{}".format(path, e))
    try:
        reader = csv.reader(f)
        header = next(reader, None)
        if not header:
            die("CSV {} 为空".format(path))
        for row in reader:
            yield row
    finally:
        f.close()


def csv_row_to_memo(row, has_detail):
    row = [c.strip() for c in row]
    if len(row) < 2:
        return None
    title = row[0]
    link = row[-1]
    sid = subject_id_from_link(link)
    if not sid:
        return None
    if has_detail:
        if len(row) < 5:
            return None
        rating_num = row[1]
        rating_date = row[2]
        comment = row[3]
    else:
        rating_num = ""
        rating_date = ""
        comment = ""
    rating_word = rating_from_num(rating_num)
    comment = strip_movie_useful(comment)
    if not comment.strip():
        return None
    category = category_from_link(link)
    status = CATEGORY_LABELS.get(category, "看过")
    if rating_date:
        dt = parse_rating_date(rating_date)
        ts = int(dt.replace(tzinfo=datetime.timezone(datetime.timedelta(0))).timestamp())
        create_time = datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).isoformat()
    else:
        dt = None
        ts = 0
        create_time = ""
    return {
        "uid": UID_PREFIX + sid,
        "content": build_content(status, title, comment, rating_word, link),
        "create_time": create_time,
        "ts": ts,
        "title": title,
    }


def read_csv_items(paths):
    items = []
    for path in paths:
        has_detail = None
        count = 0
        for row in csv_reader(path):
            if has_detail is None:
                has_detail = len(row) >= 5
            item = csv_row_to_memo(row, has_detail)
            if item:
                items.append(item)
                count += 1
        print("  CSV {}：解析 {} 条带短评收藏".format(path, count))
    return items


def find_import_csv():
    found = []
    try:
        names = os.listdir(".")
    except OSError as e:
        die("扫描当前目录失败：{}".format(e))
    for n in sorted(names):
        if CSV_NAME_RE.match(n):
            found.append(n)
    return found


class APIWriter:
    def __init__(self, base, token, user):
        self.base = base.rstrip("/")
        self.token = token
        self.user = user
        self.timeout = DEFAULT_TIMEOUT

    def _request(self, method, path, query=None, payload=None):
        url = self.base + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        data = None
        headers = {"Authorization": "Bearer " + self.token}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        return http_req(url, method=method, data=data, headers=headers, timeout=self.timeout)

    def set_timeout(self, timeout):
        self.timeout = timeout

    def close(self):
        pass

    def list_existing_uids(self):
        uids = set()
        page_token = ""
        while True:
            query = {"pageSize": "1000"}
            if self.user:
                query["filter"] = 'creator == "{0}"'.format(self.user)
            if page_token:
                query["pageToken"] = page_token
            code, body = self._request("GET", "/api/v1/memos", query)
            if code != 200:
                raise RuntimeError("memos 列表请求失败（HTTP {0}）：{1}".format(code, truncate(body.decode("utf-8", "replace"), 200)))
            out = json.loads(body.decode("utf-8"))
            for m in out.get("memos") or []:
                name = m.get("name") or ""
                if name.startswith("memos/"):
                    uids.add(name[len("memos/"):])
            page_token = out.get("nextPageToken") or ""
            if not page_token:
                break
        return uids

    def list_douban_owned(self):
        return sorted(u for u in self.list_existing_uids() if u.startswith(UID_PREFIX))

    def create(self, uid, content, visibility, create_time, ts, tag, tag_in_content=True):
        if tag and tag_in_content:
            content += "\n#" + tag
        payload = {"content": content, "visibility": visibility}
        if tag:
            payload["tags"] = [tag]
        if create_time:
            payload["createTime"] = create_time
        code, body = self._request("POST", "/api/v1/memos", {"memoId": uid}, payload)
        if code == 200:
            return True
        api_code = None
        try:
            api_code = json.loads(body.decode("utf-8")).get("code")
        except ValueError:
            pass
        if api_code == 6:
            return False
        raise RuntimeError("创建 memo 失败：HTTP {0}：{1}".format(code, truncate(body.decode("utf-8", "replace"), 200)))

    def delete(self, uid):
        code, body = self._request("DELETE", "/api/v1/memos/" + urllib.parse.quote(uid, safe=""))
        if code in (200, 404):
            return
        raise RuntimeError("删除 memo 失败：HTTP {0}：{1}".format(code, truncate(body.decode("utf-8", "replace"), 200)))


def sign_in(base, username, password, timeout):
    url = base.rstrip("/") + "/api/v1/auth/signin"
    payload = json.dumps({"password_credentials": {"username": username, "password": password}}).encode("utf-8")
    code, body = http_req(url, method="POST", data=payload,
                          headers={"Content-Type": "application/json"}, timeout=timeout)
    if code != 200:
        raise RuntimeError("memos 登录失败（HTTP {0}）：{1}".format(code, truncate(body.decode("utf-8", "replace"), 200)))
    try:
        out = json.loads(body.decode("utf-8"))
    except ValueError as e:
        raise RuntimeError("解析 memos 登录响应失败：{}".format(e))
    token = out.get("accessToken") or out.get("token") or ""
    user = (out.get("user") or {}).get("name", "")
    if not token:
        raise RuntimeError("memos 登录未返回 access token")
    writer = APIWriter(base, token, user)
    writer.set_timeout(timeout)
    return writer


class DBWriter:
    def __init__(self, path, username):
        self.conn = sqlite3.connect(path)
        self.conn.execute("PRAGMA busy_timeout = 3000")
        try:
            row = self.conn.execute("SELECT id FROM user WHERE username = ?", (username,)).fetchone()
        except sqlite3.Error as e:
            self.conn.close()
            raise RuntimeError("查询 memos 用户失败：{}".format(e))
        if row is None:
            self.conn.close()
            raise RuntimeError("memos 数据库中没有用户 {!r}，请检查 user 配置".format(username))
        self.user_id = row[0]

    def list_existing_uids(self):
        rows = self.conn.execute("SELECT uid FROM memo").fetchall()
        return set(r[0] for r in rows if r[0])

    def list_douban_owned(self):
        rows = self.conn.execute(
            "SELECT uid FROM memo WHERE creator_id = ? AND uid LIKE ? ORDER BY uid",
            (self.user_id, UID_PREFIX + "%")).fetchall()
        return [r[0] for r in rows]

    def create(self, uid, content, visibility, create_time, ts, tag, tag_in_content=True):
        exists = self.conn.execute("SELECT COUNT(1) FROM memo WHERE uid = ?", (uid,)).fetchone()[0]
        if exists > 0:
            return False
        if tag and tag_in_content:
            content += "\n#" + tag
        payload = {}
        if tag:
            payload["tags"] = [tag]
        self.conn.execute(
            "INSERT INTO memo (uid, creator_id, created_ts, updated_ts, row_status, content, visibility, pinned, payload)"
            " VALUES (?, ?, ?, ?, 'NORMAL', ?, ?, 0, ?)",
            (uid, self.user_id, ts, ts, content, visibility, json.dumps(payload)))
        self.conn.commit()
        return True

    def delete(self, uid):
        self.conn.execute("DELETE FROM memo WHERE uid = ? AND creator_id = ?", (uid, self.user_id))
        self.conn.commit()

    def close(self):
        self.conn.close()


def open_writer(cfg, timeout):
    if cfg["api"]:
        if cfg["token"]:
            writer = APIWriter(cfg["api"].rstrip("/"), cfg["token"], cfg["user"])
        elif cfg["password"] and cfg["user"]:
            writer = sign_in(cfg["api"], cfg["user"], cfg["password"], timeout)
        else:
            raise RuntimeError("API 模式需要 --token，或 --user 与 --password（memos >= 0.30）")
        writer.set_timeout(timeout)
        existing = writer.list_existing_uids()
        return writer, existing
    writer = DBWriter(cfg["db"], cfg["user"])
    try:
        existing = writer.list_existing_uids()
    except Exception:
        writer.close()
        raise
    return writer, existing


def visibility_value(s):
    return {"protected": VIS_PROTECTED, "public": VIS_PUBLIC}.get(s, VIS_PRIVATE)


def sync(cfg):
    timeout = cfg.get("timeout") or DEFAULT_TIMEOUT
    api_mode = bool(cfg.get("api"))
    if not api_mode and not cfg.get("db"):
        cfg["db"] = DEFAULT_DB

    import_paths = as_str_list(cfg.get("import_csv"))
    user_id = (cfg.get("douban_user_id") or "").strip()
    if not import_paths:
        import_paths = find_import_csv()
        if import_paths:
            print("检测到当前目录下的油猴 CSV（自动初始导入）：{}".format(", ".join(import_paths)))
    if not import_paths and not user_id:
        die("缺少 douban_user_id（RSS 同步用；或用 --import-csv 只做初始导入）")

    try:
        if import_paths:
            print("正在从油猴 CSV 导入历史收藏：{}".format(", ".join(import_paths)))
            items = read_csv_items(import_paths)
        else:
            print("正在从豆瓣 RSS（{}）拉取最近兴趣…".format(RSS_FEED_URL.format(uid=user_id)))
            items = [it for it in (rss_item_to_memo(i) for i in fetch_rss_items(user_id, timeout)) if it]
    except Exception as e:
        die(str(e))

    items.sort(key=lambda it: it["ts"], reverse=True)

    state = load_state(cfg.get("state") or "")
    incremental = (not cfg.get("full")) and state["last_updated_ts"] > 0
    if incremental:
        print("增量模式：跳过时间戳 <= {} 的旧条目（--full 强制全量）".format(fmt_ts(state["last_updated_ts"])))
    else:
        print("全量模式：首次运行或无有效状态，处理本次抓取的全部条目")

    writer = None
    existing = set()
    if not cfg.get("dry_run"):
        writer, existing = open_writer(cfg, timeout)
        close_after = True
    else:
        close_after = False

    created = skipped = total = 0
    max_ts = 0
    success_max_ts = state["last_updated_ts"]
    has_error = False
    try:
        for it in items:
            total += 1
            ts = it["ts"]
            if ts > max_ts:
                max_ts = ts
            if incremental and ts <= state["last_updated_ts"]:
                break
            if cfg.get("dry_run"):
                content = it["content"]
                if cfg.get("tag") and cfg.get("tag_in_content"):
                    content += "\n#" + cfg.get("tag")
                print("  [dry-run] {}\n{}\n".format(it["uid"], content))
                created += 1
                if ts > success_max_ts:
                    success_max_ts = ts
                continue
            if it["uid"] in existing:
                skipped += 1
                if ts > success_max_ts:
                    success_max_ts = ts
                continue
            try:
                ok = writer.create(it["uid"], it["content"], visibility_value(cfg.get("visibility")),
                                   it.get("create_time") or "", ts, cfg.get("tag") or "",
                                   cfg.get("tag_in_content", True))
            except Exception as e:
                print("  创建 {} 失败：{}".format(it["uid"], e))
                has_error = True
                continue
            if ok:
                if cfg.get("verbose"):
                    print("  已创建 {}：{}".format(it["uid"], it["title"]))
                created += 1
            else:
                skipped += 1
            if ts > success_max_ts:
                success_max_ts = ts
    finally:
        if close_after:
            writer.close()

    if not cfg.get("dry_run") and not has_error and success_max_ts > state["last_updated_ts"]:
        state["last_updated_ts"] = success_max_ts
        try:
            save_state(cfg.get("state") or "", state)
        except OSError as e:
            die("保存状态文件失败：{}".format(e))
    elif has_error:
        print("本次同步存在 memos 写入失败，未更新增量状态文件（下次将重试）")

    action = "dry-run 待创建" if cfg.get("dry_run") else "创建"
    print("\n完成：扫描 {} 条，{} {} 条，跳过 {} 条".format(total, action, created, skipped))
    return 0


def uninstall(cfg):
    if not cfg.get("api") and not cfg.get("db"):
        cfg["db"] = DEFAULT_DB
    timeout = cfg.get("timeout") or DEFAULT_TIMEOUT
    writer, _ = open_writer(cfg, timeout)
    try:
        uids = writer.list_douban_owned()
        if not uids:
            print("未找到已导入的豆瓣 memo（uid 以 douban- 开头）")
        elif cfg.get("dry_run"):
            for uid in uids:
                print("  [dry-run] 将删除 {}".format(uid))
            print("\n完成：dry-run 待删除 {} 条".format(len(uids)))
        else:
            deleted = 0
            for uid in uids:
                try:
                    writer.delete(uid)
                except Exception as e:
                    print("  删除 {} 失败：{}".format(uid, e))
                    continue
                if cfg.get("verbose"):
                    print("  已删除 {}".format(uid))
                deleted += 1
            print("\n完成：删除 {} 条豆瓣 memo，失败 {} 条".format(deleted, len(uids) - deleted))
    finally:
        writer.close()

    if not cfg.get("dry_run"):
        try:
            os.remove(cfg.get("state") or "")
            print("已重置增量状态文件 {}（下次同步为全量）".format(cfg.get("state")))
        except OSError:
            pass
    return 0


def main(argv):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)
        sys.stderr.reconfigure(line_buffering=True)
    config_path = find_config_path(argv)
    cfg = load_config(config_path)
    args = build_parser(cfg).parse_args(argv)
    cfg.update({k: v for k, v in vars(args).items() if k in cfg})
    if cfg.get("delete"):
        return uninstall(cfg)
    return sync(cfg)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))