using System.Globalization;

namespace Upcam.Desktop;

internal sealed record AppLaunchOptions(bool Smoke, bool Background, bool AfterUpdate, int? ParentPid, string? HandoffEvent)
{
    internal static AppLaunchOptions Parse(IReadOnlyList<string> arguments, int currentPid)
    {
        bool smoke = false, background = false, after = false;
        int? parent = null;
        string? handoff = null;
        var seen = new HashSet<string>(StringComparer.Ordinal);
        for (int index = 0; index < arguments.Count; index++)
        {
            string option = arguments[index];
            if (!seen.Add(option)) throw new ArgumentException("Duplicate application option");
            switch (option)
            {
                case "--ui-smoke-test": smoke = true; break;
                case "--background": background = true; break;
                case "--after-update": after = true; break;
                case "--wait-for-parent":
                    if (++index >= arguments.Count ||
                        !int.TryParse(arguments[index], NumberStyles.None, CultureInfo.InvariantCulture, out int value) ||
                        value <= 0 || value == currentPid) throw new ArgumentException("Invalid parent process");
                    parent = value;
                    break;
                case "--handoff-event":
                    if (++index >= arguments.Count || !Guid.TryParseExact(arguments[index], "N", out _))
                        throw new ArgumentException("Invalid handoff identity");
                    handoff = arguments[index];
                    break;
                default: throw new ArgumentException("Unsupported application option");
            }
        }
        if (after && (parent is null || !smoke && handoff is null)) throw new ArgumentException("Update handoff requires a parent process and acknowledgement");
        if (handoff is not null && parent is null) throw new ArgumentException("Handoff identity requires a parent process");
        return new AppLaunchOptions(smoke, background, after, parent, handoff);
    }
}
