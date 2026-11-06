using System.Security.Cryptography;
using System.Text;
using Workbench.Sdk;

namespace Workbench.Core;

internal sealed class PluginSecretStore(
    string userRoot, string pluginId, ISecretProtector protector, Action<PluginDiagnostic> diagnostic) : IPluginSecretStore, IDisposable
{
    private readonly object _sync = new();
    private readonly SemaphoreSlim _gate = new(1, 1);
    private readonly Dictionary<string, PluginSecretDefinition> _definitions = new(StringComparer.Ordinal);
    private bool _open = true;
    private bool _disposed;
    private static readonly UTF8Encoding Encoding = new(false, true);

    public bool IsSupported => protector.IsSupported;

    public void Register(PluginSecretDefinition definition)
    {
        ArgumentNullException.ThrowIfNull(definition);
        PluginRegistrationContext.ValidateMetadata(definition.Id, definition.Title);
        lock (_sync)
        {
            if (!_open) throw new InvalidOperationException("Secrets can only be declared during InitializeAsync.");
            if (!_definitions.TryAdd(definition.Id, definition)) throw new InvalidOperationException("Duplicate secret ID.");
        }
    }

    public IReadOnlyList<PluginSecretDefinition> GetDefinitions()
    {
        lock (_sync) return Array.AsReadOnly(_definitions.Values.ToArray());
    }

    internal void CloseRegistration() { lock (_sync) _open = false; }
    public void Dispose() { lock (_sync) { _open = false; _disposed = true; } }

    public async Task<string?> ReadAsync(string id, CancellationToken cancellationToken = default)
    {
        ValidateAccess(id);
        await _gate.WaitAsync(cancellationToken);
        byte[]? plaintext = null;
        byte[]? ciphertext = null;
        var entropy = Entropy(id);
        try
        {
            var path = SecretPath(id);
            if (!File.Exists(path)) return null;
            FileSystemGuard.EnsureRegularFile(path);
            ciphertext = await File.ReadAllBytesAsync(path, cancellationToken);
            plaintext = protector.Unprotect(ciphertext, entropy);
            return Encoding.GetString(plaintext);
        }
        catch (OperationCanceledException) when (cancellationToken.IsCancellationRequested) { throw; }
        catch (Exception) { throw Failure("SecretReadFailed"); }
        finally
        {
            if (plaintext is not null) CryptographicOperations.ZeroMemory(plaintext);
            if (ciphertext is not null) CryptographicOperations.ZeroMemory(ciphertext);
            CryptographicOperations.ZeroMemory(entropy);
            _gate.Release();
        }
    }

    public async Task WriteAsync(string id, string value, CancellationToken cancellationToken = default)
    {
        ValidateAccess(id);
        ArgumentNullException.ThrowIfNull(value);
        await _gate.WaitAsync(cancellationToken);
        byte[]? plaintext = null;
        byte[]? ciphertext = null;
        var entropy = Entropy(id);
        string? temporary = null;
        try
        {
            var path = SecretPath(id);
            plaintext = Encoding.GetBytes(value);
            ciphertext = protector.Protect(plaintext, entropy);
            var directory = Path.GetDirectoryName(path)!;
            Directory.CreateDirectory(directory);
            FileSystemGuard.RejectLinks(directory);
            temporary = FileSystemGuard.ContainedPath(directory, $".{Guid.NewGuid():N}.tmp");
            await using (var stream = new FileStream(temporary, FileMode.CreateNew, FileAccess.Write, FileShare.None,
                81920, FileOptions.Asynchronous | FileOptions.WriteThrough))
            {
                await stream.WriteAsync(ciphertext, cancellationToken);
                await stream.FlushAsync(cancellationToken);
                stream.Flush(flushToDisk: true);
            }
            cancellationToken.ThrowIfCancellationRequested();
            FileSystemGuard.RejectLinks(path);
            if (File.Exists(path)) FileSystemGuard.EnsureRegularFile(path);
            File.Move(temporary, path, overwrite: true);
            temporary = null;
        }
        catch (OperationCanceledException) when (cancellationToken.IsCancellationRequested) { throw; }
        catch (Exception) { throw Failure("SecretWriteFailed"); }
        finally
        {
            if (plaintext is not null) CryptographicOperations.ZeroMemory(plaintext);
            if (ciphertext is not null) CryptographicOperations.ZeroMemory(ciphertext);
            CryptographicOperations.ZeroMemory(entropy);
            if (temporary is not null)
            {
                try { File.Delete(temporary); }
                catch (Exception) { diagnostic(new(PluginDiagnosticCode.SecretStorageFailed, "SecretTemporaryCleanupFailed")); }
            }
            _gate.Release();
        }
    }

    public async Task DeleteAsync(string id, CancellationToken cancellationToken = default)
    {
        ValidateAccess(id);
        await _gate.WaitAsync(cancellationToken);
        try
        {
            cancellationToken.ThrowIfCancellationRequested();
            var path = SecretPath(id);
            if (File.Exists(path)) { FileSystemGuard.EnsureRegularFile(path); File.Delete(path); }
        }
        catch (OperationCanceledException) when (cancellationToken.IsCancellationRequested) { throw; }
        catch (Exception) { throw Failure("SecretDeleteFailed"); }
        finally { _gate.Release(); }
    }

    private void ValidateAccess(string id)
    {
        JsonPluginSettings.ValidateKey(id);
        lock (_sync)
        {
            if (_disposed) throw new PluginSecretStoreException("SecretStoreClosed");
            if (!_definitions.ContainsKey(id)) throw new PluginSecretStoreException("SecretNotDeclared");
        }
        if (!IsSupported) throw new PluginSecretStoreException("SecretStorageUnsupported");
    }

    private string SecretPath(string id) => FileSystemGuard.ContainedPath(userRoot,
        Path.Combine("secrets", pluginId, Convert.ToHexStringLower(SHA256.HashData(Encoding.GetBytes(id))) + ".bin"));

    private byte[] Entropy(string id) => SHA256.HashData(Encoding.GetBytes($"Workbench.Secrets.v1\0{pluginId}\0{id}"));

    private PluginSecretStoreException Failure(string code)
    {
        diagnostic(new(PluginDiagnosticCode.SecretStorageFailed, code));
        return new(code);
    }
}
