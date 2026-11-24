import json
import threading

import pytest

from app.errors import DomainIssue
from app.main import create_app
from app.waiver.policy import due_date_of, due_month_of, original_amount_cents, split_amount


# ---------- 领域纯函数 --------------------------------------------------------

def make_payload(month, amount, day="15", label="X1"):
    """构造一个最小可用的批次 payload，rows 中带 free_end 与 fee_cents。"""
    return {"rows": [{"label": label, "free_end": f"{month}-{day}", "fee_cents": amount}],
            "summary": {"PASS": 1, "REVIEW": 0, "REJECT": 0},
            "project": "0839 测试", "rule": "测试规则", "profile": "custom",
            "source": {"profile": "custom", "filename": "case.csv", "sha256": "x"},
            "counts": {}, "ledger": {"balanced": True}}


def test_due_month_uses_earliest_free_end():
    payload = {"rows": [
        {"free_end": "2026-11-09", "fee_cents": 100},
        {"free_end": "2026-10-09", "fee_cents": 200},
    ]}
    assert due_month_of(payload) == "2026-10"
    assert due_date_of(payload) == "2026-10-09"


def test_original_amount_sums_fee_cents():
    payload = {"rows": [{"fee_cents": 52000}, {"fee_cents": 16000}, {"fee_cents": 0}]}
    assert original_amount_cents(payload) == 68000


def test_split_conserves_amount_within_quota():
    waiver, paid = split_amount(5000, 100000)
    assert (waiver, paid) == (5000, 0)
    assert waiver + paid == 5000


def test_split_caps_waiver_at_remaining_quota():
    waiver, paid = split_amount(5000, 3000)
    assert (waiver, paid) == (3000, 2000)
    assert waiver + paid == 5000


def test_split_zero_quota_means_full_payment():
    waiver, paid = split_amount(5000, 0)
    assert (waiver, paid) == (0, 5000)


# ---------- HTTP 夹具 ---------------------------------------------------------

@pytest.fixture
def client(tmp_path, monkeypatch):
    # 固定每月额度 100000 分，便于断言。
    monkeypatch.setenv("WAIVER_MONTHLY_QUOTA_CENTS", "100000")
    app = create_app(str(tmp_path / "workbench.sqlite3"))
    app.config.update(TESTING=True)
    return app.test_client()


def create_run(client, payload):
    """直接落库一个自定义批次并登记待签，返回 run 行。"""
    store = client.application.extensions["runs"]
    row = store.create("key-" + json.dumps(payload, sort_keys=True), "custom", payload)
    store.reserve_pending(row["id"], due_month_of(payload), due_date_of(payload),
                          original_amount_cents(payload))
    return row


def approve(client, run_id, revision=0):
    return client.post(f"/api/runs/{run_id}/review",
                       json={"decision": "APPROVED", "comment": "签发", "expected_revision": revision})


# ---------- 守恒：减免 + 实收 = 原复算金额 -------------------------------------

def test_waiver_plus_paid_equals_original(client):
    run = create_run(client, make_payload("2026-11", 30000))
    data = approve(client, run["id"]).get_json()
    w = data["waiver"]
    assert w["waiver_cents"] + w["paid_cents"] == w["original_cents"] == 30000


def test_partial_waiver_when_quota_insufficient(client):
    # 先把 11 月额度用掉 80000，剩 20000；新账单 50000 只能减 20000。
    first = create_run(client, make_payload("2026-11", 80000, day="02"))
    approve(client, first["id"])
    second = create_run(client, make_payload("2026-11", 50000, day="20"))
    w = approve(client, second["id"]).get_json()["waiver"]
    assert w["waiver_cents"] == 20000
    assert w["paid_cents"] == 30000
    assert w["waiver_cents"] + w["paid_cents"] == 50000


def test_zero_waiver_full_payment_when_pool_exhausted(client):
    first = create_run(client, make_payload("2026-11", 100000, day="02"))
    approve(client, first["id"])
    second = create_run(client, make_payload("2026-11", 50000, day="20"))
    w = approve(client, second["id"]).get_json()["waiver"]
    assert w["waiver_cents"] == 0
    assert w["paid_cents"] == 50000


# ---------- 月度额度池独立、同月共用 ------------------------------------------

def test_quotas_are_separate_by_month(client):
    oct_run = create_run(client, make_payload("2026-10", 100000))
    approve(client, oct_run["id"])
    nov_run = create_run(client, make_payload("2026-11", 100000))
    response = approve(client, nov_run["id"])
    assert response.status_code == 200  # 11 月有独立的满额度
    status = {q["due_month"]: q for q in client.get("/api/waiver/quotas").get_json()["quotas"]}
    assert status["2026-10"]["remaining_cents"] == 0
    assert status["2026-11"]["remaining_cents"] == 0


