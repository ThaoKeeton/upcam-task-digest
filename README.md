# upcam-task-digest

UPCAM 日报格式模板、企业 v3 只读采集器、确定性日报及离线回归测试。
默认使用虚构 fixtures；显式 `--collect` 使用已验证的 v3 `q` 更新时间筛选。
没有钉钉 HTTP 发送器和定时任务，不会退回无筛选的全项目历史扫描。
当前最近更新时间筛选已依据交接的独立只读验证适配；未识别事件、评论语义及持久状态存储仍需复核，不能直接启用自动发送。

## 使用

需要 Python 3.10+，无第三方依赖。在仓库根目录运行：

```sh
python render_digest.py examples/digest.json
python -m unittest discover -s tests -v
```

模板见 [templates/daily_digest.json](templates/daily_digest.json)，输入样例见
[examples/digest.json](examples/digest.json)，完整预期输出见
[examples/digest.md](examples/digest.md)。样例全部虚构，链接使用 example.com。

## 固定格式

- 标题为加粗的 `Teambition｜UPCAM 项目任务动态日报`。
- 主标题、`🗓️ 日期：YYYY-MM-DD`、`📊 更新 N 项：…` 连续三行紧凑排列；日期使用 🗓️，更新概览使用 📊。
- 分类顺序固定为 `✅ 已解决`、`💬 工作进展`、`🆕 新建任务`、`📝 其他变更`，加粗并带全角括号数量。
- 空分类不显示（概览也省略）；全空时只显示标题、日期及 `📊 更新 0 项`。
- 同一分类只出现一次；同一任务链接不可重复出现。上游应先合并同一任务的事件，再给每个任务指定唯一分类，不能按事件数重复计数。
- 每个非空分类内均从 1 编号，即使只有一项也显示 `1.`。源码使用 `1\.` 转义句点，避免生成有序列表及其缩进。保持该分类内输入顺序。
- 每个任务三行：原始任务标题与直接链接同行；摘要下一行；`当前：状态｜负责人：姓名` 再下一行。不截断或改写输入标题，仅转义 Markdown 控制字符，确保可见文字保留。
- 概览后、任务间、分类间使用全角空白字符行保留视觉空行，不使用分隔线。分类标题与首项紧挨，不插入空白行。

## Markdown 换行约定

任务标题行及摘要行末尾使用两个 ASCII 空格加 LF（`  \n`）作为硬换行；不要清除这两个尾随空格。
任务三行均无前导缩进，编号为普通文本，让摘要、负责人及长标题续行回到卡片左边缘。
主标题及日期行、分类标题行也使用两个 ASCII 空格加 LF，下一行紧接内容。
概览后、任务间、分类间使用 `\n\n\u3000\n\n`：以空行包围一个仅含 U+3000 全角空格的段落，避免客户端折叠普通空行。不要删除或替换该字符。
直接链接采用 `[标题](<URL>)`，兼容 URL 中的括号。
输入标题、摘要、状态和负责人必须是非空单行字符串，避免破坏布局。
展示层统一先将 `&` 编码为 `&amp;`，再转义 Markdown 控制符，防止 `&rlm;`、`&lrm;`、数字实体等
在渲染时变成控制字符，也让 `&lt;br&gt;` 保持字面显示。该变换仅发生于输出，原始正文、引用和审计不变。
实体回归使用标准 HTML 解析器；有本地 pandoc 时另外执行 CommonMark→HTML→文本的真实渲染回归，
无 pandoc 时仅跳过这项可选渲染测试，其余实体与审计回归继续运行。运行程序本身无新增依赖。

测试核查实际渲染器输出、CLI 输出与完整快照，以及顺序、计数、编号、空分类、重复任务和换行。
自动测试仅验证 Markdown 源文本。用户提供的此前截图已目视验证转义编号、任务内部换行和任务间留白；本次紧凑标题布局的钉钉实际渲染仍待验证。
接入发送流程时应原样传递生成的 Markdown，后续以钉钉真实显示效果核对兼容性。

## 数据边界

仓库仅保存模板和虚构样例。不要提交真实任务、人员信息、Webhook、签名或凭据。
生产配置、数据、日报、审计与防重账本必须放在仓库之外。采集器读取
`TEAMBITION_APP_ID` 和 `TEAMBITION_APP_SECRET`，本地签发 300 秒 HS256 JWT，
不落盘、不打印令牌或原始错误响应。现有渲染器和批准格式保持不变。

