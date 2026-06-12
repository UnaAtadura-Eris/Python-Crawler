import csv
import hashlib
import html
import logging
import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
import os
import pandas as pd
import requests

from dotenv import load_dotenv

# 解决中文乱码
import sys
import io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')


# =========================
# 配置区
# =========================

SEARCH_KEYWORDS = [
    "Twitter",
    "TikTok",
    "Netflix",
    "Steam",
    "codex",
    "Instagram",
    "Gemini",
    "GitHub",
    "Docker",
    "YouTube",
    "Telegram",
    "WhatsApp",
    "Facebook",
    "Reddit",
    "Snapchat",
    "科学上网",
    "Spotify",
    "Discord",
    "Epic Games",
    "留学",
    "海外",
    "翻墙",
]

COMMENT_PATTERNS = [
    # r"翻.?墙",
    # r"科.?学.?上.?网",
    # r"魔.?法",
    # r"梯.?子",
    # r"VPN",
    # r"机场",
    # r"节点",
    r".*?(有偿|付费|报酬|给钱|给米).*?",
    r".*?(怎么|如何|求|推荐|教|带).*?(上网|翻墙|科学上网|魔法|梯子|VPN|机场|节点).*?",
]

# 仅保留最近 N 天内的评论
DAYS_LIMIT = 3

# 搜索结果最大页数
SEARCH_MAX_PAGES = 20

# 每个视频主评论最大采集页数
COMMENT_MAX_PAGES = 5

# 每条主评论楼中楼最大采集页数
SUB_REPLY_MAX_PAGES = 1

# 评论每页数量，B站接口常见最大值为 20
COMMENT_PAGE_SIZE = 20
SUB_REPLY_PAGE_SIZE = 20

# 并发数量，建议不要太大
MAX_WORKERS = 5

# 请求超时时间
REQUEST_TIMEOUT = 10

# 请求失败重试次数
MAX_RETRIES = 1

# 请求间隔，避免过快
REQUEST_SLEEP_MIN = 1.0
REQUEST_SLEEP_MAX = 2.5

# 输出文件
OUTPUT_CSV = "bilibili_comments_result.csv"

# 日志文件
LOG_FILE = "bilibili_crawler.log"

# 填入浏览器中的 Cookie（建议填写，否则搜索接口容易返回空）
# 打开 B站 -> F12 -> Network -> 任意请求 -> Headers -> Cookie
# 至少需要包含 SESSDATA
load_dotenv()
COOKIE = os.environ.get("BILIBILI_COOKIE", "")

# =========================
# 签名相关
# =========================

# B站 wbi 签名所需的 mixin key（会定期失效，失效后需要更新）
# 如果出现 -352 错误，说明此 key 已过期，需要重新抓取
# 抓取方式：https://api.bilibili.com/x/web-interface/nav 返回的 wbi_img 字段
_MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
]

_wbi_keys_cache: Dict = {}
_wbi_keys_lock = threading.Lock()


def _get_wbi_keys(session: requests.Session) -> Tuple[str, str]:
    """从 B站接口动态获取 img_key 和 sub_key，带缓存（每小时刷新）。"""
    with _wbi_keys_lock:
        now = time.time()
        if _wbi_keys_cache.get("expire", 0) > now:
            return _wbi_keys_cache["img_key"], _wbi_keys_cache["sub_key"]

        try:
            resp = session.get(
                "https://api.bilibili.com/x/web-interface/nav",
                timeout=REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
            nav = resp.json()

            wbi_img = nav["data"]["wbi_img"]
            img_url: str = wbi_img["img_url"]
            sub_url: str = wbi_img["sub_url"]

            img_key = img_url.rsplit("/", 1)[-1].split(".")[0]
            sub_key = sub_url.rsplit("/", 1)[-1].split(".")[0]

            raw = img_key + sub_key
            mixin_key = "".join(raw[i] for i in _MIXIN_KEY_ENC_TAB)[:32]

            _wbi_keys_cache["img_key"] = img_key
            _wbi_keys_cache["sub_key"] = sub_key
            _wbi_keys_cache["mixin_key"] = mixin_key
            _wbi_keys_cache["expire"] = now + 3600

            logging.info("WBI keys 刷新成功 img_key=%s", img_key)
            print(f"[签名] WBI keys 刷新成功")

        except Exception as exc:
            logging.warning("获取 WBI keys 失败，将跳过签名: %s", exc)
            print(f"[签名] 获取 WBI keys 失败: {exc}，请求将不带签名")
            _wbi_keys_cache["img_key"] = ""
            _wbi_keys_cache["sub_key"] = ""
            _wbi_keys_cache["mixin_key"] = ""
            _wbi_keys_cache["expire"] = now + 300  # 失败后 5 分钟再重试

        return _wbi_keys_cache["img_key"], _wbi_keys_cache["sub_key"]


def _add_wbi_sign(params: dict, session: requests.Session) -> dict:
    """为请求参数添加 wts / w_rid 签名。"""
    _get_wbi_keys(session)  # 确保 keys 已加载
    mixin_key = _wbi_keys_cache.get("mixin_key", "")
    if not mixin_key:
        return params  # 获取失败时跳过签名

    params = dict(params)
    params["wts"] = int(time.time())

    # 过滤特殊字符
    chr_filter = re.compile(r"[!'()*]")
    query_parts = []
    for k in sorted(params.keys()):
        v = chr_filter.sub("", str(params[k]))
        query_parts.append(f"{k}={v}")

    query_str = "&".join(query_parts)
    w_rid = hashlib.md5((query_str + mixin_key).encode()).hexdigest()
    params["w_rid"] = w_rid

    return params


# =========================
# 日志配置
# =========================

logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    encoding="utf-8",
)

