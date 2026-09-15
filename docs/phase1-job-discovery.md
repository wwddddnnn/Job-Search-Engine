# Phase 1 Job Discovery 设计说明

## 范围

Phase 1 实现用户手动触发的职位发现流程：保存统一 SearchConfig，创建并推进 SearchRun，调用唯一启用的 JobsPipe Provider，保存每次请求与完整原始响应，规范化职位，并以 `provider + external_id` 保证同一 Provider 职位的幂等更新。该阶段不实现 TheirStack、多 Provider 并发、跨 Provider canonical dedup、scheduler、LLM、Matching、Application、MCP 或浏览器发现。

## 运行管道

```text
SaveSearchConfig
  → StartSearchRun (manual)
  → SearchRunService
  → JobSearchProvider (JobsPipeProvider)
  → ProviderRequest + RawJobPayload persistence
  → JobNormalizer
  → provider identity upsert: provider + external_id
  → CanonicalJob + JobSnapshot
  → SearchRun completion + audit event
```

SearchRunService 是唯一编排入口。HTTP/CLI/UI 未来只能调用这个服务，不能直接向 JobsPipe 请求或写入 `job_sources`。每一页 JobsPipe 响应均先持久化，然后处理其中 `data` 数组；这保证过程失败时仍能诊断、重新解析和恢复。

## 统一 SearchConfig

| 字段 | 语义 | JobsPipe 映射 |
|---|---|---|
| `job_titles` | 想找的职位标题/关键词；至少一项 | `job_title_or` |
| `locations` | 城市或区域文本 | `job_location_or` |
| `country_codes` | ISO 3166-1 alpha-2 国家代码 | `job_country_code_or` |
| `region_codes` | ISO 3166-2 省/州代码 | `region_or` |
| `work_arrangements` | `remote`、`hybrid`、`onsite` | `work_arrangement_or` |
| `seniority_levels` | 用户选择的资历级别 | `job_seniority_or` |
| `posted_within_days` | Provider 声称发布时间的最大天数 | `posted_at_max_age_days` |
| `limit` | 每页/每次请求的期望数量 | `limit`，实际值受 Provider plan 限制 |
| `hard_constraints` | 用户必须满足的产品语义约束 | Phase 1 持久化；尚不扩大为 Provider 私有 filter。 |
| `preferred_constraints` | 用户偏好，用于未来 Matching | Phase 1 持久化，不用于发现淘汰。 |

SearchConfig 永远不保存 JobsPipe API key、cursor 或 Provider 专用原始字段。cursor 属于一次 SearchRun 的 ProviderRequest 历史；API key 属于环境配置。

## Provider 契约

`JobSearchProvider` 是领域 port，提供 `search(config, cursor=None)` 并返回 `ProviderSearchPage`。返回值保留 provider query、完整 raw response、数据项、metadata 与 next cursor。JobsPipeProvider 是该 port 的唯一 Phase 1 实现，内部通过可注入的 HTTP client 使用 `POST https://api.jobspipe.dev/v1/jobs/search` 和 Bearer token。

HTTP adapter 必须将 401、402、429、504 与网络/无效 JSON 问题转成结构化 ProviderError；错误记录可保存 HTTP status、retryable 标记与安全摘要，但不得保存 Authorization header 或 API key。

## 迁移 0002

| 表 | 用途 | Phase 1 关键约束 |
|---|---|---|
| `search_configs` | SearchConfig 的稳定版本化存储 | `id` 主键；version 必须正数。 |
| `search_runs` | 一次完整发现操作 | 保存 config snapshot、trigger、provider、状态、计数、correlation ID。 |
| `provider_requests` | SearchRun 的每次 Provider 请求与分页元数据 | `(search_run_id, request_sequence)` 唯一；请求 payload 脱敏。 |
| `raw_provider_responses` | 每页完整原始 Provider JSON | content hash 与 response metadata；追加留存。 |
| `raw_job_payloads` | 每个职位的单条 raw JSON | 与 raw response 关联；保留 external ID 与 hash。 |
| `canonical_jobs` | 当前仅由一个 source 支持的系统职位实体 | 不以 provider external ID 作主键；current snapshot 可更新。 |
| `job_sources` | Provider identity 和来源时间线 | `UNIQUE(provider, external_id)`；管理 first/last seen。 |
| `job_snapshots` | 可供后续 Matching/Application 消费的不可变规范化版本 | 保留 snapshot JSON、content hash、normalizer version。 |

Phase 1 不做跨 Provider canonical dedup。遇到一个此前未见的 `(provider, external_id)` 时，新建 canonical job 与 job source；再次见到时更新该 source 的 observed fields 并仅在 normalized content hash 改变时写入新 snapshot。迁移仍保留 canonical/source 两层，因此后续加入 TheirStack 时可以增加 candidate/merge decision 表，而不推翻现有主键。

## 标准化规则

normalizer 接收单条 JobsPipe 原始对象，输出 `NormalizedJob`。`id` 和非空 `job_title` 是硬要求，不能安全标准化时记录 ProviderError 并将该 run 设为 `partially_succeeded`。公司名、URL、描述、地点、薪资和资历等可选字段尽力保留。URL 规范化仅删除 fragment 和常见 tracking 参数，不能破坏原始 source URL；完整 Provider JSON 始终保留。时间解析只填入可安全理解的 ISO/常见时间字符串，原始值独立保存。

## 测试策略

单元测试覆盖 SearchConfig 验证、JobsPipe query mapping、HTTP error mapping、job normalization 和 URL/date handling。服务集成测试使用可注入 fake provider，不访问网络，覆盖 SearchRun 成功、分页、部分失败、raw data 落库、同一 provider external ID 的再次发现、snapshot 变化和审计事件。附加 CLI/本地数据库验证不需要真实 API key；缺少 key 的真实 provider 初始化必须报明确的配置错误。
