using System.Text.Json;

namespace Workbench.App;

internal sealed record RunOptions(bool SmokeTest, string UserRoot, string? ReportPath)
{
    public static readonly string[] SampleIds = ["com.projectworkbench.summary", "com.projectworkbench.test-cases"];
    private readonly List<object> _startupStages = [];
    public IReadOnlyList<object> StartupStages => _startupStages.ToArray();

    public void TraceStartup(string stage)
    {
        if (ReportPath is null) return;
        _startupStages.Add(new { stage, threadId = Environment.CurrentManagedThreadId, utc = DateTimeOffset.UtcNow });
        WriteReport(new { succeeded = false, stage, userRoot = UserRoot, startupStages = StartupStages });
    }

    public static RunOptions Parse(string[] args)
    {
        var smoke = args.Contains("--smoke-test", StringComparer.Ordinal);
        string? report = null;
        for (var i = 0; i < args.Length; i++)
            if (args[i] == "--smoke-report" && i + 1 < args.Length)
                report = Path.GetFullPath(args[++i]);
        var root = smoke
            ? Path.Combine(Path.GetTempPath(), "ProjectWorkbench-smoke", Guid.NewGuid().ToString("N"))
            : Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "ProjectWorkbench");
        return new(smoke, root, smoke ? report ?? Path.Combine(root, "smoke-result.json") : null);
    }

    public void WriteReport(object result)
    {
        if (ReportPath is null) return;
        Directory.CreateDirectory(Path.GetDirectoryName(ReportPath)!);
        File.WriteAllText(ReportPath, JsonSerializer.Serialize(result, new JsonSerializerOptions { WriteIndented = true, PropertyNamingPolicy = JsonNamingPolicy.CamelCase }));
    }

    public void WriteFailure(string stage, Exception error, string? nativeMessage = null) => WriteReport(new
    {
        succeeded = false,
        stage,
        userRoot = UserRoot,
        error = error.ToString(),
        nativeMessage,
        errorHResult = $"0x{unchecked((uint)error.HResult):X8}",
        startupStages = StartupStages,
        completedUtc = DateTimeOffset.UtcNow
    });
}
