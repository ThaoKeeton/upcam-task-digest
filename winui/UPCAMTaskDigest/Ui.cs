using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Automation;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Media;

namespace Upcam.Desktop;

public sealed partial class MainWindow
{
    private static T Identify<T>(T control, string id) where T : DependencyObject
    {
        AutomationProperties.SetAutomationId(control, id);
        return control;
    }

    private static TextBlock Paragraph(string text, bool secondary = false, double size = 14)
    {
        var block = new TextBlock { Text = text, TextWrapping = TextWrapping.Wrap, FontSize = size };
        if (secondary) block.Foreground = (Brush)Application.Current.Resources["TextFillColorSecondaryBrush"];
        return block;
    }

    private static StackPanel Column(params UIElement[] children)
    {
        var panel = new StackPanel { Spacing = 12 };
        foreach (var item in children) panel.Children.Add(item);
        return panel;
    }

    private static FrameworkElement Card(UIElement child) => new Border
    {
        Padding = new Thickness(20), CornerRadius = new CornerRadius(8),
        Background = (Brush)Application.Current.Resources["CardBackgroundFillColorDefaultBrush"],
        BorderBrush = (Brush)Application.Current.Resources["CardStrokeColorDefaultBrush"],
        BorderThickness = new Thickness(1), Child = child
    };

    private static ScrollViewer LocalScroll(UIElement child) => new()
    {
        Content = child, VerticalScrollBarVisibility = ScrollBarVisibility.Auto,
        HorizontalScrollBarVisibility = ScrollBarVisibility.Disabled, Padding = new Thickness(0, 0, 6, 0)
    };

    private Button ActionButton(string label, string id, Func<Task> action, bool accent = false)
    {
        var button = Identify(new Button { Content = label }, id);
        if (accent) button.Style = (Style)Application.Current.Resources["AccentButtonStyle"];
        button.Click += async (_, _) => await RunOperationAsync(label + "…", action);
        return button;
    }

    private Button NavigationButton(string label, string route)
    {
        var button = new Button { Content = label };
        button.Click += (_, _) => Navigate(route);
        return button;
    }

    private TextBox SettingText(string header, string key, string id, string? placeholder = null)
    {
        var input = Identify(new TextBox { Header = header, Text = _state.Text(key), PlaceholderText = placeholder ?? "" }, id);
        var settingsAtCreation = _state.Settings;
        // TextChanged is deferred by WinUI. Commit synchronously before a
        // page switch/save, and ignore controls belonging to replaced state.
        input.TextChanging += (_, _) =>
        {
            if (ReferenceEquals(_state.Settings, settingsAtCreation)) _state.Set(key, input.Text);
        };
        return input;
    }

    private TextBox WindowInput(bool start)
    {
        var input = Identify(new TextBox
        {
            Header = start ? "开始（本机时间）" : "结束（不含，本机时间）",
            Text = start ? _state.ManualStart : _state.ManualEnd,
            PlaceholderText = "YYYY-MM-DD HH:MM"
        }, start ? "window-start" : "window-end");
        input.TextChanged += (_, _) =>
        {
            if (start) _state.ManualStart = input.Text;
            else _state.ManualEnd = input.Text;
        };
        return input;
    }

    private FrameworkElement TimeInputs() => Pair(WindowInput(true), WindowInput(false));

    private static Grid Pair(FrameworkElement first, FrameworkElement second)
    {
        var grid = new Grid { ColumnSpacing = 12, RowSpacing = 12 };
        grid.ColumnDefinitions.Add(new ColumnDefinition());
        grid.ColumnDefinitions.Add(new ColumnDefinition());
        grid.Children.Add(first); grid.Children.Add(second); Grid.SetColumn(second, 1);
        return grid;
    }

    private static AdaptiveWrapPanel Toolbar(params UIElement[] controls)
    {
        var panel = new AdaptiveWrapPanel();
        foreach (UIElement control in controls)
        {
            panel.Children.Add(control);
        }
        return panel;
    }

    private static Grid FillLayout(FrameworkElement header, FrameworkElement content)
    {
        var grid = new Grid { RowSpacing = 12 };
        grid.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
        grid.RowDefinitions.Add(new RowDefinition { Height = new GridLength(1, GridUnitType.Star) });
        grid.Children.Add(header); grid.Children.Add(content); Grid.SetRow(content, 1);
        return grid;
    }

    private ToggleSwitch Toggle(string label, string key, string id)
    {
        var toggle = Identify(new ToggleSwitch { Header = label, IsOn = _state.Flag(key), OnContent = "已开启", OffContent = "已关闭" }, id);
        toggle.Toggled += (_, _) => _state.Set(key, toggle.IsOn);
        return toggle;
    }

    private static string Value(System.Text.Json.Nodes.JsonNode? node, string key, string fallback = "") =>
        node?[key]?.GetValue<string>() ?? fallback;
}
