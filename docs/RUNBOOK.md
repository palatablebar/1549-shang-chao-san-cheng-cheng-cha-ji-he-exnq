# WEIGH RECEIPT运行手册

更换班次或复算派单时使用。先确认 `/health` 返回 `ok`，再检查以下入口和凭证中的 `run_id`。

| 文档或路由 | 用途 |
|---|---|
| `sample_data/scale_slips_sample.json` | 同时提供拟合、校准、回放和当班的样例 |
| `GET /api/weighttally/overview` | 默认额度和样例处置 |
| `POST /api/weighttally/run` | 用 `capacity` 和 `strategy` 调整派单，也可同时提供 `records` 与 `as_of` |
| `GET /api/weighttally/stages/<环节>` | 样例的分环节核对，可传相同额度、策略 |

导入 JSON 对象时 `records` 为 1–2500 条单据，`as_of` 为整数工日；默认第 96 日。额度接受 1–60，策略接受 `balanced` 或 `focus`。入站异常保留于第一环节。整批缺少成熟窗口时响应 422 并给出原因。

```sh
docker compose exec -T app python -m weighttally.entry evaluate --input sample_data/scale_slips_sample.json --capacity 9
```

在网页筛选单号、作业区或原因，点击结果中的单号查看完整字段与处置证据。下载 JSON 可检视结果、对照原始资料摘要和模型特征版本。派单环节另附「当班复测单」：加签结果在 `sections[6].recheck_rows`，逐票凭据含选中的改测量和前后风险；`recheck_summary` 与 `recheck_ledger` 用于和原派单用量对账。复测只占余位，不改变原有派单。