## 离线日报

```sh
python digest_pipeline.py --config tests/fixtures/config.json --fixture tests/fixtures/collection.json --date 2026-01-02
python -m unittest discover -s tests -v
```

第一个命令返回 JSON（`digest`、原样渲染的 `markdown`、`audit`、`summary_inputs`、`issues`、`scope`）。
示例有意未标记生产口径已核验，退出码为 2；0 表示通过配置与数据检查，1 表示采集或结构错误。
退出码 0 不代表已发送或已证明生产系统可靠。原始动态文本不会被当成指令或自动编造工作摘要。
评论输出数量、明确标注的原文摘录和“语义归纳待复核”，不推断完成情况。生产输出可能含企业信息，切勿提交或写进 CI 日志。
**摘要功能仍不完整**：已提取可审查的评论内容，但没有自动语义总结。含评论任务统一待复核，不能直接发送。
`summary_inputs` 保留完整 raw_content、解析后的 parsed_payload、comment 正文 body、清晰文本 text、事件 ID、createTime、附件/提及证据和复核原因，semantic_summary 固定为 null。
评论 content 按 JSON 字符串解析，正文取内部 comment；attachments、dingFiles、files、mentions 保存在 payload_evidence，
事件外层证据独立保存在 extra_evidence，不合并覆盖。原始字符串及完整已解析对象均保留，未知字段也不会丢弃。
仅将作者 comment 中精确匹配的 `<br>` 转成换行，结果置于 normalized_body；body/raw_content 仍保持原文。
卡片 text 仅合并显示空白并转义 Markdown，不进行通用 HTML 清洗、实体解码或属性解释；`<br/>`、`<BR>`、
带属性标签、其他 HTML、残缺标签及控制字符继续待审核。只有换行标记而无正文也待审核。
quote 只接受字符串并独立保存到 quote_evidence，注明 quote_role=quoted-context-not-author-opinion；
不会拼进作者 text、摘要摘录或当作作者的新意见。内外层引用分来源保留，不覆盖、不推断原作者身份。
非空引用会阻止把“收到”等简单正文静默筛掉；引用结构异常、包含未知标记或控制字符时继续待复核。
引用原文不规范化、不执行，也不下载其中的链接或图片。
重复键、非标准数值、损坏 JSON、错误包裹类型、缺失正文或错误附件类型均待复核，不猜测正文。
renderMode 支持 text/markdown；两者的普通正文均作为字面文本供审核，再由现有渲染器转义 Markdown 控制符，不执行或直接渲染输入的链接、图片和格式。
未知模式继续待复核。HTML/未知富文本、结构化正文，以及非换行/制表符的 Unicode 控制/格式字符不进入卡片摘录，
原文仍在 body/parsed_payload/raw_content 中可审查，包括 NUL、DEL、C1 与双向文本控制字符。
mentions 支持列表或字典，作为不透明证据完整保留，不解析为已核验姓名或替换正文引用。
isOnlyNotifyMentions 只接受布尔值并保留；为 true 或存在非空提及/附件时，确认性正文也不从摘要候选中排除。
内部与外层元数据冲突、未知字段或异常类型仍待复核。
只对已成功解析、无格式歧义且正文完全匹配“收到/好的/谢谢/ok/👍”、没有附件或提及等额外证据的回复标记 omit_from_summary；这只是候选过滤，
原文不会从复核输入删除，因为确认回复在上下文中也可能构成实质决定。“好的，但仍有阻塞”等完整保留。
正文普通文本仅合并显示空白；富文本、结构化内容、缺失内容均保留原始证据并待复核，不尝试猜测格式或丢弃附件。
附件链接只作为证据保留，不下载、不请求外部资源。所有评论的 semantic_review_required 始终为 true，提取成功不等于摘要审核通过。
卡片摘录最多 240 字符，超出明确提示复核输入有全文；评论及附件证据超过 64KiB 则失败而非静默截断。
未知时间的评论保留证据并标记是否属于本日待复核。所有评论内容均视为数据，不执行其中的指令。

## 受控人员与状态映射

显式 `--collect` 在采集前通过 `load_runtime_mappings` 从 `TEAMBITION_USER_MAP_JSON` 加载用户 ID→姓名 JSON 对象，
只在内存中使用；不会落盘、打印整个映射或把它加入报告/audit。库调用需显式调用该加载函数，再传给 `build_digest`。
`--fixture` 不读取环境映射，避免离线回归意外接触真实数据；所有测试仅注入虚构环境字典。

