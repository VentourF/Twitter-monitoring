# -*- coding: utf-8 -*-
"""Twitter monitoring —— 监控 X (Twitter) 指定用户的新推文，并全网搜索免费情报推送。

用法:
    python monitor.py          # 持续轮询监控
    python monitor.py --test   # 只发一条测试消息验证各推送通道
    python monitor.py --once   # 只轮询一次后退出（配合系统计划任务使用）
    python monitor.py --search # 手动执行一轮免费情报搜索
    python monitor.py --digest # 手动生成并发送一条免费情报日报
"""

import argparse
import asyncio
import html
import json
import logging
import re
import smtplib
import sqlite3
import sys
import time
import zoneinfo
from datetime import datetime, timedelta
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr
from logging.handlers import RotatingFileHandler
from pathlib import Path

import httpx
from twikit import Client
from twikit.errors import TooManyRequests, Unauthorized, NotFound

import xclient  # X 接口适配层：导入即打齐 twikit 兼容补丁
import store    # 存储层：push_records 的 SQLite/MySQL/PostgreSQL 多后端



try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
STATE_PATH = BASE_DIR / "state.json"
COOKIES_PATH = BASE_DIR / "cookies.json"
LOG_PATH = BASE_DIR / "monitor.log"
DB_PATH = BASE_DIR / "records.db"


def init_db() -> None:
    """按 config.json 的 database 段初始化存储（SQLite / MySQL / PostgreSQL，默认 SQLite）。"""
    store.init(load_config(), DB_PATH)


def record_push(handle, display, status, text="", tag="", tweet_id="", url="",
                tweet_created="", translation="", time_conv=None, results=None,
                source="monitor") -> None:
    store.get().push_record(handle, display, status, text=text, tag=tag,
                            tweet_id=tweet_id, url=url, tweet_created=tweet_created,
                            translation=translation, time_conv=time_conv,
                            results=results, source=source)


def tweet_seen(tweet_id) -> bool:
    """该推文是否已入库（任意管线），用于跨管线去重。"""
    return store.get().tweet_seen(tweet_id)


# 面板/CLI 共用的运行状态与互斥锁（防止循环轮询和手动检查同时跑导致重复推送）
MONITOR_STATE = {"running": False, "last_check": None, "last_error": None,
                 "intel_last_search_ts": 0.0, "intel_digest_retry_ts": 0.0,
                 "intel": {},
                 "x_account_idx": 0, "x_account_cooldown": {},
                 "health": {"main": {"ok": None, "at": "", "error": ""}}}
CHECK_LOCK = asyncio.Lock()

TELEGRAM_MSG_LIMIT = 4096
RATE_LIMIT_WAIT = 905  # X 的非官方接口限流窗口约 15 分钟
MAX_BACKOFF = 3600
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
BJT = zoneinfo.ZoneInfo("Asia/Shanghai")

# 推文里常见的美式时刻表述 → 时区。PST/PDT/PT 统一按洛杉矶本地时间（自动处理夏令时）
TZ_MAP = {
    "PT": "America/Los_Angeles", "PST": "America/Los_Angeles",
    "PDT": "America/Los_Angeles", "PACIFIC": "America/Los_Angeles",
    "ET": "America/New_York", "EST": "America/New_York",
    "EDT": "America/New_York", "EASTERN": "America/New_York",
    "CT": "America/Chicago",
    "UTC": "UTC", "GMT": "UTC",
}
TIME_REF_RE = re.compile(
    r"\b(?:(?P<hour>\d{1,2})(?::(?P<minute>[0-5]\d))?\s*(?P<ampm>a\.?m\.?|p\.?m\.?)?"
    r"|(?P<noon>noon)|(?P<mid>midnight))\s*"
    r"(?P<tz>PT|PST|PDT|PACIFIC|ET|EST|EDT|EASTERN|CT|UTC|GMT)\b",
    re.I,
)
WEEKDAY_CN = "一二三四五六日"

# “疑似重置”识别：直接说 reset，或 Day N/ 每日系列前缀（实测对 151 条历史零漏报）
RESET_TAG = "【疑似重置】"
RESET_PATTERNS = [re.compile(r"\breset\b", re.I), re.compile(r"^\s*day\s*\d", re.I)]
# 官方账号（twitter.official 列表）：推文打【官方】标签，便于一眼识别厂商公告
OFFICIAL_TAG = "【官方】"


def is_reset_like(text: str) -> bool:
    return any(rx.search(text or "") for rx in RESET_PATTERNS)


# 免费情报：X 全网关键词搜索，发现即推 + 每日日报（config.json 的 intel 段）
INTEL_TAG = "【免费情报】"
# X 搜索自带语义扩展（噪音大），所以查询语句管召回、本地 must_match 正则管精确。
DEFAULT_INTEL_QUERIES = [
    '("free model" OR "free models" OR "free credits" OR "free tokens" OR "free tier" '
    'OR "free api") (GPT OR Claude OR Gemini OR LLM OR ChatGPT OR AI OR DeepSeek OR Kimi '
    'OR GLM OR Qwen) lang:en min_faves:5 '
    '-filter:replies -airdrop -crypto -web3 -nft',
    '("限时免费" OR "免费额度" OR "送token" OR "白嫖" OR "免费API" OR "免费试用") '
    '(GPT OR Claude OR Gemini OR LLM OR AI OR OpenAI OR DeepSeek OR Kimi OR GLM '
    'OR 大模型 OR 模型 OR 通义 OR 豆包 OR 智谱) lang:zh min_faves:2 -filter:replies',
]
# 本地精确过滤：必须**同时**命中「模型相关词」和「免费相关词」才算情报。
# 光有"白嫖""免费"不算——否则"白嫖实习生""白嫖多邻国和Spotify"这类无关推文全进来。
# 两个 (?=...) 前瞻分别扫描全文，等价于 AND；(?<![a-z0-9]) 代替 \b，
# 因为中文算 \w，"白嫖ChatGPT"这种中文紧邻英文时 \b 会失效。
DEFAULT_INTEL_MATCH = (
    r"^(?=[\s\S]*?(?:(?<![a-z0-9])(?:models?|llms?|ai|gpt[\w.-]*|chatgpt|claude|gemini|grok"
    r"|llama|mistral|deepseek|qwen|kimi|glm|mimo|sonnet|opus|codex|copilot|cursor|windsurf"
    r"|cline|opencode|perplexity|midjourney|sora|veo|flux|openai|anthropic|groq|openrouter"
    r"|cerebras|moonshot|zhipu|minimax)(?![a-z0-9])|大模型|模型|智能体|通义|豆包|千问|文心"
    r"|智谱|深度求索))"
    r"(?=[\s\S]*?(?:(?<![a-z0-9])(?:free|trial|giveaway|promo|waiv\w*|100\s*%\s*off)"
    r"(?![a-z0-9])|免费|白嫖|限时免费|限免|羊毛|额度|试用|送))"
)
# 本地排除：成人内容 / 加密货币推销 / 中文垃圾营销（视频会员、互关涨粉、拉新返利等）
DEFAULT_INTEL_EXCLUDE = (
    r"onlyfans|nsfw|nudes?|porn|undress|adult video|hookup|escort|sexy"
    r"|airdrop|presale"
    r"|陪聊|陪睡|同城约|约见|车队|晒收益|分成计划|智商税"
    r"|约炮|援交|福利姬|口交|反差|绿帽|绿奴|偷情|贡母|狗链"
    r"|多邻国|duolingo|spotify|netflix|youtube premium|爱奇艺|腾讯视频|优酷|网易云"
    r"|互关|互粉|涨粉|引流|兼职|日赚|返利|优惠券|外卖|快递|话费|流量卡|贷款|信用卡"
    r"|机票|酒店|门票|彩票|博彩|赌"
)


