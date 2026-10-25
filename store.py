# -*- coding: utf-8 -*-
"""存储层：push_records 的多后端支持（SQLite / MySQL / PostgreSQL）。

config.json 的 database 段（修改后重启生效）：
    不配置或 backend 留空            -> SQLite（默认，records.db）
    {"backend": "mysql",    ...}     -> MySQL
    {"backend": "postgres", ...}     -> PostgreSQL

所有字段按文本（VARCHAR/TEXT）存取，时间戳统一北京时间 "YYYY-MM-DD HH:MM:SS"
字符串，字典序比较即时间序，三种后端行为一致。
"""

import json
import sqlite3
import zoneinfo
from pathlib import Path

BJT = zoneinfo.ZoneInfo("Asia/Shanghai")

_store = None  # 模块级单例，init() 时创建


def _db_cfg(cfg: dict) -> dict:
    d = cfg.get("database") or {}
    backend = str(d.get("backend") or "").strip().lower()
    if backend in ("pg", "pgsql", "postgresql"):
        backend = "postgres"
    if backend not in ("", "sqlite", "mysql", "postgres"):
        raise RuntimeError(
            f"database.backend 不支持: {backend!r}（可选 mysql / postgres，留空为 SQLite）")
    try:
        port = int(d.get("port") or 0)
    except (TypeError, ValueError):
        port = 0
    return {
        "backend": backend or "sqlite",
        "host": str(d.get("host") or "").strip(),
        "port": port,
        "user": str(d.get("user") or "").strip(),
        "password": str(d.get("password") or ""),
        "name": str(d.get("name") or "").strip(),
    }


