param(
    [string]$Configuration = "Release"
)

$OldErrorActionPreference = $ErrorActionPreference
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Source = Join-Path $PSScriptRoot "mf_single_frame_helper.cpp"
$OutDir = Join-Path $PSScriptRoot "build\$Configuration"
$OutExe = Join-Path $OutDir "mf_single_frame_helper.exe"

try {
    New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

    if (-not (Get-Command cl.exe -ErrorAction SilentlyContinue)) {
        throw "cl.exe not found. Run this from a Visual Studio Developer PowerShell."
    }

    if (-not (Test-Path $Source)) {
        throw "Source file not found: $Source"
    }

    $CompileFlags = @("/nologo", "/std:c++17", "/EHsc", "/W4")
    if ($Configuration -ieq "Debug") {
        $CompileFlags += @("/Od", "/Zi")
    } else {
        $CompileFlags += @("/O2")
    }
    $Libraries = @(
        "mf.lib",
        "mfplat.lib",
        "mfreadwrite.lib",
        "mfuuid.lib",
        "ole32.lib",
        "propsys.lib"
    )

    cl.exe @CompileFlags "/Fe:$OutExe" $Source @Libraries

    if ($LASTEXITCODE -ne 0) {
        throw "cl.exe failed with exit code $LASTEXITCODE"
    }

    Write-Host "Built $OutExe"
} finally {
    $ErrorActionPreference = $OldErrorActionPreference
}
