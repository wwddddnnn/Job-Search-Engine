# Phase 2 Career Foundation 设计说明

## 范围

Phase 2 实现 Career Profile / Experience Library 的**后端基础**：导入简历文档、提取文本、运行抽取草稿、用户逐项核验、发布 ProfileVersion、保存证据链与修订审计。实现仅使用 Python 3.12 标准库与 SQLite，测试使用标准库 `unittest`，不访问网络。

本阶段**明确不含**（已与用户确认的范围边界）：

- **UI / HTTP / CLI 适配层**：核验界面放到 **Phase 2.5**（细节另议）。本阶段只交付 domain + application service + SQLite 实现 + 测试，与 Phase 1 的交付形态一致。
- **真实 LLM provider**：本阶段只定义抽取 port，用 fake 实现跑测试。未配置 provider 时必须报**明确的配置错误**，不允许静默降级，更不允许伪造草稿。真实 provider 接入是 Phase 2 完成后的独立一步。
- Matching、Application、Agent/MCP、浏览器，以及跨用户/多租户。

## 运行管道

```text
ImportResumeDocument (idempotency key)
  → ResumeDocument：原文件受控存储 + content_hash + metadata
  → ResumeTextExtractor（可注入）→ 提取文本留存 → document_texts
  → StartExtractionRun
  → CareerExtractionPort（本阶段默认未配置；测试注入 fake）
  → 草稿 schema 校验 → llm_extraction_runs.output_ref（草稿）
  → ConfirmExperienceFacts（用户逐项 accept / edit / reject / clarify）
  → CareerProfile + ProfileVersion
     + experiences / experience_achievements / skills / experience_skills
     + experience_evidence + revision audit
  → GetCareerProfileSnapshot / GetVerifiedEvidencePack
```

`ImportResumeDocument` 与 `StartExtractionRun` 都先落库再处理：文件、文本、抽取输出都是**先留存后消费**，保证任何一步失败都能诊断、重跑和回放。

## 模块边界

```text
src/job_search_assistant/
├── career/                          # 领域：类型、不变量、状态机、port（不 import sqlite）
│   ├── types.py                     # ResumeDocument / ResumeText / ExtractionRun /
│   │                                # CareerProfile / ProfileVersion / Experience /
│   │                                # ExperienceAchievement / Skill / ExperienceEvidence
│   ├── extraction.py                # 抽取草稿的 schema 校验与草稿项类型
│   ├── ports.py                     # ResumeTextExtractorPort / CareerExtractionPort /
│   │                                # DocumentStoragePort
│   └── store.py                     # CareerStore port
├── app_services/career.py           # ImportResumeDocument / StartExtractionRun /
│                                    # ConfirmExperienceFacts / GetCareerProfileSnapshot /
│                                    # GetVerifiedEvidencePack（事务 + 幂等 + 审计）
└── infrastructure/
    ├── files/document_storage.py     # 受控文件存储（原文件 + 提取文本）
    └── sqlite/career_store.py        # CareerStore 的 SQLite 实现
```

依赖方向沿用 Phase 0：`core` 不被任何领域或 SQLite 反向依赖；`career/` 不得 import `infrastructure/sqlite` 或文件存储的具体实现；adapter/UI 不得直连数据库。

## 状态机（落实 §9.2）

```text
[*] → Imported              原文件与元数据已保存
Imported → TextExtracted    parser 成功
Imported → ExtractionFailed parser 失败                    （终态）
TextExtracted → DraftExtracting   StartExtractionRun
DraftExtracting → DraftReady      模型输出通过 schema 校验
DraftExtracting → DraftFailed     模型/解析错误             （终态）
DraftReady → UnderReview          用户开始核验
UnderReview → ProfileVersionPublished  用户显式确认         （终态）
UnderReview → DraftReady          用户要求修改/重新提取
```

硬性约束：

1. **`DraftReady` 绝不等于已发布。** 只有 `ConfirmExperienceFacts` 能产生新的 `ProfileVersion`。
2. **重跑抽取新建 `llm_extraction_runs` 记录**，绝不覆盖或删除旧 run 与旧输出。
3. **非法状态跃迁必须被拒绝**并返回结构化领域错误（例如 `Imported → DraftReady`、`DraftFailed → ProfileVersionPublished`）。
4. 模型以「推断」补全了原文没有的事实，该项必须标记为需澄清（`needs_clarification`），不得进入 verified evidence。

