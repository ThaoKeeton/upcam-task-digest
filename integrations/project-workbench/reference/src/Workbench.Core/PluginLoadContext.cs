using System.Reflection;
using System.Runtime.Loader;
using Workbench.Sdk;

namespace Workbench.Core;

internal sealed class PluginLoadContext : AssemblyLoadContext
{
    private static readonly HashSet<string> FrameworkNames = ReadFrameworkNames();
    private readonly string _packageRoot;
    private readonly AssemblyDependencyResolver _resolver;
    private readonly Dictionary<string, Assembly> _shared = new(StringComparer.OrdinalIgnoreCase);
    private readonly HashSet<string> _sharedNames;

    internal PluginLoadContext(ValidatedPackage package, IReadOnlyCollection<Assembly> shared, IReadOnlyCollection<string> sharedNames)
        : base($"Workbench:{package.Manifest!.Id}:{Guid.NewGuid():N}", isCollectible: false)
    {
        _packageRoot = package.Path;
        _resolver = new AssemblyDependencyResolver(FileSystemGuard.ContainedPath(package.Path, package.Manifest.EntryAssembly));
        foreach (var assembly in shared.Prepend(typeof(IWorkbenchPlugin).Assembly))
            _shared.Add(assembly.GetName().Name!, assembly);
        _sharedNames = sharedNames.ToHashSet(StringComparer.OrdinalIgnoreCase);
    }

    protected override Assembly? Load(AssemblyName assemblyName)
    {
        var name = assemblyName.Name ?? throw new FileLoadException("An assembly name is required.");
        if (_shared.TryGetValue(name, out var shared))
            return shared;
        if (_sharedNames.Contains(name) || FrameworkNames.Contains(name))
            return Default.LoadFromAssemblyName(assemblyName);
        var resolved = _resolver.ResolveAssemblyToPath(assemblyName)
            ?? throw new FileNotFoundException($"Plugin dependency '{name}' was not found inside '{_packageRoot}'.", name);
        return LoadFromAssemblyPath(ValidateResolvedFile(resolved));
    }

    protected override nint LoadUnmanagedDll(string unmanagedDllName)
    {
        var resolved = _resolver.ResolveUnmanagedDllToPath(unmanagedDllName);
        return resolved is null ? nint.Zero : LoadUnmanagedDllFromPath(ValidateResolvedFile(resolved));
    }

    private string ValidateResolvedFile(string path)
    {
        var contained = FileSystemGuard.ContainedPath(_packageRoot, Path.GetRelativePath(_packageRoot, path));
        FileSystemGuard.EnsureRegularFile(contained);
        return contained;
    }

    private static HashSet<string> ReadFrameworkNames()
    {
        // This catalog comes from the target framework's SDK reference pack,
        // never from host-private DLLs colocated with a self-contained CoreLib.
        using var stream = typeof(PluginLoadContext).Assembly.GetManifestResourceStream("Workbench.Core.FrameworkAssemblies")
            ?? throw new InvalidOperationException("The .NET framework assembly catalog is missing.");
        using var reader = new StreamReader(stream);
        var names = reader.ReadToEnd().Split(['\r', '\n'], StringSplitOptions.RemoveEmptyEntries)
            .ToHashSet(StringComparer.OrdinalIgnoreCase);
        names.Add(typeof(object).Assembly.GetName().Name!);
        return names;
    }
}
