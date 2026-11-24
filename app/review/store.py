import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from app.errors import DomainIssue
from app.waiver.policy import monthly_quota_cents, split_amount


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.init()

    def connect(self):
        connection = sqlite3.connect(self.path, timeout=15)
        connection.row_factory = sqlite3.Row
        return connection

    def init(self):
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript('''
            CREATE TABLE IF NOT EXISTS runs(
              id TEXT PRIMARY KEY,
              request_key TEXT UNIQUE NOT NULL,
              created_at TEXT NOT NULL,
              profile TEXT NOT NULL,
              status TEXT NOT NULL,
              revision INTEGER NOT NULL,
              payload TEXT NOT NULL,
              comment TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS waiver_quotas(
              due_month TEXT PRIMARY KEY,
              quota_cents INTEGER NOT NULL,
              used_cents INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS waiver_grants(
              run_id TEXT PRIMARY KEY,
              due_month TEXT NOT NULL,
              amount_cents INTEGER NOT NULL,
              waiver_cents INTEGER NOT NULL,
              paid_cents INTEGER NOT NULL,
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS waiver_pendings(
              run_id TEXT PRIMARY KEY,
              due_month TEXT NOT NULL,
              due_date TEXT NOT NULL,
              amount_cents INTEGER NOT NULL,
              created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_waiver_pending_month ON waiver_pendings(due_month);
            CREATE INDEX IF NOT EXISTS idx_waiver_grant_month ON waiver_grants(due_month);
            ''')

    def find_by_request(self, request_key):
        with self.connect() as db:
            row = db.execute("SELECT * FROM runs WHERE request_key=?", (request_key,)).fetchone()
        return dict(row) if row else None

    def create(self, request_key, profile, payload):
        existing = self.find_by_request(request_key)
        if existing:
            return existing
        run_id = uuid.uuid4().hex[:12]
        with self.connect() as db:
            db.execute("INSERT INTO runs(id,request_key,created_at,profile,status,revision,payload) VALUES(?,?,?,?,?,?,?)",
                       (run_id, request_key, datetime.now(timezone.utc).isoformat(), profile, "DRAFT", 0,
                        json.dumps(payload, ensure_ascii=False)))
        return self.get(run_id)

    def get(self, run_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        return dict(row) if row else None

    # ---- 减免额度 --------------------------------------------------------

    def _ensure_quota(self, db, due_month):
        row = db.execute("SELECT * FROM waiver_quotas WHERE due_month=?", (due_month,)).fetchone()
        if row is None:
            quota = monthly_quota_cents()
            db.execute("INSERT INTO waiver_quotas(due_month, quota_cents, used_cents) VALUES(?,?,0)",
                       (due_month, quota))
            return quota, 0
        return row["quota_cents"], row["used_cents"]

    def reserve_pending(self, run_id, due_month, due_date, amount_cents):
        """登记待签账单，不扣额度。后到期账单也允许先登记。"""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._ensure_quota(db, due_month)
            db.execute(
                "INSERT INTO waiver_pendings(run_id, due_month, due_date, amount_cents, created_at) "
                "VALUES(?,?,?,?,?) ON CONFLICT(run_id) DO UPDATE SET "
                "due_month=excluded.due_month, due_date=excluded.due_date, amount_cents=excluded.amount_cents",
                (run_id, due_month, due_date, amount_cents, datetime.now(timezone.utc).isoformat()))

    def approve_with_waiver(self, run_id, comment, expected_revision, due_month, due_date, amount_cents):
        """签发并扣减额度，全部在同一个串行写事务内完成。

        - 版本号不符 -> STALE_REVIEW，事务回滚，额度不动。
        - 公平性/额度不满足 -> DomainIssue，事务回滚，额度不动。
        - 重试同一已签发账单 -> 返回原减免记录，不重复扣减。
        """
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if row is None:
                raise DomainIssue("RUN_NOT_FOUND", 404)
            if row["revision"] != expected_revision:
                raise DomainIssue("STALE_REVIEW", 409)

            existing = db.execute("SELECT * FROM waiver_grants WHERE run_id=?", (run_id,)).fetchone()
            if existing is not None:
                # 幂等：已成功签发，直接沿用原减免拆分。
                quota, used = self._ensure_quota(db, existing["due_month"])
                waiver = self._waiver_result(existing, quota, used, duplicate=True)
            else:
                quota, used = self._ensure_quota(db, due_month)
                remaining = quota - used

                # 公平性：同一月度额度池内，到期日更早的待签账单优先占用额度。
                # 后到期账单即使先申请签发，也不能挤掉更早到期账单能用的额度；
                # 到期日相同的待签账单不互相阻塞。不同月份各有独立额度池。
                earlier_need = db.execute(
                    "SELECT COALESCE(SUM(amount_cents),0) AS need "
                    "FROM waiver_pendings WHERE due_month=? AND due_date < ? AND run_id<>?",
                    (due_month, due_date, run_id)).fetchone()["need"]
                headroom = max(0, remaining - int(earlier_need))
                if int(earlier_need) > 0 and headroom <= 0:
                    # 仅当确实存在更早到期的待签账单、且额度已不足以同时容纳时才阻断；
                    # 若没有更早待签账单，即使额度为零也允许签发（全额实收，减免为零）。
                    raise DomainIssue("WAIVER_EARLIER_PENDING", 409)

                waiver_cents, paid_cents = split_amount(amount_cents, headroom)
                now = datetime.now(timezone.utc).isoformat()
                db.execute(
                    "INSERT INTO waiver_grants(run_id, due_month, amount_cents, waiver_cents, paid_cents, created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (run_id, due_month, amount_cents, waiver_cents, paid_cents, now))
                db.execute("UPDATE waiver_quotas SET used_cents=used_cents+? WHERE due_month=?",
                           (waiver_cents, due_month))
                db.execute("DELETE FROM waiver_pendings WHERE run_id=?", (run_id,))
                granted = db.execute("SELECT * FROM waiver_grants WHERE run_id=?", (run_id,)).fetchone()
                _, new_used = self._ensure_quota(db, due_month)
                waiver = self._waiver_result(granted, quota, new_used, duplicate=False)

            db.execute("UPDATE runs SET status='APPROVED', revision=revision+1, comment=? WHERE id=?",
                       (comment, run_id))
            updated = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            return dict(updated), waiver

    def hold(self, run_id, comment, expected_revision):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if row is None:
                return None, "missing"
            if row["revision"] != expected_revision:
                return None, "stale"
            db.execute("UPDATE runs SET status='HELD', revision=revision+1, comment=? WHERE id=?",
                       (comment, run_id))
        return self.get(run_id), None

    def quota_status(self, due_month=None):
        with self.connect() as db:
            if due_month is not None:
                quota, used = self._ensure_quota(db, due_month)
                return [self._month_status(db, due_month, quota, used)]
            rows = db.execute("SELECT * FROM waiver_quotas ORDER BY due_month").fetchall()
            return [self._month_status(db, item["due_month"], item["quota_cents"], item["used_cents"]) for item in rows]

    def grant_for(self, run_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM waiver_grants WHERE run_id=?", (run_id,)).fetchone()
        return dict(row) if row else None

    def _month_status(self, db, due_month, quota, used):
        pending = db.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(amount_cents),0) AS amount "
            "FROM waiver_pendings WHERE due_month=?", (due_month,)).fetchone()
        return {
            "due_month": due_month,
            "quota_cents": quota,
            "used_cents": used,
            "remaining_cents": quota - used,
            "pending_runs": pending["n"],
            "pending_amount_cents": pending["amount"],
        }

    @staticmethod
    def _waiver_result(row, quota, used, duplicate):
        return {
            "due_month": row["due_month"],
            "original_cents": row["amount_cents"],
            "waiver_cents": row["waiver_cents"],
            "paid_cents": row["paid_cents"],
            "quota_cents": quota,
            "used_cents": used,
            "remaining_cents": quota - used,
            "already_issued": duplicate,
        }
