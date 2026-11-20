#requires -Version 5.1
[CmdletBinding()]
param([Parameter(Mandatory = $true)][string]$DriverPath, [Parameter(Mandatory = $true)][string]$ReportPath)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
if ($PSVersionTable.PSEdition -cne 'Desktop') { throw 'Windows PowerShell 5.1 is required.' }
$tokens = $null
$errors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile([IO.Path]::GetFullPath($DriverPath), [ref]$tokens, [ref]$errors)
if ($errors.Count -ne 0) { throw 'Driver syntax did not parse.' }
foreach ($name in @('Read-Sha256', 'Get-SafeFailure')) {
    $definitions = @($ast.FindAll({ param($item) $item -is [Management.Automation.Language.FunctionDefinitionAst] -and $item.Name -ceq $name }, $false))
    if ($definitions.Count -ne 1) { throw 'Exactly one expected pure helper is required.' }
    . ([scriptblock]::Create($definitions[0].Extent.Text))
}
# This test extracts only the pure helper definitions. No UI, app, profile or provider is opened.
$report = [IO.Path]::GetFullPath($ReportPath)
if ([IO.File]::Exists($report)) { throw 'A new helper report is required.' }
$fixture = [IO.Path]::Combine([IO.Path]::GetDirectoryName($report), ('hash-fixture-' + [Guid]::NewGuid().ToString('N') + '.txt'))
[IO.File]::WriteAllBytes($fixture, [Text.Encoding]::ASCII.GetBytes('abc'))
$abc = Read-Sha256 $fixture
if ($abc -cne 'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad') { throw 'Known-vector SHA256 failed.' }
$legacy = $null
try { $legacy = [ordered]@{ succeeded = $true; matches = ((Get-FileHash -LiteralPath $fixture -Algorithm SHA256).Hash -ieq $abc); failure = $null } }
catch { $legacy = [ordered]@{ succeeded = $false; matches = $null; failure = (Get-SafeFailure $_) } }
[IO.File]::WriteAllBytes($fixture, [Text.Encoding]::ASCII.GetBytes('abcd'))
$abcd = Read-Sha256 $fixture
if ($abcd -cne '88d4266fd4e6338d13b845fcf289579d209c897823b9217da3e161936f031589' -or $abcd -ceq $abc) { throw 'Changed bytes were not detected.' }
$missingRejected = $false
try { $null = Read-Sha256 ($fixture + '.absent') }
catch { $missingRejected = $true; $null = Get-SafeFailure $_ }
if (-not $missingRejected) { throw 'Missing file was not rejected.' }
$payload = [ordered]@{ succeeded = $true; knownVector = $true; changedBytesDetected = $true; missingFileRejected = $true; legacyHashCmdlet = $legacy }
[IO.File]::WriteAllText($report, ($payload | ConvertTo-Json -Depth 5), (New-Object Text.UTF8Encoding($false)))
Write-Output 'Windows PowerShell SHA256 helper checks passed.'
