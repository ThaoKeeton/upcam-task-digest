# Teambition 采集与内容规则

本文保留并更新主分支既有的采集、字段映射、动态归类和时间窗口契约。桌面应用遵循这些规则；未知、未核验或覆盖不完整时自动发送会停止。具体日报版式和内容筛选规则仍由仓库模板版本维护。

## 受控人员与状态映射

桌面应用不要求用户手写映射 JSON。用户可对选定时间窗进行只读项目探测，应用仅从完整响应中列出样本实际出现的成员与状态 ID；采样动态里的状态名作为待核对建议，不能自动标记为已确认。当前已批准的 Teambition 路由不提供成员名册或状态列表，成员姓名及“哪些状态代表已解决”仍须由用户在表格中核对。完整翻页、当前 Project ID 和动态字段样本通过后，保存原生应用配置即采用所选范围，无额外确认勾选；探测本身不验证网页自定义视图等价性。

项目视图范围默认是所填 Project ID 下、当前可读任务的动态按发生时间筛选所得结果。应用提供受限的稳定字段筛选器，与 `view.task_equals` 一致；修改租户、项目、归档选项或筛选条件会使视图核验失效。探测无任务、无动态样本或分页不完整时不会自动将核验标记改为 true。

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
生产采集从 `/api/v3/project/{projectId}/task/query` 遍历指定项目当前可读任务，省略可选的 `q` 参数。不得用 `updated < 截止时间` 或其他任务更新时间谓词替代活动历史选择：窗口内有动态的任务可能在窗口结束后继续更新；活动时间也可能比任务更新时间略晚，接口未承诺二者的单调关系。

日期及可选起止时间在采集前固定为 Asia/Shanghai 的原有半开区间 `[开始, 结束)`，显式日期必须是 `YYYY-MM-DD`。上午、下午和手动窗口不变。任务详情和全部活动页读取后，只有动态 `createTime` 决定日报窗口归属。缺失时区或无效动态时间仍阻断，不用任务 `created`、`updated` 或当前状态猜测。

所有请求固定至 `https://open.teambition.com`，只允许以下 GET，禁止重定向，不提供通用写接口：

- `/api/v3/project/{projectId}/task/query`：遍历所有页，省略 `q`；每页保持用户的 includeArchived 选项，默认 false，pageSize 默认 100（1–100）。
- `/api/v3/task/query`：逐任务详情，校验项目、任务身份、归档标记和带时区的 updated 版本。版本与初始任务列表不符时阻断。
- `/api/v3/task/{taskId}/activity/list`：读取全部活动页，不依赖顺序，不遇到旧事件就提前停止，不以当前成员名单过滤历史作者。

初次任务遍历和各任务活动读取完成后，再完整读取项目任务列表；成员集合或返回记录版本不一致即阻断。本地稳定字段条件在完整读取后过滤，不缩减基础 API 扫描量。`nextPageToken` 只有明确空值才结束；空页带 token 继续，循环/缺失 token、超页数、同 ID 不同内容、跨任务绑定或详情不一致均失败，不输出部分日报。相同 ID 同内容仅做原始身份去重。

请求至少间隔 250ms；GET 超时/连接失败、429 及特定 5xx 最多尝试 3 次，遵守有界 Retry-After。桌面路径按批推进，沿用 schema 1 的 `max_tasks_per_run`（默认 300、整数 1–700）作为每批任务数，不再作为项目总数上限。项目任务列表独立最多 100 页。每批保留现有请求、分页、返回记录、响应大小和时长预算：每个动态分页流最多 50 页、最多 50,000 条返回记录、单页 8 MiB、累计 128 MiB；请求/时间预算随批次大小计算，最多 1,800 次请求及 600 秒。

桌面全项目最多 10,000 个当前可读任务；整次采集最多 50,000 次请求尝试（包括重试）、1,000,000 条返回记录、512 MiB 响应、128 MiB 保留 JSON 和两小时（含进度缓存年龄），来源重核最多三轮。既有非批次入口保留各自原预算。任何项目、批次或整次预算耗尽、页面不完整或版本无法核验都会阻断后续摘要与发送，不能截断任务或分页、降级或当成零更新。缩短日报时间窗及本机额外筛选不会减少基础历史扫描量。

