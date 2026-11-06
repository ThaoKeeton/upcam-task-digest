using System.Security.Cryptography;

namespace Workbench.Core;

internal interface ISecretProtector
{
    bool IsSupported { get; }
    byte[] Protect(byte[] plaintext, byte[] entropy);
    byte[] Unprotect(byte[] ciphertext, byte[] entropy);
}

/// <summary>DPAPI is a current-Windows-user storage convention, not an in-process plugin sandbox.</summary>
internal sealed class WindowsSecretProtector : ISecretProtector
{
    public bool IsSupported => OperatingSystem.IsWindows();

    public byte[] Protect(byte[] plaintext, byte[] entropy)
    {
        if (!OperatingSystem.IsWindows()) throw new PlatformNotSupportedException();
        return ProtectedData.Protect(plaintext, entropy, DataProtectionScope.CurrentUser);
    }

    public byte[] Unprotect(byte[] ciphertext, byte[] entropy)
    {
        if (!OperatingSystem.IsWindows()) throw new PlatformNotSupportedException();
        return ProtectedData.Unprotect(ciphertext, entropy, DataProtectionScope.CurrentUser);
    }
}
