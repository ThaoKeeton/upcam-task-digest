using System.Text.Json;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.Windows.Storage.Pickers;
using Workbench.Core;

namespace Workbench.App;

internal sealed partial class MainWindow
{
    private StackPanel HomePage()
    {
        var page = Ui.Page("项目工作台", "一个原生窗口，承载独立插件的页面、命令与设置。");
        var enabled = Manager.Plugins.Count(p => p.State == PluginState.Enabled);
        page.Children.Add(Ui.Card(Ui.Text($"已发现 {Manager.Plugins.Count} 个包 · 已启用 {enabled} 个插件 · {Manager.Contributions.Commands.Count} 个命令", 20)));
        page.Children.Add(Ui.Text("开始使用", 20));
        page.Children.Add(Ui.Text("打开“插件”，审查本地包并明确确认信任后启用。启用后，原生页面和插件设置会出现在左侧导航。"));
        page.Children.Add(Ui.Text("此版本仅含工作摘要和测试用例两个本地演示。Teambition、钉钉、AI 与电脑自动化尚未接入。示例不读取真实业务数据。"));
        page.Children.Add(Ui.Text("插件与主程序同进程运行；停用会撤销注册并请求停止，但不会卸载程序集。更新或移除插件前请退出工作台。"));
        AddDiagnostics(page);
        return page;
    }

