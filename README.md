# UPCAM 站会日报

Windows 桌面应用把 Teambition 采集、OpenAI 摘要、钉钉发送、模型设置、自动运行时间和本机运行记录集中在一个界面。日报模板和编辑规则由本仓库维护，随程序版本一起发布；用户不需要编写日报格式。当前应用版本为 `0.3.4`，随附规则修订为 `2026-11-04.3`。

## 启动

下载成功 Windows 工作流的 **UPCAMTaskDigest-WinUI3-windows-x64** artifact，完整解压 ZIP，运行 `UPCAMTaskDigest.exe`。保留 DLL 和 `worker` 子文件夹。新版采用 C#、XAML、WinUI 3 / Windows App SDK 和 WinUIEx，参考 PowerToys 原生设置窗口。完整升级及操作说明见 [WINUI_APP.md](WINUI_APP.md)。

开发构建需要 Windows、.NET 8 SDK 和 Python 3.12，具体命令以 Windows 工作流为准。原 Tk 源码保留为旧版维护入口。

设置、模型缓存、运行历史和发送防重账本放在当前 Windows 用户的 `%LOCALAPPDATA%\UPCAM\TaskDigest`。Teambition App ID/Secret、钉钉 Webhook/签名密钥和 OpenAI API Key 使用当前 Windows 用户 DPAPI 加密保存；不会写入仓库、模型缓存或运行日志。模型缓存只保存模型元数据和当前 Key 的 SHA-256 指纹，不保存 Key。

## 首次配置

1. 在侧边栏“账户与模型”输入 Teambition Tenant ID、Project ID、Teambition App 凭据、钉钉机器人 Webhook/签名密钥和 OpenAI API Key。
2. 点击“刷新当前 Key 可用模型”。模型列表只在手动刷新时请求；成功结果在本机缓存，之后启动直接使用。刷新失败保留旧缓存。缓存只对刷新时使用的 API Key 生效。
3. 选择模型、reasoning effort 和 API 服务等级。默认模型是 `gpt-6-luna`、默认 reasoning effort 是 `max`、默认服务等级是标准费用。
4. 在“项目范围”选择含任务动态的时间窗并保存配置，程序自动只读核验并发现映射 ID；在“成员映射”和“状态映射”核对姓名与状态后保存即可。租户、项目、归档、筛选条件或 Teambition 凭据变化会重新核验，不再要求额外勾选范围确认。日报版式及筛选编辑规则由仓库维护。未知字段、未知事件或映射不完整会阻断自动发送。
5. 在“自动运行”页设置时间与采集任务上限；要无人值守时勾选自动执行和当前 Windows 用户登录启动。默认任务上限 300，可设为 1–700，仍受请求、分页、记录、响应大小和运行时长硬限制。
6. 可在“账户与模型”点击“只读测试连接”。它只查询限定项目数据，不调用 OpenAI，也不发钉钉消息。

默认调度是上午 08:50 和下午 16:50。上午采集前一日 16:50 到当日 08:50；下午采集当日 00:00 到 16:50，均使用电脑本机时间。自动执行仅在桌面应用运行且电脑未休眠时工作。应用重开后会补跑最近 24 小时内、且自动执行已启用期间错过的时段，并使用该时段原定采集窗口；启用前或超过 24 小时的时段跳过。已有记录（包括阻断或发送结果不明）不会重跑，避免重复发送。可选当前用户登录启动，启动后在系统托盘运行；只有电脑开机、该用户已登录且联网时才可运行。

## 自动日报流程

应用先在本机按固定的半开时间区间采集 Teambition，再校验项目、视图、分页、任务/动态身份、时间边界、映射及仓库规则。通过本地校验后，只把需要摘要的评论正文、必要的历史引用文本、任务标题和不透明的 `task-N` 临时键发送 OpenAI Responses API；不发送 Teambition 凭据、任务/引用 ID、链接预览元数据、负责人映射、原始 API 响应或附件。正文自身包含的 URL 仍属于正文。请求使用结构化 JSON 输出和 `store=false`，并在本机再次验证模型输出和整份日报来源。

完全没有任务动态且筛选/分页覆盖已确认时，程序直接按仓库格式生成“经完整采集，筛选时间段内未发现任务动态”，不调用 OpenAI API；含有状态等非评论动态时也不需要模型摘要。分页不完整、视图等价性未知、任务更新时间在窗口内但动态证据缺失、未知动作/时间、映射问题或其他来源歧义不会被当成“无动态”，而是阻断发送。

只有附件、正文为空的已支持评论结构会按实际文件引用记录确定性说明，明确未读取文件内容，不调用 OpenAI，也不会当成没有动态。有正文时，已核验的不可用链接预览元数据保留本机，只提取正文；含未知结构、来源冲突或未支持富文本仍会阻断。

遇到阻断，先点“只读诊断”，再点“复制诊断文字”取得按类型汇总的本机说明。v5 脱敏结构还提供动作指纹、有限标记分类及提交来源字段形状，用于离线定位；不导出评论正文、任意标记/属性或未知动作原文。诊断边界与离线匹配见 [DIAGNOSTICS.md](DIAGNOSTICS.md)。

