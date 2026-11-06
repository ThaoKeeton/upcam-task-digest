# 业务插件平台：运行服务、包维护和钉钉安装闭环

## 目标与已有事实

继续在 `feat/native-plugin-workbench` 开发，保留其他仓库及独立应用。用户要求持续推进、自主选择一般技术方案、基于 WinUI，并最终用真正独立的业务插件验证。基线 `6565180` 已通过 Linux/Windows Core 各 86 项和 Windows Server 2025 原生 13 项；Windows 11 人工验收仍未执行。

钉钉仓库 `codex/background-monitoring` 的已发布基线为 `e85dbd5d278554461199e0f158d6a5923e914bab`，CI `42201402589` 成功。`DingTalkInsights.Core` 目标 net8.0，无 UI 依赖；独立 Desktop 目标 net8.0-windows，使用 Windows App SDK 2.6.5。平台使用 .NET 10 / Windows App SDK 1.8.260921001。因此业务核心可复用，Desktop DLL 不能直接加载，插件必须独立编译为与平台一致的 WinUI 类库。

## 实施者选择与范围

选择兼容扩展 API 1，增加可选 `IPluginRuntimeContext`，保留原 `IPluginContext`、入口和视图工厂。相比继续完全交由插件管理，平台可观察已登记任务、控制恢复和保存凭据；相比完整进程隔离，本阶段仍是可信同进程代码，无法隔离 native crash、恶意代码或无限循环。以后可为业务计算增加无窗口工作进程，不能把独立应用窗口嵌入平台。

包维护选择暂存后在下次启动应用，保留代码历史和更新前的数据备份。绝不在已加载程序集的进程内替换 DLL。回滚默认仅回退代码并保留当前业务数据；不自动还原历史数据、发送回执或凭据，避免抹去新记录或重放已发送内容。跨数据格式降级兼容由业务插件声明和验证，备份不等于任意 schema 的自动回滚。

这一阶段交付后台任务/凭据服务、安装与重启维护、原生管理页面，以及独立钉钉插件的模拟业务闭环。真实 DWS、OpenAI、Webhook、真实凭据、多日监控及 Windows 11 人工验收不在自动测试中冒充完成。

## 固定契约

- `.NET 10`、`win-x64`、Windows App SDK `1.8.260921001`，unpackaged 自包含目录；不 trim、AOT 或单文件。
- `WorkbenchApi.Version == 1` 保持不变。两个 SDK 的程序集版本保持 `1.0.0.0`，包/文件版本升至 `0.2.0`，主程序 `HostVersion=0.2.0`。依赖新服务的清单声明 `minimumHostVersion: "0.2.0"`。
- `IPluginRuntimeContext : IPluginContext` 提供 `BackgroundTasks`、`Secrets` 和只读 `StopReason`。新服务以 `WorkbenchRuntimeServices.Version == 1` 标记。旧插件无需实现新成员。
- `IPluginBackgroundTasks` 在 Initialize 内 `Register(PluginBackgroundTask)`；提供 `GetSnapshots()`、`StartAsync(id, token)`、`StopAsync(id, token)`、`SetResumeOnStartupAsync(id, enabled, token)`。
- `PluginBackgroundTask` 为 `Id`、`Title`、`Func<IPluginBackgroundExecution, Task> RunAsync`，另有默认 false 的 init 元数据 `RequiresPluginStart`，快照也暴露该值。为 true 时宿主通用 Start 不执行委托，SetResume(true)/Restore 拒绝并清除旧意图；插件自己的 context Start 可代表它已经完成业务确认，Stop 始终可用。这是宿主策略提示，不是对可信插件强制授权的沙箱。
- 执行上下文有 `CancellationToken`、`StartReason`、动态只读 `StopReason`。开始原因区分 `UserRequest` 与 `Restore`；停止原因区分 `None`、`UserRequest`、`PluginDisabled`、`HostShutdown`、`InitializationFailed`。
- `PluginBackgroundTaskState` 至少为 `Stopped/Running/Stopping/Completed/Faulted`；快照含 ID、标题、状态、恢复开关、期望运行状态、时间和安全错误码，不含原始异常信息。操作返回 SDK 自己的 `PluginTaskOperationResult(bool Succeeded, string Message)`，不引用 Core 类型。
- `IPluginSecretStore` 提供 `IsSupported`、`Register(PluginSecretDefinition)`、`GetDefinitions()`、`ReadAsync(id, token): Task<string?>`、`WriteAsync(id, value, token)`、`DeleteAsync(id, token)`。定义仅含 ID、标题、描述，不含值。只有声明过的键可读写。
- 钉钉建议凭据 ID 为 `openai.api-key`、`dingtalk.webhook-url`、`dingtalk.webhook-secret`。Webhook URL 本身也可能包含密钥，不能放入普通 JSON 设置或日志。DWS profile 只保存引用，不自动复制独立版凭据。

## 后台生命周期

登记任务不执行代码；Initialize 成功、贡献提交后才允许启动。任务委托在受观察的线程池 Task 中运行，不阻塞 WinUI dispatcher；插件不得在委托中访问 UI。每任务单飞，多次 Start、Start/Stop 交错不能重复启动。异常变成 Faulted 和安全错误码，不传播到 UI，不自动无限重试。

恢复默认关闭。显式开启恢复且任务期望运行、包指纹不变、插件已信任/启用时，下一次平台启动才恢复，并给出 `StartReason=Restore`。显式停止或停用清除期望运行；平台退出保留恢复意图。包变更清除旧任务运行意图，需要重新启动，不能把新代码的启用当作业务联网/发送授权。

