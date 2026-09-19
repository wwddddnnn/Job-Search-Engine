# Job Search Assistant

本地优先的个人求职助手，**模块化单体**：Python 3.12 标准库 + SQLite，**运行时零第三方依赖**——
不需要 `pip install` 任何包，不需要 Node.js，不需要前端构建。目标是让「找职位 → 核验职业事实 →
匹配 → 申请准备」这条链路全部本地可审计、可回放，并让 LLM 只在受控、可撤销的位置参与。

## 核心不变量

1. **LLM 输出一律是草稿**；未经用户显式确认的事实不会进入已发布版本，也不会出现在 `VerifiedEvidencePack` 里。
2. 只有**显式确认项**为 `verified`；无文档来源的用户补充记为 `user_assertion`，不伪装成简历来源。
3. 原文件与提取文本**长期留存且可回放**；替换文件产生**新** document，不覆盖历史。
4. 重跑抽取**新建** run；历史 run 与其输出不被改写或删除。
5. 每次确认产生可引用的 `ProfileVersion`，并有 before/after 修订审计可回溯。
6. **所有写路径必须经 Application Service**（携带 `RequestContext` + idempotency key，由服务层写审计）；
   adapter 不得直连领域表。

## 现在开发到哪了

**当前在 Phase 2.5「人工整理与核验 UI」，S1、S2a 与 S2b 已通过审查，
S3 / S4 待开始。**

| 阶段 | 内容 | 状态 |
|---|---|---|
| **Phase 0 基础层** | 模块边界、校验和保护的 SQLite 迁移、错误模型、RequestContext、幂等、审计 | ✅ 已验收 |
| **Phase 1 Job Discovery** | SearchConfig 持久化、JobsPipe Provider 适配、分页搜索运行、原始响应留存、规范化、同 source 去重、不可变 job snapshot | ✅ 已验收 |
| **Phase 2 Career Foundation** | 迁移 0004 + `career/` 领域层、受控文件存储、纯文本提取器、导入 / 抽取 / 确认 / 证据包 / 快照五个应用服务 | ✅ S1–S5 全部验收 |
| **Phase 2.5 人工整理与核验 UI** | 多份 Markdown、中英文界面、自动保存 / 部分发布、多套 API 配置与 Mock 优化 / 合并 | 🚧 S1、S2a、S2b 已通过审查 / S3、S4 待开始 |
| **B 步 真实 LLM** | 接入真实 provider（当前只有确定性假 provider） | ⏳ 待开始 |
| **Phase 3 Job Matching** | MatchingPolicy、hard filter、shortlist、MatchRun / Result、失效重算 | ⏳ 未开始 |
| **Phase 4–7** | Application Core、Agent-ready Contracts、MCP Adapter、受控浏览器投递 | ⏳ 未开始 |

分支约定：**每个切片一个功能分支、一轮 builder + reviewer 循环、每轮一次提交**。
`main` 上的可运行基线是 **Phase 2 全部收尾（S1–S5）**；Phase 2.5 的实现分别落在 `phase2.5/*`
功能分支上，尚未合并回 `main`。

## 快速开始（Easy start）

### 0. 前置

- Git；Python **3.12 或更高**（`pyproject.toml` 里 `requires-python = ">=3.12"`）。
- **不需要装任何依赖**：运行时零第三方依赖，测试用标准库 `unittest`，前端是原生模块化 JS。
- 不需要 Node.js、不需要前端构建、不需要数据库服务（SQLite 就在仓库目录下的文件里）。

### 1. 取得代码

```bash
git clone https://github.com/wwddddnnn/Job-Search-Engine.git
cd Job-Search-Engine
```

想跑某个还在飞的切片，就 `git checkout phase2.5/s2a-web-entry`（切之前先确认该分支已推送到远端）。

### 2. 准备解释器

任意 3.12+ 的解释器都能跑；本机习惯用 conda：

```bash
conda create -n Job-Search-Engine python=3.12 -y --solver classic
# 本机 conda 的 libmamba 求解器插件是坏的：必须显式 --solver classic。
```

然后把解释器路径写进仓库根 **`dev.env` 的 `PYBIN=` 一行**：

```bash
PYBIN=/path/to/python3.12
```