def intel_cfg(cfg: dict) -> dict:
    """读取 intel 配置，缺省字段自动补默认值。"""
    ic = cfg.get("intel") or {}
    queries = [str(q).strip() for q in (ic.get("queries") or []) if str(q).strip()]
    try:
        interval = max(5, int(ic.get("search_interval_minutes") or 10))
    except (TypeError, ValueError):
        interval = 10
    return {
        "enabled": bool(ic.get("enabled", True)),
        "queries": queries or list(DEFAULT_INTEL_QUERIES),
        "search_interval_minutes": interval,
        "digest_time": str(ic.get("digest_time") or "09:00").strip() or "09:00",
        "must_match": str(ic.get("must_match") or DEFAULT_INTEL_MATCH).strip(),
        "exclude_match": str(ic.get("exclude_match") or DEFAULT_INTEL_EXCLUDE).strip(),
    }

log = logging.getLogger("monitor")


def setup_logging() -> None:
    # httpx 每个请求都打一行带巨型 URL 的 INFO，淹没有用信息，压到 WARNING
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            # 单文件 5MB，最多留 3 份历史，防止日志无限增长
            RotatingFileHandler(LOG_PATH, maxBytes=5 * 1024 * 1024,
                                backupCount=3, encoding="utf-8"),
        ],
    )


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        sys.exit(f"缺少配置文件 {CONFIG_PATH}，请按照 README.md 填写 config.json 后再运行。")
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def x_accounts(cfg: dict) -> list[dict]:
    """全部可用 X 账号：twitter.accounts 优先，兼容旧的单账号 auth_token/ct0 写法。"""
    tw = cfg.get("twitter") or {}
    accs = []
    for a in tw.get("accounts") or []:
        if not isinstance(a, dict):
            continue
        at, ct = (a.get("auth_token") or "").strip(), (a.get("ct0") or "").strip()
        if at and ct:
            accs.append({"auth_token": at, "ct0": ct, "label": (a.get("label") or "").strip()})
    if not accs:
        at, ct = (tw.get("auth_token") or "").strip(), (tw.get("ct0") or "").strip()
        if at and ct:
            accs.append({"auth_token": at, "ct0": ct, "label": ""})
    return accs


def pick_account(cfg: dict) -> dict | None:
    """按轮换规则选账号：优先用没在冷却里的，全在冷却则选最早恢复的。"""
    accs = x_accounts(cfg)
    if not accs:
        return None
    now = time.time()
    cd = MONITOR_STATE.setdefault("x_account_cooldown", {})
    cur = MONITOR_STATE.get("x_account_idx", 0)
    for off in range(len(accs)):
        i = (cur + off) % len(accs)
        if cd.get(str(i), 0) <= now:
            MONITOR_STATE["x_account_idx"] = i
            return accs[i]
    i = min(range(len(accs)), key=lambda j: cd.get(str(j), 0))
    MONITOR_STATE["x_account_idx"] = i
    return accs[i]


def cool_current_account(seconds: float) -> None:
    """把当前使用的账号冷却一段时间（限流/失效时调用，期间轮换跳过它）。"""
    idx = MONITOR_STATE.get("x_account_idx")
    if idx is not None:
        MONITOR_STATE.setdefault("x_account_cooldown", {})[str(idx)] = time.time() + seconds


def validate_config(cfg: dict) -> list[str]:
    tw = cfg.get("twitter", {})
    handles = [h.strip().lstrip("@") for h in (tw.get("monitor") or []) if h.strip()]
    if not handles:
        raise RuntimeError("config.json: twitter.monitor 未配置要监控的用户名。")

    has_accounts = bool(x_accounts(cfg))
    has_password = tw.get("username", "").strip() and tw.get("password", "").strip()
    if not (has_accounts or has_password or COOKIES_PATH.exists()):
        raise RuntimeError(
            "config.json: 缺少 X 账号认证。推荐在 twitter.accounts 里填账号 cookie"
            "（支持多账号轮换，面板可编辑），或填 username + password 用密码登录，见 README.md。"
        )

    tg = cfg.get("telegram") or {}
    tg_ok = bool(tg.get("bot_token") and str(tg.get("chat_id", "")).strip())
    em = cfg.get("email") or {}
    em_ok = bool(em.get("smtp_host") and em.get("username") and em.get("password"))
    sc = cfg.get("serverchan") or {}
    sc_ok = bool(sc.get("sendkey", "").strip())
    if not (tg_ok or em_ok or sc_ok):
        raise RuntimeError(
            "config.json: 至少配置一个通知渠道：telegram / email / serverchan，"
            "可以多个都配，见 README.md。"
        )
    return handles


def load_state() -> dict:
    if STATE_PATH.exists():
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(state: dict) -> None:
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def split_message(text: str, limit: int = TELEGRAM_MSG_LIMIT) -> list[str]:
    chunks = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n")
    chunks.append(text)
    return chunks


def make_http(cfg: dict) -> httpx.AsyncClient:
    return httpx.AsyncClient(proxy=cfg.get("proxy") or None)


async def send_telegram(http: httpx.AsyncClient, bot_token: str, chat_id: str, text: str) -> bool:
    chunks = split_message(text)
    for i, chunk in enumerate(chunks):
        resp = await http.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": chunk,
                "parse_mode": "HTML",
                "disable_web_page_preview": False,
            },
            timeout=30,
        )
        data = resp.json()
        if not data.get("ok"):
            log.error("Telegram 发送失败: %s", data)
            return False
        if i < len(chunks) - 1:
            await asyncio.sleep(1.5)  # 同一 chat 连续发消息太快会被 Telegram 限流
    return True


def email_from_name(em: dict) -> str:
    """邮件发件人显示名，config email.from_name 可自定义。"""
    return (em.get("from_name") or "").strip() or "Twitter monitoring"


def send_email_sync(em: dict, subject: str, body: str, retries: int = 2) -> None:
    """发送邮件。QQ/163 等邮箱在连续发信时会中途断连，失败自动重连重试。"""
    last_err: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            msg = MIMEText(body, "plain", "utf-8")
            msg["Subject"] = Header(subject, "utf-8")
            msg["From"] = formataddr((email_from_name(em), em["username"]))
            msg["To"] = em.get("to", "").strip() or em["username"]
            port = int(em.get("smtp_port", 465))
            if port == 465:
                server = smtplib.SMTP_SSL(em["smtp_host"], port, timeout=30)
            else:
                server = smtplib.SMTP(em["smtp_host"], port, timeout=30)
                server.starttls()
            try:
                server.login(em["username"], em["password"])
                server.sendmail(em["username"], [msg["To"]], msg.as_string())
            finally:
                try:
                    server.quit()
                except Exception:
                    pass
            return
        except Exception as e:
            last_err = e
            log.warning("邮件发送失败（第 %d/%d 次）: %r", attempt, retries, e)
            if attempt < retries:
                time.sleep(2)  # 重连前稍等，给 SMTP 服务器缓口气
    raise last_err  # type: ignore[misc]