## 迁移 0004

表名沿用架构文档 §8.2 的 Career Documents & Facts 表组；持久化形态遵循 §8.1（原文件走文件存储，run/输出/审计走表）。

| 表 | 用途 | 关键约束 |
|---|---|---|
| `resume_documents` | 简历原文件与其元数据 | `content_hash` 必须保存；替换文件**新建** document，不覆盖历史 |
| `document_texts` | 每次文本提取的结果 | 关联 document + extractor_version；追加留存；保留原文定位信息 |
| `llm_extraction_runs` | 一次抽取运行 | 记录 `input_hash`、model、prompt_version、schema_version、status、output_ref；追加留存 |
| `career_profiles` | 单用户职业档案 | 保留 owner/tenant 预留字段；`current_version_id` 指向已发布版本 |
| `profile_versions` | 用户确认后的可引用版本 | `(profile_id, version)` 唯一；version 从 1 递增；**不可变** |
| `experiences` | 一段经历 | 关联 profile_version 而非 profile；带 `verification_status` |
| `experience_achievements` | 经历下的一条成果 | 数值/指标必须有来源或用户显式确认 |
| `skills` / `experience_skills` | 归一化技能及其关联 | 归一化只为 deterministic matching 服务，**不抹去原文** |
| `experience_evidence` | 支持事实的最低粒度证据 | 必须有 `source_document_id` + 原文摘录/定位；或 `source_type = user_assertion` + 确认时间 |

迁移本身遵循 Phase 0 既定机制：文件名 `NNNN_description.sql`、一个事务执行、`schema_migrations` 记录版本与 checksum、已应用迁移不可修改（改动 schema 必须新增迁移）。

## 应用服务契约（落实 §7.3）

| 用例 | 输入 | 成功输出 | 约束 |
|---|---|---|---|
| `ImportResumeDocument` | file ref、metadata、idempotency key | document id、parse status | content hash 必须保存；格式/病毒检查在 adapter；同 key 重放返回先前结果，不同 payload 报冲突 |
| `StartExtractionRun` | document id、extractor policy | extraction run id | 输出只能是 draft；记录 model/prompt/schema/input hash；provider 未配置 → 明确配置错误 |
| `ConfirmExperienceFacts` | profile version、draft changes、evidence refs | new profile version | 只有显式确认的项才能为 verified；被拒/待澄清项不得进入新版本 |
| `GetCareerProfileSnapshot` | profile ref/version | snapshot | 默认最小化返回，**不等于**原始简历全文 |
| `GetVerifiedEvidencePack` | profile ref/version、task context | evidence pack | 每项必须带 evidence id、scope、verified state |

所有写用例遵循 Phase 0 规范：创建或接收 `RequestContext` → 校验输入 → 以幂等 scope/key 取得或创建执行记录 → **在一个事务中**改变业务状态并写审计事件 → 标记幂等记录完成 → 返回序列化安全结果。文件读取与 LLM 调用**不得**持有数据库事务锁。

## 事实可信度（落实 §10.3）

| 信息层级 | 可用于 Matching | 可用于 Application 草稿 | 可自动提交 |
|---|---:|---:|---:|
| 原始文档文本 | 否，需经 Career 投影 | 否 | 否 |
| LLM extraction draft | 否，除非经用户确认 | 可作为待审阅建议 | 否 |
| 用户编辑但未确认 | 否 | 可展示供确认 | 否 |
| Verified evidence | 是 | 是 | 仅 claim 映射 + 审阅 + 提交确认均通过时 |
| 用户对特定问题的即时回答 | 通常不回写 Career 事实 | 是 | 仅限该 application 且用户确认范围内 |

硬规则：

1. 原始文本、未确认 draft、未确认编辑**都不得**进入 `VerifiedEvidencePack`。
2. 由多个原文片段组合而成的事实，保留多个 evidence 引用。
3. 用户新补充但无文档来源的事实：记 `source_type = user_assertion` + 确认时间，**不得**伪装成来自简历。
4. `user_verified = true` 的语义是「用户已确认该表述可作为本人真实职业信息使用」，不是「模型抽取得差不多」。

## 受控文件存储