> **换机器时这一步必须做。** `dev.env` 里记录的是原机器的 conda 绝对路径，而
> `codex-hermes-loop.sh` 和本项目约定的验收测试命令都读它——不改就会跑到不存在或版本不符的解释器。
> 同名环境变量优先级更高，也可以临时覆盖：`PYBIN=/path/to/python ./codex-hermes-loop.sh "任务"`。

（可选）想直接用 `job-search-assistant` 这个命令名，而不是 `python -m job_search_assistant`：
`pip install -e .` —— 项目没有第三方依赖，这一步不会从网上拉包。

### 3. 初始化数据库并跑测试

```bash
PYTHONPATH=src python -m job_search_assistant init-db
PYTHONPATH=src python -m job_search_assistant db-info
PYTHONPATH=src python -m unittest discover -s tests
```

用 `dev.env` 锁定的解释器（更稳妥，也是 CI/审查轮的口径）：

```bash
source dev.env
PYTHONPATH=src "$PYBIN" -m unittest discover -s tests
```

- 默认数据库是 `.job-search-assistant/job-search-assistant.sqlite`（在 `.gitignore` 里，删掉整个
  `.job-search-assistant/` 目录即可重置）。
- `--database` 与 `--migrations` 是**全局参数，必须写在子命令之前**：

```bash
PYTHONPATH=src python -m job_search_assistant --database /tmp/x.sqlite init-db   # ✅
```

- 测试**不访问外部网络、LLM、MCP 或浏览器**；HTTP 测试仅使用本机 loopback。
- 取 `dev.env` 里的路径时别用 `grep PYBIN dev.env`（注释里也有 `PYBIN=`），要用 `grep '^PYBIN=' dev.env`。

### 4. 本地启动（S2a / S2b）

在仓库根使用项目**既有 Conda 环境**，无需创建新环境，**不需要 Node.js、不需要前端构建**：

```bash
PYTHONPATH=src /opt/homebrew/Caskroom/miniconda/base/envs/Job-Search-Engine/bin/python -m job_search_assistant serve --port 8000 --host 127.0.0.1
```

打开 **http://127.0.0.1:8000/**，在终端按 **Ctrl-C** 停止，正常返回 0。
也可先 `source dev.env`，再运行 `PYTHONPATH=src "$PYBIN" -m job_search_assistant serve`。
服务只接受 `127.0.0.1`；端口占用时打印明确错误并返回 1，改用 `--port 8001` 后重新启动。
`--port 0` 可分配空闲端口，终端会打印实际地址。静态资源全部随项目提供，不连接 CDN。

数据库与迁移沿用上文默认值，`--database` / `--migrations` 仍放在 `serve` 之前。
文档保存在数据库同目录的 `documents/`，导入临时文件位于 `incoming/`，请求结束后清理；
默认均在 `.job-search-assistant/` 内。界面语言、文档和草稿重启后保留。
已有多个档案时，本地单用户界面固定打开创建时间最早的档案；此切片没有档案切换功能。

S2a 支持 Markdown 导入、多文档只读浏览、中英文、渲染/源码切换、打开或创建审核草稿。
S2b 已交付选区建条目、编辑、确认、拒绝、待澄清、删除、撤销、自动保存、预览发布与历史版本。
S2b 的审查修补已通过并提交；S3 / S4 尚未开始。
Markdown 支持标题、段落、列表、强调、链接和围栏代码块子集，不承诺完整 CommonMark；
源码视图显示完整原文，界面语言不改变简历内容。

#### S2a 人工验收

1. 用新的独立数据库启动（例如全局参数 `--database /tmp/jsa-s2a-check/app.sqlite`），
   不清空已有 `.job-search-assistant/` 或演练库。首页应同时显示「还没有文档」和
   「尚无已发布档案」，可继续导入；不能显示数据损坏或失败。
2. 点击「打开审核草稿」，应显示草稿已保存与版本号，仍显示未发布档案的正常空状态。
   导入两份不同的 UTF-8 `.md`，点击列表来回切换，检查文件名、时间、状态与正文对应。
3. 切换中英文，文案立即切换，原文不翻译；切换渲染/源码，检查标题、列表、强调、链接、
   代码块，以及源码的空行、空格和完整内容。切语言不重建正文，已有正文选区应保留。
