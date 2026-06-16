param(
    [string]$Configuration = "Release"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Source = Join-Path $PSScriptRoot "mf_single_frame_helper.cpp"
$OutDir = Join-Path $PSScriptRoot "build\$Configuration"
$OutExe = Join-Path $OutDir "mf_single_frame_helper.exe"

New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

if (-not (Get-Command cl.exe -ErrorAction SilentlyContinue)) {
    throw "cl.exe not found. Run this from a Visual Studio Developer PowerShell."
}

cl.exe /nologo /std:c++17 /EHsc /O2 /W4 `
    /Fe:$OutExe `
    $Source `
    mf.lib mfplat.lib mfreadwrite.lib mfuuid.lib ole32.lib propsys.lib

if ($LASTEXITCODE -ne 0) {
    throw "cl.exe failed with exit code $LASTEXITCODE"
}

Write-Host "Built $OutExe"
