# -*- coding: utf-8 -*-
"""核心逻辑单元测试（不依赖网络、不碰真实数据）。

运行: .venv/Scripts/python tests/test_core.py
"""

import json
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import monitor as m

tmp = Path(tempfile.mkdtemp())
m.DB_PATH = tmp / "records.db"
m.STATE_PATH = tmp / "state.json"

# 1) 旧库迁移：没有 source 列的表自动补列，旧数据回填为 monitor
with sqlite3.connect(m.DB_PATH) as c:
    c.execute("""CREATE TABLE push_records (id INTEGER PRIMARY KEY AUTOINCREMENT, tweet_id TEXT UNIQUE,
        handle TEXT, display TEXT, tag TEXT, text TEXT, translation TEXT, time_conv TEXT,
        url TEXT, tweet_created TEXT, pushed_at TEXT, channels TEXT, status TEXT)""")
    c.execute("INSERT INTO push_records (tweet_id, handle, status) VALUES ('999', 'old_user', 'success')")
m.init_db()
cols = [r[1] for r in sqlite3.connect(m.DB_PATH).execute("PRAGMA table_info(push_records)")]
assert "source" in cols, cols
old = sqlite3.connect(m.DB_PATH).execute(
    "SELECT source FROM push_records WHERE tweet_id='999'").fetchone()
assert old[0] == "monitor", old
journal = sqlite3.connect(m.DB_PATH).execute("PRAGMA journal_mode").fetchone()[0]
assert journal.lower() == "wal", journal  # WAL 已开启

# 2) record_push 带 source + tweet_seen 去重
m.record_push("foo", "Foo Bar", status="success", text="hello", tweet_id="123",
              url="u", source="intel_search")
assert m.tweet_seen("123") and not m.tweet_seen("456")

# 3) intel_cfg 默认值与越界修正
cfg = m.intel_cfg({})
assert cfg["enabled"] and len(cfg["queries"]) == 2
assert cfg["search_interval_minutes"] == 10 and cfg["digest_time"] == "09:00"
assert cfg["must_match"] and cfg["exclude_match"]
cfg2 = m.intel_cfg({"intel": {"enabled": False, "queries": ["q1"],
                              "search_interval_minutes": 3, "digest_time": "21:30"}})
assert not cfg2["enabled"] and cfg2["queries"] == ["q1"]
assert cfg2["search_interval_minutes"] == 5 and cfg2["digest_time"] == "21:30"

# 4) 日报格式：空 / 有内容 / 超长截断 / 译文在前 / 自定义落款
t, h, p, md = m.format_digest_bodies([], signature="X")
assert "没有搜到" in p and p.endswith("-- X")
rows = [{"display": "Someone", "handle": "someone", "text": "free credits " * 40,
         "translation": "免费额度 " * 60, "url": "https://x.com/someone/status/1",
         "pushed_at": "2026-10-25 08:00:00"}]
t, h, p, md = m.format_digest_bodies(rows, signature="Y")
assert "1 条" in t and "…" in p and "免费情报日报" in t
assert "<a href=" in h and "Twitter monitoring 测试" not in p
idx_tr = p.find("🌐")
idx_tx = p.find("📄")
assert 0 < idx_tr < idx_tx  # 译文在原文之前
assert "-- Y" in p

# 5) 邮件格式：情报邮件标题用 📡 且优先中文译文，普通推送用 🔔
intel_p = {"display": "A", "tag": m.INTEL_TAG, "text": "free tokens here",
           "translation": "免费token在此", "media": "", "created": "x", "url": "u",
           "time_conv": []}
subject, body = m.format_email(intel_p)
assert subject.startswith("📡") and "免费token在此" in subject
normal_p = dict(intel_p, tag="")
subject, _ = m.format_email(normal_p)
assert subject.startswith("🔔")

# 6) 发件人名：默认值与自定义
assert m.email_from_name({}) == "Twitter monitoring"
assert m.email_from_name({"from_name": "自定义"}) == "自定义"

# 7) 日报调度判断与窗口起点
st = {"intel": {"last_digest": "2026-10-24"}}
assert m.digest_due({"intel": {"enabled": True, "digest_time": "00:00"}}, st)
assert not m.digest_due({"intel": {"enabled": True, "digest_time": "23:59"}}, st)
assert m.intel_digest_window({"intel": {"last_digest_at": "2026-10-24 09:00:00"}}) \
    == "2026-10-24 09:00:00"

# 8) 日报收录范围：只取情报行；baseline 不受窗口限制
m.record_push("a", "A", status="success", text="x", tweet_id="1", source="monitor", url="")
m.record_push("b", "B", status="baseline", text="y", tweet_id="2", source="intel_search", url="")
rs = m.intel_rows_since("2999-01-01 00:00:00")  # 窗口之外，baseline 仍应收录
assert {r["tweet_id"] for r in rs} == {"2"}, rs
rs = m.intel_rows_since("2000-01-01 00:00:00")
assert {r["tweet_id"] for r in rs} == {"2", "123"}

# 9) 长消息分片
chunks = m.split_message("ab\n" * 3000, limit=1000)
assert all(len(c) <= 1000 for c in chunks) and "".join(chunks).count("ab") == 3000

# 10) 时区换算：tomorrow 10am PST → 北京时间（含星期）
conv = m.convert_time_refs("free model tomorrow 10am PST",
                           datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc))
assert conv and "10-27 01:00（周二）" in conv[0], conv