async def send_serverchan(sendkey: str, title: str, desp: str) -> bool:
    """Server酱是国内服务，直连即可，不走代理。"""
    async with httpx.AsyncClient() as direct:
        resp = await direct.post(
            f"https://sctapi.ftqq.com/{sendkey.strip()}.send",
            data={"title": title, "desp": desp},
            timeout=30,
        )
    data = resp.json()
    if data.get("code") != 0:
        log.error("Server酱推送失败: %s", data)
    return data.get("code") == 0


async def deliver(cfg: dict, http: httpx.AsyncClient, parts: dict) -> dict[str, bool]:
    """把一条推文推送到所有已配置的渠道，返回各渠道是否成功。"""
    results: dict[str, bool] = {}

    tg = cfg.get("telegram") or {}
    if tg.get("bot_token") and str(tg.get("chat_id", "")).strip():
        try:
            results["telegram"] = await send_telegram(
                http, tg["bot_token"], str(tg["chat_id"]), format_telegram(parts))
        except Exception as e:
            log.error("Telegram 推送出错: %r", e)
            results["telegram"] = False

    em = cfg.get("email") or {}
    if em.get("smtp_host") and em.get("username") and em.get("password"):
        subject, body = format_email(parts, email_from_name(em))
        try:
            await asyncio.to_thread(send_email_sync, em, subject, body)
            results["email"] = True
        except Exception as e:
            log.error("邮件推送出错: %r", e)
            results["email"] = False

    sc = cfg.get("serverchan") or {}
    if sc.get("sendkey", "").strip():
        title, desp = format_serverchan(parts)
        try:
            results["serverchan"] = await send_serverchan(sc["sendkey"], title, desp)
        except Exception as e:
            log.error("Server酱推送出错: %r", e)
            results["serverchan"] = False

    return results


async def deliver_digest(cfg: dict, http: httpx.AsyncClient, title: str,
                         html_body: str, plain_body: str, md_body: str) -> dict[str, bool]:
    """把日报这类自定义内容推送到所有已配置渠道，返回各渠道是否成功。"""
    results: dict[str, bool] = {}

    tg = cfg.get("telegram") or {}
    if tg.get("bot_token") and str(tg.get("chat_id", "")).strip():
        try:
            results["telegram"] = await send_telegram(
                http, tg["bot_token"], str(tg["chat_id"]), html_body)
        except Exception as e:
            log.error("Telegram 推送出错: %r", e)
            results["telegram"] = False

    em = cfg.get("email") or {}
    if em.get("smtp_host") and em.get("username") and em.get("password"):
        try:
            await asyncio.to_thread(send_email_sync, em, title, plain_body)
            results["email"] = True
        except Exception as e:
            log.error("邮件推送出错: %r", e)
            results["email"] = False

    sc = cfg.get("serverchan") or {}
    if sc.get("sendkey", "").strip():
        try:
            results["serverchan"] = await send_serverchan(sc["sendkey"], title, md_body)
        except Exception as e:
            log.error("Server酱推送出错: %r", e)
            results["serverchan"] = False

    return results


def convert_time_refs(text: str, created_utc: datetime | None) -> list[str]:
    """找出推文里带时区的时刻（如 'tomorrow 10am PST'），换算成北京时间。
    只处理明确带时区标签的表述，避免把 '12h cache' 这类时长误当时刻。"""
    if created_utc is None:
        return []
    results, seen = [], set()
    for m in TIME_REF_RE.finditer(text):
        if m.group("noon"):
            hour, minute = 12, 0
        elif m.group("mid"):
            hour, minute = 0, 0
        else:
            hour, minute = int(m.group("hour")), int(m.group("minute") or 0)
            ampm = (m.group("ampm") or "").replace(".", "").lower()
            if ampm:
                if not 1 <= hour <= 12:
                    continue
                if ampm == "pm" and hour != 12:
                    hour += 12
                elif ampm == "am" and hour == 12:
                    hour = 0
            elif not 0 <= hour <= 23:
                continue
        tz_name = TZ_MAP[m.group("tz").upper()]
        src_tz = zoneinfo.ZoneInfo(tz_name)
        # 相对方位词（tomorrow/today）可在时间词前面或紧跟其后，以发帖时他在该时区的日期为准
        context = text[max(0, m.start() - 40):m.start()].lower()
        after = text[m.end():m.end() + 12].lower()
        rel = re.search(r"\b(tomorrow|tmrw|tonight|today)\b", context) \
            or re.match(r"\s*,?\s*(tomorrow|tmrw|tonight|today)\b", after)
        days = 1 if rel and rel.group(1) in ("tomorrow", "tmrw") else 0
        src_dt = (created_utc.astimezone(src_tz) + timedelta(days=days)).replace(
            hour=hour, minute=minute, second=0, microsecond=0)
        bj = src_dt.astimezone(BJT)
        src_expr = (f"{rel.group(1)} " if rel else "") + m.group(0).strip()
        line = f"{src_expr} → 北京时间 {bj:%m-%d %H:%M}（周{WEEKDAY_CN[bj.weekday()]}）"
        if line not in seen:
            seen.add(line)
            results.append(line)
        if len(results) >= 4:  # 防止奇怪的推文刷屏
            break
    return results


def tweet_parts(handle: str, tweet, official: bool = False) -> dict:
    created, created_dt = "", None
    if tweet.created_at:
        # 形如 'Wed Oct 10 20:19:24 +0000 2018'
        try:
            created_dt = datetime.strptime(tweet.created_at, "%a %b %d %H:%M:%S %z %Y")
            created = created_dt.astimezone(BJT).strftime("%Y-%m-%d %H:%M")
        except ValueError:
            created = str(tweet.created_at)
    text = clean_tweet_text(tweet)
    tag = (OFFICIAL_TAG if official else "") + (RESET_TAG if is_reset_like(text) else "")
    return {
        "handle": handle,
        "text": text,
        "time_conv": convert_time_refs(text, created_dt),
        "media": tweet_media_note(tweet),
        "tag": tag,
        "created": created,
        "url": f"https://x.com/{handle}/status/{tweet.id}",
    }


def _tweet_entities(tweet) -> dict:
    """合并普通推文(legacy)和长推文(note tweet)两处可能存放的 entities。"""
    ents: dict[str, list] = {"urls": [], "media": []}
    legacy = getattr(tweet, "_legacy", None) or {}
    raw = legacy.get("entities") or {}
    for key in ents:
        for item in raw.get(key) or []:
            ents[key].append(item)
    try:
        ntr = tweet._note_tweet_results  # 长推文的 entities 在这里
    except Exception:
        ntr = None
    if ntr:
        es = (ntr.get("result") or {}).get("entity_set") or {}
        for key in ents:
            for item in es.get(key) or []:
                if item not in ents[key]:
                    ents[key].append(item)
    return ents


