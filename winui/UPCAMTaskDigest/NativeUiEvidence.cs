using System.Runtime.InteropServices;
using Windows.Graphics.Imaging;
using Windows.Storage;

namespace Upcam.Desktop;

/// <summary>Captures this app's native window/icons only during isolated UI smoke runs.</summary>
internal static class NativeUiEvidence
{
    internal static async Task CaptureWindowAsync(IntPtr window, string path)
    {
        if (!GetWindowRect(window, out var rect)) throw new InvalidOperationException("Native window bounds unavailable.");
        int width = rect.Right - rect.Left, height = rect.Bottom - rect.Top;
        byte[] pixels = Render(width, height, dc => PrintWindow(window, dc, 2));
        await WriteAsync(path, pixels, width, height);
    }

    internal static byte[] IconPixels(IntPtr icon, int size) =>
        Render(size, size, dc => DrawIconEx(dc, 0, 0, icon, size, size, 0, IntPtr.Zero, 3));

    internal static Task CaptureIconAsync(IntPtr icon, int size, string path) =>
        WriteAsync(path, IconPixels(icon, size), size, size);

    private static byte[] Render(int width, int height, Func<IntPtr, bool> draw)
    {
        if (width < 1 || height < 1 || width > 4096 || height > 4096)
            throw new InvalidOperationException("Native capture dimensions exceeded the test budget.");
        IntPtr screen = GetDC(IntPtr.Zero), memory = IntPtr.Zero, bitmap = IntPtr.Zero, previous = IntPtr.Zero;
        try
        {
            memory = CreateCompatibleDC(screen);
            bitmap = CreateCompatibleBitmap(screen, width, height);
            if (memory == IntPtr.Zero || bitmap == IntPtr.Zero) throw new InvalidOperationException("Native capture allocation failed.");
            previous = SelectObject(memory, bitmap);
            PatBlt(memory, 0, 0, width, height, 0x00000042);
            if (!draw(memory)) throw new InvalidOperationException("Native capture draw failed.");
            SelectObject(memory, previous); previous = IntPtr.Zero;
            var info = new BitmapInfo { Header = new BitmapInfoHeader
            {
                Size = 40, Width = width, Height = -height, Planes = 1, BitCount = 32,
                SizeImage = (uint)checked(width * height * 4)
            }};
            byte[] pixels = new byte[checked(width * height * 4)];
            if (GetDIBits(memory, bitmap, 0, (uint)height, pixels, ref info, 0) != height)
                throw new InvalidOperationException("Native capture pixels unavailable.");
            for (int i = 3; i < pixels.Length; i += 4) pixels[i] = 255;
            return pixels;
        }
        finally
        {
            if (previous != IntPtr.Zero) SelectObject(memory, previous);
            if (bitmap != IntPtr.Zero) DeleteObject(bitmap);
            if (memory != IntPtr.Zero) DeleteDC(memory);
            if (screen != IntPtr.Zero) ReleaseDC(IntPtr.Zero, screen);
        }
    }

    private static async Task WriteAsync(string path, byte[] pixels, int width, int height)
    {
        StorageFolder folder = await StorageFolder.GetFolderFromPathAsync(Path.GetDirectoryName(path)!);
        StorageFile file = await folder.CreateFileAsync(Path.GetFileName(path), CreationCollisionOption.ReplaceExisting);
        using var stream = await file.OpenAsync(FileAccessMode.ReadWrite);
        var encoder = await BitmapEncoder.CreateAsync(BitmapEncoder.PngEncoderId, stream);
        encoder.SetPixelData(BitmapPixelFormat.Bgra8, BitmapAlphaMode.Ignore, (uint)width, (uint)height, 96, 96, pixels);
        await encoder.FlushAsync();
    }

    [StructLayout(LayoutKind.Sequential)] private struct Rect { public int Left, Top, Right, Bottom; }
    [StructLayout(LayoutKind.Sequential)] private struct BitmapInfoHeader
    {
        public uint Size; public int Width, Height; public ushort Planes, BitCount;
        public uint Compression, SizeImage; public int XPelsPerMeter, YPelsPerMeter;
        public uint ColorsUsed, ColorsImportant;
    }
    [StructLayout(LayoutKind.Sequential)] private struct BitmapInfo { public BitmapInfoHeader Header; public uint Color; }
    [DllImport("user32.dll")] private static extern bool GetWindowRect(IntPtr hwnd, out Rect rect);
    [DllImport("user32.dll")] private static extern IntPtr GetDC(IntPtr hwnd);
    [DllImport("user32.dll")] private static extern int ReleaseDC(IntPtr hwnd, IntPtr dc);
    [DllImport("user32.dll")] private static extern bool PrintWindow(IntPtr hwnd, IntPtr dc, uint flags);
    [DllImport("user32.dll")] private static extern bool DrawIconEx(IntPtr dc, int x, int y, IntPtr icon, int width, int height, uint step, IntPtr brush, uint flags);
    [DllImport("gdi32.dll")] private static extern IntPtr CreateCompatibleDC(IntPtr dc);
    [DllImport("gdi32.dll")] private static extern IntPtr CreateCompatibleBitmap(IntPtr dc, int width, int height);
    [DllImport("gdi32.dll")] private static extern IntPtr SelectObject(IntPtr dc, IntPtr item);
    [DllImport("gdi32.dll")] private static extern bool DeleteObject(IntPtr item);
    [DllImport("gdi32.dll")] private static extern bool DeleteDC(IntPtr dc);
    [DllImport("gdi32.dll")] private static extern bool PatBlt(IntPtr dc, int x, int y, int width, int height, uint operation);
    [DllImport("gdi32.dll")] private static extern int GetDIBits(IntPtr dc, IntPtr bitmap, uint start, uint lines, byte[] pixels, ref BitmapInfo info, uint usage);
}
