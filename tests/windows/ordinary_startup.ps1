param(
    [Parameter(Mandatory=$true)][string]$ApplicationDirectory,
    [Parameter(Mandatory=$true)][string]$EvidenceDirectory,
    [string[]]$Cases = @('fresh', 'legacy', 'system', 'zh-CN', 'en-US', 'invalid-bootstrap')
)

$ErrorActionPreference = 'Stop'
if ($env:GITHUB_ACTIONS -ne 'true') { throw 'Ordinary startup fixture is restricted to ephemeral Windows Actions' }
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
$exe = Join-Path ([IO.Path]::GetFullPath($ApplicationDirectory)) 'UPCAMTaskDigest.exe'
if (-not (Test-Path -LiteralPath $exe -PathType Leaf)) { throw 'Complete test application is missing' }
$known = [Environment]::GetFolderPath([Environment+SpecialFolder]::LocalApplicationData)
if (-not [string]::Equals([IO.Path]::GetFullPath($known), [IO.Path]::GetFullPath($env:LOCALAPPDATA), [StringComparison]::OrdinalIgnoreCase)) {
    throw 'CI KnownFolder and worker LOCALAPPDATA differ; no profile was accessed'
}
$testProfileDirectory = Join-Path $known 'UPCAM/TaskDigest'
if (Test-Path -LiteralPath $testProfileDirectory) { throw 'A pre-existing profile must never be inspected or replaced' }
[IO.Directory]::CreateDirectory([IO.Path]::GetFullPath($EvidenceDirectory)) | Out-Null
$report = [Collections.Generic.List[object]]::new()
$failures = 0

