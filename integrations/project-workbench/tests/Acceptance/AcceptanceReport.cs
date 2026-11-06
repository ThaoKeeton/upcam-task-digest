using System.Text.Json;

namespace Workbench.Digest.Acceptance;

internal sealed record CheckResult(string Id, string Outcome, bool Required, string Detail, long ElapsedMilliseconds);
internal sealed record EvidenceFile(string Kind, string Path, long Bytes, uint? Width = null, uint? Height = null);

internal sealed class AcceptanceReport(string path)
{
    private readonly object _sync = new();
    private readonly List<CheckResult> _checks = [];
    private readonly List<EvidenceFile> _artifacts = [];
    private readonly List<string> _roots = [];
    private readonly DateTimeOffset _started = DateTimeOffset.UtcNow;
    private bool _finished;
    internal string Path { get; } = path;

    internal bool Succeeded
    {
        get { lock (_sync) return _finished && _checks.Count > 0 && _checks.All(c => !c.Required || c.Outcome == "passed"); }
    }

    internal async Task<bool> CheckAsync(string id, Func<Task> action, bool required = true)
    {
        var clock = System.Diagnostics.Stopwatch.StartNew();
        try
        {
            await action();
            Add(new(id, "passed", required, "Observed by this native acceptance process.", clock.ElapsedMilliseconds));
            return true;
        }
        catch (Exception error)
        {
            // Worker lines, exception messages/stacks and native UI contents may contain private data.
            // Only our fixed assertion text and an unexpected exception's type are reportable.
            var detail = error is AcceptanceFailure ? error.Message : $"Unexpected {error.GetType().Name}; raw exception suppressed.";
            Add(new(id, "failed", required, detail, clock.ElapsedMilliseconds));
            return false;
        }
    }

    internal void Fail(string id, string detail) => Add(new(id, "failed", true, detail, 0));
    internal void Skip(string id, string reason, bool required = true)
    {
        lock (_sync)
        {
            if (_checks.Any(c => c.Id == id)) return;
            _checks.Add(new(id, "skipped", required, reason, 0));
            WriteLocked();
        }
    }

    internal void OwnRoot(string root)
    {
        lock (_sync) { _roots.Add(root); WriteLocked(); }
    }

    internal void AddArtifact(EvidenceFile artifact)
    {
        lock (_sync) { _artifacts.Add(artifact); WriteLocked(); }
    }

    internal void Finish()
    {
        lock (_sync) { _finished = true; WriteLocked(); }
    }

    private void Add(CheckResult result)
    {
        lock (_sync) { _checks.Add(result); WriteLocked(); }
    }

    private void WriteLocked()
    {
        OwnedStore.RejectLinks(Path);
        Directory.CreateDirectory(System.IO.Path.GetDirectoryName(Path)!);
        var document = new
        {
            schema = "upcam-workbench-native-acceptance-v1",
            stage = _finished ? "completed" : "running",
            succeeded = _finished && _checks.Count > 0 && _checks.All(c => !c.Required || c.Outcome == "passed"),
            startedUtc = _started,
            updatedUtc = DateTimeOffset.UtcNow,
            processId = Environment.ProcessId,
            platformReference = "6565180b17130f6d494978291df4138de3fdb0b4",
            platformApi = 1,
            dotnet = Environment.Version.ToString(),
            windowsAppSdk = "1.8.260921001",
            hostBoundary = "Dedicated native WinUI acceptance host using the actual pinned Workbench.Core/SDK; not the complete production host UI.",
            productionNetworkBoundary = "Production performs only unconfigured local setup/status. Empty UI, authorization and SQLite counts are observed; transient network requests are not instrumented or proven absent by this harness. Synthetic business operations use transport-free injected factories; their no-network regression evidence belongs to backend tests.",
            evidenceBoundary = "Only passed checks ran and met assertions. A skipped check is unverified. Screenshots are native RenderTargetBitmap captures and require visual review. Shutdown/restart checks recreate the actual manager in this process; they do not claim a separate full host process launch.",
            isolatedUserRoots = _roots.ToArray(),
            checks = _checks.ToArray(),
            artifacts = _artifacts.ToArray()
        };
        var temporary = Path + "." + Guid.NewGuid().ToString("N") + ".writing";
        try
        {
            using (var output = new StreamWriter(new FileStream(temporary, FileMode.CreateNew, FileAccess.Write, FileShare.None)))
                output.Write(JsonSerializer.Serialize(document, new JsonSerializerOptions { WriteIndented = true, PropertyNamingPolicy = JsonNamingPolicy.CamelCase }));
            File.Move(temporary, Path, overwrite: true);
        }
        finally { if (File.Exists(temporary)) File.Delete(temporary); }
    }
}