环境变量缺失时沿用显式配置；若变量已配置，则空字符串、空对象、null、数组、重复键（包括同值重复）、
无效 ID、空姓名、非字符串姓名、首尾空白或控制/格式字符均失败。环境与配置中的 owner_names 仅在同 ID 同名时合并，
同 ID 不同姓名直接阻断，绝不静默覆盖。不同 ID 同姓名允许存在，不能因重名推断同一人。
错误仅报告固定原因，不回显变量值、用户 ID 或姓名。单份 JSON 上限 1MiB。

人员映射不会改变 view.verified/api_contract_verified 等审核标记；未知 ID 仍显示待复核并保持发送阻断。
生成的受控日报会按既有格式包含所涉任务的负责人姓名，但不会输出整个映射或无关成员；不得把生产报告作为公开日志。
映射值不得提交至代码、测试样例、仓库或公开输出，也不要把环境变量内容作为命令参数或诊断文本输出。

状态直接使用受控配置文件中的 status_names（ID→名称）和 resolved_status_ids（解决状态 ID 列表），
必须来自已核验资料；没有状态映射时不猜名称/解决状态。重复键、空状态名、重复解决 ID 或解决 ID 缺少名称映射均失败。
配置 JSON 同样严格拒绝所有层级的重复键。代码不查询状态列表、不新增状态权限，也不会从人员环境变量推导状态。

## 采集与归类契约

显式采集入口：`python digest_pipeline.py --config /受控目录/config.json --collect`（本轮未执行）。
采用已交接验证的 `/api/v3/project/{projectId}/task/query` 的 `q` 参数，例如虚构日期：
`updated >= '2026-01-02T00:00:00+08:00' AND updated < '2026-01-03T00:00:00+08:00'`。
该口径是**当前任务最近更新时间在昨天区间内**，不是昨日任意事件的历史全覆盖。昨天有评论但今天再次更新的任务
可能不属于此视图；这是沿用用户已设置视图的明确范围，不增加旁路全量扫描。
日期在任何采集/文件读取前固定为 Asia/Shanghai 的昨天，再贯穿归类和报告窗口，跨午夜不改变日期。
使用 `[00:00, 次日00:00)` 半开区间；无时区的动态时间待复核。
显式日期严格要求 `YYYY-MM-DD`；不接受 `20260102`、ISO 周日期等别名。
库调用 `build_digest` 必须传入采集前固定的日期，不再提供采集后推算昨天的默认值。
采集结果记录 tenant_id/project_id，必须与配置一致；报告 scope 绑定相同来源身份。
所有请求固定至 `https://open.teambition.com`，只允许以下 GET，禁止重定向，不提供通用写接口：

- `/api/v3/project/{projectId}/task/query`：每页保留相同、由固定日期生成的 q；默认 includeArchived=false，pageSize 默认 100（可显式设为 1–100）。
- `/api/v3/task/query`：逐任务详情、校验项目与任务身份。
- `/api/v3/task/{taskId}/activity/list`：仅在候选任务确定后读取动态。事件使用 createTime 窗口，不能以任务时间或当前状态证明昨日事件。

按 `nextPageToken` 翻页直到明确空值；空页带 token 继续，重复 token、页数上限、缺失分页字段、
同 ID 不同内容、详情不匹配均失败，不输出“完整日报”。相同 ID 相同内容去重。
每次请求至少间隔 250ms；GET 超时/连接失败、429 与特定 5xx 最多尝试 3 次。
遵守 Retry-After（超过 60 秒预算直接失败），鉴权/业务错误不重试。响应大小限制 8MiB。
默认硬预算：每个分页流 20 页、整个客户端 300 次请求（重试计入）、100 个候选任务、10,000 条返回记录、120 秒。
超过预算、等待/响应越过截止时间均失败，不把部分结果输出为完整日报；不假设活动有序而提前停页。
`_collect_bounded_fixture` 只用于注入虚构 transport 的通用离线分页测试，标记 selection.mode=offline-unfiltered、view_equivalent=false，
不是生产入口。`Client.collect` 只调用带固定 q 的生产路径，查询错误、超预算或结构异常均不回退该测试路径。
返回候选的 updated 必须显式带时区且处于所选半开区间；详情 updated 与列表不一致时失败，防止采集中途状态变化。
selection 记录模式 v3-project-updated-half-open、固定日期、起止时间、q、basis=current-latest-updated 和
historical_event_coverage=false，并进入报告 scope。生成日报时重新校验选择契约与日期、任务范围一致。
这仍不是数据库一致性快照；列表完成后的新增/移出候选以及权限不可见项目内容不能据此保证覆盖。
接口没有提供一致性快照；分页完成只意味着本次可见数据遍历完成，不保证并发修改、已删除内容、
授权不可见任务、或已归档历史的完整性。