def clean_tweet_text(tweet) -> str:
    """清洗推文原文: 去掉 t.co 短链和媒体占位链接（还原真实链接）、合并多余空行。"""
    text = tweet.text or ""
    # X 接口返回的文本自带 HTML 转义（& → &amp;），先还原，避免各渠道二次转义
    text = html.unescape(text)
    ents = _tweet_entities(tweet)

    # 媒体占位链接（t.co → pic.twitter.com 的那种）直接删掉，媒体数量单独一行展示
    for m in ents["media"]:
        u = m.get("url")
        if u and u in text:
            text = text.replace(u, " ")

    # 其余 t.co 短链还原为真实链接
    for u in ents["urls"]:
        short, expanded = u.get("url"), u.get("expanded_url") or u.get("display_url") or u.get("url")
        if short and expanded and short in text:
            text = text.replace(short, expanded)

    # 兜底: 清掉残余的 t.co / pic.twitter.com 裸链接
    text = re.sub(r"https?://(t\.co|pic\.twitter\.com)/\w+", "", text)

    # 整理空白: 去行尾空格、合并连续空行
    lines = [line.rstrip() for line in text.splitlines()]
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()

    if text.startswith("RT @"):
        text = "🔁 转推 " + text[4:]
    return text or "(无文字内容)"


def tweet_media_note(tweet) -> str:
    """统计推文里的媒体数量，生成如 '📎 2 张图片 + 1 个视频' 的说明行。"""
    seen, types = set(), []
    for m in _tweet_entities(tweet)["media"]:
        key = m.get("media_key") or m.get("url") or id(m)
        if key in seen:
            continue
        seen.add(key)
        types.append(m.get("type"))
    photos = types.count("photo")
    gifs = types.count("animated_gif")
    videos = types.count("video")
    parts = []
    if photos:
        parts.append(f"{photos} 张图片")
    if gifs:
        parts.append(f"{gifs} 个 GIF")
    if videos:
        parts.append(f"{videos} 个视频")
    return "📎 " + " + ".join(parts) if parts else ""


BING_TK = {"ig": None, "key": None, "token": None, "base": None, "exp": 0.0}


async def _bing_translate(text: str) -> str:
    """Bing 网页翻译接口，国内直连免费无 key。token 失效自动重取一次。"""
    now = time.time()
    async with httpx.AsyncClient(follow_redirects=True) as h:
        if not (BING_TK["token"] and now < BING_TK["exp"]):
            r = await h.get("https://www.bing.com/translator",
                            headers={"User-Agent": BROWSER_UA, "Accept-Language": "en"}, timeout=30)
            ig = re.search(r'IG:"([^"]+)"', r.text)
            params = re.search(r'params_AbusePreventionHelper\s*=\s*\[(\d+),"([^"]+)",(\d+)\]', r.text)
            if not (ig and params):
                raise RuntimeError("Bing token 提取失败")
            BING_TK.update(ig=ig.group(1), key=params.group(1), token=params.group(2),
                           base=f"{r.url.scheme}://{r.url.host}",
                           exp=min(int(params.group(3)) / 1000 - 60, now + 1800))
        r2 = await h.post(
            f'{BING_TK["base"]}/ttranslatev3?IG={BING_TK["ig"]}&IID=translator.503.4',
            data={"fromLang": "auto-detect", "text": text, "to": "zh-Hans",
                  "token": BING_TK["token"], "key": BING_TK["key"]},
            headers={"User-Agent": BROWSER_UA, "Referer": f'{BING_TK["base"]}/translator'},
            timeout=30,
        )
    if r2.status_code == 200 and r2.text.strip():
        BING_TK["exp"] = min(BING_TK["exp"], now + 1800)  # 正常应答，token 继续用
        return r2.json()[0]["translations"][0]["text"]
    BING_TK["exp"] = 0  # token 失效，下轮重取
    raise RuntimeError(f"Bing 翻译接口异常: HTTP {r2.status_code}")