日常日报不需要人工审核或逐次批准。采集和摘要校验通过后，程序自动发送到设置的钉钉机器人。账本会在发送前持久化占位，同一个项目、报告日期、时间窗口和接收目标最多尝试一次。明确成功记为已确认；超时、拒绝、异常响应或发送后账本写入失败均视为结果不明/阻断，不会自动重试。结果不明时需要先在钉钉核实状态，再按操作说明处理；不要删除或替换账本来绕过防重。

## OpenAI 模型和 Fast

`/v1/models` 只返回 API Key 可见的模型 ID 等基础元数据，不保证 Responses 端点权限，也不提供 reasoning effort。应用仅为内置官方资料已核验的模型显示 reasoning 档位；其他模型只提供 `default`。`gpt-6-luna` 的 `max` 档位在当前应用资料中已核验。模型列表中的模型仍可能因账户权限或项目设置而无法生成 Responses。

API 快速服务等级与 ChatGPT/Codex 界面里的快速模式设置是两回事。应用通过 Responses API 请求参数 `service_tier=fast` 显式启用 Fast；当前官方模型定价资料列出 `gpt-6-luna`、`gpt-6-astra`、`gpt-6.1-sol`、`gpt-6.1-terra` 的 Fast 档。Fast 默认关闭；它按 token 收取高于标准处理的费用、速度提升不保证，且是否可用仍受账户、地区、模型和项目权限限制。标准模式会显式发送 `service_tier=default`，因此不会继承 API 项目级 Fast 默认。Fast 请求若被 API 拒绝，本次日报会阻断，不会发送。

OpenAI 文档：[`/v1/models`](https://developers.openai.com/api/reference/resources/models/methods/list)、[reasoning effort](https://developers.openai.com/api/docs/guides/reasoning)、[Fast mode](https://developers.openai.com/api/docs/guides/fast-mode)、[Fast mode 定价](https://developers.openai.com/api/docs/pricing?latest-pricing=fast)、[Responses `service_tier`](https://developers.openai.com/api/reference/resources/responses/methods/create)。`store=false` 不等同于零保留；请查看[数据控制说明](https://developers.openai.com/api/docs/guides/your-data)了解 API 的默认滥用监控留存和账户策略。

## 代码与本地验证

- `winui/UPCAMTaskDigest/`：C# / XAML 原生窗口、设置卡片、映射列表、对话框、托盘和异步工作进程客户端。
- `worker_entry.py`、`upcam_app/worker_api.py`：私有本机协议，复用既有业务核心、DPAPI 和存储格式，不返回密钥值。
- `desktop_app.py`：保留的旧版 Tk 界面。
- `upcam_app/workflow.py`：本地采集、预检、AI 摘要和自动发送的编排。
- `teambition_readonly.py`、`digest_pipeline.py`、`activity_adapter.py`：限域只读采集和确定性日报规则。
- `openai_summarizer.py`、`automated_digest.py`：最小输入、Responses API 结构化摘要和独立验证。
- `delivery_guard.py`、`upcam_app/dingtalk.py`：一次性持久化防重门禁与钉钉传输。
- `templates/daily_digest.json`：随程序版本打包的日报格式和内容规则。
- `DATA_COLLECTION_CONTRACT.md`：延续并更新主分支的 Teambition 采集、字段映射和动态归类规则。
- `tests/fixtures/`：全部为虚构数据。测试通过替身传输模拟外部 API，不请求真实企业、OpenAI 或钉钉服务。

运行测试：

```powershell
python -m unittest discover -s tests -v
```

`.github/workflows/windows-build.yml` 构建真实 WinUI 3 应用和自包含 Python 工作进程，执行 Windows DPAPI、原生控件交互和多尺寸布局检查，上传目录 ZIP、截图和报告（14 天）。所有服务均使用隔离虚构配置与模拟传输，不访问生产 API、不发送真实日报、不修改启动注册表，不部署用户电脑。真实高 DPI 和目标电脑验收范围见 [WINUI_APP.md](WINUI_APP.md)。

## 数据范围与边界

- 采集筛选使用已交接验证的 Teambition v3 `updated` 半开区间查询；它代表当前最近更新时间落在窗口内，不是历史任意事件的全量追溯。
- 分页完整只证明当次可见结果已遍历，不代表数据库一致性快照，也无法证明权限不可见任务或已删除数据的完整性。证据存在歧义时程序停止。
- 摘要会把评论正文交给用户选择的 OpenAI API 模型。`store=false` 可避免 Responses API 常规存储，但不能代替账户侧数据保留设置。
- Windows 版支持系统托盘、关闭窗口时隐藏和托盘菜单显式退出；可选择当前用户登录时启动并以托盘模式运行。该开关只写入当前用户的 Run 项，不需要管理员权限。关机、休眠、用户未登录或断网期间不会运行。
- 本应用不在后台上传运行历史、映射或模板更新；更新日报规则需打包并发布兼容的新程序版本。
- `manual_review.py` 和对应测试保留为仓库已有的离线分析工具，不是桌面应用发送流程的审核门禁。
