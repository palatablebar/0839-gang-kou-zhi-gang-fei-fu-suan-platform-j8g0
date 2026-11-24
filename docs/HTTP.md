# 接口

- `GET /`：工作台。
- `GET /api/catalog`：阶段、字段和样例。
- `POST /api/stages/{stage}`：按阶段返回处理结果。
- `POST /api/runs`：执行完整批次并保存草稿。
- `GET /api/runs/{id}`：读取批次。
- `POST /api/runs/{id}/review`：签发或暂缓，提交 `decision`、`comment`、`expected_revision`。签发成功返回 `waiver` 减免拆分。
- `GET /api/runs/{id}/evidence`：下载已签发证据。
- `GET /api/waiver/quotas`：查看各到期月份的减免额度池，可选 `?due_month=YYYY-MM` 过滤。
- `GET /api/examples/{profile}`：下载样例。
- `GET /healthz`：健康检查。

## 减免额度

- 每个到期月份有独立的固定减免额度（`WAIVER_MONTHLY_QUOTA_CENTS`，默认 100000 分），同月份批次共用一个额度池。
- 到期月份取批次内最早免堆截止日（`free_end`）所在月份。
- 签发时在同一事务内完成版本校验、公平性检查与额度扣减：后到期账单可以先签，但只能用为同池更早到期待签账单预留额度之后的剩余。
- 每张账单返回 `waiver.waiver_cents + waiver.paid_cents = waiver.original_cents`（减免 + 实收 = 原复算金额）。
- 签发失败（阻断、版本陈旧、公平性不足）整体回滚，不扣额度；同一账单重试不会重复扣减。
- 错误码：`GATE_BLOCKED`（存在 REJECT）、`STALE_REVIEW`（版本号陈旧）、`WAIVER_EARLIER_PENDING`（更早到期账单待签且额度不足）。
