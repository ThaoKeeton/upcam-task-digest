using System.Globalization;

namespace Upcam.Desktop;

/// <summary>Collection counts describe candidates read, never report-window updates.</summary>
internal static class CollectionProgressText
{
    internal static string Format(CollectionProgress value, TimeSpan elapsed, string? operationLabel = null)
    {
        string phase = UiStrings.T(operationLabel ?? (value.Phase switch
        {
            "listing" => "列出项目任务", "loading_cache" => "恢复已读取任务进度",
            "collecting" => "读取任务及动态", "reconciling" => "重新核对项目变化",
            "validating" => "校验诊断结果", _ => "准备只读采集"
        }));
        // A listing pass has not established a final denominator. During a
        // closing scan, Completed can also belong to the previous candidate set.
        string counts = value.Phase is "listing" or "reconciling"
            ? UiStrings.F("已列出候选任务 {0} 项（非时段内更新数）", value.Total)
            : UiStrings.F("候选任务读取进度 {0}/{1} 项（非时段内更新数）", value.Completed, value.Total);
        return UiStrings.F("{0} · 已耗时 {1}:{2} · {3} · 请求 {4}", phase,
            (int)elapsed.TotalMinutes, elapsed.Seconds.ToString("00", CultureInfo.InvariantCulture),
            counts, value.Requests);
    }
}