停用先撤销页面/命令，设置原因并异步取消令牌，再等待取消回调、插件 Stop 和已登记任务。默认等待预算 5 秒，可由测试缩短。超时保留实例、上下文和未完成 Stop Task，显示 Stopping；不能称为已停用、重新创建第二实例或释放用户根锁。重试等待复用同一 Stop，完成后才清理。退出失败保留进程和锁，用户仍能查看原因并重试；不自动强杀。Stop 实现必须合作并快速返回可等待 Task；同进程的同步阻塞 UI/恶意代码无法被超时抢占。未登记任务仍由可信插件自己负责，平台不能发现所有线程或子进程。

业务适配应把 `HostShutdown` 映射为保留业务恢复意图，把 `UserRequest/PluginDisabled` 映射为撤销；Restore 仍必须经过业务自己的配置指纹、授权、预算和去重校验。宿主锁不代表独立版与插件之间已互斥；业务侧应提供共享资源所有权，并明确旧独立版监控必须停止，不能宣称已阻止所有旧版本重复发送。

## 设置、凭据与诊断

普通偏好继续按插件原子保存 JSON；后台意图单独由宿主保存，绑定包指纹。凭据使用 Windows DPAPI CurrentUser，密文位于 `UserRoot/secrets/<plugin-id>/`，文件名由键哈希生成，熵绑定插件和键。非 Windows 默认不可用，不能退化为明文。损坏密文返回安全错误，不输出值；缓冲区尽量清零，但不声称可擦除托管字符串。

它是同一 Windows 用户下的安全存储约定，不是对其他可信插件的隔离。测试只用合成值和新临时目录，不读 Windows 现有凭据、实际账号或独立版配置。宿主捕获的插件异常只提供类别/错误码，不把原始异常消息写入 UI/日志；插件自己的返回消息仍必须遵守不含密钥的约定。

## 安装、更新、移除与恢复

保留本地目录安装，增加根目录含 plugin.json 的 ZIP 安装。拒绝路径穿越、绝对路径、ADS、设备名、链接、大小写碰撞和重复目标；上限 1000 文件、解压总量 100 MiB、单文件 50 MiB，复制时也计数，不能只信 ZIP 元数据。安装不执行、不信任、不启用。

每插件只允许一个待执行维护操作，可取消。更新、回滚、移除只针对用户管理目录，bundled 示例不原地修改。StageUpdate 验证 ID、严格更高版本、API/host 兼容性和内容指纹；暂存副本复验后写原子 journal。下次启动拿到用户根独占锁、发现/加载插件之前 ApplyPending。

Apply 通过同卷目录移动切换代码，保留旧代码历史。更新前只复制数据、设置、后台意图和加密凭据到只读意义上的历史备份，不自动写回它们。备份遇到链接、变化或超过 1 GiB/10000 文件即拒绝更新并保留旧版本；不自动删除历史或业务数据。journal 描述准备、旧包移动、新包落位、提交，重放必须幂等；阶段中断或新副本损坏时恢复可用旧包，未知/损坏 journal 只报告，不能猜路径或清除目录。

宿主备份范围仅为其 UserRoot 管理目录。钉钉通过用户明确连接的独立版规范状态文件、外部 DWS profile 等不自动加入备份，不能把平台包回滚宣传为这些外部数据的备份或降级迁移。业务运行时仍负责规范文件的互斥、格式和发送回执保护。

任何当前进程曾加载过的包都不能 Apply；保守记录进程内已加载包路径，停用不清除。维护成功后新包仍需指纹信任；回滚不继承未经确认的新信任。移除保留数据/设置/凭据，提供代码历史作为恢复来源。

## 原生界面与业务验证

原生导航增加后台任务和凭据管理。凭据只显示配置状态，输入框默认空白，保存/删除由用户明确触发，不自动展示已存值。插件页提供 ZIP/目录安装、暂存更新、回滚、移除、取消维护及等待重启状态；任务页显示安全状态和启停/恢复开关。保持原生 ThemeResource 和停用前移除控件引用。

平台保持没有业务程序集静态引用。独立钉钉适配包必须来自其仓库的明确提交和成功 CI，含 plugin.json、WinUI 插件 DLL、业务 Core 和私有依赖，排除 Desktop EXE/DLL、重复宿主 SDK/WinUI/runtime 和凭据。可在集成测试中保存带来源/SHA256 的固定外部包作为测试输入，避免私有跨仓库 CI 引入额外 PAT；它不是平台分发的默认 bundled 插件，也不在平台仓库维护业务实现。

Windows 测试必须实际安装该包、创建页面和设置、用模拟输入调用真正的业务 Core、启动后台工作、切走页面仍工作、停止不再写状态、退出进程后重新启动验证恢复、验证缺配置/托管任务失败不会损坏状态，并运行重启更新/回滚/移除流程。保留旧 13 项原生检查。脚本记录实际 PID/退出码/包来源与哈希、用户根、状态和检查结果；故障或超时不跳过为通过。当前执行器为 Linux，真正 WinUI/DPAPI/进程检查来自 Windows CI；Server 2025 与 Windows 11 人工验收严格分开。

## 明确边界

不嵌入独立应用窗口，不复用不兼容 Desktop DLL，不声称沙箱或热卸载。不访问真实凭据、不运行真实 DWS/OpenAI/Webhook、不发业务消息。无 Windows 服务、系统登录自动启动或退出期间实时采集承诺。代码回滚与业务数据迁移/发送去重是不同问题；没有完成实际验证的部分明确保留为待验。
