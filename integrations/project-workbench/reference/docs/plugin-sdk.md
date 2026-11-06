# 原生插件 SDK

插件独立编译为类库，通过本地清单发现，在用户确认信任后加载到工作台进程。主程序没有对两个示例插件的项目或程序集引用；构建脚本在发布之后单独暂存插件包。

## 契约与运行时

`Workbench.Sdk` 目标为 `net10.0`，不引用 UI。API 版本为 `WorkbenchApi.Version == 1`。入口类型是实现 `IWorkbenchPlugin` 的具体类，具有公共无参数构造函数。

两个 SDK 的包版本为 `0.2.0`，文件版本为 `0.2.0.0`，程序集版本保持 `1.0.0.0`。旧的 API 1 和 `IPluginContext` 不变。可选扩展 `WorkbenchRuntimeServices.Version == 1` 提供后台任务和声明式凭据存储；使用它的插件必须声明 `minimumHostVersion: "0.2.0"`。契约、恢复语义、停止所有权和凭据安全边界见 [运行时服务 v1](runtime-services.md)。

```csharp
public interface IWorkbenchPlugin
{
    Task InitializeAsync(IPluginContext context, CancellationToken cancellationToken);
    Task StopAsync(CancellationToken cancellationToken);
}

public interface IPluginContext
{
    string PluginId { get; }
    string DataDirectory { get; }
    CancellationToken Lifetime { get; }
    IPluginSettings Settings { get; }
    void RegisterView(IPluginView view);
    void RegisterCommand(PluginCommand command);
    void RegisterSetting(PluginSetting setting);
}
```

只在 `InitializeAsync` 内注册。初始化成功前贡献不会发布；失败或取消会撤销注册并请求停止。工厂只注册，不要在初始化中创建 WinUI 控件。`DataDirectory` 在激活时由 Core 创建，路径为所选 `UserRoot/data/<plugin-id>`，拒绝现有路径中的符号链接。草稿、CSV 和其他非设置文件应使用此目录；它是存储约定，不限制可信插件的文件访问权限。不要自行推导真实用户目录，否则会破坏隔离冒烟。

`IPluginSettings.GetAsync<T>(key, defaultValue, token)` 和 `SetAsync<T>(key, value, token)` 支持可序列化的 JSON 值，按插件保存到 `settings/<plugin-id>/settings.json`。损坏或类型不匹配的值返回默认值并给出诊断。`PluginSetting` 是字符串设置的描述元数据，原生设置页面由插件提供。

本地视图、命令和设置 ID 长度最多 128，格式为 `[A-Za-z][A-Za-z0-9]*(?:[._-][A-Za-z0-9]+)*` 的整串匹配，不能包含路径或尾部换行；标题非空且最多 200 字符。插件 ID 使用小写点分命名空间，例如 `com.example.notes`，可在分段中使用单连字符分隔。

## WinUI 原生页面和设置

引用 `Workbench.WinUI.Sdk`，目标为 `net10.0-windows10.0.19041.0`，Windows App SDK 固定 `1.8.260921001`、x64。强类型视图定义如下：

```csharp
public enum WinUiViewKind { Page, Settings }
public sealed record WinUiView(
    string Id, string Title, Func<FrameworkElement> CreateView,
    WinUiViewKind Kind = WinUiViewKind.Page) : IPluginView;
```

注册示例：

```csharp
context.RegisterView(new WinUiView("notes", "本地笔记", () =>
{
    context.Lifetime.ThrowIfCancellationRequested();
    return new TextBox { Header = "笔记", AcceptsReturn = true };
}));
context.RegisterView(new WinUiView("preferences", "笔记",
    () => CreatePreferences(context), WinUiViewKind.Settings));
context.RegisterCommand(new PluginCommand("export", "本地导出", async token =>
{
    context.Lifetime.ThrowIfCancellationRequested();
    var path = Path.Combine(context.DataDirectory, "notes.txt");
    await File.WriteAllTextAsync(path, "示例", token);
    return new PluginCommandResult(true, $"已保存：{path}");
}));
```

主程序用最小的 `App.xaml` 声明标准控件资源和主程序卡片样式，由固定版本的 WinUI SDK 生成入口、XAML 元数据与 `InitializeComponent`，并构建主程序自己的资源图。业务页面及插件工厂仍由 C# 创建；这不增加外部插件 XAML/PRI 加载接口，也不改变单个 WinUI 主窗口的架构。