selection.mode 为 `v3-project-activities-half-open`，basis 为 `activity-createTime`，candidate_scope 为 `current-visible-project-tasks`，q 为 null，view_equivalent、historical_event_coverage、snapshot_guaranteed 均为 false，另记录固定日期、起止时间和 window_kind。报告与发送重算必须匹配新合同；旧版更新时间选择结果不能直接发送。`_collect_bounded_fixture` 仍仅用于注入合成传输的离线测试，不是生产回退路径。

恢复开始和每轮结束都重新完整列出项目任务；仅候选对象仍一致的完整任务可复用，新增或变化任务重新读详情及全部动态页，已不属于当前集合的任务从临时缓存移除。最终任务列表必须与完整任务记录匹配，最多三轮仍在变化时阻断。此重核是可观察的一致性检查，API 不保证每条活动都改变 `task.updated`，也不保证分页游标形成原子快照。当前权限不可见、永久删除、已移出项目的任务和历史项目成员关系无法由这些接口恢复；用户不纳入归档时也不覆盖该范围。`snapshot_guaranteed` 和 `historical_event_coverage` 均保持 false。

### 短期加密进度

仅完整读取任务详情及全部动态分页后才缓存该任务，不能保存半条任务流作为可复用结果；中断任务下次从动态第一页重新读取，不恢复不透明分页游标。进度绑定租户、项目、归档选择、精确冻结报告窗口、采集策略和 Teambition App 凭据对的 SHA-256 身份，不能跨来源、窗口或凭据身份复用。

Windows 当前用户 DPAPI 加密受控 SQLite 中的任务行和元数据；不存 API Key、JWT、请求头、Webhook 或签名凭据，无明文磁盘回退。成功后清理本次进度，陈旧或不兼容进度安全清理；取消、断电或中断保留未完成进度，自原采集开始最多两小时，下次必须先重列核验。缓存损坏、资源耗尽、版本歧义或未完成分页不能被当作零更新，也不得到达 OpenAI 或钉钉。

原生自动运行页的“清除未完成的采集进度”不发起 API 请求，只清理临时采集缓存，不清除 settings、credentials、history 或发送 ledger，不改变自动发送授权或防重状态。缓存不存发送状态，也不安排自动重试；它不是历史快照或历史覆盖承诺。开发与验收仅使用合成凭据及替身传输，不以此宣称真实 API 已验证。

