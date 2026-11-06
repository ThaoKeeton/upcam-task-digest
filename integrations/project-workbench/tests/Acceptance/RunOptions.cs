namespace Workbench.Digest.Acceptance;

internal sealed record RunOptions(string PackageDirectory, string? ProductionPackageDirectory, string ReportPath)
{
    internal static RunOptions Parse(string[] arguments)
    {
        var values = new Dictionary<string, string>(StringComparer.Ordinal);
        for (var index = 0; index < arguments.Length; index += 2)
        {
            var key = arguments[index];
            if (key is not ("--package-directory" or "--production-package-directory" or "--report") ||
                index + 1 == arguments.Length || !values.TryAdd(key, arguments[index + 1]))
                throw new AcceptanceFailure("Use each supported option once, followed by an absolute path.");
        }
        if (!values.TryGetValue("--package-directory", out var package) ||
            !values.TryGetValue("--report", out var report))
            throw new AcceptanceFailure("--package-directory and --report are required absolute paths.");
        var production = values.GetValueOrDefault("--production-package-directory");
        foreach (var path in new[] { package, production, report }.OfType<string>())
        {
            if (!Path.IsPathFullyQualified(path) || path.StartsWith(@"\\", StringComparison.Ordinal))
                throw new AcceptanceFailure("Acceptance inputs and report must be absolute local paths.");
            OwnedStore.RejectLinks(path);
        }
        package = Path.TrimEndingDirectorySeparator(Path.GetFullPath(package));
        production = production is null ? null : Path.TrimEndingDirectorySeparator(Path.GetFullPath(production));
        report = Path.GetFullPath(report);
        if (!Directory.Exists(package) || production is not null && !Directory.Exists(production))
            throw new AcceptanceFailure("A supplied package directory does not exist.");
        if (OwnedStore.IsWithin(package, report) || production is not null && OwnedStore.IsWithin(production, report))
            throw new AcceptanceFailure("The report must be outside both input packages.");
        if (production is not null && string.Equals(package, production, StringComparison.OrdinalIgnoreCase))
            throw new AcceptanceFailure("Acceptance and production packages must be different directories.");
        return new(package, production, report);
    }
}

/// <summary>Messages in this exception are fixed harness assertions, never worker responses.</summary>
internal sealed class AcceptanceFailure(string message) : Exception(message) { }
