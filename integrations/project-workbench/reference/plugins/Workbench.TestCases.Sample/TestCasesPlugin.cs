using System.Text;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Automation;
using Microsoft.UI.Xaml.Controls;
using Workbench.Sdk;
using Workbench.WinUI.Sdk;

namespace Workbench.TestCases.Sample;

public sealed class TestCasesPlugin : IWorkbenchPlugin
{
    private string _suiteName = "原生工作台演示";
    private readonly List<SampleCase> _rows =
    [
        new("EX-001", "启用本地插件", "原生页面出现在主窗口"),
        new("EX-002", "编辑摘要示例", "草稿保存到本地数据目录"),
        new("EX-003", "停用示例插件", "页面与命令入口撤销")
    ];

    public async Task InitializeAsync(IPluginContext context, CancellationToken cancellationToken)
    {
        _suiteName = await context.Settings.GetAsync("suiteName", "原生工作台演示", cancellationToken);
        context.RegisterSetting(new("suiteName", "示例用例集名称", "原生工作台演示"));
        context.RegisterView(new WinUiView("cases", "测试用例 · 示例", () => CreatePage(context)));
        context.RegisterView(new WinUiView("preferences", "测试用例", () => CreateSettings(context), WinUiViewKind.Settings));
        context.RegisterCommand(new("export-csv", "导出本地示例 CSV", async token =>
        {
            context.Lifetime.ThrowIfCancellationRequested();
            var lines = new[] { "suite,id,action,expected" }.Concat(_rows.Select(row => string.Join(",", new[] { _suiteName, row.Id, row.Action, row.Expected }.Select(Csv))));
            var path = Path.Combine(context.DataDirectory, "sample-test-cases.csv");
            await File.WriteAllTextAsync(path, string.Join("\r\n", lines) + "\r\n", new UTF8Encoding(true), token);
            return new(true, $"演示 CSV 已导出到本地：{path}。未执行真实测试或电脑自动化。");
        }, "导出当前本地示例行，不读取业务系统、不执行自动化。"));
    }

    private FrameworkElement CreatePage(IPluginContext context)
    {
        context.Lifetime.ThrowIfCancellationRequested();
        var panel = Panel("测试用例 · 本地示例", "编辑以下预置示例行，再到“命令”运行“导出本地示例 CSV”。编辑内容保留到停用，CSV 文件保留在本地。");
        panel.Children.Add(Text("这些用例是示例数据，不表示实际测试已执行或通过。"));
        var list = Named(new ListView { SelectionMode = ListViewSelectionMode.None, HorizontalContentAlignment = HorizontalAlignment.Stretch }, "可编辑的示例测试用例列表");
        foreach (var row in _rows)
        {
            var cell = new Grid { ColumnSpacing = 12, Padding = new Thickness(0, 8, 0, 8), HorizontalAlignment = HorizontalAlignment.Stretch };
            cell.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
            cell.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            cell.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            var id = Text(row.Id); id.VerticalAlignment = VerticalAlignment.Center;
            var action = Named(new TextBox { Header = "步骤", Text = row.Action, TextWrapping = TextWrapping.Wrap, AcceptsReturn = true }, $"{row.Id} 示例步骤");
            var expected = Named(new TextBox { Header = "预期结果", Text = row.Expected, TextWrapping = TextWrapping.Wrap, AcceptsReturn = true }, $"{row.Id} 示例预期结果");
            action.TextChanged += (_, _) => { if (!context.Lifetime.IsCancellationRequested) row.Action = action.Text; };
            expected.TextChanged += (_, _) => { if (!context.Lifetime.IsCancellationRequested) row.Expected = expected.Text; };
            Grid.SetColumn(action, 1); Grid.SetColumn(expected, 2);
            cell.Children.Add(id); cell.Children.Add(action); cell.Children.Add(expected);
            list.Items.Add(cell);
        }
        panel.Children.Add(list);
        panel.Children.Add(Text($"本地导出位置：{Path.Combine(context.DataDirectory, "sample-test-cases.csv")}"));
        return Scroll(panel);
    }

    private FrameworkElement CreateSettings(IPluginContext context)
    {
        context.Lifetime.ThrowIfCancellationRequested();
        var panel = Panel("测试用例 · 示例设置", "此偏好随插件 JSON 设置保存；不连接任何测试管理系统。");
        var name = Named(new TextBox { Header = "示例用例集名称", Text = _suiteName }, "示例用例集名称");
        var status = Text("名称写入导出文件的 suite 列。");
        var save = Named(new Button { Content = "保存偏好" }, "保存测试用例示例偏好");
        save.Click += async (_, _) =>
        {
            try
            {
                context.Lifetime.ThrowIfCancellationRequested();
                var value = name.Text;
                await context.Settings.SetAsync("suiteName", value, context.Lifetime);
                _suiteName = value;
                status.Text = "偏好已保存。";
            }
            catch (OperationCanceledException) { status.Text = "插件已停用，操作已取消。"; }
            catch (Exception error) { status.Text = $"保存失败：{error.Message}"; }
        };
        panel.Children.Add(name); panel.Children.Add(save); panel.Children.Add(status);
        return Scroll(panel);
    }

    public Task StopAsync(CancellationToken cancellationToken) => Task.CompletedTask;
    private static string Csv(string text) => "\"" + text.Replace("\"", "\"\"") + "\"";
    private sealed class SampleCase(string id, string action, string expected)
    {
        public string Id { get; } = id;
        public string Action { get; set; } = action;
        public string Expected { get; set; } = expected;
    }
    private static StackPanel Panel(string title, string description)
    {
        var panel = new StackPanel { Spacing = 16, Padding = new Thickness(28) };
        panel.Children.Add(Text(title, 28)); panel.Children.Add(Text(description)); return panel;
    }
    private static TextBlock Text(string value, double size = 14) => new() { Text = value, FontSize = size, TextWrapping = TextWrapping.Wrap, IsTextSelectionEnabled = true };
    private static ScrollViewer Scroll(UIElement content) => new() { Content = content, HorizontalScrollBarVisibility = ScrollBarVisibility.Disabled, HorizontalContentAlignment = HorizontalAlignment.Stretch };
    private static T Named<T>(T element, string name) where T : DependencyObject { AutomationProperties.SetName(element, name); return element; }
}