def test_same_month_shares_one_pool(client):
    a = create_run(client, make_payload("2026-11", 60000, day="03"))
    b = create_run(client, make_payload("2026-11", 60000, day="20"))
    approve(client, a["id"])
    wb = approve(client, b["id"]).get_json()["waiver"]
    # 共用 100000 的池子：a 用掉 60000，b 最多再减 40000。
    assert wb["waiver_cents"] == 40000
    assert wb["paid_cents"] == 20000


# ---------- 公平性：后到期不能挤掉同池更早到期的待签账单 -----------------------

def test_later_due_bill_cannot_crowd_out_earlier_pending(client):
    # 同月池 100000：11-05 待签 90000，11-20 账单 50000 先来申请。
    # 后到期账单可以签，但只能用为早账单预留 90000 之后的剩余 10000。
    early = create_run(client, make_payload("2026-11", 90000, day="05"))
    later = create_run(client, make_payload("2026-11", 50000, day="20"))
    response = approve(client, later["id"])
    assert response.status_code == 200
    w = response.get_json()["waiver"]
    assert w["waiver_cents"] == 10000
    assert w["paid_cents"] == 40000
    # 早账单的 90000 额度仍被保留：剩余池恰为 90000。
    status = client.get("/api/waiver/quotas?due_month=2026-11").get_json()["quotas"][0]
    assert status["remaining_cents"] == 90000
    # 早账单仍处于待签。
    assert status["pending_runs"] == 1


def test_later_due_bill_blocked_when_nothing_left_after_earlier_reservation(client):
    # 早到期待签 100000 已占满全部预留空间，后到期账单无额度可用 -> 阻断。
    create_run(client, make_payload("2026-11", 100000, day="05"))
    later = create_run(client, make_payload("2026-11", 50000, day="20"))
    response = approve(client, later["id"])
    assert response.status_code == 409
    assert response.get_json()["code"] == "WAIVER_EARLIER_PENDING"
    # 失败不扣额度。
    status = client.get("/api/waiver/quotas?due_month=2026-11").get_json()["quotas"][0]
    assert status["used_cents"] == 0


def test_later_bill_signs_normally_after_earlier_is_resolved(client):
    early = create_run(client, make_payload("2026-11", 90000, day="05"))
    later = create_run(client, make_payload("2026-11", 50000, day="20"))
    approve(client, early["id"])  # 更早到期的先签发
    response = approve(client, later["id"])
    assert response.status_code == 200


def test_later_bill_uses_only_residual_after_reserving_for_earlier(client):
    # 同月池 100000：早到期待签 30000，晚到期账单 50000 先申请。
    # 预留 30000 给早账单，晚账单最多用 70000，故 50000 可全额减免。
    create_run(client, make_payload("2026-11", 30000, day="05"))
    later = create_run(client, make_payload("2026-11", 50000, day="20"))
    response = approve(client, later["id"])
    assert response.status_code == 200
    assert response.get_json()["waiver"]["waiver_cents"] == 50000


def test_same_due_day_bills_do_not_block_each_other(client):
    a = create_run(client, make_payload("2026-11", 40000, day="15", label="A"))
    b = create_run(client, make_payload("2026-11", 40000, day="15", label="B"))
    assert approve(client, a["id"]).status_code == 200
    assert approve(client, b["id"]).status_code == 200


def test_other_month_pending_does_not_block_current_month(client):
    # 不同月份各有独立额度池：10 月待签不应阻塞 11 月签发。
    create_run(client, make_payload("2026-10", 90000, day="05"))
    nov = create_run(client, make_payload("2026-11", 50000, day="10"))
    assert approve(client, nov["id"]).status_code == 200


# ---------- 失败不扣额度、重试不重复扣减 ---------------------------------------

def test_failed_issue_does_not_deduct(client):
    create_run(client, make_payload("2026-11", 100000, day="05"))
    later = create_run(client, make_payload("2026-11", 50000, day="20"))
    approve(client, later["id"])  # 失败
    approve(client, later["id"])  # 再试一次，仍失败
    status = client.get("/api/waiver/quotas?due_month=2026-11").get_json()["quotas"][0]
    assert status["used_cents"] == 0


def test_stale_revision_does_not_deduct(client):
    run = create_run(client, make_payload("2026-11", 40000))
    approve(client, run["id"], revision=0)  # 成功，rev 变为 1
    stale = client.post(f"/api/runs/{run['id']}/review",
                        json={"decision": "APPROVED", "comment": "旧版本", "expected_revision": 0})
    assert stale.status_code == 409
    status = client.get("/api/waiver/quotas?due_month=2026-11").get_json()["quotas"][0]
    assert status["used_cents"] == 40000  # 没有第二次扣减


