import json

from app.errors import DomainIssue
from app.review.policy import approval_allowed
from app.waiver.policy import due_date_of, due_month_of, original_amount_cents


def create_run(store, payload):
    row = store.create(payload["run_key"], payload["profile"], payload)
    # 草稿即成为待签账单：登记到期月份/到期日与原复算金额，不扣任何额度。
    store.reserve_pending(row["id"], due_month_of(payload), due_date_of(payload),
                          original_amount_cents(payload))
    return serialize(row)


def approve(store, run_id, decision, comment, expected_revision):
    row = store.get(run_id)
    if row is None:
        raise DomainIssue("RUN_NOT_FOUND", 404)
    payload = json.loads(row["payload"])
    if decision not in {"APPROVED", "HELD"}:
        raise DomainIssue("BAD_REQUEST")
    if decision == "APPROVED" and not approval_allowed(payload):
        raise DomainIssue("GATE_BLOCKED", 409)

    comment = comment.strip()
    if decision == "APPROVED":
        # 版本校验、公平性检查、额度扣减、状态更新在同一事务内提交；
        # 任一步失败整体回滚，不扣额度，重试也不会重复扣减。
        updated, waiver = store.approve_with_waiver(
            run_id, comment, expected_revision,
            due_month_of(payload), due_date_of(payload), original_amount_cents(payload))
        result = serialize(updated)
        result["waiver"] = waiver
        return result

    updated, problem = store.hold(run_id, comment, expected_revision)
    if problem == "missing":
        raise DomainIssue("RUN_NOT_FOUND", 404)
    if problem == "stale":
        raise DomainIssue("STALE_REVIEW", 409)
    return serialize(updated)


def serialize(row):
    if row is None:
        return None
    payload = json.loads(row["payload"])
    payload.update(run_id=row["id"], status=row["status"], revision=row["revision"],
                   comment=row["comment"], created_at=row["created_at"])
    return payload
