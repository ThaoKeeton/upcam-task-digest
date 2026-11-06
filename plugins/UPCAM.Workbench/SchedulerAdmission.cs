namespace Upcam.Workbench;

/// <summary>Rejects old or overlapping automatic executions without waiting behind a configuration mutation.</summary>
internal sealed class SchedulerAdmission
{
    private readonly object _sync = new();
    private long _epoch;
    private bool _changing;

    internal IDisposable BeginMutation()
    {
        lock (_sync)
        {
            if (_changing) throw new InvalidOperationException("已有业务配置 / 目录切换正在进行。" );
            _changing = true;
            return new Mutation(this, ++_epoch);
        }
    }

    internal long Capture()
    {
        lock (_sync)
        {
            if (_changing) throw new InvalidOperationException("配置 / 目录切换期间拒绝启动自动任务；请完成后明确重试。" );
            return _epoch;
        }
    }

    internal void Validate(long epoch)
    {
        lock (_sync)
            if (_changing || epoch != _epoch)
                throw new InvalidOperationException("自动授权所属的配置代已改变；拒绝旧执行，请完成配置后明确启动。" );
    }

    private sealed class Mutation(SchedulerAdmission owner, long epoch) : IDisposable
    {
        private int _disposed;
        public void Dispose()
        {
            if (Interlocked.Exchange(ref _disposed, 1) != 0) return;
            lock (owner._sync)
                if (owner._epoch == epoch) owner._changing = false;
        }
    }
}
