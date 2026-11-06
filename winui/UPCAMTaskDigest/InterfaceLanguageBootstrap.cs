// Adapted architectural pattern from Microsoft PowerToys (MIT).
// Copyright (c) Microsoft Corporation. All rights reserved.
// See THIRD_PARTY_NOTICES.md and LICENSES/PowerToys-MIT.txt for source and license.
using System.Text.Json;

namespace Upcam.Desktop;

internal static class InterfaceLanguageBootstrap
{
    internal static string Read(string path)
    {
        try
        {
            var info = new FileInfo(path);
            if (!info.Exists || info.Length > 4096 || (info.Attributes & FileAttributes.ReparsePoint) != 0) return "system";
            using var document = JsonDocument.Parse(File.ReadAllBytes(path));
            if (document.RootElement.ValueKind != JsonValueKind.Object ||
                !document.RootElement.TryGetProperty("language", out var language) || language.ValueKind != JsonValueKind.String)
                return "system";
            return language.GetString() is "zh-CN" ? "zh-CN" : language.GetString() is "en-US" ? "en-US" : "system";
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException or JsonException) { return "system"; }
    }
}