try {
    foreach ($case in $Cases) {
        if ($case -notin @('fresh', 'legacy', 'system', 'zh-CN', 'en-US', 'invalid-bootstrap')) { throw 'Unsupported test case' }
        if (Test-Path -LiteralPath $testProfileDirectory) { throw 'Test profile was not cleaned; refusing to reuse it' }
        $owner = [guid]::NewGuid().ToString('N')
        [IO.Directory]::CreateDirectory($testProfileDirectory) | Out-Null
        $marker = Join-Path $testProfileDirectory '.ordinary-startup-test-owner'
        [IO.File]::WriteAllText($marker, $owner)
        $process = $null
        $settingsHash = $null
        $entry = [ordered]@{ case=$case; ordinaryArguments=$true; ready=$false; settingsPreserved=$true; outcome='pending' }
        $start = Get-Date
        try {
            if ($case -ne 'fresh') {
                # Schema 1 with no credentials or usable project. Both automatic
                # network-capable features are explicitly off in these fixtures.
                $settings = [ordered]@{
                    schema_version=1; tenant_id=''; project_id='';
                    auto_send_enabled=$false; start_with_windows=$false;
                    owner_names=@{ 'aaaaaaaaaaaaaaaaaaaaaaaa'='CI synthetic member' };
                    status_names=@{ 'bbbbbbbbbbbbbbbbbbbbbbbb'='CI synthetic status' };
                    resolved_status_ids=@()
                }
                if ($case -ne 'legacy') {
                    $settings.ui_language = if ($case -in @('zh-CN', 'en-US')) { $case } else { 'system' }
                    $settings.auto_check_updates = $false
                }
                $settingsFile = Join-Path $testProfileDirectory 'settings.json'
                [IO.File]::WriteAllText($settingsFile, ($settings | ConvertTo-Json -Depth 8), [Text.UTF8Encoding]::new($false))
                $settingsHash = (Get-FileHash -LiteralPath $settingsFile -Algorithm SHA256).Hash
            }
            if ($case -in @('system', 'zh-CN', 'en-US')) {
                [IO.File]::WriteAllText((Join-Path $testProfileDirectory 'language.json'), (@{language=$case} | ConvertTo-Json), [Text.UTF8Encoding]::new($false))
            } elseif ($case -eq 'invalid-bootstrap') {
                [IO.File]::WriteAllText((Join-Path $testProfileDirectory 'language.json'), '{invalid synthetic json', [Text.UTF8Encoding]::new($false))
            }
            if ($case -eq 'legacy') {
                & python "$PSScriptRoot/legacy_profile.py" prepare $testProfileDirectory $owner
                if ($LASTEXITCODE -ne 0) { throw 'Could not prepare the test-owned legacy persistence fixture' }
            }
            $process = Start-Process -FilePath $exe -WorkingDirectory (Split-Path $exe) -PassThru
            $deadline = [DateTime]::UtcNow.AddSeconds(35)
            while ([DateTime]::UtcNow -lt $deadline) {
                $process.Refresh()
                if ($process.HasExited) {
                    $entry.exitCode = $process.ExitCode
                    $entry.outcome = 'exited-before-ready'
                    throw 'Ordinary application exited before a ready main window'
                }
                $condition = [System.Windows.Automation.PropertyCondition]::new([System.Windows.Automation.AutomationElement]::ProcessIdProperty, $process.Id)
                $window = [System.Windows.Automation.AutomationElement]::RootElement.FindFirst([System.Windows.Automation.TreeScope]::Children, $condition)
                if ($null -ne $window) {
                    $saveCondition = [System.Windows.Automation.PropertyCondition]::new([System.Windows.Automation.AutomationElement]::AutomationIdProperty, 'save-settings')
                    $save = $window.FindFirst([System.Windows.Automation.TreeScope]::Descendants, $saveCondition)
                    if ($null -ne $save -and $save.Current.IsEnabled) {
                        $entry.ready=$true
                        $entry.outcome='ready'
                        break
                    }
                }
                Start-Sleep -Milliseconds 100
            }
            if (-not $entry.ready) { $entry.outcome='ready-timeout'; throw 'Ordinary application did not initialize its main window' }
            if ($null -ne $settingsHash) {
                $entry.settingsPreserved = (Get-FileHash -LiteralPath $settingsFile -Algorithm SHA256).Hash -eq $settingsHash
                if (-not $entry.settingsPreserved) { throw 'Ordinary startup unexpectedly changed saved settings' }
            }
            if (-not (Test-Path -LiteralPath (Join-Path $testProfileDirectory 'history.sqlite3'))) { throw 'Ordinary worker did not initialize local storage' }
            if ($case -eq 'legacy') {
                $loadedMappings = $false
                foreach ($label in @('1 位成员 · 1 个状态', '1 members · 1 statuses')) {
                    $labelCondition = [System.Windows.Automation.PropertyCondition]::new([System.Windows.Automation.AutomationElement]::NameProperty, $label)
                    if ($null -ne $window.FindFirst([System.Windows.Automation.TreeScope]::Descendants, $labelCondition)) { $loadedMappings = $true }
                }
                if (-not $loadedMappings) { throw 'Legacy mappings were not loaded into the actual home page' }
                & python "$PSScriptRoot/legacy_profile.py" verify $testProfileDirectory $owner
                if ($LASTEXITCODE -ne 0) { throw 'Legacy persistence was not preserved across ordinary startup' }
                $entry.legacyPersistencePreserved = $true
            }
            Write-Output "Ordinary startup case $case passed; no synthetic launch argument"
        } catch {
            $failures++
            if ($entry.outcome -eq 'ready') { $entry.outcome='assertion-failed' }
            $entry.harnessErrorType = $_.Exception.GetType().Name
            # Only fixed classification evidence is retained, never event message
            # bodies, exception contents, profile data or credentials.
            $events = @(Get-WinEvent -FilterHashtable @{LogName='Application'; StartTime=$start} -ErrorAction SilentlyContinue |
                Where-Object { $_.ProviderName -in @('.NET Runtime','Application Error') -and $_.Message.Contains('UPCAMTaskDigest') })
            $entry.managedAppConstructorSeen = [bool](@($events | Where-Object { $_.Message.Contains('Upcam.Desktop.App..ctor') }).Count)
            $entry.argumentExceptionSeen = [bool](@($events | Where-Object { $_.Message.Contains('System.ArgumentException') }).Count)
            $entry.invalidArgumentHresultSeen = [bool](@($events | Where-Object { $_.Message.Contains('80070057') }).Count)
        } finally {
            if ($null -ne $process) {
                if (-not $process.HasExited) { $process.Kill($true); $process.WaitForExit(5000) | Out-Null }
                $process.Dispose()
            }
            $report.Add([pscustomobject]$entry)
            [IO.File]::WriteAllText((Join-Path $EvidenceDirectory 'ordinary-startup-report.json'), (@($report) | ConvertTo-Json -Depth 8), [Text.UTF8Encoding]::new($false))
            if ((Test-Path -LiteralPath $marker) -and [IO.File]::ReadAllText($marker) -eq $owner) {
                Remove-Item -LiteralPath $testProfileDirectory -Recurse -Force
            } else { throw 'Test ownership marker was lost; profile was not removed' }
        }
    }
    if ($failures -gt 0) { throw "$failures ordinary startup cases failed; inspect the safe test report" }
} finally {
    if (Test-Path -LiteralPath (Join-Path $EvidenceDirectory 'ordinary-startup-report.json')) {
        Get-Content -LiteralPath (Join-Path $EvidenceDirectory 'ordinary-startup-report.json')
    }
}
