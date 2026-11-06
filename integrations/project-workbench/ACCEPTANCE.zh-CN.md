# UPCAM 合成数据验收包

供 project-workbench 联合验收使用。插件 ID 为 `com.upcam.task-digest.acceptance`，版本 0.1.0；它是使用 `AcceptanceBuild=true` 编译的独立变体，不能通过重命名正式包来获得。

## 下载内容与兼容范围

下载 CI 产物 `UPCAMTaskDigest-workbench-acceptance-0.1.0-windows` 并解压，得到：

- `UPCAMTaskDigest-workbench-acceptance-0.1.0-win-x64.zip`：可安装插件。
- 同名 `.sha256`：内层插件 ZIP 的 SHA-256。
- `acceptance-handoff.json`：实际 ZIP 哈希、字节数、构建提交、源码头提交、源码树和 SDK 版本。
- 本说明文件。

宿主最低版本为 0.2.0、API 1、Runtime Services 1；锁定 SDK 源码为 `d2005d469aef6a385c134b6914fb6dfa9cff20b2`，.NET 10 / Windows App SDK `1.8.260921001`，Windows x64。其他宿主版本需另行验证。

PR 构建中，`buildCommit` 是 GitHub 的测试合并提交，`sourceHeadCommit` 是该 PR 的源码提交；`sourceTree` 标识实际构建内容。哈希可检测下载或构建内容变化，不代表独立可信的发布者签名。

## 安装

1. 在独立 Windows 测试账户，或平台自行创建的全新隔离测试用户根目录中运行宿主。不要清空已有用户目录来制造隔离；本包不提供额外宿主启动参数。
2. 完全退出 UPCAM 独立版并停用正式日报插件，释放它们与验收插件共用的工作进程互斥标识。
3. 先核对内层 ZIP 的 SHA-256，再完整解压。安装时选择含 `plugin.json` 的 **`com.upcam.task-digest.acceptance`** 文件夹，不能选择 `worker` 子目录或 ZIP 文件本身。
4. 使用宿主现有的本地文件夹安装流程，或实际 `PluginManager.InstallAsync` / `DiscoverAsync` / `EnableAsync` 流程。安装不等于启用，首次启用仍使用宿主的明确信任机制。
5. 打开插件原生页面。应看到 **“CI 虚构数据模式 · 全部服务操作均为模拟。”** 和 `Ready · CI synthetic data`。若没有这些标识，停止本次验收，检查是否选错正式包。

本包只创建自己的默认合成数据目录，包含 `.plugin-synthetic.json` 标记。**不填写真实凭据，不导入真实配置，不接续真实独立版数据目录。** 未标记的非空目录或真实凭据文件会被拒绝，合成目录也不能被正式版当作生产目录打开。

## 联合验收操作顺序

以下顺序与 UPCAM 的原生 CI 用例一致；所有外部服务均由本地模拟替身提供，**不访问真实 Webhook，不向钉钉发送消息**，不请求真实 Teambition 或 OpenAI API。

1. 打开日报页，点击“预览虚构样例”，检查正文与原生页面正常显示。此操作不产生模拟发送记录。
2. 设置页 →“项目范围”→“只读验证项目并发现映射 ID”，使用默认虚构项目和时间段；点击“保存本机业务配置”。保持自动发送关闭。
3. 设置页 →“账户与模型”→ OpenAI →“刷新已保存 Key 可用模型”。保留预置模型，无需填写任何 API 密钥或 Webhook。
4. 日报页 →“运行与诊断”→“只读诊断”，应出现模拟诊断结果。
5. 点击“生成并发送日报”。**在这个验收变体中只执行模拟发送**，随后到“运行记录”点击“刷新运行记录”，应看到一条模拟成功记录。此记录不是钉钉送达证据。
6. 在设置页或手动时间段中编辑草稿并切换导航，检查草稿保留；停用插件后检查私有工作进程结束，再重新启用检查持久化数据。

需要测试后台任务时，先在“自动运行”中把两个执行时间设置为未来、明确开启自动发送并保存，然后使用“启动主机自动任务”“停止主机自动任务”和单独的启动恢复开关。保持测试账户与数据隔离。SDK 的 Running 状态只表示执行已被调度；完成授权应结合 `scheduler-status` 命令的 `lastCheckSucceeded`、`lastCheckOperation` 和检查版本判断。恢复失败不得生成替代授权或重放请求。

平台使用自己的隔离验收宿主时，可通过实际贡献目录获取 `WinUiView`，在 UI 线程创建并附加页面。已注册的 `status`、`preview-sample`、`scheduler-start`、`scheduler-stop` 和 `scheduler-status` 命令由实际 `PluginManager.ExecuteCommandAsync` 调用。不要直接反射创建插件入口，也不要把它作为另一个应用窗口嵌套。

## 结果和清理

保留平台与 UPCAM 的 JSON 验收报告及必要截图。模拟历史中的 `sent` 仅代表本地替身返回成功，不证明任何真实消息已送达。不要导出凭据文件或生成的恢复校验密钥。

退出宿主或停用插件，确认其私有工作进程已结束后再清理本次专用测试目录。保留实际安装的插件包用于复现；生产配置和真实数据不属于清理范围。CI 下载产物通常保留 14 天，以该次产物记录的到期时间为准。
