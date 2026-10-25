# -*- coding: utf-8 -*-
"""X (Twitter) 接口适配层。

X 前端频繁改版，这里集中处理 twikit 与现网接口之间的所有兼容问题，
X 再改版时优先只改这个文件：

1. x-client-transaction-id 指纹头：twikit 2.3.3 内置算法依赖旧的 ondemand.s
   引用方式（已变），改用上游 XClientTransaction(>=1.0.3) 的算法从首页
   webpack manifest 解析。搜索接口（SearchTimeline）强制校验该头，
   占位值会 404，必须生成真实 ID；
2. SearchTimeline GraphQL queryId：X 改版会换掉 queryId，twikit 内置的
   已 404。这里启动时替换为从现网前端提取的 ID，404 时可自动重新提取；
3. 用户对象新格式：X 已把搜索结果里的用户对象切换为无 legacy 字段的
   新结构（core/avatar/profile_bio...），这里合成 twikit 需要的旧结构。
"""

import json
import logging
import re
from pathlib import Path

import httpx

log = logging.getLogger("monitor")

BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# 2026-10 从 x.com 前端 main bundle 提取；若再次改版，refresh_search_qid 会自动更新
SEARCH_QID = "f7SuGIm11eLbQs7c0K9X9w"


def apply_search_qid(qid: str) -> None:
    """把 SearchTimeline 的 GraphQL 端点替换为指定 queryId。"""
    from twikit.client.gql import Endpoint
    Endpoint.SEARCH_TIMELINE = f"https://x.com/i/api/graphql/{qid}/SearchTimeline"


def _install_patches() -> None:
    """给 twikit 打上全部运行时补丁（导入本模块即生效）。"""
    from twikit.user import User
    from twikit.x_client_transaction.transaction import ClientTransaction

    orig_user_init = User.__init__

    def user_init(self, client, data):  # noqa: ANN001
        data.setdefault("is_blue_verified", False)
        if "legacy" not in data:
            # X 新版用户对象（搜索接口已切换）：没有 legacy 字典，字段挪到了
            # core/avatar/profile_bio 等新位置。从新字段合成 twikit 需要的 legacy，
            # 监控实际只用到 name/screen_name，其余给安全默认值。
            core = data.get("core") or {}
            data["legacy"] = {
                "name": core.get("name", ""),
                "screen_name": core.get("screen_name", ""),
                "created_at": core.get("created_at", ""),
                "profile_image_url_https": (data.get("avatar") or {}).get("image_url", ""),
                "profile_banner_url": (data.get("banner") or {}).get("image_url"),
                "url": None,
                "location": data.get("location") if isinstance(data.get("location"), str) else "",
                "description": (data.get("profile_bio") or {}).get("description", ""),
                "entities": {},
                "pinned_tweet_ids_str": [],
                "verified": False,
                "possibly_sensitive": bool(data.get("possibly_sensitive")),
                "can_dm": False, "can_media_tag": False, "want_retweets": False,
                "default_profile": True, "default_profile_image": True,
                "has_custom_timelines": False,
                "followers_count": 0, "fast_followers_count": 0,
                "normal_followers_count": 0, "friends_count": 0,
                "favourites_count": 0, "listed_count": 0,
                "media_count": 0, "statuses_count": 0,
                "is_translator": False, "translator_type": "none",
                "withheld_in_countries": [], "protected": False,
            }
        legacy = data.get("legacy")
        if isinstance(legacy, dict):
            entities = legacy.setdefault("entities", {})
            if isinstance(entities, dict):
                for key in ("description", "url"):
                    sub = entities.setdefault(key, {})
                    if isinstance(sub, dict):
                        sub.setdefault("urls", [])
            for key in ("pinned_tweet_ids_str", "withheld_in_countries"):
                legacy.setdefault(key, [])
        return orig_user_init(self, client, data)

    User.__init__ = user_init

    # 恢复真实的 x-client-transaction-id 生成（依赖 XClientTransaction>=1.0.3）
    import bs4
    from x_client_transaction.transaction import ClientTransaction as NewClientTransaction
    from x_client_transaction.utils import handle_x_migration_async, get_ondemand_file_url

    async def _real_init(self, session, headers):  # noqa: ANN001
        self._ct_impl = None
        try:
            home = await handle_x_migration_async(session)
            od_url = get_ondemand_file_url(home)
            od_resp = await session.request(method="GET", url=od_url, headers=headers)
            self._ct_impl = NewClientTransaction(home, od_resp.text)
            self.home_page_response = home
            log.info("X 事务 ID 模块初始化成功")
        except Exception as e:
            # home_page_response 置为 truthy 防止 twikit 每个请求都重试 init；
            # 退回占位 ID：用户推文接口不校验，仅搜索接口受影响
            self.home_page_response = bs4.BeautifulSoup("", "html.parser")
            log.warning("X 事务 ID 初始化失败，退回占位 ID（搜索接口可能 404）: %r", e)

    def _real_tid(self, method=None, path=None, **kwargs):  # noqa: ANN001
        impl = getattr(self, "_ct_impl", None)
        if impl:
            return impl.generate_transaction_id(method=method, path=path)
        return "0" * 32

    ClientTransaction.init = _real_init
    ClientTransaction.generate_transaction_id = _real_tid


_install_patches()
apply_search_qid(SEARCH_QID)


async def refresh_search_qid(http: httpx.AsyncClient, cookies_path: Path) -> bool:
    """搜索接口 404 时，从登录首页引用的前端 main bundle 重新提取 queryId。

    http 需是已配置代理的客户端（x.com 与 twimg 都要走代理）。
    成功返回 True。
    """
    global SEARCH_QID
    try:
        ck = {}
        if Path(cookies_path).exists():
            data = json.loads(Path(cookies_path).read_text(encoding="utf-8"))
            items = data if isinstance(data, list) \
                else [{"name": k, "value": v} for k, v in data.items()]
            ck = {c["name"]: c["value"] for c in items
                  if c.get("name") in ("auth_token", "ct0")}
        r = await http.get("https://x.com/home", headers={"User-Agent": BROWSER_UA},
                           cookies=ck or None, timeout=30, follow_redirects=True)
        scripts = sorted(set(re.findall(
            r'https://abs\.twimg\.com/responsive-web/client-web/main[^"\']+\.js', r.text)))
        if not scripts:
            log.warning("[情报] 首页未找到 main bundle，无法更新 queryId")
            return False
        r2 = await http.get(scripts[0], headers={"User-Agent": BROWSER_UA}, timeout=60)
        m = re.search(r'queryId:"([A-Za-z0-9_-]+)",operationName:"SearchTimeline"', r2.text) \
            or re.search(r'operationName:"SearchTimeline",queryId:"([A-Za-z0-9_-]+)"', r2.text)
        if not m:
            log.warning("[情报] main bundle 里未找到 SearchTimeline queryId")
            return False
        if m.group(1) != SEARCH_QID:
            log.info("[情报] 搜索接口 queryId 已更新: %s", m.group(1))
        SEARCH_QID = m.group(1)
        apply_search_qid(SEARCH_QID)
        return True
    except Exception as e:
        log.warning("[情报] 重新提取搜索 queryId 失败: %r", e)
        return False
