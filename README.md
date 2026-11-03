# upcam-task-digest

UPCAM 日报格式模板、企业 v3 只读采集器、确定性日报及离线回归测试。
默认使用虚构 fixtures；仅显式 `--collect` 才访问 Teambition。没有钉钉 HTTP 发送器和定时任务。
生产 API 字段映射、用户“昨天”视图口径与持久状态存储仍待核验，不能直接启用自动发送。

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

测试核查实际渲染器输出、CLI 输出与完整快照，以及顺序、计数、编号、空分类、重复任务和换行。
自动测试仅验证 Markdown 源文本。用户提供的此前截图已目视验证转义编号、任务内部换行和任务间留白；本次紧凑标题布局的钉钉实际渲染仍待验证。
接入发送流程时应原样传递生成的 Markdown，后续以钉钉真实显示效果核对兼容性。

## 数据边界

仓库仅保存模板和虚构样例。不要提交真实任务、人员信息、Webhook、签名或凭据。
生产配置、数据、日报、审计与防重账本必须放在仓库之外。采集器只读取
`TEAMBITION_APP_ID` 和 `TEAMBITION_APP_SECRET`，本地签发 300 秒 HS256 JWT，
不落盘、不打印令牌或原始错误响应。现有渲染器和批准格式保持不变。

## 离线日报

```sh
python digest_pipeline.py --config tests/fixtures/config.json --fixture tests/fixtures/collection.json --date 2026-01-02
python -m unittest discover -s tests -v
```

第一个命令返回 JSON（`digest`、原样渲染的 `markdown`、`audit`、`issues`、`scope`）。
示例有意未标记生产口径已核验，退出码为 2；0 表示通过配置与数据检查，1 表示采集或结构错误。
退出码 0 不代表已发送或已证明生产系统可靠。原始动态文本不会被当成指令或自动编造工作摘要。
评论仅确定性输出“评论 N 条”，不推断完成情况。生产输出可能含企业信息，切勿提交或写进 CI 日志。
**摘要功能仍不完整**：当前只统计评论数量，没有阅读或总结实际评论内容，不能将输出称为已完成的工作内容摘要。

## 采集与归类契约

生产采集入口（本轮未执行）：`python digest_pipeline.py --config /受控目录/config.json --collect`。
日期在任何采集/文件读取前固定为 Asia/Shanghai 的昨天，再贯穿归类和报告窗口，跨午夜不改变日期。
使用 `[00:00, 次日00:00)` 半开区间；无时区的动态时间待复核。
显式日期严格要求 `YYYY-MM-DD`；不接受 `20260102`、ISO 周日期等别名。
库调用 `build_digest` 必须传入采集前固定的日期，不再提供采集后推算昨天的默认值。
采集结果记录 tenant_id/project_id，必须与配置一致；报告 scope 绑定相同来源身份。
所有请求固定至 `https://open.teambition.com`，只允许以下 GET，禁止重定向，不提供通用写接口：

- `/api/v3/project/{projectId}/task/query`：全项目分页，显式 `includeArchived=false` 默认排除归档/回收站任务。
- `/api/v3/task/query`：逐任务详情、校验项目与任务身份。
- `/api/v3/task/{taskId}/activity/list`：逐任务所有动态页，不按任务 created/updated 或当前状态裁剪。

按 `nextPageToken` 翻页直到明确空值；空页带 token 继续，重复 token、页数上限、缺失分页字段、
同 ID 不同内容、详情不匹配均失败，不输出“完整日报”。相同 ID 相同内容去重。
每次请求至少间隔 250ms；GET 超时/连接失败、429 与特定 5xx 最多尝试 3 次。
遵守 Retry-After（超过 60 秒预算直接失败），鉴权/业务错误不重试。响应大小限制 8MiB。
接口没有提供一致性快照；分页完成只意味着本次可见数据遍历完成，不保证并发修改、已删除内容、
授权不可见任务、或已归档历史的完整性。

生产配置应参考虚构 `tests/fixtures/config.json`，包含 tenant_id、project_id、include_archived，
以及通过既有只读测试脱敏核验的以下内容（不能复制 demo.* 映射作为真实动作）：

- `activity_fields.time/action`：动态时间和动作的点分字段路径；缺省仅是待验证适配值。
- `action_rules`：精确动作值映射至 comment/created/status/other；状态动作需 `target_field`。
- `resolved_status_ids`、`status_names`、`owner_names`：已核验 ID 映射；无法识别则待复核。
- `view.task_equals`：已核验的稳定视图字段等值筛选；目前只接受 executorId、tasklistId、stageId、tfsId、scenariofieldconfigId、visible、parentTaskId 的字符串等值。
- `view.verified` 与 `view.evidence`：记录实际“昨天”视图核对依据；`api_contract_verified` 记录字段契约已核验。

默认这些核验标记均为 false。它们是操作方的确认记录，不能靠改成 true 替代实际核验。
当前没有交接完整昨天视图筛选条件与真实动作枚举，故不能宣称已复现该视图的全部语义。
`view` 仅接受 verified、evidence、task_equals 三个字段；未知字段、拼写错误、错误类型及不支持的筛选
在采集前直接失败，即使零任务也不能静默忽略。不支持的视图规则必须先扩展适配器并补测试。
同任务事件合并一次：存在未知/歧义时归其他变更待复核；否则最后一次状态变更为已解决优先，
其次评论、其次新建、最后其他。后续重开覆盖此前解决；同一时刻多次状态变更待复核。
分类在动态与标题、负责人、当前状态等所有复核检查后决定；缺失负责人等情况也不能留在已解决分类。
创建任务字段与当前 isDone 都不能证明昨天新建/解决。当前状态与负责人仅作当前信息展示。
未知时间会保留候选任务并明确“是否属于本日待复核”，不将其声称为昨日已发生动态。

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
本轮未调用企业 API 或钉钉；公开文档的字段表未完整可读，响应 envelope、详情参数及真实事件字段/枚举
仍需以既有授权测试的脱敏契约补齐，再进行获准的生产验收。离线模拟不能证明真实 API 契约正确。
此外需要确认任务直接链接、成员/状态映射刷新方式、全项目权限/归档范围、真实数据量与运行预算，
核对“昨天”视图成员集合及钉钉实际排版，验证持久共享账本，再单独接入发送器。未设置任何定时任务。