生产配置应参考虚构 `tests/fixtures/config.json`，包含 tenant_id、project_id、include_archived，
以及通过既有只读测试脱敏核验的以下内容（demo-* ID 与人员均为虚构）：

- `activity_fields`：固定为 time=createTime、action=action；不允许回退任务 created 或任意覆盖字段。
- 动作采用明确适配表：create→新建、comment→评论复核、involve.add/update_content→其他变更；update_executor 需通过结构校验。standard 仅识别受限标题候选，其余保持未知待复核，未来动作同样待复核。
- `update.taskflowstatus`：安全解析 content JSON 字符串，要求 tfsId、oldTfsId、taskflowstatus、oldTaskflowstatus、isDone 齐全且类型、ID/名称映射一致；重复 JSON 键、非标准数值、缺失/错误字段都待复核。当前只支持已核验的字符串状态名，其他结构不猜测。
- 事件 isDone 只验证结构类型并以 event_is_done 保留，**不假设它表达目标状态，也不要求与解决状态一致**。解决与否由事件目标状态 ID 及已核验映射判断。
- 同一任务当日状态事件按时间排序，前一目标 ID 必须衔接后一旧状态 ID；任意同时间多次状态变化、事件链断裂、最后目标与当前 tfsId 不符或当前完成标记冲突均待复核。当前状态只用于校验，不能凭它生成昨日解决事件。oldTfsId 与 tfsId 相同的事件标记待复核，即使目标名称为已解决，也不能据此认定进入已解决。
- update_executor 的 content 按有界、无重复键的 JSON 对象解析，要求 _executorId/_oldExecutorId；姓名仅从 owner_names 的 ID 映射获得，缺失不猜测。content.payload 和事件外层 payload 分开保留；fromAutomation 必须为布尔值，双来源冲突则待复核。该标记只描述来源，不能推断操作人身份。执行人事件按时间排序后检查旧/新 ID 链连续性、任何同一时刻的多条变更、末次目标与当前 executorId 一致性；任一冲突均阻断发送，不能用事件 ID 排序消除同刻歧义。
- standard 只对 title 精确为“关联了提交”，或以该短语加冒号/空白分隔后续文本，并且有 customCreator.name 的 JSON 识别 commit-link-title 候选。该受限前缀后的内容允许 LF/CRLF 多行，原始 title 不归一化、不截断；额外控制字符仍拒绝识别。否定句、前置描述、无分隔后缀及其他标题不匹配；未匹配不推断为提交。候选仍需复核，customCreator.name 仅是来源原文，不作为已核验成员或实际提交的证明。
- audit.status_transitions 保留状态 ID、原始事件布尔值及事件 ID；audit.parsed_changes 保留执行人/来源/标题候选结构，原始 activity_evidence 始终保留。audit.executor_conflict/status_noop 明确记录执行人冲突及状态 ID 未变化的复核原因。
- 每条动态必须具有匹配的 boundToObjectId 和 boundToObjectType=task；缺失或跨任务/类型冲突即失败。若同时存在 taskId，也必须一致。
- `resolved_status_ids`、`status_names`、`owner_names`：已核验 ID 映射；无法识别则待复核。
- `view.task_equals`：已核验的稳定视图字段等值筛选；目前只接受 executorId、tasklistId、stageId、tfsId、scenariofieldconfigId、visible、parentTaskId 的字符串等值。
- `view.verified` 与 `view.evidence`：记录实际“昨天”视图核对依据；`api_contract_verified` 记录字段契约已核验。