原文件与提取文本落在 `.job-search-assistant/documents/`（与 Phase 1 数据库同根目录，可用参数覆盖），以 `content_hash` 命名；数据库只存引用与 hash，不存全文 blob。遵循 §8.1：结构化主表保存可查询字段，原始输入走文件存储，二者用 hash 关联。

## 测试策略

- **domain 单测**：状态机全部合法跃迁；至少 3 类非法跃迁被拒；evidence 规则（无来源事实必须为 `user_assertion`）；`profile_versions` 递增且不可变。
- **application service 集成测试**（真实 SQLite + fake extractor / fake LLM + 临时目录）：
  - 全链路 `import → text → extract → confirm → pack`
  - parser 失败 → `ExtractionFailed`；schema 校验失败 → `DraftFailed`
  - **未确认的草稿项不得出现在 `VerifiedEvidencePack`**（本阶段最关键的一条）
  - 重跑抽取不覆盖旧 run 与旧输出
  - 幂等键重放返回先前结果、同 key 不同 payload 报冲突
  - 审计事件带 actor、目标与 correlation id
- **迁移测试**：0004 幂等、checksum 保护（沿用 Phase 0 机制）。
- 全部测试**不访问网络**、不调用真实 LLM / MCP / 浏览器。

## 验收标准（Phase 2 退出条件）

1. LLM 输出默认 draft；未经用户确认的事实**不出现**在 `VerifiedEvidencePack` 中。
2. 原文件与提取文本长期留存且可回放；替换文件产生新 document，不覆盖历史。
3. 重跑抽取新建 run；历史 run 与历史输出不被改写或删除。
4. 只有显式确认项为 verified；无文档来源的用户补充记为 `user_assertion`，不伪装成简历来源。
5. 每次确认产生可引用的 `ProfileVersion`，并有 before/after 修订审计可回溯。
6. adapter 层不得绕开 application service 直连数据库。
7. 全部测试通过：

```bash
PYTHONPATH=src /opt/homebrew/Caskroom/miniconda/base/envs/Job-Search-Engine/bin/python -m unittest discover -s tests
```

且不访问网络、LLM、MCP 或浏览器。

## 实现切片（执行顺序）

Phase 2 按以下顺序逐片交付，每片一个功能分支、一轮 builder + reviewer 循环：

| 片 | 范围 | 完成标志 |
|---|---|---|
| **S1** | 迁移 0004 + `career/` 领域类型与 port（types / ports / store / extraction 草稿 schema） | 迁移幂等且受 checksum 保护；领域单测（状态机合法与非法跃迁、evidence 规则）通过 |
| **S2** | `ImportResumeDocument` + 受控文件存储 + `ResumeTextExtractorPort` 的实现骨架 | 原文件与提取文本留存可回放；替换文件产生新 document |
| **S3** | `StartExtractionRun` + `CareerExtractionPort`（本阶段 fake）+ 草稿 schema 校验 | 输出只能是 draft；重跑新建 run 且不覆盖旧输出 |
| **S4** | `ConfirmExperienceFacts` → `ProfileVersion` + 修订审计（复用 `audit_events`） | 只有显式确认项为 verified；before/after 可回溯 |
| **S5** | `GetVerifiedEvidencePack` + `GetCareerProfileSnapshot` | 未确认事实进不了 pack；每项带 evidence id/scope/verified state；快照最小化 |

每片的验收 = 本文档「验收标准」中与之相关的条目 + 上表该片完成标志。

## 后续阶段（不在本阶段内）

- **Phase 2.5**：核验 UI（用户已确认要做，细节待定）。
- **B 步**：接入真实 LLM provider（用户已确认在 Phase 2 完成后进行）。
- **Phase 3**：Job Matching。

## 已确认的设计决策

1. **`RevisionAudit` 复用 Phase 0 的 `audit_events`**，action 走 `career.*` 命名空间，不新建平行审计表 —— 理由：§8.2 把审计集中在 Audit & Operations，平行表会制造两套审计来源。
2. **受控文件存储位置**：`.job-search-assistant/documents/<content_hash>`（与 Phase 1 数据库同根目录，可用参数覆盖）。
3. **本阶段不含 UI**（核验界面 → Phase 2.5）与**不含真实 LLM provider**（→ Phase 2 完成后的 B 步）。两者均由用户确认，不属于本阶段缺口。