thread_local = threading.local()


def get_session() -> requests.Session:
    """为每个线程创建独立 Session。"""
    if not hasattr(thread_local, "session"):
        session = requests.Session()
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            ),
            "Referer": "https://www.bilibili.com/",
            "Origin": "https://www.bilibili.com",
        }
        if COOKIE and COOKIE.strip():
            headers["Cookie"] = COOKIE.strip()
        session.headers.update(headers)
        thread_local.session = session
    return thread_local.session


def request_json(
    url: str,
    params: Optional[dict] = None,
    sign: bool = False,
) -> Optional[dict]:
    """带重试、签名的 JSON 请求。"""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            time.sleep(random.uniform(REQUEST_SLEEP_MIN, REQUEST_SLEEP_MAX))

            session = get_session()
            _params = dict(params) if params else {}

            if sign:
                _params = _add_wbi_sign(_params, session)

            response = session.get(url, params=_params, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()

            data = response.json()
            code = data.get("code")

            if code != 0:
                msg = data.get("message", "")
                logging.warning(
                    "接口返回异常 code=%s message=%s url=%s params=%s",
                    code, msg, url, _params,
                )
                print(f"[接口异常] code={code} msg={msg} url={url}")

                # -352 是签名失败，-101 是未登录，这两种情况重试无意义
                if code in (-352, -101, -403):
                    return None

                return None

            return data

        except Exception as exc:
            logging.warning(
                "请求失败 attempt=%s/%s url=%s error=%s",
                attempt, MAX_RETRIES, url, exc,
            )
            print(f"[请求失败] 第{attempt}/{MAX_RETRIES}次 {url} {exc}")

            if attempt == MAX_RETRIES:
                logging.error("请求最终失败 url=%s", url)
                return None

            time.sleep(1.5 * attempt)

    return None


def clean_text(text: str) -> str:
    """清洗 HTML 标签与空白字符。"""
    if not text:
        return ""
    text = html.unescape(text)
    text = re.sub(r"<.*?>", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def timestamp_to_str(ts: int) -> str:
    """时间戳转可读时间。"""
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def search_videos() -> List[Dict]:
    """搜索 Bilibili 视频，支持关键词和分页，带 WBI 签名。"""
    videos = []
    seen_bvid = set()

    search_url = "https://api.bilibili.com/x/web-interface/search/type"

    for keyword in SEARCH_KEYWORDS:
        for page in range(1, SEARCH_MAX_PAGES + 1):
            print(f"[搜索] 关键词：{keyword} 页码：{page}")

            params = {
                "search_type": "video",
                "keyword": keyword,
                "page": page,
            }

            # 搜索接口需要 WBI 签名
            data = request_json(search_url, params=params, sign=True)

            if not data:
                print(f"[搜索] 关键词「{keyword}」第{page}页请求失败，跳过")
                continue

            result_data = data.get("data", {}) or {}
            results = result_data.get("result", []) or []

            print(f"[搜索] 关键词「{keyword}」第{page}页，返回 {len(results)} 个视频")

            if not results:
                print(f"[搜索] 没有结果，可能需要检查 Cookie 或签名")

            for item in results:
                bvid = item.get("bvid")
                aid = item.get("aid")

                if not bvid or not aid or bvid in seen_bvid:
                    continue

                seen_bvid.add(bvid)

                video = {
                    "title": clean_text(item.get("title", "")),
                    "bvid": bvid,
                    "aid": aid,
                    "up_name": clean_text(item.get("author", "")),
                    "pub_time": timestamp_to_str(item.get("pubdate", 0)),
                    "url": item.get("arcurl") or f"https://www.bilibili.com/video/{bvid}",
                    "keyword": keyword,
                }
                videos.append(video)

    print(f"[搜索完成] 共获取视频 {len(videos)} 个")
    return videos


def match_comment(content: str) -> Tuple[bool, str]:
    """匹配评论内容，命中任意正则即保留。"""
    for pattern in COMMENT_PATTERNS:
        if re.search(pattern, content, flags=re.IGNORECASE):
            return True, pattern
    return False, ""


def parse_reply_item(reply: Dict, video: Dict) -> Optional[Dict]:
    """解析单条评论或回复。"""
    ctime = reply.get("ctime", 0)
    comment_time = datetime.fromtimestamp(ctime)

    if comment_time < datetime.now() - timedelta(days=DAYS_LIMIT):
        return None

    content = clean_text(reply.get("content", {}).get("message", ""))

    matched, matched_pattern = match_comment(content)
    if not matched:
        return None

    member = reply.get("member", {}) or {}

    return {
        "video_title": video["title"],
        "video_url": video["url"],
        "bvid": video["bvid"],
        "up_name": video["up_name"],
        "comment_id": str(reply.get("rpid", "")),
        "comment_author": clean_text(member.get("uname", "")),
        "comment_time": timestamp_to_str(ctime),
        "comment_content": content,
        "matched_pattern": matched_pattern,
    }


def get_sub_replies(video: Dict, root_rpid: int) -> List[Dict]:
    """获取楼中楼回复。"""
    rows = []
    sub_reply_url = "https://api.bilibili.com/x/v2/reply/reply"

    for page in range(1, SUB_REPLY_MAX_PAGES + 1):
        params = {
            "type": 1,
            "oid": video["aid"],
            "root": root_rpid,
            "pn": page,
            "ps": SUB_REPLY_PAGE_SIZE,
        }

        data = request_json(sub_reply_url, params=params, sign=True)
        if not data:
            break

        replies = data.get("data", {}).get("replies", []) or []
        if not replies:
            break

        for reply in replies:
            row = parse_reply_item(reply, video)
            if row:
                rows.append(row)

    return rows


def get_comments(video: Dict) -> List[Dict]:
    """获取单个视频的主评论和楼中楼回复。"""
    rows = []
    reply_url = "https://api.bilibili.com/x/v2/reply"

    print(f"[评论] 开始采集：{video['title']} ({video['bvid']})")

    for page in range(1, COMMENT_MAX_PAGES + 1):
        print(f"[评论] {video['bvid']} 主评论页：{page}")

        params = {
            "type": 1,
            "oid": video["aid"],
            "pn": page,
            "ps": COMMENT_PAGE_SIZE,
            "sort": 2,
        }

        data = request_json(reply_url, params=params, sign=True)
        if not data:
            break

        reply_data = data.get("data", {}) or {}
        replies = reply_data.get("replies", []) or []

        print(f"[评论] {video['bvid']} 第{page}页，获取到 {len(replies)} 条主评论")

        if not replies:
            break

        for reply in replies:
            main_row = parse_reply_item(reply, video)
            if main_row:
                rows.append(main_row)

            root_rpid = reply.get("rpid")
            reply_count = reply.get("rcount", 0)

            if root_rpid and reply_count > 0:
                sub_rows = get_sub_replies(video, root_rpid)
                rows.extend(sub_rows)

    print(f"[评论完成] {video['bvid']} 命中评论 {len(rows)} 条")
    return rows


def save_csv(rows: List[Dict], output_file: str = OUTPUT_CSV) -> None:
    """保存 CSV，并按评论 ID 去重。"""
    if not rows:
        print("[保存] 没有符合条件的评论，未生成 CSV")
        return

    df = pd.DataFrame(rows)

    columns = [
        "video_title",
        "video_url",
        "bvid",
        "up_name",
        "comment_id",
        "comment_author",
        "comment_time",
        "comment_content",
        "matched_pattern",
    ]

    df = df[columns]
    df = df.drop_duplicates(subset=["comment_id"], keep="first")
    
    df.to_csv(
        output_file,
        index=False,
        encoding="utf-8-sig",
        quoting=csv.QUOTE_MINIMAL,
    )

    print(f"[保存完成] {output_file}，共 {len(df)} 条")


def main() -> None:
    """主入口。"""
    print("[开始] Bilibili 视频评论采集")
    print(f"[配置] 最近 {DAYS_LIMIT} 天，搜索页数 {SEARCH_MAX_PAGES}，评论页数 {COMMENT_MAX_PAGES}")

    if not COOKIE or not COOKIE.strip():
        print("[警告] 未填写 Cookie，搜索接口可能返回空结果，建议填写 SESSDATA")

    videos = search_videos()
    if not videos:
        print("[结束] 未搜索到视频，请检查：")
        print("  1. Cookie 是否填写（尤其是 SESSDATA）")
        print("  2. 查看日志文件了解接口返回的 code")
        return

    all_rows = []

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        future_map = {
            executor.submit(get_comments, video): video
            for video in videos
        }

        finished = 0
        total = len(future_map)

        for future in as_completed(future_map):
            video = future_map[future]
            finished += 1

            try:
                rows = future.result()
                all_rows.extend(rows)
                print(f"[进度] {finished}/{total} 完成：{video['bvid']}，累计命中 {len(all_rows)} 条")
            except Exception as exc:
                logging.exception("采集视频评论失败 bvid=%s error=%s", video.get("bvid"), exc)
                print(f"[错误] {video['bvid']} 采集失败，详情见日志")

    save_csv(all_rows)
    print("[结束] 采集完成")


if __name__ == "__main__":
    main()