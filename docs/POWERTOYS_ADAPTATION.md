# PowerToys 常规设置适配记录

参考固定为 Microsoft 官方仓库 [microsoft/PowerToys 的提交 `2d28a562bfb5f8a76c4a43bbd8c3939f827bb368`](https://github.com/microsoft/PowerToys/commit/2d28a562bfb5f8a76c4a43bbd8c3939f827bb368)。下列来源均以该提交为准。本次主要适配常规设置、主题、语言、更新交接和托盘恢复模式，没有整段搬入 PowerToys 设置程序或 runner。

许可归属见 [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md)。PowerToys 源码采用 MIT；实际引用的 Community Toolkit 包拥有独立的 MIT 许可。两份官方原文分别保存在 [PowerToys-MIT.txt](../LICENSES/PowerToys-MIT.txt) 和 [CommunityToolkit-MIT.txt](../LICENSES/CommunityToolkit-MIT.txt)。

## 逐项对应

| 部分 | 固定版本的官方依据 | 本应用采用方式与差异 |
| --- | --- | --- |
| 设置控件和版本 | [Directory.Packages.props](https://github.com/microsoft/PowerToys/blob/2d28a562bfb5f8a76c4a43bbd8c3939f827bb368/Directory.Packages.props#L22)、[PowerToys.Settings.csproj](https://github.com/microsoft/PowerToys/blob/2d28a562bfb5f8a76c4a43bbd8c3939f827bb368/src/settings-ui/Settings.UI/PowerToys.Settings.csproj#L118)、[GeneralPage.xaml](https://github.com/microsoft/PowerToys/blob/2d28a562bfb5f8a76c4a43bbd8c3939f827bb368/src/settings-ui/Settings.UI/SettingsXAML/Views/GeneralPage.xaml#L181) | 直接引用 `CommunityToolkit.WinUI.Controls.SettingsControls` **8.2.251219**，常规页实际使用 `SettingsCard`、`SettingsExpander`。保留本应用的状态、自动化 ID 和 worker 协议，给行内输入控件设置本地化的无障碍名称。 |
| 主题 | [Services/ThemeService.cs](https://github.com/microsoft/PowerToys/blob/2d28a562bfb5f8a76c4a43bbd8c3939f827bb368/src/settings-ui/Settings.UI/Services/ThemeService.cs)、[Helpers/WindowHelper.cs](https://github.com/microsoft/PowerToys/blob/2d28a562bfb5f8a76c4a43bbd8c3939f827bb368/src/settings-ui/Settings.UI/Helpers/WindowHelper.cs#L49) | `system/light/dark` 对应 `ElementTheme.Default/Light/Dark`，设置窗口根元素 `RequestedTheme`。由实际主题驱动页面和对话框；单窗口应用沿用集中处理方法。 |
| 原生标题按钮 | [Controls/TitleBar/TitleBar.WASDK.cs](https://github.com/microsoft/PowerToys/blob/2d28a562bfb5f8a76c4a43bbd8c3939f827bb368/src/settings-ui/Settings.UI/SettingsXAML/Controls/TitleBar/TitleBar.WASDK.cs#L51) | 监听根元素 `ActualThemeChanged`，更新原生标题按钮前景和透明背景。本应用继续使用已有的原生 TitleBar，并保留高对比度处理。 |
| 语言启动顺序 | [SettingsXAML/App.xaml.cs](https://github.com/microsoft/PowerToys/blob/2d28a562bfb5f8a76c4a43bbd8c3939f827bb368/src/settings-ui/Settings.UI/SettingsXAML/App.xaml.cs#L86)、[ManagedCommon/LanguageHelper.cs](https://github.com/microsoft/PowerToys/blob/2d28a562bfb5f8a76c4a43bbd8c3939f827bb368/src/common/ManagedCommon/LanguageHelper.cs) | 采用独立 `language.json` 作为启动用的界面语言快照，在 `InitializeComponent` 前设置 `Microsoft.Windows.Globalization.ApplicationLanguages.PrimaryLanguageOverride`。快照仅供界面启动使用，不包含凭据或项目数据；业务配置继续由 worker 管理。 |
| 语言资源与生效提示 | [Helpers/ResourceLoaderInstance.cs](https://github.com/microsoft/PowerToys/blob/2d28a562bfb5f8a76c4a43bbd8c3939f827bb368/src/settings-ui/Settings.UI/Helpers/ResourceLoaderInstance.cs)、[GeneralViewModel.cs](https://github.com/microsoft/PowerToys/blob/2d28a562bfb5f8a76c4a43bbd8c3939f827bb368/src/settings-ui/Settings.UI/ViewModels/GeneralViewModel.cs#L1001)、[Strings/en-us/Resources.resw](https://github.com/microsoft/PowerToys/blob/2d28a562bfb5f8a76c4a43bbd8c3939f827bb368/src/settings-ui/Settings.UI/Strings/en-us/Resources.resw) | PowerToys 使用 PRI、`x:Uid` 和 `.resw`，语言变更提示重启。本应用保留已覆盖界面的独立英文 JSON 与 `UiStrings.T/F`，同步 `RootGrid.Language`，保存后可显式重启以更新启动期原生资源。只提供 `system/zh-CN/en-US`；中文系统语言归简体中文，其他系统语言回退 English。 |
| 更新交接的进程身份 | [Update/PowerToys.Update.cpp：InstallNewVersionStage1](https://github.com/microsoft/PowerToys/blob/2d28a562bfb5f8a76c4a43bbd8c3939f827bb368/src/Update/PowerToys.Update.cpp#L185) | 借鉴在旧进程退出前取得其进程句柄的模式，等待父进程真正结束，再竞争单实例锁。等待超时、权限失败或无法确认交接时应停止新进程，不能直接照搬上游忽略等待返回值的行为。 |
| 更新启动前的文件读锁 | [Update/PowerToys.Update.cpp：InstallNewVersionStage2](https://github.com/microsoft/PowerToys/blob/2d28a562bfb5f8a76c4a43bbd8c3939f827bb368/src/Update/PowerToys.Update.cpp#L238)、[common/updating/installer.cpp](https://github.com/microsoft/PowerToys/blob/2d28a562bfb5f8a76c4a43bbd8c3939f827bb368/src/common/updating/installer.cpp#L244) | 借鉴以只读共享打开待启动文件、拒绝写入及删除共享，并跨校验和启动保留句柄的模式。在持有 EXE 读锁后再次执行 worker 的 `prepare_update`，重新核对来源、包摘要和展开目录后启动。读锁只固定被打开的 EXE，不能据此宣称整个目录不可修改。 |
| 托盘恢复 | [runner/tray_icon.cpp](https://github.com/microsoft/PowerToys/blob/2d28a562bfb5f8a76c4a43bbd8c3939f827bb368/src/runner/tray_icon.cpp#L262) | 借鉴初次注册失败后的重试及 Explorer `TaskbarCreated` 重注册。本应用在托盘关闭或注册失败时保持可见窗口，禁用依赖托盘的隐藏选项；保留现有显示和退出入口。 |

实现对应位置为 [MainWindow.General.cs](../winui/UPCAMTaskDigest/MainWindow.General.cs)、[MainWindow.xaml.cs](../winui/UPCAMTaskDigest/MainWindow.xaml.cs)、[App.xaml.cs](../winui/UPCAMTaskDigest/App.xaml.cs)、[NativeDesktop.cs](../winui/UPCAMTaskDigest/NativeDesktop.cs)、[UiStrings.cs](../winui/UPCAMTaskDigest/UiStrings.cs)、[worker_api.py](../upcam_app/worker_api.py) 和 [updates.py](../upcam_app/updates.py)。该表记录采用方式；实际验收以最终源码和 Windows CI 证据为准。

## 更新的信任与恢复边界

PowerToys 在锁定的安装器句柄上运行 `WinVerifyTrust`，并检查 Microsoft 发布者及 PowerToys 产品身份。本应用没有移植这套 Authenticode、Microsoft 发布者、产品标识或 MSI 身份验证，也没有获得 Microsoft 的签名身份。界面中的“校验”指固定仓库的 HTTPS Release 来源、元数据与 SHA-256 完整性检查，不表示二进制已经过 Authenticode 验证。

本应用使用 `ThaoKeeton/upcam-task-digest` 的正式稳定 Release，采用当前用户目录的完整 ZIP 分目录安装。安装前重新检查已保存包与展开目录；旧目录保留，新进程成功初始化后才修复已启用的用户登录启动路径。详细下载、归档和来源限制见 [UPDATES.md](../UPDATES.md)。仓库权限和维护者账户安全仍属于更新信任边界。

更新包 EXE 的读锁缩小该文件在校验与启动之间被替换的窗口。它不锁住所有 DLL、运行时、worker 或父目录，不是同一用户下恶意进程的完整隔离边界。二次整包核验与 EXE 读锁应分别说明其范围。

保留旧版本目录供用户手动恢复，不宣称已实现通用自动回滚。新进程启动失败、完整性检查失败、超时或启动项修复失败，各自需要明确结果；旧目录存在本身不能证明运行状态已经恢复。更新不会以强制终止的方式丢弃未保存设置或中断正在发送的任务。

## 本轮不适用的上游功能

- PowerToys 的完整设置 ViewModel、runner IPC、模块管理和更新器没有整套移植；本应用继续采用既有 WinUI 与 Python worker 的职责划分。
- 保留当前用户已有的登录启动集成。PowerToys 的任务计划、管理员运行、UAC 提权和系统范围安装不属于本应用的常规设置适配范围。
- 本轮不引入 PowerToys 的其他工具、全局快捷键、预览通道、备份产品或遥测配置。
- 语言菜单只列实际实现的两种语言及系统默认；不复制 PowerToys 的其他语言选项。项目标题、成员姓名、映射、日报 Markdown 和原始诊断维持业务原文。

## 核对与分发

- 两份许可证文件为官方原文副本，保留原字符和换行。PowerToys 源文件对应 Git blob `b2d78c9e34231fea76d5eaf8a6d80dbb737b2948`；Community Toolkit 对应 Git blob `5aefa46c69bc08edf566449260feb5c025d7a39b`，后者的固定原文来源详见第三方声明。
- 控件包版本为 `8.2.251219`。本应用保持 `Microsoft.WindowsAppSDK 2.6.5` 和 `WinUIEx 2.8.0`；它们的上游许可与随包声明继续适用，本记录不猜测或重新指定其许可证。
- 构建发行包需带上 `THIRD_PARTY_NOTICES.md` 和 `LICENSES` 中的原文，并保留其他依赖随包提供的许可声明。
- 包引用成功、模式适配完成与运行验收是分别需要证据的事项。原生语言资源、浅深色及系统主题、窄窗口布局、托盘不可用恢复和更新交接，以目标 Windows 环境的合成测试与构建产物为证据；本文不自行宣布这些检查已通过。