    private StackPanel PluginsPage()
    {
        var page = Ui.Page("插件", "清单发现与本地安装不会运行插件代码。首次启用需要明确的信任确认。");
        var actions = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 12 };
        var install = Ui.Named(new Button { Content = "从本地文件夹安装" }, "从本地文件夹安装插件包");
        install.Click += async (_, _) => await RunAsync(InstallAsync);
        var refresh = Ui.Named(new Button { Content = "重新发现" }, "重新发现本地插件包");
        refresh.Click += async (_, _) => await RunAsync(async () =>
        {
            DetachPluginViews();
            await Manager.DiscoverAsync();
            RefreshNavigation();
            Navigate(new("plugins"));
        });
        actions.Children.Add(install);
        actions.Children.Add(refresh);
        page.Children.Add(actions);
        page.Children.Add(Ui.Text($"可写安装目录：{Path.Combine(_options.UserRoot, "Plugins")}\n随应用分发的包目录：{Path.Combine(AppContext.BaseDirectory, "Plugins")}"));
        AddDiagnostics(page, includePackages: false);
        foreach (var package in Manager.Plugins)
        {
            var manifest = package.Manifest;
            var body = new StackPanel { Spacing = 10 };
            body.Children.Add(Ui.Text(manifest?.Name ?? Path.GetFileName(package.PackagePath), 20));
            body.Children.Add(Ui.Text(manifest is null ? "清单无效" : $"{manifest.Id} · v{manifest.Version} · API {manifest.ApiVersion}\n{manifest.Description}"));
            body.Children.Add(Ui.Text($"状态：{package.State} · 已信任：{package.IsTrusted} · 下次启动启用：{package.DesiredEnabled}\n{package.PackagePath}"));
            if (manifest is not null) body.Children.Add(Ui.Text($"声明能力：{string.Join("、", manifest.Capabilities)}"));
            foreach (var diagnostic in package.Diagnostics) body.Children.Add(Ui.Text($"[{diagnostic.Code}] {diagnostic.Message}"));
            if (manifest is not null)
            {
                var id = manifest.Id;
                var button = Ui.Named(new Button
                {
                    Content = package.State == PluginState.Enabled ? "停用" : "启用",
                    IsEnabled = package.State != PluginState.Invalid
                }, $"{(package.State == PluginState.Enabled ? "停用" : "启用")} {manifest.Name}");
                button.Click += async (_, _) => await RunAsync(() => package.State == PluginState.Enabled ? DisableAsync(id) : EnableAsync(package));
                body.Children.Add(button);
            }
            page.Children.Add(Ui.Card(body));
        }
        return page;
    }

    private async Task InstallAsync()
    {
        var picker = new FolderPicker(AppWindow.Id) { CommitButtonText = "安装此插件包" };
        var folder = await picker.PickSingleFolderAsync();
        if (folder is null) return;
        var result = await Manager.InstallAsync(folder.Path);
        if (result.Succeeded)
        {
            DetachPluginViews();
            await Manager.DiscoverAsync();
            RefreshNavigation();
            Navigate(new("plugins"));
        }
        Notice(result.Succeeded ? "已安装，尚未启用" : "安装失败", result.Message,
            result.Succeeded ? InfoBarSeverity.Success : InfoBarSeverity.Error);
    }

    private StackPanel CommandsPage()
    {
        var page = Ui.Page("命令", "只执行当前启用插件的命令。运行结果与错误显示在窗口上方。");
        if (Manager.Contributions.Commands.Count == 0) page.Children.Add(Ui.Text("暂无命令。请先启用本地示例插件。"));
        foreach (var command in Manager.Contributions.Commands)
        {
            var body = new StackPanel { Spacing = 10 };
            body.Children.Add(Ui.Text(command.Title, 20));
            body.Children.Add(Ui.Text($"{command.PluginId} / {command.Id}\n{command.Description}"));
            var button = Ui.Named(new Button { Content = "运行" }, $"运行 {command.Title}");
            button.Click += async (_, _) => await RunAsync(async () =>
            {
                var result = await Manager.ExecuteCommandAsync(command.PluginId, command.Id);
                Notice(result.Succeeded ? "命令完成" : "命令失败", result.Message,
                    result.Succeeded ? InfoBarSeverity.Success : InfoBarSeverity.Error);
            });
            body.Children.Add(button);
            page.Children.Add(Ui.Card(body));
        }
        AddDiagnostics(page);
        return page;
    }

    private StackPanel SettingsPage()
    {
        var page = Ui.Page("工作台设置", "主题作用于同一个窗口内的主程序和原生插件控件。");
        var theme = Ui.Named(new ComboBox { Header = "应用主题", HorizontalAlignment = HorizontalAlignment.Stretch }, "应用主题");
        theme.Items.Add("跟随系统"); theme.Items.Add("浅色"); theme.Items.Add("深色");
        theme.SelectedIndex = (int)_root.RequestedTheme;
        theme.SelectionChanged += async (_, _) => await RunAsync(async () =>
        {
            _root.RequestedTheme = (ElementTheme)theme.SelectedIndex;
            var path = Path.Combine(_options.UserRoot, "host-settings.json");
            var temporary = path + ".tmp";
            await File.WriteAllTextAsync(temporary, JsonSerializer.Serialize(new HostPreferences(_root.RequestedTheme)));
            File.Move(temporary, path, overwrite: true);
            Notice("设置已保存", "主题偏好将在下次启动时恢复。", InfoBarSeverity.Success);
        });
        page.Children.Add(theme);
        page.Children.Add(Ui.Text($"本地用户目录：{_options.UserRoot}"));
        page.Children.Add(Ui.Text("插件设置通过左侧各插件的“设置”入口打开。JSON 设置与数据文件不是凭据保险箱。"));
        AddDiagnostics(page);
        return page;
    }

    private void LoadTheme()
    {
        var path = Path.Combine(_options.UserRoot, "host-settings.json");
        if (!File.Exists(path)) return;
        try
        {
            var preference = JsonSerializer.Deserialize<HostPreferences>(File.ReadAllText(path));
            if (preference is null || !Enum.IsDefined(preference.Theme)) throw new JsonException("无效的主题值。");
            _root.RequestedTheme = preference.Theme;
        }
        catch (Exception error) when (error is JsonException or IOException or UnauthorizedAccessException)
        {
            Notice("主题设置无法读取", $"使用系统主题。{error.Message}", InfoBarSeverity.Warning);
        }
    }

    private void AddDiagnostics(StackPanel page, bool includePackages = true)
    {
        var messages = Manager.Diagnostics.Select(d => $"[主程序/{d.Code}] {d.Message}");
        if (includePackages) messages = messages.Concat(Manager.Plugins.SelectMany(p => p.Diagnostics.Select(d => $"[{p.Manifest?.Id ?? p.PackagePath}/{d.Code}] {d.Message}")));
        var lines = messages.Distinct().ToArray();
        if (lines.Length == 0) return;
        page.Children.Add(Ui.Text("诊断", 20));
        page.Children.Add(Ui.Text(string.Join("\n\n", lines)));
    }

    private sealed record HostPreferences(ElementTheme Theme);
}
