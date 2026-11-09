using System.Diagnostics;
using Microsoft.UI.Xaml;

namespace Upcam.Desktop;

public sealed partial class MainWindow
{
    private CancellationTokenSource? _diagnosticCancellation;
    private int _diagnosticGeneration;

    private void RequireDiagnosticSaved()
    {
        if (_state.Dirty || _state.SecretDrafts.Any(value => !string.IsNullOrEmpty(value.Value)))
            throw new InvalidOperationException("请先保存本机配置和凭据，再执行只读诊断；诊断不会代为保存。");
    }

    private void CancelDiagnostic_Click(object sender, RoutedEventArgs args)
    {
        if (_diagnosticCancellation is null) return;
        CancelDiagnosticButton.IsEnabled = false;
        SetStatus("正在停止只读诊断…");
        _diagnosticCancellation.Cancel();
    }

    private async Task DiagnoseWithProgressAsync()
    {
        using var cancellation = new CancellationTokenSource();
        _diagnosticCancellation = cancellation;
        int generation = ++_diagnosticGeneration;
        var elapsed = Stopwatch.StartNew();
        CollectionProgress current = new("starting", 0, 0, 0, 0, 0);
        void UpdateProgress()
        {
            if (generation != _diagnosticGeneration || cancellation.IsCancellationRequested) return;
            SetStatus(CollectionProgressText.Format(current, elapsed.Elapsed));
        }
        var progress = new Progress<CollectionProgress>(value => { current = value; UpdateProgress(); });
        var timer = DispatcherQueue.CreateTimer();
        timer.Interval = TimeSpan.FromSeconds(1);
        timer.Tick += (_, _) => UpdateProgress();
        CancelDiagnosticButton.Visibility = Visibility.Visible;
        CancelDiagnosticButton.Content = UiStrings.T("停止诊断");
        CancelDiagnosticButton.IsEnabled = true;
        _state.Output = UiStrings.T("正在只读采集整个项目；缩短时间范围不会减少基础扫描。可使用下方“停止诊断”，已完成的短期进度会保留。未调用 OpenAI；未发送钉钉。");
        Navigate("run");
        UpdateProgress();
        timer.Start();
        bool interrupted = false;
        try
        {
            var result = await Worker.SendAsync("diagnose", _state.WindowPayload(), progress, cancellation.Token);
            ApplyDiagnosticResult(result);
            Navigate("run");
            SetStatus(Value(result, "safety", "未调用 OpenAI；未发送钉钉。"));
        }
        catch (OperationCanceledException)
        {
            interrupted = true;
            _state.Output = UiStrings.T("只读诊断已停止；未调用 OpenAI、未发送钉钉。已完整采集的短期进度保留，可在两小时内重新诊断；不会自动重试。");
        }
        catch (TimeoutException)
        {
            interrupted = true;
            _state.Output = UiStrings.T("只读诊断等待超时，已停止工作进程；未调用 OpenAI、未发送钉钉。可检查网络后重新诊断，已完整采集的短期进度保留；不会自动重试。");
        }
        finally
        {
            ++_diagnosticGeneration;
            timer.Stop();
            CancelDiagnosticButton.Visibility = Visibility.Collapsed;
            _diagnosticCancellation = null;
        }
        if (interrupted)
        {
            // Do not replay the request, clear checkpoints, or copy profile data.
            // A replacement worker opens the same default profile and ledger.
            _state.ExportAvailable = false;
            _state.DiagnosticText = "";
            _state.DiagnosticFileStem = "";
            try
            {
                if (_worker is not null) { await _worker.StopAsync(); _worker.Dispose(); }
                _worker = new WorkerClient(_smoke);
                ApplyInitializationResult(await _worker.SendAsync("initialize"));
                _savedAuto = _state.Flag("auto_send_enabled");
                Navigate("run");
                SetStatus(_state.Output);
            }
            catch (Exception)
            {
                DisableWorkerOperations();
                SetStatus("诊断已停止，但工作进程未能恢复。请退出并重新打开应用；原有配置没有删除。");
            }
        }
    }
}