默认这些核验标记均为 false。它们是操作方的确认记录，不能靠改成 true 替代实际核验。
已接入交接的动态字段、已观察动作以及经独立只读验证的最近更新时间筛选；不扩大为历史任意事件口径。
`view` 仅接受 verified、evidence、task_equals 三个字段；未知字段、拼写错误、错误类型及不支持的筛选
在采集前直接失败，即使零任务也不能静默忽略。不支持的视图规则必须先扩展适配器并补测试。
同任务事件合并一次：存在未知/歧义时归其他变更待复核；否则最后一次状态变更为已解决优先，
其次新建、最后其他；含评论因语义待复核暂归其他变更。后续重开覆盖此前解决；同一时刻多次状态变更待复核。
分类在动态与标题、负责人、当前状态等所有复核检查后决定；缺失负责人等情况也不能留在已解决分类。
创建任务字段与当前 isDone 都不能证明昨天新建/解决。当前状态与负责人仅作当前信息展示。
未知时间会保留候选任务并明确“是否属于本日待复核”，不将其声称为昨日已发生动态。
已由更新时间视图选中但没有本日可归类动态的任务也保留为“动态证据不足，待复核”，不静默漏掉视图成员。

## 独立发送防重边界

`delivery_guard.py` 提供可注入发送函数的独立门禁，仓库没有实际发送函数、Webhook 读取或调度器。
只有 `ready_to_send=true` 且无 issues 的报告才可尝试。未来发送适配器必须原样传入 Markdown，
且仅在 HTTP 与钉钉业务响应均确认成功后返回严格的 `True`；不能用“HTTP 200”代替业务成功。

防重键为企业、项目、上海日期、稳定接收目标 ID；正文 hash 只存档，不加入防重键。
发送前要求参数中的企业/项目与报告 scope 完全一致，严格核验报告日期、Asia/Shanghai 时区、
对应午夜起止窗口及完整分页标记；缺失来源、日期别名或不匹配窗口均在写账本/调用发送函数前拒绝。
键从已核验的报告来源与规范日期生成，不能用外部参数重新标注来源。此绑定是可信进程内的数据一致性校验，
不是对任意外部报告的数字签名验证；发送适配器必须只接收可信生成流程的报告。
使用同一 SQLite 主键事务抢占并提交 `in_flight` 后才调用发送函数；明确成功改为 `confirmed`，
任何异常/拒绝/非明确成功记为 `unknown`。崩溃遗留的 in_flight、unknown、confirmed 均阻止再发，
包括正文变化后的重跑。没有自动重试、超时释放或重置接口。

账本须由部署阶段显式 `initialize_ledger(path)` 创建，路径必须在仓库外。保存返回的 UUID 至独立配置；
`DeliveryGuard(path, expected_identity, persistent_storage_verified=True)` 只打开既有账本，丢失、损坏、
身份不符或未验证持久存储时拒绝发送。数据库只记录 scope hash、正文 hash、状态，不存消息和凭据。
未知结果必须先人工对照接收端与账本确认，不能删除账本、换目标别名或换数据库绕过保护。

**验证范围**：离线测试证明同一数据库上并发抢占、关闭重开、崩溃遗留与写入失败的保守阻断。
没有证明云运行环境跨执行保留磁盘，也未验证共享文件锁、备份恢复/回滚、磁盘故障、独立多副本数据库。
未经实际部署验证，不得声称跨运行防重可靠。即使存储可靠，也仅提供至多一次尝试，不能保证恰好一次送达；
抢占后、网络发送前崩溃可能漏发，这是防止未知结果重复发送的明确取舍。

## 依据及未完成的生产验证

