"""滞港费减免额度领域规则。

- 船公司每月有固定的减免额度，同一到期月份的批次共用一个额度池。
- 后到期的账单可以先申请，但不能挤掉已待签且到期更早的账单可用的额度。
- 每张账单：减免 + 实收 = 原复算金额（整数分，守恒）。
"""
import os

from app.errors import DomainIssue

# 每月固定减免额度（单位：分）。可用环境变量覆盖，便于测试与运维配置。
DEFAULT_MONTHLY_QUOTA_CENTS = 100_000


def monthly_quota_cents():
    try:
        value = int(os.getenv("WAIVER_MONTHLY_QUOTA_CENTS", str(DEFAULT_MONTHLY_QUOTA_CENTS)))
    except ValueError:
        value = DEFAULT_MONTHLY_QUOTA_CENTS
    return max(0, value)


def due_month_of(payload):
    """批次（账单）的到期月份，取批次内最早的免堆截止日所在月份。

    免堆截止日 `free_end` 是滞港费开始计收的日期，即账单到期口径。
    一个批次内各箱的到期日可能跨月，取最早者代表整批账单的到期月份，
    保证同一批次只占用一个月度额度池。
    """
    return due_date_of(payload)[:7]


def due_date_of(payload):
    """批次内最早的免堆截止日（ISO 日期字符串），用于同池内按到期日排公平次序。"""
    earliest = None
    for item in payload.get("rows", []):
        free_end = item.get("free_end")
        if not free_end:
            continue
        day = str(free_end)[:10]
        if earliest is None or day < earliest:
            earliest = day
    if earliest is None:
        raise DomainIssue("WAIVER_DUE_MONTH_UNKNOWN", 409)
    return earliest


def original_amount_cents(payload):
    """原复算金额：批次内各箱复算滞港费（应收分）之和。"""
    total = 0
    for item in payload.get("rows", []):
        fee = item.get("fee_cents")
        if fee is None:
            raise DomainIssue("WAIVER_AMOUNT_UNKNOWN", 409)
        total += int(fee)
    return total


def split_amount(amount_cents, remaining_cents):
    """按当月剩余额度拆分一张账单。

    减免不超过账单原复算金额，也不超过额度池剩余；实收补足差额。
    返回 ``(waiver_cents, paid_cents)``，满足减免 + 实收 = 原复算金额。
    """
    amount_cents = int(amount_cents)
    remaining_cents = int(remaining_cents)
    if amount_cents < 0 or remaining_cents < 0:
        raise DomainIssue("BAD_REQUEST")
    waiver = min(amount_cents, remaining_cents)
    paid = amount_cents - waiver
    return waiver, paid
