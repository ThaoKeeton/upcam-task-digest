[CmdletBinding()]
param(
    [ValidateSet('Debug', 'Release')][string]$Configuration = 'Release',
    [switch]$BuildAcceptance
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
if (-not $IsWindows) { throw 'Native plugin packaging requires Windows.' }
$repo = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../..'))
$outputRoot = Join-Path $repo 'dist/workbench-plugin'
$project = Join-Path $repo 'plugins/UPCAM.Workbench/UPCAM.Workbench.csproj'

function Invoke-Checked {
    param([string]$Tool, [string[]]$Arguments)
    & $Tool @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Tool failed with exit code $LASTEXITCODE" }
}

function Write-PackageInventory {
    param([string]$Directory, [string]$Id, [bool]$Synthetic)
    $files = @(Get-ChildItem -LiteralPath $Directory -File -Recurse | Sort-Object FullName | ForEach-Object {
        @{
            path = [IO.Path]::GetRelativePath($Directory, $_.FullName).Replace('\', '/')
            bytes = $_.Length
            sha256 = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
        }
    })
    $sourceCommit = (& git -C $repo rev-parse HEAD).Trim()
    if ($LASTEXITCODE -ne 0 -or $sourceCommit -notmatch '^[0-9a-f]{40}$') { throw 'Cannot identify source commit.' }
    $metadata = @{
        schemaVersion = 1
        pluginId = $Id
        pluginVersion = '0.1.1'
        sourceCommit = $sourceCommit
        backendVersion = '0.4.6'
        protocolVersion = 1
        hostReferenceCommit = 'd2005d469aef6a385c134b6914fb6dfa9cff20b2'
        minimumHostVersion = '0.2.0'
        runtimeServicesVersion = 1
        assemblyTargetFramework = 'net10.0-windows10.0.19041.0'
        windowsAppSdk = '1.8.260921001'
        workerExecutable = 'worker/UPCAMPluginWorker.exe'
        syntheticAcceptance = $Synthetic
        publisherSignatureVerified = $false
        files = $files
    }
    $metadata | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (Join-Path $Directory 'plugin-package.json') -Encoding utf8NoBOM
    Invoke-Checked -Tool python -Arguments @((Join-Path $PSScriptRoot 'audit_package.py'), $Directory)
}

function Build-PluginAssembly {
    param([bool]$Synthetic)
    $flavor = if ($Synthetic) { 'acceptance' } else { 'production' }
    $compiled = Join-Path $outputRoot "compiled-$flavor"
    New-Item -ItemType Directory -Path $compiled -Force | Out-Null
    $acceptanceValue = if ($Synthetic) { 'true' } else { 'false' }
    Invoke-Checked -Tool dotnet -Arguments @('build', $project, '-c', $Configuration,
        '--no-incremental', '-p:Platform=x64', "-p:AcceptanceBuild=$acceptanceValue", '-o', $compiled)
}

function Build-Package {
    param([string]$Id, [bool]$Synthetic)
    $flavor = if ($Synthetic) { 'acceptance' } else { 'production' }
    $compiled = Join-Path $outputRoot "compiled-$flavor"
    $package = Join-Path $outputRoot "packages/$Id"
    New-Item -ItemType Directory -Path $package -Force | Out-Null
    # The plugin has no private managed dependencies. SDK, WinUI, WinRT and .NET
    # copies in the compiler output belong to the host and must not be shipped.
    foreach ($name in @('UPCAM.Workbench.dll', 'UPCAM.Workbench.deps.json')) {
        Copy-Item -LiteralPath (Join-Path $compiled $name) -Destination $package
    }
    $manifest = if ($Synthetic) { 'plugin.acceptance.json' } else { 'plugin.json' }
    Copy-Item -LiteralPath (Join-Path (Split-Path $project -Parent) $manifest) -Destination (Join-Path $package 'plugin.json')
    Copy-Item -LiteralPath (Join-Path $outputRoot 'worker-build/UPCAMPluginWorker') -Destination (Join-Path $package 'worker') -Recurse
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'README.zh-CN.md') -Destination $package
    Copy-Item -LiteralPath (Join-Path $repo 'THIRD_PARTY_NOTICES.md') -Destination $package
    Copy-Item -LiteralPath (Join-Path $repo 'LICENSES') -Destination $package -Recurse
    Write-PackageInventory -Directory $package -Id $Id -Synthetic $Synthetic
    $archive = Join-Path $outputRoot "UPCAMTaskDigest-workbench-$flavor-0.1.1-win-x64.zip"
    Compress-Archive -LiteralPath $package -DestinationPath $archive
    $hash = (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant()
    "$hash  $([IO.Path]::GetFileName($archive))" | Set-Content -LiteralPath "$archive.sha256" -Encoding utf8NoBOM
    Write-Host "Built $flavor plugin package: $archive"
}

Push-Location $repo
try {
    Invoke-Checked -Tool python -Arguments @((Join-Path $PSScriptRoot 'verify_reference.py'))
    if (Test-Path -LiteralPath $outputRoot) { Remove-Item -LiteralPath $outputRoot -Recurse -Force }
    New-Item -ItemType Directory -Path $outputRoot -Force | Out-Null
    # Compile both native variants first so compiler diagnostics are not hidden
    # behind Python packaging or process-test failures.
    Build-PluginAssembly -Synthetic $false
    if ($BuildAcceptance) { Build-PluginAssembly -Synthetic $true }
    Invoke-Checked -Tool pyinstaller -Arguments @('--clean', '--noconfirm', '--onedir', '--console',
        '--name', 'UPCAMPluginWorker', '--collect-all', 'tzdata',
        '--add-data', ((Join-Path $repo 'templates') + ';templates'),
        '--add-data', ((Join-Path $repo 'examples') + ';examples'),
        '--distpath', (Join-Path $outputRoot 'worker-build'),
        '--workpath', (Join-Path $repo 'build/workbench-worker'),
        '--specpath', (Join-Path $repo 'build'), (Join-Path $repo 'plugin_worker_entry.py'))
    Build-Package -Id 'com.upcam.task-digest' -Synthetic $false
    if ($BuildAcceptance) {
        Build-Package -Id 'com.upcam.task-digest.acceptance' -Synthetic $true
        Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'ACCEPTANCE.zh-CN.md') -Destination $outputRoot
    }
}
finally { Pop-Location }