# 11) 多账号：解析（accounts 优先，兼容旧单账号字段）
assert m.x_accounts({"twitter": {}}) == []
legacy = m.x_accounts({"twitter": {"auth_token": "A", "ct0": "B"}})
assert legacy == [{"auth_token": "A", "ct0": "B", "label": ""}]
multi = m.x_accounts({"twitter": {"auth_token": "X", "ct0": "Y",
                                  "accounts": [{"auth_token": "A", "ct0": "B", "label": "小号1"},
                                               {"auth_token": "", "ct0": ""}]}})
assert multi == [{"auth_token": "A", "ct0": "B", "label": "小号1"}]

# 12) 多账号轮换：默认用第一个；冷却后跳过；全冷却选最早恢复的
m.MONITOR_STATE["x_account_idx"] = 0
m.MONITOR_STATE["x_account_cooldown"] = {}
accs = m.x_accounts({"twitter": {"accounts": [
    {"auth_token": "A", "ct0": "B", "label": "a"},
    {"auth_token": "C", "ct0": "D", "label": "b"},
    {"auth_token": "E", "ct0": "F", "label": "c"}]}})
cfgm = {"twitter": {"accounts": accs}}
assert m.pick_account(cfgm)["label"] == "a"
m.cool_current_account(3600)                       # 冷却 a
assert m.pick_account(cfgm)["label"] == "b"        # 轮到 b
m.MONITOR_STATE["x_account_cooldown"] = {"0": m.time.time() + 999,
                                         "1": m.time.time() + 999,
                                         "2": m.time.time() + 100}
assert m.pick_account(cfgm)["label"] == "c"        # 全冷却 → 最早恢复的 c
assert m.MONITOR_STATE["x_account_idx"] == 2

# 13) 适配层生效：SearchTimeline 端点已替换为现网 queryId
import xclient
from twikit.client.gql import Endpoint
assert xclient.SEARCH_QID in Endpoint.SEARCH_TIMELINE
assert Endpoint.SEARCH_TIMELINE.startswith("https://x.com/i/api/graphql/")

# 14) 存储层：配置解析 + SQLite 全流程
import store as store_mod

d = store_mod._db_cfg({})
assert d["backend"] == "sqlite"
assert store_mod._db_cfg({"database": {"backend": "PG", "host": "h", "name": "n"}})["backend"] == "postgres"
assert store_mod._db_cfg({"database": {"backend": "mysql", "host": "h", "name": "n", "port": "3307"}})["port"] == 3307
try:
    store_mod._db_cfg({"database": {"backend": "oracle"}})
    assert False, "未知 backend 应报错"
except RuntimeError:
    pass
try:
    store_mod.Store({"database": {"backend": "mysql"}}, tmp / "must_fail.db")
    assert False, "mysql 缺 host/name 应报错"
except RuntimeError:
    pass

spath = tmp / "store.db"
s = store_mod.Store({}, spath)
s.init()
s.push_record("u1", None, status="success", text="t1", tweet_id="T1",
              url="u", source="intel_search")
s.push_record("u1", None, status="success", text="t1-new", tweet_id="T1",
              url="u", source="intel_search")  # 同 tweet_id 幂等覆盖
assert s.tweet_seen("T1") and not s.tweet_seen("T2")
rows = s.records(10)
assert len(rows) == 1 and rows[0]["text"] == "t1-new" and rows[0]["display"] == "@u1"
s.push_record("b", "Bee", status="baseline", text="base", tweet_id="T2",
              url="", source="intel_search")
assert [r["tweet_id"] for r in s.intel_rows_since("2999-01-01")] == ["T2"]  # 窗口外仅 baseline
s.mark_baselines_covered()
assert s.intel_rows_since("2999-01-01") == []          # baseline 已覆盖，窗口外无收录
assert [r["tweet_id"] for r in s.intel_rows_since("2000-01-01")] == ["T1"]  # 仅剩 success 行
s.push_record("m", "M", status="success", text="m", tweet_id="T3",
              url="", source="monitor")
stt = s.stats()
assert stt["total"] == 2 and stt["today"] == 2 and stt["intel_today"] == 1, stt
assert {r["tweet_id"] for r in s.records(10, "intel")} == {"T1", "T2"}
assert {r["tweet_id"] for r in s.records(10, "monitor")} == {"T3"}
s.clear()
assert s.records(10) == []

# 15) monitor 委托层走存储（含旧库迁移）
m.init_db()
m.record_push("dele", None, status="success", text="via-delegate",
              tweet_id="D1", url="", source="monitor")
assert m.tweet_seen("D1")

# 16) 官方账号标签：官方推文带【官方】，与【疑似重置】可叠加，普通推文无标签
class _ResetTweet:
    text = "Day 1 reset"
    created_at = None
    id = "111"

class _NormalTweet:
    text = "just saying hi"
    created_at = None
    id = "222"

p = m.tweet_parts("someone", _ResetTweet(), official=True)
assert p["tag"] == m.OFFICIAL_TAG + m.RESET_TAG, p["tag"]
p = m.tweet_parts("someone", _ResetTweet(), official=False)
assert p["tag"] == m.RESET_TAG
p = m.tweet_parts("someone", _NormalTweet(), official=True)
assert p["tag"] == m.OFFICIAL_TAG
p = m.tweet_parts("someone", _NormalTweet(), official=False)
assert p["tag"] == ""

print("ALL CORE TESTS PASSED")
