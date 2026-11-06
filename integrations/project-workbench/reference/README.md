# Project Workbench

Windows 原生插件工作台基础版：同一个 WinUI 3 窗口承载独立插件提供的原生页面、设置与异步命令。使用标准 Windows 控件，不嵌入其他应用窗口，不使用 WebView。

当前包含两个独立构建的本地示例：工作摘要（可编辑草稿）和测试用例（可编辑列表、CSV 导出）。示例明确使用预置数据；Teambition、钉钉、Figma、AI 和电脑自动化尚未接入，也不会发送业务消息。

## 在 Windows 构建和启动

主要开发与验收环境为受支持的 **Windows 11 24H2 或更新版本、x64**。安装 [.NET 10 SDK](https://learn.microsoft.com/dotnet/core/install/windows)（仓库固定 `10.0.403`，允许更新补丁）和 PowerShell 7。若使用 IDE，需要 Visual Studio 2026 18.0+；CLI 构建无需安装 IDE。首次还原需要 NuGet 网络访问。

```powershell
pwsh -File .\scripts\build.ps1
.\artifacts\native\app\Workbench.App.exe
```

脚本清理 `artifacts/native`，发布自包含的 .NET/Windows App SDK 应用目录，并分别编译、暂存两个插件包。分发和解压时必须保留 **整个 `artifacts/native/app/` 目录**，不能只复制 EXE。固定 Windows App SDK `1.8.260921001`；不裁剪、不 AOT、不单文件发布。API 目标为 `net10.0-windows10.0.19041.0`，该 API 下限不表示 Windows 10 已获运行时支持或通过兼容性验收；Windows 10 企业版/LTSC 需另行验证。

首次打开“插件”，审查包详情，再点击“启用”并明确确认信任。启用后左侧出现插件页面及各自设置入口。“命令”可生成本地摘要草稿或导出演示 CSV。设置页支持跟随系统、浅色和深色主题。

摘要的未保存编辑在切换页面时保留；点击“保存本地草稿”才写入文件。停用插件或退出会丢弃未保存编辑，重新启用时读取已保存草稿。生成命令只创建第一份草稿，发现现有文件或编辑过的草稿就明确拒绝覆盖；已有草稿请继续在页面编辑并保存。

插件是与主程序同进程运行的可信代码，能够使用当前用户权限。能力声明和程序集加载上下文不是权限沙箱。停用会撤销注册、清除页面引用并请求停止，但不会卸载程序集。更新或删除包前必须退出工作台并重启。

## 本地文件与插件安装

正常运行使用 `%LOCALAPPDATA%/ProjectWorkbench/`；同一个用户根目录只允许一个工作台实例持有写入锁。

```text
ProjectWorkbench/
  host.lock                         # 进程持有的独占句柄；文件本身可在退出后保留
  host-settings.json                # 工作台主题
  plugin-state.json                 # 内容指纹信任与下次启动启用偏好
  Plugins/<plugin-id>/              # 从本地文件夹安装的新包
  settings/<plugin-id>/settings.json
  data/<plugin-id>/                 # 草稿和导出
```

随应用分发的 `app/Plugins/` 是另一个发现目录；运行期间不向该目录写入用户数据。安装 UI 复制选中的本地开发包，不执行、不自动批准、不启用；安装后重新发现。拒绝重复 ID、覆盖更新、路径穿越、符号链接和缺失入口。相同 ID 出现在用户目录与分发目录时，两者都显示诊断。JSON 文件不是凭据保险箱。

## 验证

跨平台真实 Core 测试：

```sh
dotnet test ProjectWorkbench.Core.slnx -c Release --nologo
```

Windows 原生冒烟（先构建）：

```powershell
pwsh -File .\scripts\native-smoke.ps1 -TimeoutSeconds 120
```

冒烟使用新的 `%TEMP%/ProjectWorkbench-smoke/<随机 ID>/` 用户根目录，只复制、发现两个已知分发示例，不读取真实用户设置或已安装插件。它在真实 WinUI UI 线程创建并附加页面和设置控件，执行本地命令；编辑真实摘要 TextBox，跨页面导航后检查草稿、调用保存按钮并验证重复生成保留编辑；停用/重新启用时检查恢复已保存版本，最后停止/关闭，输出 `artifacts/native/native-smoke.json`。失败、超时或无法初始化 WinUI 都返回非零退出码。临时数据保留用于排查，位置写入报告。它不代替主题外观、DPI、键盘和屏幕阅读器的人工验收。

GitHub Actions 对 PR（含草稿）以及 `main`、`feat/native-plugin-workbench` 分支提交运行 Linux Core 测试及 Windows Core/发布/原生冒烟。产物 `workbench-win-x64` 是完整应用目录，`native-smoke-report` 是 JSON 报告，另有两平台测试结果。当前开发环境是 Linux；跨目标 C# 编译不证明 Windows 原生启动成功。[Windows 验证记录和待验清单](docs/windows-verification.md) 区分这些证据。

- [插件 SDK、清单和包布局](docs/plugin-sdk.md)
- [Core 生命周期与存储约定](src/Workbench.Core/README.md)
- [设计范围](docs/superpowers/specs/2026-11-06-native-workbench-design.md)