官方依据：[项目任务查询（q 可选）](https://github.com/teambition/openapi-sdk-golang/blob/master/api/openapi.yaml#L3901)、[任务动态分页接口](https://github.com/teambition/openapi-sdk-golang/blob/master/api_task.go#L1748)、[任务更新时间字段](https://github.com/teambition/openapi-sdk-golang/blob/master/model_search_project_tasks_v3_response_result_inner.go#L19)。企业审计日志虽然支持时间筛选，但不等于任务动态流，不能用它代替完整任务活动记录。

生产配置应参考虚构 `tests/fixtures/config.json`，包含 tenant_id、project_id、include_archived，
以及通过既有只读测试脱敏核验的以下内容（demo-* ID 与人员均为虚构）：

- `activity_fields`：固定为 time=createTime、action=action；不允许回退任务 created 或任意覆盖字段。
- 动作采用明确适配表：create→新建、comment→评论复核、involve.add/update_content→其他变更；update_executor 需通过结构校验。move.cross.from.stage.to.stage 仅按下述已核验结构记录位置变更；standard 区分下述展示通知与旧的宽泛标题候选。未知动作或未支持结构继续阻断。
- `update.taskflowstatus`：安全解析 content JSON 字符串，要求 tfsId、oldTfsId、taskflowstatus、oldTaskflowstatus、isDone 齐全且类型、ID/名称映射一致；重复 JSON 键、非标准数值、缺失/错误字段都待复核。当前只支持已核验的字符串状态名，其他结构不猜测。
- 事件 isDone 只验证结构类型并以 event_is_done 保留，**不假设它表达目标状态，也不要求与解决状态一致**。解决分类由事件旧/新状态 ID 及已核验映射判断：必须在窗口内从集合外进入集合内，且窗口末仍在集合内；集合内部迁移不重复计算。
- 同一任务窗口内状态事件按时间排序，前一目标 ID 必须衔接后一旧状态 ID；任意同时间多次状态变化、事件链断裂、校验链最后目标与当前 tfsId 不符或当前完成标记不是布尔值均阻断。配置的“已解决”是分类集合，不必包含所有 API 已完成状态：目标不在该集合但当前 isDone=true 时仍属其他状态变化，不误判成冲突，也不分类为已解决。目标在集合内时，当前 isDone 必须为 true；否则阻断。窗口结束后已核验的同类状态事件可在本地延伸校验链到当前状态，不进入窗口计数或摘要；当前完成标记针对延伸链的最终状态核验。当前状态只用于校验，不能凭它生成解决事件。oldTfsId 与 tfsId 相同的事件继续阻断，即使名称为已解决，也不能据此认定进入新状态。
- move.cross.from.stage.to.stage：content 必须是有界、无重复键、仅有 from/to 的 JSON；两端仅含 tasklist（_id/title）与 stage（_id/name），ID 为有效 24 字符 ID，名称为非空、无标记和控制字符的单行 UTF-8。两端位置 ID 组合必须变化，同 ID 的名称不得冲突。按时间核对位置链、同刻歧义及末次目标与当前 tasklistId/stageId；任一冲突或未支持结构均阻断。本机诊断记录“任务位置变更 N 条”，正式摘要不复制该技术计数，不据工作池/完成池等名称或当前完成标记推断解决，位置原文不交给模型。
- update_executor 的 content 按有界、无重复键的 JSON 对象解析，要求 _executorId/_oldExecutorId；姓名仅从 owner_names 的 ID 映射获得，缺失不猜测。content.payload 和事件外层 payload 分开保留；fromAutomation 必须为布尔值，双来源冲突则待复核。该标记只描述来源，不能推断操作人身份。执行人事件按时间排序后检查旧/新 ID 链连续性、任何同一时刻的多条变更、末次目标与当前 executorId 一致性；任一冲突均阻断发送，不能用事件 ID 排序消除同刻歧义。
- standard 只对 title 精确为“关联了提交”，或以该短语加冒号/空白分隔后续文本，并且有 customCreator.name 的 JSON 识别 commit-link-title 候选。该受限前缀后的内容允许 LF/CRLF 多行，原始 title 不归一化、不截断；额外控制字符仍拒绝识别。否定句、前置描述、无分隔后缀及其他标题不匹配；未匹配不推断为提交。候选仍需复核，customCreator.name 仅是来源原文，不作为已核验成员或实际提交的证明。
- association-display-notice：仅接受精确 title/customCreator 对象、customCreator 仅含 name，标题完整符合“关联了提交 「非空内容」”或“关联了合并请求 「非空内容」”。标题可在括号内容内含 LF/CRLF，不接受其他控制字符、标记或额外/冲突来源载荷。按动态 ID 去重后在本机审计统计通知，不按标题、创建者或假定提交 ID 去重。source_verified/object_verified 仍为 false，不推断平台、实际提交数、作者、合并或解决，原文不交给 OpenAI。v0.3.7 正式内容筛选不再复制通知计数和来源诊断语；原始证据继续保留，未符合严格展示结构的旧候选继续阻断。
- 仅附件评论只适配已核验的空字符串正文、空 attachments/dingFiles、无重复 24 字符文件 ID 列表、text/markdown 模式与布尔 isOnlyNotifyMentions；文件数量与未读取状态记录在本机审计，正式摘要为空，不下载、不推断内容或进度，不调用 OpenAI。它仍是评论动态，不能输出“无动态”。内外层冲突、未知附件/图片/数据或不明空正文仍阻断。
- hrefPreview 仅适配精确的不可用预览空信息结构，URL 必须是无凭据 HTTP(S) 并与正文完整 URL 标记匹配；元数据完整保留本机，预览错误文本不进入模型。非空预览信息、额外字段、未绑定链接或来源冲突仍阻断，不据此推断链接内容。
- upload.customfield.work / customfield.create.v2：仅接受已核验的严格有界附件引用结构。字段、任务、文件身份及名称必须一致；上传事件可含同任务同字段的多个不同文件，字段创建仅支持已核验的单文件结构。两条不同原始动态仍保留，按任务内 file ID 合并一致引用；同 ID 的字段或名称冲突阻断。不下载或推断内容，不把引用或自动添加参与者当成人工进展，不向模型发送文件名或附件元数据。
- audit.post_window_context 仅保留有窗口内对应事件流时所需的后续状态、执行人、位置原始事件；audit.status_validation_transitions 是延伸校验链。窗口内 event_counts、status_transitions、parsed_changes 和 activity_evidence 不混入这些后续事件。附件身份并集及冲突记录在 attachment_references / attachment_conflict。
- audit.status_transitions 保留状态 ID、原始事件布尔值及事件 ID；audit.parsed_changes 保留执行人、位置与通知/标题候选结构，原始 activity_evidence 始终保留。audit.executor_conflict/status_noop/position_conflict 明确记录对应冲突；position_changes 保留位置链证据。审计业务内容只存本机受控结果，不提交仓库、测试夹具或公开日志。
- 每条动态必须具有匹配的 boundToObjectId 和 boundToObjectType=task；缺失或跨任务/类型冲突即失败。若同时存在 taskId，也必须一致。
- `resolved_status_ids`、`status_names`、`owner_names`：已核验 ID 映射；无法识别则待复核。
- `view.task_equals`：已核验的稳定视图字段等值筛选；目前只接受 executorId、tasklistId、stageId、tfsId、scenariofieldconfigId、visible、parentTaskId 的字符串等值。
- `view.verified` 与 `view.evidence`：记录实际项目范围只读核验依据；`api_contract_verified` 记录字段契约已核验。

默认这些核验标记均为 false。它们是操作方的确认记录，不能靠改成 true 替代实际核验。
已接入经证据核验的动态字段和动作；动态按发生时间筛选，扫描范围仍受当前项目可见性及资源预算约束。
`view` 仅接受 verified、evidence、task_equals 三个字段；未知字段、拼写错误、错误类型及不支持的筛选
在采集前直接失败，即使零任务也不能静默忽略。不支持的视图规则必须先扩展适配器并补测试。
同任务事件合并一次：原始诊断阶段存在未知/歧义时保持硬性阻断，含评论可暂记语义待处理。正式生成完成摘要后重新分类：窗口内有集合外进入已解决集合、且窗口末仍在集合内优先，其次有实质评论摘要的工作进展，再次有窗口内新建事件的新建，最后其他。后续重开覆盖此前解决；当前状态自身不能生成解决事件。同刻冲突和缺失映射等硬性问题在请求模型前停止。

正式摘要保留任务沟通中的有效进展、问题、结论、风险、阻塞、下一步及明确的交接/交付事实。程序不附加文件数量、未读取状态、处理覆盖声明或系统执行人迁移日志，不复制原始诊断计数，也不解析诊断字符串来拼接正式内容。文件/执行人证据继续校验并留在本机审计；当前状态和负责人仍保留独立信息行。没有补充时 summary 为显式空字符串；任意非确认评论的模型空回复仍阻断。分类和显示内容在发送验证时按相同规则重建，并逐项比较 digest 与 Markdown；不推断附件内容或宣称覆盖未读取内容。

## 标题、时间与任务链接

标题编号来自任务 detail 的真实 `uniqueId`（正整数或 ASCII 数字字符串，保留字符串前导零）以及 API `project.uniqueIdPrefix` 或已确认的本机前缀。任务、嵌套项目标识和前缀冲突时失败；缺失编号不得从 API 长 ID、标题、评论或提交信息猜测。编号后一个空格连接原始 content，冒号和其他标点保持。编号及显示设置不扩充模型输入，模型仍只得到原始任务标题和必要评论。

可选 `task_link_view_id` 保存在既有 view 过滤对象之外，接受当前项目的完整“全部”视图地址或 24 字符 ID；构造 `/project/{projectId}/tasks/view/{viewId}/task/{taskId}`。未设置时使用官方 `/task/{taskId}`，不使用旧的短项目路由。手动前缀及视图绑定企业/项目；切换来源清空旧值，后台拒绝陈旧绑定。

头部使用 🕒 时间范围和 📊 更新。窗口来自采集前冻结并校验的 Asia/Shanghai 起止时间，跨日显示两端日期，同日可省略结束日期；显示不改变结束边界右开的采集语义。分类内从 1 编号，条目间保持既定空段，当前状态和负责人保留独立信息行。

编号字段资料：[任务详情](https://open.teambition.com/docs/apis/6321c6d2912d20d3b5a4a7b8)、[官方 Task SDK](https://github.com/teambition/teambition-sdk/blob/release/src/schemas/Task.ts)、[官方任务链接规范](https://github.com/teambition/tb-skills/blob/main/dingtalk-teambition/SKILL.md)。这些资料支持字段与路由实现，不等于目标企业实际响应、真实链接跳转或已发消息均已验收。
创建任务字段与当前 isDone 都不能证明昨天新建/解决。当前状态与负责人仅作当前信息展示。
未知时间会保留候选任务并明确“是否属于本日待复核”，不将其声称为昨日已发生动态。
当前可读任务没有窗口动态且 updated 在窗口之外时可跳过；若 updated 在窗口内但窗口动态证据为空，保留“动态证据不足”阻断，不能默认为零更新。


## 结构化引用与保存范围

评论引用支持有界、严格单层的 JSON 字符串，字段精确为 `_id`（24 字符有效 ID）和 `content`（UTF-8 纯文本）；重复/额外字段、数组、嵌套或多重编码、富文本、控制字符、解码编码异常或内外结构化引用冲突均不放行。不进行 HTML 清洗或递归解码。原始引用与引用身份只保留为本机证据；必要文本作为带固定引用角色和当前评论序号的上下文提供给摘要模型，不能作为当前作者的新观点、工作或完成依据，引用 ID 不进入模型请求。

原生应用保存项目配置即采用该范围，不再另需手动确认勾选。保存时由工作进程针对当前租户、项目、归档、筛选条件和 Teambition 凭据执行只读核验；仅有匹配范围和凭据的真实完整样本可建立新核验记录。失败仍保存填写内容，但清除核验标记并关闭自动执行。未变的原有已验证范围保留。客户端自报成功、缺失分页、范围/身份不匹配或筛选字段缺失不能建立核验记录。保存、核验和发现映射不调用 OpenAI 或发送器。

## 自动发送门禁说明

桌面应用把采集范围、映射、编辑规则、模型与推理设置绑定到生成结果，并在本机重新验证后才允许发送。校验失败、来源覆盖不完整或结果存在歧义时停止。每次发送前持久化占位；手动命令使用独立请求 ID，已成功时段可由新手动操作再次发送，无额外确认，同一请求不得重放。定时任务继续按来源、窗口及目标防重。进行中、请求超时、服务端结果不明或本机记账失败时阻断后续发送，不自动重试；兼容扩展账本但不删除旧记录。`manual_review.py` 是保留的离线分析工具，不参与桌面应用的发送流程。

批次预算先预留再结算：请求尝试、最多 8 MiB + 1 字节的响应额度和分页大小上限行数在实际请求前一并持久化，完整读取及解析后一次事务退回差额。未知响应保留预占；低剩余额度下同步缩小请求页大小和读取上限，拒绝超量及截断前缀。collection_stats.records / response_bytes 为预算计数，可能包含这些预占，与已确认的原始任务动态数量分开。
