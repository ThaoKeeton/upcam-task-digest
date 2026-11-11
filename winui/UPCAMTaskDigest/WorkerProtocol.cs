using System.Text;
using System.Text.Json.Nodes;

namespace Upcam.Desktop;

/// <summary>Only fixed phases and counts may cross the progress boundary.</summary>
internal sealed record CollectionProgress(string Phase, int Completed, int Total, int Requests, int Batches, int Passes)
{
    internal static CollectionProgress Parse(JsonObject value)
    {
        try
        {
            if (value.Count != 6) throw new InvalidOperationException();
            string phase = value["phase"]!.GetValue<string>();
            if (phase is not ("starting" or "listing" or "loading_cache" or "collecting" or "reconciling" or "validating"))
                throw new InvalidOperationException();
            int Count(string name, int ceiling)
            {
                int count = value[name]!.GetValue<int>();
                return count >= 0 && count <= ceiling ? count : throw new InvalidOperationException();
            }
            return new(phase, Count("completed", 10000), Count("total", 10000),
                Count("requests", 50000), Count("batches", 50000), Count("passes", 3));
        }
        catch (Exception) { throw new InvalidOperationException("本机采集进度格式无效；未自动重试。"); }
    }
}

/// <summary>Preserves trailing lines when several progress frames share a pipe read.</summary>
internal sealed class WorkerLineReader(Stream stream, int maxBytes = 32 * 1024 * 1024)
{
    private readonly byte[] _buffer = new byte[8192];
    private int _offset, _count;
    internal bool HasBufferedData => _offset < _count;

    internal async Task<string?> ReadAsync(CancellationToken cancellationToken)
    {
        using var result = new MemoryStream();
        while (true)
        {
            if (_offset == _count)
            {
                _count = await stream.ReadAsync(_buffer.AsMemory(), cancellationToken).ConfigureAwait(false);
                _offset = 0;
                if (_count == 0)
                {
                    if (result.Length == 0) return null;
                    throw new InvalidOperationException("工作进程响应中断；未自动重试，请查看本机记录。");
                }
            }
            int newline = Array.IndexOf(_buffer, (byte)0x0a, _offset, _count - _offset);
            int length = (newline < 0 ? _count : newline) - _offset;
            if (result.Length + length > maxBytes)
                throw new InvalidOperationException("本机响应超过显示上限；未自动重试。请使用更小的诊断范围。");
            result.Write(_buffer, _offset, length);
            _offset += length;
            if (newline < 0) continue;
            _offset++;
            try { return new UTF8Encoding(false, true).GetString(result.ToArray()).TrimEnd('\r'); }
            catch (DecoderFallbackException)
            { throw new InvalidOperationException("工作进程响应编码无效；未自动重试。"); }
        }
    }
}