class Store:
    def __init__(self, cfg: dict, sqlite_path: Path):
        d = _db_cfg(cfg)
        self.backend = d["backend"]
        self.p = d
        self.sqlite_path = Path(sqlite_path)
        if self.backend != "sqlite" and not (self.p["host"] and self.p["name"]):
            raise RuntimeError(
                f"database.{self.backend} 需要填写 host 和 name（数据库名）。")

    # ---------- 连接与执行 ----------

    def _remote_connect(self):
        if self.backend == "mysql":
            try:
                import pymysql
            except ImportError:
                raise RuntimeError("使用 MySQL 需先安装 PyMySQL：pip install PyMySQL")
            return pymysql.connect(
                host=self.p["host"] or "127.0.0.1",
                port=self.p["port"] or 3306,
                user=self.p["user"], password=self.p["password"],
                database=self.p["name"], charset="utf8mb4",
                autocommit=True, connect_timeout=10)
        try:
            import psycopg2
        except ImportError:
            raise RuntimeError(
                "使用 PostgreSQL 需先安装 psycopg2-binary：pip install psycopg2-binary")
        conn = psycopg2.connect(
            host=self.p["host"] or "127.0.0.1",
            port=self.p["port"] or 5432,
            user=self.p["user"], password=self.p["password"],
            dbname=self.p["name"], connect_timeout=10)
        conn.autocommit = True
        return conn

    def _query(self, sql: str, params=()) -> list[dict]:
        """SELECT，统一返回 [{列: 值}, ...]。SQL 用 ? 占位符，远程后端自动转 %s。"""
        if self.backend == "sqlite":
            with sqlite3.connect(self.sqlite_path, timeout=30) as conn:
                conn.row_factory = sqlite3.Row
                rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
            return rows
        sql = sql.replace("?", "%s")
        conn = self._remote_connect()
        try:
            cur = conn.cursor()
            cur.execute(sql, params)
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]
        finally:
            conn.close()

    def _execute(self, sql: str, params=()) -> None:
        """INSERT/UPDATE/DELETE。"""
        if self.backend == "sqlite":
            with sqlite3.connect(self.sqlite_path, timeout=30) as conn:
                conn.execute(sql, params)
            return
        sql = sql.replace("?", "%s")
        conn = self._remote_connect()
        try:
            cur = conn.cursor()
            cur.execute(sql, params)
            conn.commit()
        finally:
            conn.close()

    # ---------- 建表与迁移 ----------

    def init(self) -> None:
        if self.backend == "sqlite":
            with sqlite3.connect(self.sqlite_path, timeout=30) as conn:
                # WAL：面板读与监控写并发时不再互相阻塞（模式持久化在库文件里）
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS push_records (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        tweet_id TEXT UNIQUE,
                        handle TEXT, display TEXT, tag TEXT, text TEXT,
                        translation TEXT, time_conv TEXT, url TEXT,
                        tweet_created TEXT, pushed_at TEXT, channels TEXT,
                        status TEXT, source TEXT NOT NULL DEFAULT 'monitor'
                    )""")
                # 旧库迁移：source 区分记录来自主监控(monitor)还是免费情报(intel_*)
                cols = [r[1] for r in conn.execute("PRAGMA table_info(push_records)")]
                if "source" not in cols:
                    conn.execute("ALTER TABLE push_records "
                                 "ADD COLUMN source TEXT NOT NULL DEFAULT 'monitor'")
            return
        if self.backend == "mysql":
            self._execute("""
                CREATE TABLE IF NOT EXISTS push_records (
                    id BIGINT AUTO_INCREMENT PRIMARY KEY,
                    tweet_id VARCHAR(64) UNIQUE,
                    handle VARCHAR(128), display VARCHAR(191), tag VARCHAR(64),
                    text MEDIUMTEXT, translation MEDIUMTEXT,
                    time_conv VARCHAR(512), url VARCHAR(512),
                    tweet_created VARCHAR(64), pushed_at VARCHAR(32),
                    channels VARCHAR(512), status VARCHAR(32),
                    source VARCHAR(32) NOT NULL DEFAULT 'monitor'
                ) DEFAULT CHARSET=utf8mb4""")
            return
        self._execute("""
            CREATE TABLE IF NOT EXISTS push_records (
                id BIGSERIAL PRIMARY KEY,
                tweet_id VARCHAR(64) UNIQUE,
                handle VARCHAR(128), display VARCHAR(191), tag VARCHAR(64),
                text TEXT, translation TEXT,
                time_conv VARCHAR(512), url VARCHAR(512),
                tweet_created VARCHAR(64), pushed_at VARCHAR(32),
                channels VARCHAR(512), status VARCHAR(32),
                source VARCHAR(32) NOT NULL DEFAULT 'monitor'
            )""")

    # ---------- 业务操作 ----------

    def push_record(self, handle, display, status, text="", tag="", tweet_id="",
                    url="", tweet_created="", translation="", time_conv=None,
                    results=None, source="monitor") -> None:
        """写入/更新一条记录（按 tweet_id 幂等，等价旧版的 INSERT OR REPLACE）。"""
        vals = {
            "handle": handle,
            "display": display or f"@{handle}",
            "tag": tag,
            "text": text,
            "translation": translation or "",
            "time_conv": json.dumps(time_conv or [], ensure_ascii=False),
            "url": url,
            "tweet_created": tweet_created,
            "pushed_at": _now_str(),
            "channels": json.dumps(results or {}, ensure_ascii=False),
            "status": status,
            "source": source,
        }
        existing = self._query("SELECT id FROM push_records WHERE tweet_id = ?",
                               (str(tweet_id),))
        if existing:
            self._execute(
                "UPDATE push_records SET handle=?, display=?, tag=?, text=?, "
                "translation=?, time_conv=?, url=?, tweet_created=?, pushed_at=?, "
                "channels=?, status=?, source=? WHERE tweet_id=?",
                (vals["handle"], vals["display"], vals["tag"], vals["text"],
                 vals["translation"], vals["time_conv"], vals["url"],
                 vals["tweet_created"], vals["pushed_at"], vals["channels"],
                 vals["status"], source, str(tweet_id)))
        else:
            self._execute(
                "INSERT INTO push_records (tweet_id, handle, display, tag, text, "
                "translation, time_conv, url, tweet_created, pushed_at, channels, "
                "status, source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (str(tweet_id), vals["handle"], vals["display"], vals["tag"],
                 vals["text"], vals["translation"], vals["time_conv"], vals["url"],
                 vals["tweet_created"], vals["pushed_at"], vals["channels"],
                 vals["status"], source))

    def tweet_seen(self, tweet_id) -> bool:
        """该推文是否已入库（任意管线），用于跨管线去重。"""
        return bool(self._query("SELECT 1 FROM push_records WHERE tweet_id = ? LIMIT 1",
                                (str(tweet_id),)))

    def intel_rows_since(self, since: str) -> list[dict]:
        """日报收录范围：时间窗内的情报 + 全部未覆盖的基准行。"""
        return self._query(
            "SELECT * FROM push_records WHERE source = 'intel_search' AND ("
            "(pushed_at >= ? AND status IN ('success', 'partial', 'failed')) "
            "OR status = 'baseline') ORDER BY pushed_at ASC, id ASC", (since,))

    def mark_baselines_covered(self) -> None:
        """日报送达后，把基准行标记为已覆盖，避免后续日报重复带上它们。"""
        self._execute("UPDATE push_records SET status = 'covered' "
                      "WHERE source = 'intel_search' AND status = 'baseline'")

    def stats(self) -> dict:
        """面板统计卡片：累计/今日/疑似重置/今日情报。"""
        today = _now_str()[:10] + "%"
        ok = "status IN ('success', 'partial')"
        return {
            "total": self._query(f"SELECT COUNT(*) AS n FROM push_records WHERE {ok}")[0]["n"],
            "today": self._query(f"SELECT COUNT(*) AS n FROM push_records WHERE {ok} "
                                 "AND pushed_at LIKE ?", (today,))[0]["n"],
            "reset": self._query(f"SELECT COUNT(*) AS n FROM push_records "
                                 f"WHERE tag != '' AND {ok}")[0]["n"],
            "intel_today": self._query(
                f"SELECT COUNT(*) AS n FROM push_records WHERE source = 'intel_search' "
                f"AND {ok} AND pushed_at LIKE ?", (today,))[0]["n"],
        }

    def records(self, limit: int, source_prefix: str = "") -> list[dict]:
        """面板记录列表；source_prefix 为空取全部，'intel' 前缀同时匹配 intel_*。"""
        if source_prefix:
            return self._query(
                "SELECT * FROM push_records WHERE source LIKE ? "
                "ORDER BY id DESC LIMIT ?", (source_prefix + "%", limit))
        return self._query(
            "SELECT * FROM push_records ORDER BY id DESC LIMIT ?", (limit,))

    def clear(self) -> None:
        self._execute("DELETE FROM push_records")


def _now_str() -> str:
    return datetime_now_bjt().strftime("%Y-%m-%d %H:%M:%S")


def datetime_now_bjt():
    from datetime import datetime
    return datetime.now(BJT)


def init(cfg: dict, sqlite_path: Path) -> Store:
    """按配置创建并初始化存储（建表/迁移），返回单例。"""
    global _store
    _store = Store(cfg, sqlite_path)
    _store.init()
    return _store


def get() -> Store:
    """取当前存储单例；未初始化时按默认 SQLite 兜底。"""
    global _store
    if _store is None:
        _store = Store({}, "records.db")
        _store.init()
    return _store
