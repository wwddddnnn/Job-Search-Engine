# 本地运行与人工验收手册

本手册是 README「[快速开始](../README.md#快速开始)」的展开版：**怎么跑、数据落在哪、哪些必须人工点**。
各切片的 HTTP 契约、持久化与安全边界在 [Phase 2.5 文档](phase2.5-career-review-ui.md) 的契约节里，
本手册不复述；对应关系：S2a / S2b / S3a 契约分别见该文档的同名小节。

## 1. 本地运行

在仓库根使用项目**既有 Conda 环境**（`dev.env` 的 `PYBIN` 就是它），无需创建新环境，
**不需要 Node.js、不需要前端构建**：

```bash
source dev.env
PYTHONPATH=src "$PYBIN" -m job_search_assistant serve --port 8000 --host 127.0.0.1
```

打开 **http://127.0.0.1:8000/**，在终端按 **Ctrl-C** 停止，正常返回 0。
服务只接受 `127.0.0.1`；端口占用时打印明确错误并返回 1，改用 `--port 8001` 后重新启动。
`--port 0` 可分配空闲端口，终端会打印实际地址。静态资源全部随项目提供，不连接 CDN。

数据库与迁移沿用默认值（见 README），`--database` / `--migrations` 是**全局参数，仍放在 `serve` 之前**。
文档保存在数据库同目录的 `documents/`，导入临时文件位于 `incoming/`，请求结束后清理；
默认均在 `.job-search-assistant/` 内。界面语言、文档和草稿重启后保留。
已有多个档案时，本地单用户界面固定打开创建时间最早的档案；当前没有档案切换功能。

启动后能做什么，看 README 的切片表与各切片契约节；**S3a 的真实 LLM 调用未接入**（S3b / S4 尚未开始）。
Markdown 支持标题、段落、列表、强调、链接和围栏代码块子集，不承诺完整 CommonMark；
源码视图显示完整原文，界面语言不改变简历内容。

## 2. 人工验收

用独立数据库与自备 `.md` 演练，演练文字不代表用户已确认事实。契约细节见
[Phase 2.5 文档](phase2.5-career-review-ui.md)。

### S2a 人工验收

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

### S2b 人工验收

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

各状态下 composer 的可编辑、可聚焦与收起规则见
[Phase 2.5 文档的 S2b 契约](phase2.5-career-review-ui.md#s2b-契约已交付并通过审查)；
冲突恢复的取舍（复制后刷新）见同文档「冲突恢复取舍：选择 B」。

### S3a 人工验收

S3 已拆为 S3a（配置与凭据）和 S3b（引用 / prompt、Mock 优化、替换 / 撤销）。
本轮仅交付 S3a；**配置入口可用，真实调用未接入**。

1. 使用上述命令与独立数据库启动，在「LLM API 配置」新增两套不同的名称、API 地址、模型。
   一套填写 Key，一套留空。检查列表分别显示固定掩码和「未设置 Key」，不显示 Key 片段。
2. 编辑已有配置的名称、地址、模型，直接保存；Key 应保持已设置。点击「重新设置」才能
   输入新 Key；该输入框留空保存表示清除 Key。再次填写保存后应恢复固定掩码。
3. 轮流「选用」两套配置，检查「当前配置」标记。刷新、停止服务并以同一数据库重启，
   检查配置、当前选择和 Key 已设置状态仍在（这不等于验证了 Key 的有效性）。
4. 删除带 Key 的配置，检查列表与对应秘密文件均消失；删除当前配置后当前选择为空。
5. 切换中英文，检查表单、错误提示、掩码和「重新设置」；普通模式应明确显示
   「真实调用未接入。本阶段保存配置不发起真实请求，也不验证连通性」。
6. 保存失败可「重试原请求」（沿用相同幂等键）或「放弃重试」；失败请求只暂存在内存，
   不写浏览器存储。刷新会丢弃未完成输入；会话失效需刷新后重新填写。

## 3. 自动测试的覆盖与边界

自动测试证明不了浏览器行为：JS 逻辑与 view 分支测试跑在 macOS 系统 JavaScriptCore 上，
**非 darwin 平台会静默 skip**（unittest 计入 skipped），DOM 夹具也不模拟真实浏览器布局、
剪贴板和输入法，所以上面三项必须人工点。测试不访问外部网络，HTTP 验收用标准库真实 loopback
端口（`ThreadingHTTPServer(("127.0.0.1", 0))` + `urllib.request`）；运行环境必须允许绑定
`127.0.0.1:0`，限制 socket 的沙箱无法完成这部分验收，不能据此标记通过。
各切片新增了哪些断言，见 [Phase 2.5 文档](phase2.5-career-review-ui.md) 各契约节的验证说明。

## 4. 手工装配调用（Career 应用服务）

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
它同时演示了 README 每条不变量是怎么被断言的。