async def translate_text(http: httpx.AsyncClient, text: str) -> str | None:
    """翻译成中文。主用 Bing（国内直连），备用 Google（走代理）。失败返回 None，不影响原文推送。"""
    text = (text or "").strip()
    if not text or len(text) > 8000:
        return None
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    if cjk > len(text) * 0.3:  # 本来就基本是中文，不用翻
        return None
    try:
        out = await _bing_translate(text)
        if out and out.strip():
            return out.strip()
    except Exception as e:
        log.warning("Bing 翻译失败，尝试 Google: %r", e)
    try:
        r = await http.get(
            "https://translate.googleapis.com/translate_a/single",
            params={"client": "gtx", "sl": "auto", "tl": "zh-CN", "dt": "t", "q": text},
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        out = "".join(seg[0] for seg in data[0] if seg and seg[0]).strip()
        return out or None
    except Exception as e:
        log.warning("翻译失败（该条推送将只含原文）: %r", e)
        return None


async def build_parts(handle: str, tweet, http: httpx.AsyncClient, display: str | None = None,
                      cfg: dict | None = None) -> dict:
    official_list = ((cfg or {}).get("twitter") or {}).get("official") or []
    official = handle.lower() in (h.lower() for h in official_list)
    parts = tweet_parts(handle, tweet, official=official)
    if display and display != handle:
        parts["display"] = f"{display} (@{handle})"
    else:
        parts["display"] = f"@{handle}"
    translation = await translate_text(http, parts["text"])
    parts["translation"] = translation if translation and translation != parts["text"] else None
    return parts


def format_telegram(p: dict) -> str:
    lines = [
        f"🔔 <b>{html.escape(p['display'])}</b>{p.get('tag', '')} 发布了新推文",
        "",
        html.escape(p["text"]),
    ]
    for conv in p.get("time_conv") or []:
        lines += ["", "⏰ " + html.escape(conv)]
    if p.get("translation"):
        lines += ["", "🌐 <b>译文</b>", html.escape(p["translation"])]
    if p["media"]:
        lines += ["", html.escape(p["media"])]
    lines += ["", f"🕐 {p['created']}", f'🔗 <a href="{p["url"]}">查看原文</a>']
    return "\n".join(lines)


def format_email(p: dict, from_name: str = "Twitter monitoring") -> tuple[str, str]:
    # 有译文时标题用中文，收件箱里一眼能看懂；情报邮件用 📡 区分于普通推文推送
    snippet = (p.get("translation") or p["text"])[:40].replace("\n", " ")
    icon = "📡" if p.get("tag") == INTEL_TAG else "🔔"
    subject = f"{icon} {p['display']}{p.get('tag', '')} 新情报: {snippet}"
    lines = [
        f"{p['display']}{p.get('tag', '')} 发布了新推文",
        "────────────────────",
        "",
        p["text"],
    ]
    for conv in p.get("time_conv") or []:
        lines += ["", "⏰ " + conv]
    if p.get("translation"):
        lines += ["", "🌐 译文:", "", p["translation"]]
    if p["media"]:
        lines += ["", p["media"]]
    lines += ["", f"🕐 {p['created']}", f"🔗 原文链接: {p['url']}", "", f"-- {from_name}"]
    return subject, "\n".join(lines)


def format_serverchan(p: dict) -> tuple[str, str]:
    title = f"🔔 {p['display']}{p.get('tag', '')} 新推文: {p['text'][:24]}"
    lines = [f"**{p['display']}{p.get('tag', '')}** 发布了新推文", "", p["text"]]
    for conv in p.get("time_conv") or []:
        lines += ["", "⏰ " + conv]
    if p.get("translation"):
        lines += ["", "**🌐 译文**", "", p["translation"]]
    if p["media"]:
        lines += ["", p["media"]]
    lines += ["", f"🕐 {p['created']}", f"[查看原文]({p['url']})"]
    return title, "\n".join(lines)


async def ensure_client(cfg: dict, probe_handle: str) -> Client:
    """建立一个凭据可用的 X 客户端。

    配了账号（含旧的单账号 cookie）时按轮换规则选号：限流/失效的自动跳过，
    会话按账号各存一份（cookies.{N}.json）；没配账号时走旧的
    cookies.json / 密码登录路径。
    """
    client = Client("en-US", proxy=cfg.get("proxy") or None)
    tw_cfg = cfg["twitter"]
    account = pick_account(cfg)
    label = ""
    session_path = COOKIES_PATH
    if account:
        idx = MONITOR_STATE.get("x_account_idx", 0)
        label = account["label"] or f"#{idx + 1}"
        session_path = BASE_DIR / f"cookies.{idx}.json"
        client.set_cookies({"auth_token": account["auth_token"], "ct0": account["ct0"]})
        if session_path.exists():
            try:
                client.load_cookies(str(session_path))
            except Exception as e:
                log.warning("账号 %s 会话文件加载失败，用配置里的 cookie 重试: %s", label, e)
    elif COOKIES_PATH.exists():
        try:
            client.load_cookies(str(COOKIES_PATH))
        except Exception as e:
            log.warning("本地 cookies 加载失败，将尝试其他认证方式: %s", e)

    # 用一次真实请求验证凭据
    try:
        await client.get_user_by_screen_name(probe_handle)
    except TooManyRequests:
        log.warning("X 接口限流中（账号 %s）", label or "默认")
        raise
    except Unauthorized:
        if account:
            log.warning("X 账号 %s 的 cookie 已失效，冷却 1 小时后轮换", label)
            cool_current_account(3600)
            raise
        log.info("当前凭据无效/过期，尝试重新认证 ...")
        username = tw_cfg.get("username", "").strip()
        password = tw_cfg.get("password", "").strip()
        if not (username and password):
            sys.exit(
                "X 认证失败且无法重新登录: config 里的 cookie 已失效，"
                "也没有可用的 username + password。请更新账号 cookie 或填写账号密码，见 README.md。"
            )
        await client.login(
            auth_info_1=username,
            auth_info_2=tw_cfg.get("email", "").strip() or None,
            password=password,
            totp_secret=tw_cfg.get("totp_secret", "").strip() or None,
            cookies_file=str(COOKIES_PATH),
        )
        client.save_cookies(str(COOKIES_PATH))
        log.info("密码登录成功，cookies 已保存到 %s", COOKIES_PATH.name)
        return client
    client.save_cookies(str(session_path))
    log.info("X 凭据验证通过（账号 %s）", label or "默认")
    return client


async def poll_once(client: Client, http: httpx.AsyncClient, cfg: dict, state: dict) -> int:
    keywords = [k.strip() for k in cfg.get("keywords", []) if str(k).strip()]
    kw_regex = [re.compile(k[2:].lstrip(), re.I) for k in keywords if k.startswith("re:")]
    kw_subs = [k.lower() for k in keywords if not k.startswith("re:")]
    last_seen = state.setdefault("last_seen", {})
    user_ids = state.setdefault("user_ids", {})
    display_names = state.setdefault("display_names", {})
    pushed = 0

    for handle in [h.strip().lstrip("@") for h in cfg["twitter"]["monitor"] if h.strip()]:
        if handle not in display_names:
            user = await client.get_user_by_screen_name(handle)
            display_names[handle] = user.name or handle
            user_ids[handle] = user.id
        user_id = user_ids.get(handle)
        if not user_id:
            user = await client.get_user_by_screen_name(handle)
            user_id = user.id
            user_ids[handle] = user_id

        tweets = await client.get_user_tweets(user_id, "Tweets")
        if not tweets:
            log.info("[%s] 本轮没有取到推文", handle)
            continue

        last = int(last_seen.get(handle, 0))
        if last == 0:
            last_seen[handle] = int(tweets[0].id)
            log.info("[%s] 首次运行，记录基准推文 ID=%s（不推送历史推文）", handle, tweets[0].id)
            record_push(handle, display_names.get(handle), status="baseline",
                        text=(tweets[0].text or "")[:500], tweet_id=str(tweets[0].id),
                        url=f"https://x.com/{handle}/status/{tweets[0].id}",
                        tweet_created=tweets[0].created_at or "")
            continue

        fresh = sorted((t for t in tweets if int(t.id) > last), key=lambda t: int(t.id))
        if not fresh:
            continue

        new_last = last
        for t in fresh:
            if tweet_seen(t.id):
                log.info("[%s] 推文 %s 已在情报搜索中推送过，跳过", handle, t.id)
                new_last = max(new_last, int(t.id))
                continue
            log.info("[%s] 发现新推文 %s: %s", handle, t.id, (t.text or "")[:60].replace("\n", " "))
            if keywords:
                hit = any(rx.search(t.text or "") for rx in kw_regex) \
                    or any(s in (t.text or "").lower() for s in kw_subs)
                if not hit:
                    log.info("[%s] 推文 %s 未命中关键词，跳过推送", handle, t.id)
                    new_last = max(new_last, int(t.id))
                    record_push(handle, display_names.get(handle), status="filtered",
                                text=(t.text or "")[:500], tweet_id=str(t.id),
                                url=f"https://x.com/{handle}/status/{t.id}",
                                tweet_created=t.created_at or "")
                    continue
            parts = await build_parts(handle, t, http, display_names.get(handle), cfg)
            results = await deliver(cfg, http, parts)
            if not any(results.values()):
                log.warning("[%s] 推文 %s 所有渠道推送失败，保留该推文下轮重试", handle, t.id)
                record_push(handle, parts["display"], status="failed", text=parts["text"],
                            tag=parts.get("tag", ""), tweet_id=str(t.id), url=parts["url"],
                            tweet_created=parts["created"], translation=parts.get("translation") or "",
                            time_conv=parts.get("time_conv") or [], results=results)
                break
            for ch, ok in results.items():
                if not ok:
                    log.warning("[%s] 推文 %s 渠道 %s 推送失败（其他渠道已送达，该条不再重试）", handle, t.id, ch)
            record_push(handle, parts["display"],
                        status="success" if all(results.values()) else "partial",
                        text=parts["text"], tag=parts.get("tag", ""), tweet_id=str(t.id),
                        url=parts["url"], tweet_created=parts["created"],
                        translation=parts.get("translation") or "",
                        time_conv=parts.get("time_conv") or [], results=results)
            new_last = max(new_last, int(t.id))
            pushed += 1
            await asyncio.sleep(1.5)

        last_seen[handle] = new_last
        log.info("[%s] 本轮新推文 %d 条，成功推送 %d 条", handle, len(fresh), pushed)

    save_state(state)
    return pushed



async def search_intel(client: Client, http: httpx.AsyncClient, cfg: dict,
                       state: dict, force: bool = False) -> dict:
    """X 全网搜索免费情报：命中配置语句的新推文，翻译后立即推送并入库。

    返回 {found, pushed[, note]}。首轮只建基准不推送（历史推文不刷屏，日报会汇总）。
    """
    ic = intel_cfg(cfg)
    if not ic["enabled"] and not force:
        return {"found": 0, "pushed": 0, "note": "免费情报未启用"}
    istate = state.setdefault("intel", {})
    first_round = not istate.get("initialized")
    match_re = re.compile(ic["must_match"], re.I) if ic["must_match"] else None
    exclude_re = re.compile(ic["exclude_match"], re.I) if ic["exclude_match"] else None
    found = pushed = filtered = 0
    healed = False        # 404 自动修复每轮最多尝试一次
    any_success = False   # 至少一条语句搜索成功，首轮才建基准
    rate_limited = False
    for query in ic["queries"]:
        tweets = None
        for attempt in (1, 2):
            try:
                tweets = await client.search_tweet(query, "Latest")
                any_success = True
                break
            except TooManyRequests:
                rate_limited = True
                log.warning("[情报] X 搜索限流，本轮剩余语句跳过")
                break
            except NotFound:
                if attempt == 1 and not healed:
                    healed = True
                    log.info("[情报] 搜索接口 404，尝试自动修复（更新 queryId + 重建事务 ID）...")
                    await refresh_search_qid(http)
                    # 置空让 twikit 在下个请求里重新初始化事务 ID 模块
                    client.client_transaction.home_page_response = None
                    continue
                log.warning("[情报] 搜索接口 404（X 可能改版且自动修复未成功），跳过该语句")
                break
            except Exception as e:
                log.warning("[情报] 搜索语句失败，跳过（%s...）: %r", query[:40], e)
                break
        if tweets is None:
            continue
        for t in tweets or []:
            tid = str(t.id)
            if tweet_seen(tid):
                continue
            user = getattr(t, "user", None)
            handle = (user.screen_name if user else None) or "unknown"
            display = (user.name if user else None) or None
            url = f"https://x.com/{handle}/status/{tid}"
            text = clean_tweet_text(t)
            # X 搜索有语义扩展，本地再按关键词过滤一遍，防止垃圾推文打扰
            if (exclude_re and exclude_re.search(text)) \
                    or (match_re and not match_re.search(text)):
                filtered += 1
                record_push(handle, display, status="filtered", text=text[:500],
                            tweet_id=tid, url=url, tweet_created=t.created_at or "",
                            source="intel_search")
                continue
            found += 1
            if first_round:
                record_push(handle, display, status="baseline", text=text[:500],
                            tweet_id=tid, url=url, tweet_created=t.created_at or "",
                            source="intel_search")
                continue
            parts = await build_parts(handle, t, http, display)
            parts["tag"] = INTEL_TAG
            results = await deliver(cfg, http, parts)
            status = "success" if all(results.values()) \
                else ("partial" if any(results.values()) else "failed")
            record_push(handle, parts["display"], status=status, text=parts["text"],
                        tag=INTEL_TAG, tweet_id=tid, url=parts["url"],
                        tweet_created=parts["created"],
                        translation=parts.get("translation") or "",
                        time_conv=parts.get("time_conv") or [], results=results,
                        source="intel_search")
            if status == "failed":
                log.warning("[情报] 推文 %s 全渠道推送失败，日报仍会收录", tid)
            else:
                pushed += 1
            await asyncio.sleep(3.0)  # 连续发邮件太快会被 SMTP 服务器断连
    if first_round:
        if any_success:
            istate["initialized"] = True
            log.info("[情报] 首轮搜索完成：建基准 %d 条，过滤 %d 条（不推送，日报会汇总）", found, filtered)
        else:
            log.warning("[情报] 首轮搜索没有取得任何结果（接口异常/限流），下轮重新建基准")
    else:
        log.info("[情报] 本轮搜索：新发现 %d 条（过滤 %d 条），成功推送 %d 条", found, filtered, pushed)
    if not rate_limited:
        MONITOR_STATE["intel"]["last_search"] = datetime.now(BJT).strftime("%Y-%m-%d %H:%M:%S")
    save_state(state)
    return {"found": found, "pushed": pushed}


def intel_digest_window(state: dict) -> str:
    """日报覆盖起点：上一次日报发送时刻；从未发过则取 24 小时前。"""
    last = (state.get("intel") or {}).get("last_digest_at") or ""
    if last:
        return last
    return (datetime.now(BJT) - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")


def intel_rows_since(since: str) -> list[dict]:
    """日报收录范围：时间窗内的情报 + 全部未覆盖的基准行（首轮历史情报只有日报这条送达路径）。"""
    return store.get().intel_rows_since(since)


def mark_baselines_covered() -> None:
    """日报送达后，把基准行标记为已覆盖，避免后续日报重复带上它们。"""
    store.get().mark_baselines_covered()


def format_digest_bodies(rows: list[dict], signature: str = "Twitter monitoring") -> tuple[str, str, str, str]:
    """生成日报的 (标题, Telegram HTML, 邮箱纯文本, Server酱 Markdown)。"""
    now = datetime.now(BJT)
    title = f"📡 Twitter monitoring · 免费情报日报 {now:%Y-%m-%d}（{len(rows)} 条）"
    if not rows:
        plain = f"过去一段时间没有搜到新的免费情报。\n\n-- {signature}"
        return title, html.escape(plain), plain, plain
    lines_plain, lines_html, lines_md = [], [], []
    for i, r in enumerate(rows, 1):
        author = r.get("display") or (f"@{r['handle']}" if r.get("handle") else "未知账号")
        when = (r.get("pushed_at") or "")[:16]
        text = (r.get("text") or "").strip()
        if len(text) > 400:
            text = text[:400] + " …"
        tr = (r.get("translation") or "").strip()
        if len(tr) > 300:
            tr = tr[:300] + " …"
        link = r.get("url") or ""
        lines_plain += [f"{i}. {author} · {when}"]
        lines_html += [f"{i}. <b>{html.escape(author)}</b> · {html.escape(when)}"]
        lines_md += [f"{i}. **{author}** · {when}"]
        # 译文在前（中文优先），没有译文时只展示原文
        if tr:
            lines_plain += [f"🌐 {tr}", f"📄 {text}"]
            lines_html += [f"🌐 {html.escape(tr)}", f"📄 {html.escape(text)}"]
            lines_md += [f"**🌐 译文** {tr}", text]
        else:
            lines_plain += [text]
            lines_html += [html.escape(text)]
            lines_md += [text]
        if link:
            lines_plain.append(f"🔗 {link}")
            lines_html.append(f'🔗 <a href="{link}">查看原文</a>')
            lines_md.append(f"[查看原文]({link})")
        lines_plain.append("")
        lines_html.append("")
        lines_md.append("")
    head_plain = f"📡 Twitter monitoring · 免费情报日报 {now:%Y-%m-%d}\n共 {len(rows)} 条\n"
    head_html = f"📡 <b>Twitter monitoring · 免费情报日报 {now:%Y-%m-%d}</b>（共 {len(rows)} 条）\n"
    head_md = f"**📡 Twitter monitoring · 免费情报日报 {now:%Y-%m-%d}**（共 {len(rows)} 条）\n"
    plain = head_plain + "\n" + "\n".join(lines_plain).rstrip() + f"\n\n-- {signature}"
    html_body = head_html + "\n" + "\n".join(lines_html).rstrip()
    md_body = head_md + "\n" + "\n".join(lines_md).rstrip()
    return title, html_body, plain, md_body


async def fill_digest_translations(rows: list[dict], http: httpx.AsyncClient) -> None:
    """日报里缺译文的条目现场补翻译（译文写回数据库，之后不再重复翻）。"""
    updates = []
    for r in rows:
        text = (r.get("text") or "").strip()
        if not text or (r.get("translation") or "").strip():
            continue
        try:
            tr = await translate_text(http, text)
        except Exception as e:
            log.warning("[情报] 日报补翻译失败（%s...）: %r", text[:30], e)
            tr = None
        if tr:
            r["translation"] = tr
            updates.append((tr, str(r["tweet_id"])))
        await asyncio.sleep(0.4)  # 对翻译接口留点余量
    if updates:
        with sqlite3.connect(DB_PATH) as conn:
            conn.executemany("UPDATE push_records SET translation = ? WHERE tweet_id = ?",
                             updates)
        log.info("[情报] 日报补翻译 %d 条", len(updates))


async def build_intel_digest(cfg: dict, http: httpx.AsyncClient,
                             state: dict | None = None, force: bool = False) -> dict:
    """把最近一段时间的免费情报汇总成一条日报推送，并入库留档。"""
    ic = intel_cfg(cfg)
    if not ic["enabled"] and not force:
        return {"items": 0, "sent": False, "note": "免费情报未启用"}
    state = state if state is not None else load_state()
    rows = intel_rows_since(intel_digest_window(state))
    if rows:
        await fill_digest_translations(rows, http)
    em = cfg.get("email") or {}
    title, html_body, plain_body, md_body = format_digest_bodies(rows, email_from_name(em))
    results = await deliver_digest(cfg, http, title, html_body, plain_body, md_body)
    sent = any(results.values())
    now = datetime.now(BJT)
    summary = f"日报推送 {len(rows)} 条情报" if rows else "无新情报"
    record_push("", f"日报 {now:%m-%d}", status="success" if sent else "failed",
                text=summary, tweet_id=f"intel-digest-{now:%Y-%m-%d}",
                results=results, source="intel_digest")
    if sent:
        istate = state.setdefault("intel", {})
        istate["last_digest"] = now.strftime("%Y-%m-%d")
        istate["last_digest_at"] = now.strftime("%Y-%m-%d %H:%M:%S")
        save_state(state)
        mark_baselines_covered()
        MONITOR_STATE["intel"]["last_digest"] = istate["last_digest"]
        log.info("[情报] 日报已发送：%s", summary)
    else:
        log.warning("[情报] 日报所有渠道推送失败，稍后自动重试")
    return {"items": len(rows), "sent": sent}


def digest_due(cfg: dict, state: dict) -> bool:
    """今天到日报时间了吗（发过就不再发）。"""
    ic = intel_cfg(cfg)
    if not ic["enabled"]:
        return False
    try:
        hh, mm = (int(x) for x in ic["digest_time"].split(":")[:2])
    except (TypeError, ValueError):
        hh, mm = 9, 0
    now = datetime.now(BJT)
    if (now.hour, now.minute) < (hh, mm):
        return False
    return (state.get("intel") or {}).get("last_digest") != now.strftime("%Y-%m-%d")


async def intel_tasks(client: Client, http: httpx.AsyncClient, cfg: dict, state: dict) -> None:
    """主循环每轮末尾调用：到点的搜索 / 到点的日报。

    任何异常只记日志，不影响主监控循环。
    """
    ic = intel_cfg(cfg)
    if not ic["enabled"]:
        return
    try:
        now_ts = time.time()
        if now_ts - MONITOR_STATE["intel_last_search_ts"] >= ic["search_interval_minutes"] * 60:
            MONITOR_STATE["intel_last_search_ts"] = now_ts
            await search_intel(client, http, cfg, state)
            MONITOR_STATE["health"]["intel"] = {"ok": True, "at": _now_str(), "error": ""}
    except Exception as e:
        MONITOR_STATE["health"]["intel"] = {"ok": False, "at": _now_str(), "error": repr(e)[:200]}
        log.error("[情报] 搜索出错（不影响主监控）: %r", e)
    try:
        if digest_due(cfg, state) and time.time() >= MONITOR_STATE["intel_digest_retry_ts"]:
            r = await build_intel_digest(cfg, http, state)
            if not r.get("sent"):
                MONITOR_STATE["intel_digest_retry_ts"] = time.time() + 1800
    except Exception as e:
        MONITOR_STATE["intel_digest_retry_ts"] = time.time() + 1800
        log.error("[情报] 日报出错（不影响主监控）: %r", e)


async def _sleep_check(stop_event: asyncio.Event | None, seconds: float) -> None:
    """可被 stop_event 提前打断的休眠。"""
    deadline = time.time() + seconds
    while not (stop_event is not None and stop_event.is_set()):
        remain = deadline - time.time()
        if remain <= 0:
            return
        await asyncio.sleep(min(1, remain))


def _now_str() -> str:
    return datetime.now(BJT).strftime("%Y-%m-%d %H:%M:%S")


# 监控连续失败告警：连续多少轮失败后推送 + 两条告警的最小间隔
ALERT_THRESHOLD = 3
ALERT_COOLDOWN = 1800


async def send_system_alert(cfg: dict, http: httpx.AsyncClient, title: str, text: str) -> bool:
    """通过已配置的渠道推送系统告警/恢复通知，返回是否至少一个渠道成功。"""
    results = await deliver_digest(cfg, http, title, html.escape(text), text, text)
    return any(results.values())


async def run_forever(stop_event: asyncio.Event | None = None) -> None:
    """持续轮询。每轮重新读取 config.json，面板里改配置即时生效。"""
    MONITOR_STATE.update(running=True, last_error=None)
    client = None
    handles: list[str] = []
    backoff = 90
    cfg: dict = {}
    fail_streak = 0        # 主循环连续失败轮数
    last_alert_ts = 0.0    # 上次告警推送时间
    alerted = False        # 本轮失败周期内是否已告警过（恢复时据此决定是否报平安）
    log.info("监控循环启动（Ctrl+C 退出）")

    def _mark_main_fail(err: str) -> None:
        MONITOR_STATE["health"]["main"] = {"ok": False, "at": _now_str(), "error": err[:200]}

    async def _maybe_alert() -> None:
        """连续失败达到阈值时推送告警（带冷却，防止渠道被告警刷屏）。"""
        nonlocal last_alert_ts, alerted
        if fail_streak < ALERT_THRESHOLD or time.time() - last_alert_ts < ALERT_COOLDOWN:
            return
        last_alert_ts = time.time()
        alerted = True
        err = (MONITOR_STATE.get("health", {}).get("main") or {}).get("error", "")
        try:
            async with make_http(cfg) as http:
                await send_system_alert(
                    cfg, http,
                    f"⚠️ Twitter monitoring 连续失败 {fail_streak} 轮",
                    f"监控轮询连续 {fail_streak} 轮失败，程序仍在自动重试。\n"
                    f"最近错误：{err}\n"
                    "若长时间不恢复，请检查代理是否在线、X 账号 cookie 是否失效（面板→配置）。",
                )
            record_push("", "⚠️ 系统告警", status="success",
                        text=f"连续失败 {fail_streak} 轮：{err}",
                        tweet_id=f"alert-{int(time.time())}", source="monitor")
            log.warning("[告警] 已推送连续失败通知（%d 轮）", fail_streak)
        except Exception as e:
            log.error("[告警] 推送失败: %r", e)

    try:
        while not (stop_event is not None and stop_event.is_set()):
            try:
                cfg = load_config()
                handles = validate_config(cfg)
                interval = int(cfg.get("poll_interval_seconds", 90))
                if client is None:
                    client = await ensure_client(cfg, handles[0])
                # 每轮重读 state：面板手动搜索/日报写入的进度不会被内存旧值覆盖
                state = load_state()
                async with CHECK_LOCK:
                    async with make_http(cfg) as http:
                        await poll_once(client, http, cfg, state)
                        await intel_tasks(client, http, cfg, state)
                MONITOR_STATE["health"]["main"] = {"ok": True, "at": _now_str(), "error": ""}
                if alerted and fail_streak >= ALERT_THRESHOLD:
                    # 失败周期结束，报平安
                    alerted = False
                    try:
                        async with make_http(cfg) as http:
                            await send_system_alert(cfg, http, "✅ Twitter monitoring 监控已恢复",
                                                    "监控轮询恢复正常，之前连续失败的问题已解除。")
                        record_push("", "系统恢复", status="success", text="监控轮询恢复正常",
                                    tweet_id=f"alert-recover-{int(time.time())}", source="monitor")
                        log.info("[告警] 已推送恢复通知")
                    except Exception as e:
                        log.error("[告警] 恢复通知推送失败: %r", e)
                fail_streak = 0
                MONITOR_STATE.update(last_check=_now_str(), last_error=None)
                backoff = interval
                await _sleep_check(stop_event, interval)
            except TooManyRequests:
                fail_streak += 1
                _mark_main_fail("X 接口限流")
                await _maybe_alert()
                cool_current_account(RATE_LIMIT_WAIT)
                if len(x_accounts(cfg)) > 1:
                    client = None  # 下轮 ensure_client 自动换下一个账号
                    MONITOR_STATE["last_error"] = "X 账号限流，已轮换到其他账号"
                    log.warning("X 账号限流，冷却 %d 秒后轮换（共 %d 个账号）",
                                RATE_LIMIT_WAIT, len(x_accounts(cfg)))
                    await _sleep_check(stop_event, 60)
                else:
                    MONITOR_STATE["last_error"] = "X 接口限流中，稍后自动重试"
                    log.warning("触发 X 限流，%d 秒后重试", RATE_LIMIT_WAIT)
                    await _sleep_check(stop_event, RATE_LIMIT_WAIT)
            except Unauthorized:
                fail_streak += 1
                _mark_main_fail("X 登录态失效")
                await _maybe_alert()
                cool_current_account(3600)
                if len(x_accounts(cfg)) > 1:
                    client = None  # 下轮 ensure_client 自动换下一个账号
                    MONITOR_STATE["last_error"] = "X 登录态失效，已轮换到其他账号"
                    log.warning("X 登录态失效，轮换账号重试（共 %d 个账号）", len(x_accounts(cfg)))
                    await _sleep_check(stop_event, 60)
                else:
                    MONITOR_STATE["last_error"] = "X 登录态失效，尝试重新认证"
                    log.warning("X 登录态失效，尝试重新认证 ...")
                    try:
                        client = await ensure_client(load_config(), handles[0] if handles else "x")
                    except Exception as e:
                        MONITOR_STATE["last_error"] = f"重新认证失败: {e!r}"
                        log.error("重新认证失败: %r", e)
                    await _sleep_check(stop_event, backoff)
                    backoff = min(backoff * 2, MAX_BACKOFF)
            except Exception as e:
                fail_streak += 1
                _mark_main_fail(repr(e))
                await _maybe_alert()
                MONITOR_STATE["last_error"] = repr(e)
                log.error("轮询出错: %r，%d 秒后重试", e, backoff)
                await _sleep_check(stop_event, backoff)
                backoff = min(backoff * 2, MAX_BACKOFF)
    finally:
        MONITOR_STATE["running"] = False
        log.info("监控循环已停止")


async def test_channels(cfg: dict) -> dict[str, str]:
    results: dict[str, str] = {}

    tg = cfg.get("telegram") or {}
    if tg.get("bot_token") and str(tg.get("chat_id", "")).strip():
        try:
            async with make_http(cfg) as http:
                ok = await send_telegram(http, tg["bot_token"], str(tg["chat_id"]),
                                         "📡 Twitter monitoring 测试消息：Telegram 通道正常。")
            results["telegram"] = "✅ 已发送，请查看。" if ok else "❌ 发送失败，检查 bot_token / chat_id。"
        except Exception as e:
            results["telegram"] = f"❌ 发送失败: {e!r}"

    em = cfg.get("email") or {}
    if em.get("smtp_host") and em.get("username") and em.get("password"):
        name = email_from_name(em)
        try:
            await asyncio.to_thread(
                send_email_sync, em, f"✅ {name} 测试邮件",
                f"{name} 邮件通道正常，之后新推文和免费情报会发到这个邮箱。\n\n-- {name}")
            results["email"] = "✅ 已发送，请查收（注意垃圾箱）。"
        except Exception as e:
            results["email"] = f"❌ 发送失败: {e!r}"

    sc = cfg.get("serverchan") or {}
    if sc.get("sendkey", "").strip():
        try:
            async with httpx.AsyncClient() as direct:
                resp = await direct.post(
                    f"https://sctapi.ftqq.com/{sc['sendkey'].strip()}.send",
                    data={"title": "📡 Twitter monitoring 测试", "desp": "Server酱通道正常，之后新推文会推到这里。"},
                    timeout=30,
                )
            data = resp.json()
            results["serverchan"] = ("✅ 已发送，请在微信里查看（服务号「方糖」）。"
                                     if data.get("code") == 0 else f"❌ 发送失败: {data}")
        except Exception as e:
            results["serverchan"] = f"❌ 发送失败: {e!r}"

    if not results:
        results["none"] = "未配置任何通知渠道（telegram / email / serverchan）"
    return results


async def main() -> None:
    parser = argparse.ArgumentParser(description="监控 X 用户新推文并推送到 Telegram")
    parser.add_argument("--test", action="store_true", help="发送一条测试消息验证 Telegram 配置后退出")
    parser.add_argument("--once", action="store_true", help="只轮询一次后退出（适合配合计划任务）")
    parser.add_argument("--search", action="store_true", help="手动执行一轮免费情报搜索后退出")
    parser.add_argument("--digest", action="store_true", help="手动生成并发送一条免费情报日报后退出")
    parser.add_argument("--interval", type=int, help="覆盖 config.json 里的 poll_interval_seconds")
    args = parser.parse_args()

    setup_logging()
    init_db()
    cfg = load_config()
    try:
        handles = validate_config(cfg)
    except RuntimeError as e:
        sys.exit(str(e))
    interval = args.interval or int(cfg.get("poll_interval_seconds", 90))

    if args.test:
        for ch, msg in (await test_channels(cfg)).items():
            print(f"{ch}: {msg}")
        return
    if args.once:
        state = load_state()
        client = await ensure_client(cfg, handles[0])
        async with make_http(cfg) as http:
            n = await poll_once(client, http, cfg, state)
        log.info("单次轮询完成，推送 %d 条", n)
        return
    if args.search:
        state = load_state()
        client = await ensure_client(cfg, handles[0])
        async with make_http(cfg) as http:
            r = await search_intel(client, http, cfg, state, force=True)
        log.info("情报搜索完成：新发现 %d 条，推送 %d 条", r["found"], r["pushed"])
        return
    if args.digest:
        async with make_http(cfg) as http:
            r = await build_intel_digest(cfg, http, force=True)
        log.info("情报日报完成：%s", r)
        return
    await run_forever()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n已退出。")
