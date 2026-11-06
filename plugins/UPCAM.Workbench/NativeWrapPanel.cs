using Microsoft.UI.Xaml.Controls;
using Windows.Foundation;

namespace Upcam.Workbench;

/// <summary>Native toolbar layout; wraps actions when the host window becomes narrow.</summary>
internal sealed class NativeWrapPanel : Panel
{
    private const double Gap = 8;

    protected override Size MeasureOverride(Size availableSize)
    {
        double x = 0, y = 0, rowHeight = 0, usedWidth = 0;
        foreach (var child in Children)
        {
            child.Measure(new Size(availableSize.Width, double.PositiveInfinity));
            double width = Math.Min(child.DesiredSize.Width, availableSize.Width);
            double height = child.DesiredSize.Height;
            if (x > 0 && x + width > availableSize.Width) { y += rowHeight + Gap; x = 0; rowHeight = 0; }
            usedWidth = Math.Max(usedWidth, x + width);
            x += width + Gap;
            rowHeight = Math.Max(rowHeight, height);
        }
        return new Size(usedWidth, y + rowHeight);
    }

    protected override Size ArrangeOverride(Size finalSize)
    {
        double x = 0, y = 0, rowHeight = 0;
        foreach (var child in Children)
        {
            double width = Math.Min(child.DesiredSize.Width, finalSize.Width);
            double height = child.DesiredSize.Height;
            if (x > 0 && x + width > finalSize.Width) { y += rowHeight + Gap; x = 0; rowHeight = 0; }
            child.Arrange(new Rect(x, y, width, height));
            x += width + Gap;
            rowHeight = Math.Max(rowHeight, height);
        }
        return finalSize;
    }
}