主程序在 WinUI UI 线程调用工厂，返回的 `FrameworkElement` 直接附加到同一个窗口。使用代码创建标准控件、继承主题，给可操作控件设置 AutomationProperties.Name，并让布局随窗口尺寸伸缩。主程序卡片使用固定的主程序 ThemeResource 样式以跟随各窗口实际主题；这不增加插件资源加载接口。v0.1 不加载插件自带 XAML/PRI，不嵌入独立应用窗口，不使用 WebView。

导航会重新调用视图工厂，不缓存控件。可编辑文档应由插件实例持有状态，并由控件事件更新；保存时先捕获一个值再等待写入。摘要示例在页面切换时保留未保存文本，生成命令拒绝覆盖现有或已编辑草稿。停用或退出前需要显式保存，重新启用只恢复磁盘上的草稿；主程序没有通用文档保存/丢弃协议。

命令通过 `PluginManager.ExecuteCommandAsync` 运行，令牌链接请求取消与插件 `Lifetime`。命令应返回简洁的可显示结果。异步 UI 事件处理器应捕获异常并显示本地错误，同时尊重 `Lifetime`；不要把未处理的 `async void` 异常抛给主程序。插件创建的后台任务、订阅和资源由插件在停止时清理，不能假定停用会强制终止代码。

## 清单与目录

```json
{
  "id": "com.example.notes",
  "name": "本地笔记",
  "version": "0.1.0",
  "apiVersion": 1,
  "minimumHostVersion": "0.1.0",
  "entryAssembly": "Example.Notes.dll",
  "entryType": "Example.Notes.NotesPlugin",
  "description": "本地笔记示例",
  "capabilities": ["native-views", "local-commands", "settings", "local-files"]
}
```

```text
com.example.notes/
  plugin.json
  Example.Notes.dll
  Example.Notes.deps.json
  <插件自己的私有依赖 DLL 或包数据>
```

使用 `<EnableDynamicLoading>true</EnableDynamicLoading>`，保留 `.deps.json` 和所有私有依赖。不要复制 `Workbench.Sdk`、`Workbench.WinUI.Sdk`、WinUI、WinRT 或 .NET 框架程序集到插件目录；它们与主程序共享类型身份。两个现有示例没有私有依赖，所以构建脚本仅暂存各自 DLL、deps 和清单。新增插件若带私有依赖，需要将其纳入独立包的构建暂存步骤。

主程序共享两个 SDK、`Microsoft.WinUI`、`WinRT.Runtime`、`Microsoft.Windows.SDK.NET` 与实际分发的 Windows 投影程序集。Core 从真实 .NET 参考程序集目录生成框架程序集清单；同目录中的主程序私有 DLL 不会自动成为共享依赖。插件私有托管依赖必须从包内解析。此机制用于类型身份和依赖管理，不是安全隔离。

## 主程序生命周期与信任

启动顺序为获取用户根目录独占锁、发现清单、显示诊断，再调用 `RestoreEnabledAsync`。发现和安装均不执行代码。首次启用只有在 UI 同次进程明确确认后才调用 `EnableAsync(id, confirmTrust: true)`。信任绑定包内所有相对路径与内容的指纹；内容变化撤销继承信任。分发包也需要正常用户确认；仅明确启动的隔离冒烟会自动批准两个已知示例。

安装成功后主程序再次调用 `DiscoverAsync`。安装不覆盖现有 ID，不自动批准或启用。包错误显示在对应详情中，用户状态文件等管理器级错误也显示在首页和插件页。

停用前主程序移除当前控件与导航引用，然后调用 `DisableAsync`，即使停止失败也不继续引用这些页面。关闭应用使用 `ShutdownAsync`/`DisposeAsync`，保留下次启动的启用偏好；不能通过逐个 `DisableAsync` 实现退出。程序集不可收集，更新、移除或替换包需要退出/重启。无响应或恶意的同进程插件仍可阻塞或破坏整个进程。

可参考 [摘要示例](../plugins/Workbench.Summary.Sample/SummaryPlugin.cs)、[测试用例示例](../plugins/Workbench.TestCases.Sample/TestCasesPlugin.cs) 和 [Core 文档](../src/Workbench.Core/README.md)。
