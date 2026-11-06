using System.Globalization;

namespace Upcam.Desktop;

internal sealed record AppLaunchOptions(bool Smoke, bool Background, bool AfterUpdate, int? ParentPid)
{
    internal static AppLaunchOptions Parse(IReadOnlyList<string> arguments, int currentPid)
    {
        bool smoke = false, background = false, after = false;
        int? parent = null;
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
                default: throw new ArgumentException("Unsupported application option");
            }
        }
        if (after && parent is null) throw new ArgumentException("Update handoff requires a parent process");
        return new AppLaunchOptions(smoke, background, after, parent);
    }
}
