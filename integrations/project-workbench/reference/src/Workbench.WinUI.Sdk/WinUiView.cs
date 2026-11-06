using Microsoft.UI.Xaml;
using Workbench.Sdk;

namespace Workbench.WinUI.Sdk;

public enum WinUiViewKind { Page, Settings }

/// <summary>Code-created controls only. The host invokes factories on its WinUI UI thread.</summary>
public sealed record WinUiView(
    string Id,
    string Title,
    Func<FrameworkElement> CreateView,
    WinUiViewKind Kind = WinUiViewKind.Page) : IPluginView;
