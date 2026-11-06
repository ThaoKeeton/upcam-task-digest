using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Automation;
using Microsoft.UI.Xaml.Controls;
using Workbench.Sdk;
using Workbench.WinUI.Sdk;

namespace Workbench.Summary.Sample;

public sealed class SummaryPlugin : IWorkbenchPlugin
{
    private const string EmptyDraft = "先在“命令”中运行“生成本地示例摘要”，或直接在此编辑。";
    private string _audience = "项目团队";
    private string _draftText = EmptyDraft;
    private bool _hasDraft;

    public async Task InitializeAsync(IPluginContext context, CancellationToken cancellationToken)
    {
        _audience = await context.Settings.GetAsync("audience", "项目团队", cancellationToken);
        var path = Path.Combine(context.DataDirectory, "summary-draft.txt");
        _hasDraft = File.Exists(path);
        _draftText = _hasDraft ? await File.ReadAllTextAsync(path, cancellationToken) : EmptyDraft;
        context.RegisterSetting(new("audience", "摘要读者", "项目团队", "仅用于本地示例草稿。"));
        context.RegisterView(new WinUiView("summary", "工作摘要 · 示例", () => CreatePage(context)));
        context.RegisterView(new WinUiView("preferences", "工作摘要", () => CreateSettings(context), WinUiViewKind.Settings));
        context.RegisterCommand(new("generate-draft", "生成本地示例摘要", async token =>
        {
            context.Lifetime.ThrowIfCancellationRequested();
            token.ThrowIfCancellationRequested();
            if (_hasDraft || File.Exists(path))
                return new(false, "已有或已编辑的草稿已保留，生成命令不会覆盖。请在工作摘要页面继续编辑，并点击“保存本地草稿”保存。");
            var text = $"本地演示摘要\n读者：{_audience}\n\n已完成：原生插件页面原型。\n进行中：本地测试用例整理。\n待确认：下一次评审时间。\n\n以上均为预置示例，不来自真实业务系统，也未调用 AI。";
            // CreateNew also protects an existing file if it appears after the check above.
            await using (var output = new FileStream(path, FileMode.CreateNew, FileAccess.Write, FileShare.None, 4096, useAsync: true))
            await using (var writer = new StreamWriter(output))
                await writer.WriteAsync(text.AsMemory(), token);
            _draftText = text;
            _hasDraft = true;
            return new(true, $"示例草稿已写入本地：{path}。打开工作摘要页面可编辑。");
        }, "仅在没有现有或已编辑草稿时写入本地预置示例，不覆盖草稿，不发送消息。"));
    }

    private FrameworkElement CreatePage(IPluginContext context)
    {
        context.Lifetime.ThrowIfCancellationRequested();
        var panel = Panel("工作摘要 · 本地示例", "以下是预置演示数据；钉钉、Teambition 和 AI 尚未接入。");
        panel.Children.Add(Text("已完成 1 项  ·  进行中 1 项  ·  待确认 1 项", 20));
        panel.Children.Add(Text("• 原生插件页面原型：已完成\n• 测试用例整理：进行中\n• 下一次评审时间：待确认"));
        panel.Children.Add(Text("切换页面会保留未保存的编辑。停用插件或退出前请保存；未保存的编辑会丢弃，重新启用时读取已保存草稿。生成命令不会覆盖现有或已编辑的草稿。"));
        var path = Path.Combine(context.DataDirectory, "summary-draft.txt");
        var draft = Named(new TextBox
        {
            Header = "可编辑的本地草稿",
            AcceptsReturn = true,
            TextWrapping = TextWrapping.Wrap,
            MinHeight = 220,
            Text = _draftText
        }, "工作摘要示例草稿");
        // Synchronous text updates keep the document independent of disposable view controls.
        draft.TextChanging += (_, _) =>
        {
            if (!context.Lifetime.IsCancellationRequested && draft.Text != _draftText)
            {
                _draftText = draft.Text;
                _hasDraft = true;
            }
        };
        var status = Text($"保存位置：{path}");
        var save = Named(new Button { Content = "保存本地草稿" }, "保存工作摘要示例草稿");
        save.Click += async (_, _) => await HandleAsync(context, status, async () =>
        {
            var value = draft.Text;
            _draftText = value;
            _hasDraft = true;
            await File.WriteAllTextAsync(path, value, context.Lifetime);
            status.Text = $"已保存到 {path}";
        });
        panel.Children.Add(draft); panel.Children.Add(save); panel.Children.Add(status);
        return Scroll(panel);
    }

    private FrameworkElement CreateSettings(IPluginContext context)
    {
        context.Lifetime.ThrowIfCancellationRequested();
        var panel = Panel("工作摘要 · 示例设置", "偏好保存在该插件独立的本地 JSON 设置中。");
        var audience = Named(new TextBox { Header = "摘要读者", Text = _audience }, "示例摘要读者");
        var status = Text("此偏好只影响本地示例草稿。");
        var save = Named(new Button { Content = "保存偏好" }, "保存摘要示例偏好");
        save.Click += async (_, _) => await HandleAsync(context, status, async () =>
        {
            var value = audience.Text;
            await context.Settings.SetAsync("audience", value, context.Lifetime);
            _audience = value;
            status.Text = "偏好已保存。";
        });
        panel.Children.Add(audience); panel.Children.Add(save); panel.Children.Add(status);
        return Scroll(panel);
    }

    public Task StopAsync(CancellationToken cancellationToken) => Task.CompletedTask;

    private static async Task HandleAsync(IPluginContext context, TextBlock status, Func<Task> action)
    {
        try { context.Lifetime.ThrowIfCancellationRequested(); await action(); }
        catch (OperationCanceledException) { status.Text = "插件已停用，操作已取消。"; }
        catch (Exception error) { status.Text = $"本地操作失败：{error.Message}"; }
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