def test_grant_is_persisted_and_queryable(client):
    run = create_run(client, make_payload("2026-11", 40000))
    approve(client, run["id"])
    fetched = client.get(f"/api/runs/{run['id']}").get_json()
    assert fetched["waiver"]["waiver_cents"] == 40000
    assert fetched["waiver"]["already_issued"] is True


def test_evidence_carries_waiver_breakdown(client):
    run = create_run(client, make_payload("2026-11", 40000))
    approve(client, run["id"])
    artifact = client.get(f"/api/runs/{run['id']}/evidence").get_json()
    assert artifact["waiver"]["waiver_cents"] + artifact["waiver"]["paid_cents"] == 40000


def test_held_run_keeps_pending_and_can_approve_later(client):
    run = create_run(client, make_payload("2026-11", 30000, label="H"))
    held = client.post(f"/api/runs/{run['id']}/review",
                       json={"decision": "HELD", "comment": "暂缓", "expected_revision": 0})
    assert held.status_code == 200
    status = client.get("/api/waiver/quotas?due_month=2026-11").get_json()["quotas"][0]
    assert status["pending_runs"] == 1 and status["used_cents"] == 0
    approved = approve(client, run["id"], revision=1)
    assert approved.status_code == 200
    assert approved.get_json()["waiver"]["waiver_cents"] == 30000


def test_re_approval_with_current_revision_is_idempotent(client):
    run = create_run(client, make_payload("2026-11", 30000, label="I"))
    first = approve(client, run["id"], revision=0).get_json()
    assert first["waiver"]["already_issued"] is False
    # 用最新版本号重新提交签发：返回原记录，不重复扣减。
    again = approve(client, run["id"], revision=first["revision"])
    assert again.status_code == 200
    assert again.get_json()["waiver"]["already_issued"] is True
    status = client.get("/api/waiver/quotas?due_month=2026-11").get_json()["quotas"][0]
    assert status["used_cents"] == 30000


# ---------- 并发：合起来不超过当月剩余额度 -------------------------------------

def test_concurrent_issues_never_exceed_monthly_quota(client):
    store = client.application.extensions["runs"]
    # 同一月份 10 张账单，每张 30000，总额度 100000。
    runs = [create_run(client, make_payload("2026-11", 30000, day=f"{i + 1:02d}")) for i in range(10)]
    errors, results = [], []
    barrier = threading.Barrier(len(runs))

    def worker(run):
        barrier.wait()
        try:
            _, waiver = store.approve_with_waiver(
                run["id"], "并发", 0, "2026-11", "2026-11-15", 30000)
            results.append(waiver)
        except DomainIssue as exc:
            errors.append(exc.code)

    threads = [threading.Thread(target=worker, args=(run,)) for run in runs]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    total_used = sum(w["waiver_cents"] for w in results)
    assert total_used <= 100000
    status = client.get("/api/waiver/quotas?due_month=2026-11").get_json()["quotas"][0]
    assert status["used_cents"] == total_used
    # 已签发的每张账单都满足守恒。
    for w in results:
        assert w["waiver_cents"] + w["paid_cents"] == 30000


def test_concurrent_same_bill_retry_deducts_once(client):
    store = client.application.extensions["runs"]
    run = create_run(client, make_payload("2026-11", 40000))
    outcomes = []
    barrier = threading.Barrier(5)

    def worker():
        barrier.wait()
        try:
            _, waiver = store.approve_with_waiver(
                run["id"], "并发", 0, "2026-11", "2026-11-15", 40000)
            outcomes.append(("ok", waiver["waiver_cents"]))
        except DomainIssue as exc:
            outcomes.append(("err", exc.code))

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # 只有一个成功扣减；其余因版本号陈旧失败。
    oks = [o for o in outcomes if o[0] == "ok"]
    assert len(oks) == 1
    status = client.get("/api/waiver/quotas?due_month=2026-11").get_json()["quotas"][0]
    assert status["used_cents"] == 40000


# ---------- 真实 clean 样例端到端 ----------------------------------------------

def test_clean_sample_end_to_end(client):
    run = client.post("/api/runs", json={"profile": "clean"}).get_json()
    response = approve(client, run["run_id"])
    assert response.status_code == 200
    w = response.get_json()["waiver"]
    assert w["due_month"] == "2026-10"  # 最早免堆截止日在 10 月
    assert w["waiver_cents"] + w["paid_cents"] == w["original_cents"] == 92000


def test_dirty_sample_blocked_and_no_quota_deducted(client):
    run = client.post("/api/runs", json={"profile": "dirty"}).get_json()
    response = approve(client, run["run_id"])
    assert response.status_code == 409
    assert response.get_json()["code"] == "GATE_BLOCKED"
    quotas = client.get("/api/waiver/quotas").get_json()["quotas"]
    assert all(q["used_cents"] == 0 for q in quotas)
