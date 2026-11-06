using System.Globalization;
using System.Text.Json;

namespace Upcam.Desktop;

/// <summary>
/// Translates fixed interface text only. Pass business values as F arguments;
/// never pass task titles, names, mappings, Markdown, or raw diagnostics to T.
/// Configuring the interface does not change the process/thread cultures.
/// </summary>
internal static class UiStrings
{
    private const string EnglishResourceName = "Upcam.Desktop.Strings.en-US.json";
    private static readonly Lazy<IReadOnlyDictionary<string, string>> English = new(LoadEnglish);
    private static string _resolvedLanguage = Resolve("system", null);

    /// <summary>The actual implemented language: zh-CN or en-US.</summary>
    public static string ResolvedLanguage => Volatile.Read(ref _resolvedLanguage);

    /// <summary>
    /// Accepts system, zh-CN, or en-US. All Chinese system UI variants resolve
    /// to Simplified Chinese; other system languages and unknown choices use
    /// English. systemLanguage is an optional deterministic system UI override.
    /// </summary>
    public static void Configure(string choice, string? systemLanguage = null) =>
        Volatile.Write(ref _resolvedLanguage, Resolve(choice, systemLanguage));

    /// <summary>Looks up one whole fixed string; unknown strings pass through.</summary>
    public static string T(string text)
    {
        ArgumentNullException.ThrowIfNull(text);
        return Translate(text, ResolvedLanguage);
    }

    /// <summary>
    /// Translates a fixed composite template before formatting opaque values.
    /// Values are never looked up, parsed, or rewritten as interface resources.
    /// </summary>
    public static string F(string template, params object[] args)
    {
        ArgumentNullException.ThrowIfNull(template);
        ArgumentNullException.ThrowIfNull(args);
        string language = ResolvedLanguage;
        return string.Format(CultureInfo.GetCultureInfo(language), Translate(template, language), args);
    }

    private static string Translate(string text, string language) =>
        language == "en-US" && English.Value.TryGetValue(text, out string? translated) ? translated : text;

    private static string Resolve(string choice, string? systemLanguage)
    {
        string selected = choice?.Trim() ?? "";
        if (selected.Equals("zh-CN", StringComparison.OrdinalIgnoreCase)) return "zh-CN";
        if (!selected.Equals("system", StringComparison.OrdinalIgnoreCase)) return "en-US";

        string system = (systemLanguage ?? CultureInfo.CurrentUICulture.Name).Trim().Replace('_', '-');
        return system.Equals("zh", StringComparison.OrdinalIgnoreCase) ||
            system.StartsWith("zh-", StringComparison.OrdinalIgnoreCase) ? "zh-CN" : "en-US";
    }

    private static IReadOnlyDictionary<string, string> LoadEnglish()
    {
        // The project embeds Strings/en-US.json with this explicit LogicalName.
        // A packaging defect must be visible rather than silently advertising
        // an English interface whose resource was omitted from the application.
        using Stream stream = typeof(UiStrings).Assembly.GetManifestResourceStream(EnglishResourceName)
            ?? throw new InvalidOperationException("English interface resources are missing from this application package.");
        using JsonDocument document = JsonDocument.Parse(stream);
        if (document.RootElement.ValueKind != JsonValueKind.Object)
            throw new InvalidOperationException("English interface resources must be a JSON object.");

        var result = new Dictionary<string, string>(StringComparer.Ordinal);
        foreach (JsonProperty property in document.RootElement.EnumerateObject())
        {
            if (string.IsNullOrWhiteSpace(property.Name) || property.Value.ValueKind != JsonValueKind.String ||
                string.IsNullOrWhiteSpace(property.Value.GetString()) ||
                !result.TryAdd(property.Name, property.Value.GetString()!))
                throw new InvalidOperationException("English interface resources contain an invalid or duplicate entry.");
        }
        if (result.Count == 0)
            throw new InvalidOperationException("English interface resources are empty.");
        return result;
    }
}