4. 切换到英文后 Ctrl-C 停止，再用同一数据库启动；语言、文档列表和审核草稿应恢复。
   刷新后可重新选择任一文档；当前文档与渲染/源码选项仅为会话状态。
5. 导入包含下列内容的 `injection-check.md`（仅用于本地检查）：

   ````markdown
   # Injection check
   <script>alert('script')</script>
   [unsafe](javascript:alert(1))
   <img src=x onerror="alert('onerror')">
   [safe](https://example.com)
   **强调** 和 *斜体*
   - 列表
   ```text
   <script>alert('code')</script>
   ```
   ````

   渲染视图里原始 HTML 应显示为文字；内联 `<script>`、`javascript:` 链接和 `onerror=`
   属性都不应生效，不弹窗、不生成原文指定的图片、不执行脚本。源码视图应完整保留上述文本。
   安全链接允许 `http` / `https` / `mailto`；其余协议不产生可点击链接。
6. 导入非 `.md`、非 UTF-8 文件或超限文件应出现相应语言的错误提示；导入失败保留待重试内容。
   刷新前请先确认服务端已返回导入成功。服务重启后旧页面写请求会被 token 校验拒绝，刷新即可。

#### S2b 人工验收

使用独立演练库与自备 `.md`；演练文字不代表用户已确认事实。

1. 无草稿时显示「尚未创建审核草稿」；打开空草稿后出现手工填写引导和「新建经历」，
   无已发布版本时仍显示「尚无已发布档案」。分别切换中英文检查。
2. **必须人工点：双向选区**。在渲染与源码视图分别从前往后、从后往前选择原文，
   包含 emoji、重复段落、跨行文本，创建经历/成果/技能，检查引用原文与位置正确。
3. **必须人工点：输入法组合输入**。使用中文输入法持续输入、选词、提交，检查光标与
   组合文字不被自动保存回包打断；保存完成后刷新核对最终文字。
4. **必须人工点：粘贴大段文本**。在描述字段粘贴多段长文本，继续输入并等待已保存，
   检查光标、段落、完整文本及刷新后的内容。以上三项无法由当前无浏览器的自动测试验证。
5. 分别确认、拒绝、标记待澄清；确认经历不自动确认子项。编辑已确认条目后应显示
   「待核验」，重新确认后才进入发布变更。删除并撤销，检查仍需重新确认。
6. 有未保存 composer 或待保存编辑时不能预览/发布。确认部分条目后预览实际发布内容与
   新增/修改/删除摘要，再确认发布；未确认草稿保留。再发布一版，点击旧版本查看旧快照，
   当前档案不改变。重启后核对草稿与版本历史。
7. 用两个标签页打开同一草稿：一页保存后，在旧页创建或编辑以触发冲突。旧页文字仍可
   聚焦选中复制，composer 为只读，不显示无效的原样重试按钮。**先复制所有未保存字段**，
   再刷新页面，对照最新草稿重新粘贴、保存、确认并发布。刷新不会自动保留未保存文字。
8. 暂停服务制造保存/发布失败后恢复服务（若重启导致 token 更换，需复制后刷新）；
   检查网络失败、认证失败的输入仍可选中复制，取消收起后可再次展开且文字保留。
   网络失败保留输入，重试沿用同一幂等键。不要把冲突恢复与网络失败的原样重试混同。


composer 在冲突、认证失败、网络失败、保存中或待保存时只读，仍可聚焦、选中和复制；
校验失败仍允许编辑改错。发布期间输入禁用，已删除条目输入仍禁用。
冲突或保存失败时可点「取消」收起 composer，输入与保存队列保留；点「展开未保存条目」
可再次展开，收起不会解除预览/发布门控。发布期间不能收起。

#### S2a HTTP 契约

- `GET /api/session` 返回 `{token, language, profile}`，无档案时 `profile=null`。
- `GET /api/documents` 返回 `{id, filename, imported_at, status}` 列表；
  `GET /api/documents/{id}` 在相同字段外返回 `content` 原文。
- `POST /api/documents` 接受 `{filename, content, idempotency_key}`，返回
  `{document_id, status}`。同键同输入重放，相同键不同输入返回 409。
- `GET /api/profile` 返回快照；尚无档案时为 `null`，已创建但未发布时
  `version=0`、`profile_version_id=null`，不将存储故障吞成空档案。
- `GET /api/draft` 返回草稿或 `null`；`POST /api/draft` 接受可选的
  `{display_name, idempotency_key}`，仅打开或创建草稿。
- `GET /api/settings/ui` 返回 `{language}`；`PUT` 接受 `{language, idempotency_key?}`。
  语言仅限 `zh` / `en`。草稿创建和设置写入未提供幂等键时，由 adapter 生成请求级键。
- 写请求带 `X-JSA-Token`，所有请求校验 `Host`。请求体为 JSON，最大 **2 MiB（含 JSON 开销）**；
  未知字段拒绝。错误只返回 `{error: {code, message, correlation_id}}`，页面按 code 翻译。

#### S2b HTTP 契约

- `GET /api/profile/versions` 返回 `{id, version, created_at}` 列表，最新版本在前。
- `GET /api/profile/versions/<id>` 返回指定历史档案快照；读取不修改当前档案。
- `POST /api/review/<action>`：action 为 `create` / `edit` / `decide` / `delete` /
  `restore` / `preview` / `publish`。共有 `draft_id`、`expected_version`；除只读 preview
  外必须提供 `idempotency_key`。业务参数及返回值见
  [S2b 契约](docs/phase2.5-career-review-ui.md#s2b-契约已交付并通过审查)。
- preview 接受 `base_version_id`，返回 `{draft_id, draft_version, base_version_id,
  summary, content}`；`content` 是本次实际发布内容的 `{kind, fields}` 列表，包含保留的旧事实，
  `summary` 为新增/修改/删除列表。publish 仍须单独调用并再次校验版本。
- 选区参数为 `{document_id, start, end}`：Unicode 码点、从 0 起、左闭右开；后端生成
  `codepoint:start:end` locator。浏览器 UTF-16 下标先转换为码点，不是字节下标。
  历史证据行保留原来的 `offset:…` 形式，不做迁移或清洗；新写入统一用 `codepoint:start:end`。
- 超过 2 MiB、且声明长度不超过 4 MiB 的请求体，最多用 1 秒分块读取丢弃后返回 JSON 413。
  更大的声明或未及时传完的请求会关闭连接，客户端可能仅收到断连；2 MiB 接受上限不变。
- 新端点沿用 token、Host、JSON 和 2 MiB 守卫。review 顶层非对象返回 422
  `validation_error`；非法 JSON 仍返回 400。409 冲突按上面的复制、刷新、重新保存流程恢复。

编辑器 JS 逻辑与 view 分支测试依赖 macOS 系统 JavaScriptCore；**非 darwin 平台会静默 skip**
（unittest 计入 skipped），不能据整体测试为绿就认定执行了 JS 断言。DOM 夹具不模拟真实
浏览器布局、剪贴板或输入法，必须完成上面的三项人工验收。

自动测试用标准库真实 loopback HTTP 端口与 `urllib.request`，不访问外部网络。
运行环境必须允许绑定 `127.0.0.1:0`；限制 socket 的沙箱无法完成这部分验收，不能据此标记通过。

## 每个阶段的 DoD（完成定义）

总表来自架构文档 §11.1；每个阶段更细的验收条目和各切片完成标志在对应阶段文档里（下表最后一列）。

| 阶段 | 目标 | DoD（可验证的完成定义） |
|---|---|---|
| **Phase 0** Foundation | 本地工程骨架与架构护栏 | `migrate()` 后基础表存在且重跑无副作用；改动已执行迁移必须报错（checksum 保护）；相同 `scope/key/payload` 返回先前结果或阻止并发重复，相同 key 不同 payload 必须报冲突；每条审计带 action / target / actor / source / correlation ID；基础测试全过，且不需要外部 API、LLM、MCP、浏览器 |
| **Phase 1** Job Discovery | 可靠地发现、保存、查看职位 | 同一 Provider 职位不重复插入（`provider + external_id` 幂等 upsert，重发现产生新 snapshot）；每页原始响应先落库、可回放；无法安全规范化的记录产生 ProviderError 且 run 记 `partially_succeeded`；`SearchRunService` 是唯一编排入口，UI 不感知 JobsPipe 字段；缺 API key 的真实 provider 必须报明确配置错误，不静默降级 |
| **Phase 2** Career Foundation | 可核验的个人职业知识库 | 见下方「Phase 2 退出条件」7 条 |
| **Phase 2.5** 人工整理与核验 UI | 用户手工整理、编辑并发布职业事实 | 见下方「Phase 2.5 验收标准」11 条 |
| **Phase 3** Job Matching | 低成本、可解释、可失效的匹配结果 | 同版本输入优先命中缓存；每条解释显示真实 evidence 与 uncertainty；profile / job / policy 变更后旧结果标记 stale、走版本化重算而非改写旧结果 |
| **Phase 4** Application Core | 无 Agent 也能完整管理申请准备与追踪 | 用户可手工创建并追踪申请；不受 evidence 支持的 claim 无法被保存为可提交内容 |
| **Phase 5** Agent-ready Contracts | 让选定 runtime 安全消费后端能力 | 用一个受控 Agent 完成「检索 → 匹配 → 准备审阅包」，无浏览器、无提交 |
| **Phase 6** MCP Adapter（按需） | 多外部 Agent Host 的标准互操作 | 至少两个目标 Host 能稳定完成只读与准备 workflow；无 tool 可直连数据库 |
| **Phase 7** 受控浏览器辅助申请 | 人工监督下预填与提交 | 用户可在登录 / CAPTCHA / 未知题处接管；无 confirmation token 不可提交；失败不会误标为 Submitted |

**Phase 4–7 的统一准入条件**（架构文档 §11.3）：Application 状态机先在没有 Agent 的情况下稳定运行；
每个答案草稿有 claim→evidence 校验；review packet 与 approval token 已实现；execution event / audit
schema 已落地；无 token 的 submit command 在集成测试中被拒绝；用户能用 UI 完成全部手工 fallback。

### Phase 0 验收标准

在新机器上，以给定数据库路径调用 `migrate()` 后三张基础表存在，且迁移再次运行无副作用；修改已执行
迁移的内容后必须报错；相同 `scope/key/payload` 返回先前完成结果或阻止并发重复执行，相同
`scope/key` 但不同 payload 必须报冲突；每项审计记录携带明确动作、目标、actor、source、correlation ID；
所有基础测试通过，且无需外部 API、LLM、MCP 或浏览器。

### Phase 1 验收标准

单元测试覆盖 SearchConfig 校验、JobsPipe query 映射、HTTP 错误映射、职位规范化与 URL / 日期处理；
服务集成测试用可注入的 fake provider，不访问网络，覆盖 SearchRun 成功、分页、部分失败、raw 落库、
同一 `provider + external_id` 的再次发现、snapshot 变化与审计事件；附加 CLI / 本地库验证不需要真实
API key。详见 [docs/phase1-job-discovery.md](docs/phase1-job-discovery.md)。

### Phase 2 退出条件

1. LLM 输出默认 draft；未经用户确认的事实**不出现**在 `VerifiedEvidencePack` 中。
2. 原文件与提取文本长期留存且可回放；替换文件产生新 document，不覆盖历史。
3. 重跑抽取新建 run；历史 run 与历史输出不被改写或删除。
4. 只有显式确认项为 verified；无文档来源的用户补充记为 `user_assertion`，不伪装成简历来源。
5. 每次确认产生可引用的 `ProfileVersion`，并有 before/after 修订审计可回溯。
6. adapter 层不得绕开 application service 直连数据库。
7. 全部测试通过（`unittest`，不访问网络 / LLM / MCP / 浏览器）。

分片交付（每片一个功能分支、一轮循环）：

| 片 | 范围 | 完成标志 |
|---|---|---|
| S1 | 迁移 0004 + `career/` 领域类型与 port | 迁移幂等且受 checksum 保护；状态机合法 / 非法跃迁与 evidence 规则单测通过 |
| S2 | `ImportResumeDocument` + 受控文件存储 + 文本提取 port 骨架 | 原文件与提取文本留存可回放；替换文件产生新 document |
| S3 | `StartExtractionRun` + 抽取 port（本阶段 fake）+ 草稿 schema 校验 | 输出只能是 draft；重跑新建 run 且不覆盖旧输出 |
| S4 | `ConfirmExperienceFacts` → `ProfileVersion` + 修订审计 | 只有显式确认项为 verified；before/after 可回溯 |
| S5 | `GetVerifiedEvidencePack` + `GetCareerProfileSnapshot` | 未确认事实进不了 pack；每项带 evidence id / scope / verified state；快照最小化 |

> S4 口径：`ConfirmExperienceFacts` 只支持「接受 + 子项勾选」，不支持改写草稿值——编辑覆盖属于
> Phase 2.5 的核验 UI，不是 Phase 2 的缺口。

### Phase 2.5 验收标准

1. README 提供实际可执行的单个 Python 启动命令、本地访问地址和停止方法；使用既有环境，不要求用户
   自行装配服务对象、安装 Node.js 或执行前端构建；页面资源全部本地提供，不连 CDN。
2. 用户可切换中英文界面，简历原文语言不变。
3. 可导入多份 `.md`，查看渲染 / 源码，选中文字创建条目或直接手工添加；没有「关联为证据」操作。
4. 经历、成果、技能可编辑、核验、删除；整理与核验进度自动保存，刷新 / 重启后恢复。
5. 部分发布生成新版本，保留未修改的旧事实与尚未发布的草稿；删除 / 修改不改变历史版本。
6. 手工补充和修改具备后台来源 / 修订记录，未确认内容不进入可信事实包。
7. 可增删改多套 API 配置并切换；Key 本地保存、重启可用、界面掩码，日志与读取响应不泄露 Key。
8. Mock 模式支持选区 / 整条引用、自定义 prompt、建议与改写结果、一键替换及撤销；普通模式明确说明
   真实调用未接入。
9. 用户可手工处理重复经历；显式启动 Mock 合并会自动写入草稿、可撤销，仍需人工发布。
10. 测试覆盖自动保存顺序 / 失败、幂等重试、版本冲突、部分发布、旧结果覆盖防护与历史保留；自动测试
    不访问外部网络，本地 HTTP 验证用标准库 `ThreadingHTTPServer(("127.0.0.1", 0))` 起真实端口 +
    `urllib.request`，覆盖错误码映射、缺失 / 错误 `X-JSA-Token`、错误 `Host`、路径穿越、非法 JSON、
    超限请求体、未知字段。
11. 提供独立演练数据与人工验收步骤，演练数据不冒充用户已确认的职业事实；本阶段不要求 PDF / DOC 或
    真实 LLM 调用通过验收。

分片交付：

| 片 | 内容 | 可人工检查的结果 |
|---|---|---|
| S1 ✅ | 审核草稿服务、来源保留、自动保存、部分发布与版本语义 | 测试验证重启恢复、未确认事实隔离、历史不可变 |
| S2a ✅ | 启动入口（单命令 + README 三条信息）、JSON HTTP adapter（错误码映射、token + Host 校验、静态资源安全）、原生前端骨架、中英文切换、Markdown 导入与多文档切换、只读「渲染 / 源码」双视图、空状态文案 | 一条命令起服务 → 浏览器导入自己的 `.md` → 切换中英文 → 切换渲染 / 源码 → 重启后仍在 |
| S2b ✅ 已通过审查 | 双栏编辑：选区创建条目（带 locator）、编辑 / 核验 / 拒绝 / 待澄清 / 删除 / 撤销、自动保存状态机、发布与版本冲突提示 | 用户用自己的 `.md` 完成手工整理与发布 |
| S3 ⏳ | 多配置管理、引用 / prompt 面板、Mock 优化及一键替换 | 不需要 Key 即可演练结果应用与撤销 |
| S4 ⏳ | 多文档补充已有经历、Mock 自动合并、完整人工验收说明 | 多份简历共同形成职业档案，可测试合并和撤销 |

## 开发方式：builder + reviewer 双 agent 循环

本仓库不是「一个人写、另一个人看」，而是**两个互相独立的 agent 在同一个 checkout、同一条功能分支上
对打**：一个只负责写（builder），一个只负责审（reviewer），编排脚本 `codex-hermes-loop.sh` 负责把两者
串成循环并把每轮结果落进 `DEVELOPMENT_LOG.md`。

```text
功能分支
  └─ 第 N 轮
       ├─ builder  按任务书写代码（默认 Codex）。前置：脚本注入 docs/builder-conventions.md
       ├─ 收改动   git add -A + git diff --cached（含新增文件；DEVELOPMENT_LOG.md 除外）
       ├─ 测试门   跑 unittest —— 不通过就没有 PASS 可谈
       ├─ reviewer 拿「架构 / 阶段文档 + 本轮 diff + 测试结果」审查，输出 STATUS / COMMIT_MSG / LOG_NOTE
       ├─ 追加 DEVELOPMENT_LOG.md
       └─ commit + push（无论通过与否都提交，保证随时可回滚）
```

| STATUS | 含义 | 脚本行为 |
|---|---|---|
| `PASS` | 未发现违反不变量 / 验收标准的问题 | 提交推送，退出码 0 |
| `NEEDS_FIX` | 有明确、可指出修改方向的问题 | 提交推送，把意见带回 builder 进下一轮（上限 `MAX_ATTEMPTS`，默认 3 轮） |
| `ESCALATE` | 方向存疑 / 不可回滚决策 / 同一问题反复 | 提交推送，退出码 2，停下等人 |

另外两种自动停下（退出码 2）的情况：跑满 `MAX_ATTEMPTS` 仍未通过；reviewer 输出里读不到合法 `STATUS`。
测试门与 reviewer 结论冲突时（reviewer 说 PASS 但测试没过）自动降级为 `NEEDS_FIX`，并在日志里标原因。

用法：

```bash
git checkout -b phase2.5/s2b-editing          # 脚本拒绝在 main/master 上直接跑
./codex-hermes-loop.sh "实现双栏编辑与自动保存状态机"
```

### 为什么这样切分

- **审查方的独立性靠能力削减来保证，而不是靠提醒。** reviewer 用
  `hermes chat -Q -t vision --ignore-rules -c <REVIEWER_SESSION> --create-if-missing --query-file <prompt>`
  调用：`-t vision` 只注入一个工具，**没有**写文件、bash、读文件能力，比「让模型自觉只读」更可靠；
  `--ignore-rules` 让它不吸收本机记忆 / `AGENTS.md`，只依据架构文档与 diff 判断；`-Q` 让输出只剩最终回答，
  避免 prompt 回显与 STATUS 解析撞车。
- **两方必须看同一份文件状态。** builder 与 reviewer 原地读写同一个 `~/code/Python/Job-Search-Engine`：
  禁止 `git worktree`、禁止为开发再 clone 一份、禁止改完拷回来。否则 reviewer 审的 diff 与 builder 改的
  文件不是同一份，去重 / 合并这类结论无法复核。**换机器后这条也一样**：两边都指向本仓库这一份 checkout。
- **每轮各自提交，reviewer 看到的 diff 只含当轮改动**（不是它漏看历史）。
- **理由与代价**：双 agent 能拦住「测试过了但违反不变量」这类问题（实测 S2–S5 期间每片都有
  NEEDS_FIX 收口轮），代价是每轮多一次审查调用与一次提交；所以 builder 侧有硬性的效率约定
  （见 `docs/builder-conventions.md`：合并读取、先扫后读、合并验证、不重复确认——逐文件 `cat` 是本项目
  最大的 token 开销来源）。

### 关键文件与可调项

| 文件 / 变量 | 作用 |
|---|---|
| `codex-hermes-loop.sh` | 编排脚本（builder / reviewer 可插拔：`BUILDER=codex\|hermes\|cmd`、`REVIEWER=hermes\|opencode`） |
| `.opencode/prompts/reviewer.md` | reviewer 的 system prompt（只读 agent 定义在 `opencode.json`） |
| `docs/builder-conventions.md` | builder 必须遵守的约定，每轮由脚本注入 builder 的 prompt |
| `DEVELOPMENT_LOG.md` | 每轮时间 / 轮次 / 审查结论 / 测试结果 / 说明，由脚本写入 |
| `REVIEWER_SESSION` | reviewer 复用的**具名会话**（默认 `jse-reviewer`）。上下文由 Hermes 自动压缩兜底；**换个名字即重置上下文**：`REVIEWER_SESSION=jse-reviewer-2026q4 ./codex-hermes-loop.sh "…"` |
| `FILE_REVIEWER_SESSION` | reviewer 会话归属补齐（写 Hermes `state.db`）；`=0` 关闭，关掉后需手动核对会话归属 |
| `ARCH_DOCS` | 喂给 reviewer 的架构 / 阶段文档（默认 5 份；路径含空格时用换行分隔） |
| `MAX_CONTEXT_KB` | reviewer 输入上限（默认 300KB），超限时按文件裁剪 diff 并列出未包含的文件 |
| `TEST_CMD` / `PYBIN` | 验收测试命令与解释器（`PYBIN` 默认读 `dev.env`） |
| `KEEP_RUNS=1` / `SKIP_TESTS=1` / `DRY_RUN=1` | 保留本轮中间产物 / 跳过测试门（不建议）/ 演练不提交不推送 |
| `scripts/codex-stream-filter.py` | 把 `codex exec --json` 的事件流实时渲染成人可读行（不额外消耗 token） |
| `scripts/cap-diff.py` | 按字节预算**按文件**裁剪 diff，并列出被舍弃的文件名 |

完整差异清单（相对导入的原始套件）与本地适配的实证记录见
[docs/hermes-reviewer-kit.md](docs/hermes-reviewer-kit.md)。

## 项目结构

```text
src/job_search_assistant/
├── app_services/       # 跨领域应用服务与启动装配（build_foundation、build_local_ui）
├── adapters/web/       # Phase 2.5 的本地 HTTP 入口与静态页面（页面资源在 static/）
├── career/             # Career / Experience Library 领域（Phase 2 起）
├── core/               # 错误、请求上下文、幂等与审计抽象
├── discovery/          # Job Discovery 领域类型、Provider 与持久化 port
├── infrastructure/     # SQLite 与受控文件存储实现
├── applications/       # Application 领域（后续阶段）
└── matching/           # Job Matching 领域（后续阶段）

migrations/             # 有序、校验和保护的 SQLite 迁移（新增迁移，绝不改旧文件）
tests/                  # 标准库 unittest 测试（HTTP 仅访问本机 loopback）
docs/                   # 架构与各阶段设计说明
scripts/                # 开发辅助脚本
codex-hermes-loop.sh    # builder + reviewer 循环编排
```

依赖方向：`core` ← 领域模块 ← `app_services` ← adapter。领域代码不 import SQLite 具体实现，
HTTP / UI adapter 不直连数据库、也不调用 store 内部写接口。

## Career 应用服务怎么装配

Career 目前只有应用服务与 adapter 层，没有 CLI 业务命令；直接调用时自行装配：

```python
from job_search_assistant.app_services import ImportResumeDocument, build_foundation
from job_search_assistant.infrastructure.files import (
    FileSystemDocumentStorage, PlainTextResumeExtractor,
)
from job_search_assistant.infrastructure.sqlite import SQLiteCareerStore

foundation = build_foundation(
    database_path=".job-search-assistant/job-search-assistant.sqlite",
    migrations_path="migrations",
)
store = SQLiteCareerStore(foundation.database)
storage = FileSystemDocumentStorage(".job-search-assistant/documents")
service = ImportResumeDocument(
    store=store, storage=storage, text_extractor=PlainTextResumeExtractor(storage),
)
```

完整链路（导入 → 抽取 → 确认 → 证据包 / 快照）的调用顺序见 `tests/test_career_snapshot.py`，
它同时演示了上面每条不变量是怎么被断言的。

## 文档

- [docs/Job Search Assistant 技术架构文档.md](docs/Job%20Search%20Assistant%20技术架构文档.md)
  — 总架构：§5 不变量、§6 模块边界、§9 状态机、§11 分阶段计划与 DoD，**冲突时以它为准**
- [docs/phase0-foundation.md](docs/phase0-foundation.md) — Phase 0 设计说明与验收标准
- [docs/phase1-job-discovery.md](docs/phase1-job-discovery.md) — Phase 1 范围、管道、测试策略
- [docs/phase2-career-foundation.md](docs/phase2-career-foundation.md) — Phase 2 设计说明、退出条件、
  切片表与各切片审查遗留项
- [docs/phase2.5-career-review-ui.md](docs/phase2.5-career-review-ui.md) — Phase 2.5 交互、LLM 配置与
  Mock 范围、版本规则、人工验收标准与 S2a / S2b 契约
- [docs/builder-conventions.md](docs/builder-conventions.md) — builder 的硬性约束、效率约定、自测与提交规则
- [docs/hermes-reviewer-kit.md](docs/hermes-reviewer-kit.md) — 双 agent 套件的本地适配说明与实证记录
- `DEVELOPMENT_LOG.md` — 每轮 builder / reviewer 的结果与提交信息