路径与认证依据来自官方公开文档：[项目任务列表](https://open.teambition.com/docs/apis/6321c6d1912d20d3b5a49ec1)、
[任务详情](https://open.teambition.com/docs/apis/6321c6d2912d20d3b5a4a7b8)、
[任务动态](https://open.teambition.com/docs/apis/6363bcfb912d20d3b56fb3f4)、
[应用凭证](https://open.teambition.com/docs/documents/5db8f7e77baeb50014957fc1)。
本轮仅按交接信息构造结构等价的虚构 fixtures，未调用企业 API 或钉钉，未保存真实数据。
评论 JSON、状态事件布尔值中立处理、执行人变更及受限 standard 标题候选已适配；真实人员/状态映射及具体提交含义仍待核验，不能从候选或字段提取成功推导发送已获准。
独立只读验证交接已确认 v3 q 的半开区间筛选、与当前昨天视图集合一致、UTC 等价及毫秒级比较边界；
本轮以完全虚构 fixtures 覆盖该契约，没有重新在线验证或保存真实集合。通用语法参考另见
[官方 TQL 参考](https://github.com/teambition/tb-skills/blob/main/dingtalk-teambition/references/tql.md)，其配套脚本使用 v2，
因此 v3 兼容性的依据是交接的实测证据，而非仅凭该通用文档推断。
真实权限范围、后续并发修改、已删除/已归档任务、事件保留策略与评论是否必然改变 updated 仍有限制；
当前 updated 不能重建历史时点视图，也不能证明所有旧任务的昨日评论均覆盖。
此外需要确认任务直接链接、成员/状态映射刷新方式、全项目权限/归档范围、真实数据量与运行预算，
核对“昨天”视图成员集合及钉钉实际排版，验证持久共享账本，再单独接入发送器。未设置任何定时任务。

### 私有人工复核（离线，不自动发送）

`manual_review.py` 提供人工摘要流程，不是全自动语义总结。成功采集现在带有随机
`batch_id` 和明确时区的 `collected_at`。历史无批次文件不能进入审核；不要手工给旧业务
文件补批次冒充新采集。每次新采集都需要重新审核。当前禁止在线请求的工作阶段仅使用
虚构 fixtures 测试本流程。

准备好私有采集文件和配置后，离线生成草稿：

```sh
python manual_review.py --collection /private/current-collection.json --config /private/config.json \
  --date 2026-01-02 --output /private/review-draft.json
```

输出必须在仓库外，以独占创建、0600 权限保存；不覆盖已有文件，不把正文打印到日志。
草稿为 JSON，`ensure_ascii` 保留文本内容同时避免原始控制字符影响终端。`tasks` 包含
任务详情、精确事件 ID、完整原始动态、提取评论、独立引用、附件/文件/提及的原始元数据。
附件只保留来源元数据，不下载或证明其内容；外部评论、引用、链接中的命令一律视为证据，
程序不执行命令、不访问链接。不要把它们当成给模型或操作者的指令。

将 `review_template` 单独复制为仓库外的审核 JSON：填写审核人、带时区审核时间、每项
任务的单行人工 `summary`，确认 `evidence_verified=true`；逐项核对全部
`reviewed_event_ids`，并在 `covered_comment_ids` 中列出摘要覆盖的全部实质评论 ID。
纯确认评论仍完整保留在草稿中，但不强制列入摘要；含引用/附件等证据的评论不会按纯确认
丢弃。引用不得归为作者新意见，附件内容未核验时不得声称已验证其内容。覆盖集合校验
只能证明操作者确认过这些来源，不能自动证明自然语言摘要忠实或没有遗漏含义。

可在 `mappings` 显式补充 `owner_names`、`status_names`、`resolved_status_ids`，并由人工
确认 `mappings_verified=true`。既有 ID 的名称冲突会失败，需修正原始配置后重新生成草稿。
未知 ID 不猜测，事件链矛盾、未知动作、未支持富文本、来源/视图未核验等仍阻止发送。
`standard` 的具体含义仍待核验，不能通过填摘要解除该门禁。本离线 CLI 不加载环境人员
映射；需要人员映射时使用受保护的私有配置或由受信任调用者在内存中加载后调用函数，
不得把私有映射写入仓库。

```sh
python manual_review.py --collection /private/current-collection.json --config /private/config.json \
  --date 2026-01-02 --review /private/approved-review.json --output /private/reviewed-report.json
```

审核绑定企业、项目、报告日期、批次、完整采集文件 SHA-256 和原始配置 SHA-256；任何
源数据（包括窗口外记录）、配置或批次变化都必须重新审核。有效期固定为采集完成后
24 小时，不可通过审核文件延长；未来批次/审核时间也拒绝。生成的报告还绑定渲染结果和
全部审计字段，修改后不能直接发送。发送门禁对**所有报告**强制要求受信任的本地调用者
独立提供 `current_collection`、`current_config` 和 `current_review`（审核文件），不能从
报告或外部评论中提取这些参数。门禁重新执行完整审核、验证当前批次/证据/配置/有效期，
再逐字段比对报告，全部通过才进入原有持久化防重复机制。报告类型必须为
`human-reviewed-v1` 且必须有完整审核元数据；类型标签本身不构成信任，独立输入重建才是
验证依据。删除审核字段、伪装成普通报告或重算自带散列均不能降级。旧版普通报告即使
`ready_to_send=true` 也不再可直接发送，必须走人工审核；拒绝发生在回调和账本占位之前。
调用者必须提供真正当前的私有采集文件和独立审核文件；程序没有全局“最新批次”
服务，散列也不是签名，不能防止恶意操作者重写代码或伪造整套输入。云端持久存储与真实
发送仍未经验证；本流程不提供发送器、不设置定时。
