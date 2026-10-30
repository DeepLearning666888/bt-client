# Preflight check for the BT client field test.
#
# Purpose: answer one question in 30 seconds -- "will the app actually start
# on this machine, and if not, what is missing?" Nothing is installed, nothing
# is modified, no user data is read. Read-only.
#
# Why this exists: engine.exe imports MSVCP140.dll / VCRUNTIME140.dll /
# VCRUNTIME140_1.dll. Those are NOT part of Windows. On a machine without the
# VC++ 2015-2022 x64 redistributable the engine dies instantly with 0xc0000135
# and the UI just says "engine offline" -- indistinguishable from a real bug.
# The dev machine has the runtime, so this can only be caught here.
#
# Exit codes: 0 = READY, 2 = BLOCKED (at least one critical item missing),
#             3 = the script itself failed.
#
# This file is intentionally pure ASCII. On non-English Windows, PowerShell 5.1
# decodes BOM-less scripts with the OEM code page; non-ASCII bytes have caused
# silently dropped fields before.

[CmdletBinding()]
param(
    # '' = do the real check. 'ok' / 'no-vcruntime' / 'no-webview2' = feed a
    # synthetic sample through the verdict logic. Proves the verdict can say no.
    [string]$SelfTest = '',

    # Where to drop the captured text. Defaults to '<script dir>\out'.
    [string]$OutDir = ''
)

$ErrorActionPreference = 'Stop'

$script:Lines = New-Object System.Collections.ArrayList
$script:Results = New-Object System.Collections.ArrayList

function Say {
    # The single writer for everything this script prints. Mixing two writers
    # (Write-Host and Console.Out) garbles line breaks when stdout is redirected,
    # and one of them silently drops empty strings.
    param([string]$Text)
    [Console]::Out.WriteLine($Text)
}

function Write-Out {
    param([string]$Text)
    Say $Text
    [void]$script:Lines.Add($Text)
}

function Join-Safe {
    # Join-Path throws on an empty base, and ProgramFiles(x86) is empty on 32-bit
    # Windows. A preflight script must not die on the machine it is inspecting.
    param([string]$Base, [string]$Leaf)
    if ($Base -eq '' -or $null -eq $Base) { return '' }
    return (Join-Path $Base $Leaf)
}

function Add-Check {
    param(
        [string]$Name,
        [bool]$Ok,
        [string]$Detail,
        [string]$Hint = '',
        [bool]$Critical = $true
    )
    $row = New-Object psobject
    Add-Member -InputObject $row -MemberType NoteProperty -Name Name -Value $Name
    Add-Member -InputObject $row -MemberType NoteProperty -Name Ok -Value $Ok
    Add-Member -InputObject $row -MemberType NoteProperty -Name Detail -Value $Detail
    Add-Member -InputObject $row -MemberType NoteProperty -Name Hint -Value $Hint
    Add-Member -InputObject $row -MemberType NoteProperty -Name Critical -Value $Critical
    [void]$script:Results.Add($row)
}

function Show-Report {
    foreach ($row in $script:Results) {
        if ($row.Ok) {
            $tag = '[PASS]'
        } elseif ($row.Critical) {
            $tag = '[FAIL]'
        } else {
            $tag = '[WARN]'
        }
        Write-Out ("{0} {1,-10} {2}" -f $tag, $row.Name, $row.Detail)
        if ((-not $row.Ok) -and ($row.Hint -ne '')) {
            Write-Out ("       -> " + $row.Hint)
        }
    }

    $blocked = 0
    foreach ($row in $script:Results) {
        if ((-not $row.Ok) -and $row.Critical) { $blocked = $blocked + 1 }
    }

    Write-Out ''
    if ($blocked -eq 0) {
        Write-Out 'VERDICT: READY'
        return 0
    }
    Write-Out ("VERDICT: BLOCKED ({0})" -f $blocked)
    return 2
}

# ------------------------------------------------------------------ detection

function Get-HostInfo {
    $version = [System.Environment]::OSVersion.Version
    $product = ''
    $display = ''
    $key = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion'
    $props = Get-ItemProperty -Path $key -ErrorAction SilentlyContinue
    if ($null -ne $props) {
        if ($null -ne $props.PSObject.Properties['ProductName']) { $product = [string]$props.ProductName }
        if ($null -ne $props.PSObject.Properties['DisplayVersion']) { $display = [string]$props.DisplayVersion }
    }
    # ProductName is unreliable: Windows 11 builds still report "Windows 10 Pro"
    # in this key. The build number is the only trustworthy signal.
    if ($version.Build -ge 22000) {
        $name = 'Windows 11'
    } elseif ($version.Build -ge 10240) {
        $name = 'Windows 10'
    } else {
        $name = 'Windows (older than 10)'
    }
    return New-Object psobject -Property @{
        ProductName = $product
        DisplayVersion = $display
        Name = $name
        Build = $version.Build
        Major = $version.Major
        Is64 = [System.Environment]::Is64BitOperatingSystem
        Machine = $env:COMPUTERNAME
    }
}

function Get-WebView2 {
    # Evergreen runtime uses one fixed product GUID on every machine.
    $guid = '{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}'
    $keys = @(
        "HKLM:\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\$guid",
        "HKLM:\SOFTWARE\Microsoft\EdgeUpdate\Clients\$guid",
        "HKCU:\SOFTWARE\Microsoft\EdgeUpdate\Clients\$guid"
    )
    foreach ($key in $keys) {
        $item = Get-ItemProperty -Path $key -Name pv -ErrorAction SilentlyContinue
        if ($null -ne $item) {
            $pv = [string]$item.pv
            if ($pv -ne '') {
                return New-Object psobject -Property @{ Version = $pv; Source = 'registry' }
            }
        }
    }

    # Fallback for the rare case where the registry key is missing but the
    # runtime directory tree is there.
    $roots = @(
        (Join-Safe $env:LOCALAPPDATA 'Microsoft\EdgeWebView\Application'),
        (Join-Safe ${env:ProgramFiles(x86)} 'Microsoft\EdgeWebView\Application'),
        (Join-Safe $env:ProgramFiles 'Microsoft\EdgeWebView\Application')
    )
    foreach ($root in $roots) {
        if ($root -eq '') { continue }
        if (-not (Test-Path -LiteralPath $root)) { continue }
        $dirs = Get-ChildItem -LiteralPath $root -Directory -ErrorAction SilentlyContinue
        foreach ($dir in $dirs) {
            $exe = Join-Path $dir.FullName 'msedgewebview2.exe'
            if (Test-Path -LiteralPath $exe) {
                return New-Object psobject -Property @{ Version = $dir.Name; Source = 'files' }
            }
        }
    }
    return New-Object psobject -Property @{ Version = ''; Source = 'not-detected' }
}

function Get-VCRuntime {
    # Exactly the three DLLs that engine.exe and torrent-rasterbar.dll import.
    $names = @('msvcp140.dll', 'vcruntime140.dll', 'vcruntime140_1.dll')
    $missing = New-Object System.Collections.ArrayList
    $version = ''
    foreach ($name in $names) {
        $path = Join-Path $env:WINDIR ('System32\' + $name)
        if (-not (Test-Path -LiteralPath $path)) {
            [void]$missing.Add($name)
            continue
        }
        if ($version -eq '') {
            $version = [string](Get-Item -LiteralPath $path).VersionInfo.FileVersion
        }
    }
    return New-Object psobject -Property @{
        Version = $version
        Missing = @($missing)
    }
}

function Get-Browser {
    $candidates = @(
        @{ Name = 'Edge'; Path = (Join-Safe ${env:ProgramFiles(x86)} 'Microsoft\Edge\Application\msedge.exe') },
        @{ Name = 'Edge'; Path = (Join-Safe $env:ProgramFiles 'Microsoft\Edge\Application\msedge.exe') },
        @{ Name = 'Chrome'; Path = (Join-Safe $env:ProgramFiles 'Google\Chrome\Application\chrome.exe') },
        @{ Name = 'Chrome'; Path = (Join-Safe ${env:ProgramFiles(x86)} 'Google\Chrome\Application\chrome.exe') },
        @{ Name = 'Chrome'; Path = (Join-Safe $env:LOCALAPPDATA 'Google\Chrome\Application\chrome.exe') }
    )
    $found = New-Object System.Collections.ArrayList
    foreach ($candidate in $candidates) {
        if ($candidate.Path -eq '') { continue }
        if (-not (Test-Path -LiteralPath $candidate.Path)) { continue }
        $version = [string](Get-Item -LiteralPath $candidate.Path).VersionInfo.ProductVersion
        $entry = ('{0} {1}' -f $candidate.Name, $version)
        if (-not ($found -contains $entry)) { [void]$found.Add($entry) }
    }
    return @($found)
}

function Get-InstallState {
    # "BT client" written as char codes so this file stays pure ASCII.
    $leaf = -join @([char]0x42, [char]0x54, [char]0x20, [char]0x5BA2, [char]0x6237, [char]0x7AEF)
    $dir = Join-Path $env:LOCALAPPDATA $leaf
    $exists = Test-Path -LiteralPath $dir
    return New-Object psobject -Property @{ Dir = $dir; Exists = $exists }
}

# ------------------------------------------------------------------ real check

function Invoke-RealCheck {
    $hostInfo = Get-HostInfo
    $stamp = (Get-Date).ToString('yyyy-MM-dd HH:mm:ss')
    Write-Out '== BT client field test: preflight =='
    Write-Out ("host : {0}  build {1}  {2}" -f $hostInfo.Machine, $hostInfo.Build, $stamp)
    Write-Out ''

    $osDetail = ('{0} {1} (build {2}, x64={3})' -f $hostInfo.Name, $hostInfo.DisplayVersion, $hostInfo.Build, $hostInfo.Is64)
    $osOk = ($hostInfo.Is64 -and ($hostInfo.Build -ge 17763))
    Add-Check -Name 'OS' -Ok $osOk -Detail $osDetail `
        -Hint 'Windows 10 1809 (build 17763) or newer, 64-bit, is required.'

    $webview = Get-WebView2
    $webviewOk = ($webview.Version -ne '')
    Add-Check -Name 'WEBVIEW2' -Ok $webviewOk `
        -Detail ("{0} ({1})" -f $webview.Version, $webview.Source) `
        -Hint 'Missing. The installer then has to download it, which fails without internet. Install the Evergreen WebView2 Runtime first.'

    $vc = Get-VCRuntime
    $vcOk = ($vc.Missing.Count -eq 0)
    if ($vcOk) {
        $vcDetail = ('{0} (msvcp140 / vcruntime140 / vcruntime140_1)' -f $vc.Version)
    } else {
        $vcDetail = ('missing: ' + ($vc.Missing -join ', '))
    }
    Add-Check -Name 'VCRUNTIME' -Ok $vcOk -Detail $vcDetail `
        -Hint 'Missing. engine.exe will exit with 0xc0000135 and the UI will show "engine offline". Copy plan-b-vcruntime\*.dll next to engine.exe after installing, or install the VC++ 2015-2022 x64 redistributable.'

    $browsers = Get-Browser
    $browserOk = ($browsers.Count -gt 0)
    if ($browserOk) {
        $browserDetail = ($browsers -join '; ')
    } else {
        $browserDetail = 'neither Edge nor Chrome found'
    }
    Add-Check -Name 'BROWSER' -Ok $browserOk -Detail $browserDetail `
        -Hint 'Needed only for the rendering/fps track. Edge ships with Windows 10/11; if it is gone, install any modern browser.'

    $session = [string]$env:SESSIONNAME
    if ($session -eq '') { $session = 'unknown' }
    if ($session -like 'RDP-*') {
        Add-Check -Name 'SESSION' -Ok $false -Critical $false `
            -Detail ('remote session (' + $session + ')') `
            -Hint 'Rendering numbers taken over RDP/ToDesk/Sunlogin are software-rendered and must not be used to judge the code. Report it, still run.'
    } else {
        Add-Check -Name 'SESSION' -Ok $true -Critical $false -Detail ('local (' + $session + ')')
    }

    $install = Get-InstallState
    if ($install.Exists) {
        Add-Check -Name 'INSTALLDIR' -Ok $true -Critical $false `
            -Detail ('already present: ' + $install.Dir) `
            -Hint ''
    } else {
        Add-Check -Name 'INSTALLDIR' -Ok $true -Critical $false -Detail 'not present (first install)'
    }

    $drive = Get-PSDrive -Name 'C' -ErrorAction SilentlyContinue
    if ($null -ne $drive) {
        $freeGb = [math]::Round($drive.Free / 1GB, 1)
        Add-Check -Name 'FREESPACE' -Ok ($freeGb -ge 2) -Critical $false `
            -Detail ('C: ' + $freeGb + ' GB free') `
            -Hint 'Less than 2 GB free. The install needs about 60 MB; the rendering track needs none.'
    }

    Write-Out ''
    return (Show-Report)
}

# ------------------------------------------------------------------ self test

function Invoke-SelfTest {
    param([string]$Case)

    Write-Out ("== preflight self test: {0} ==" -f $Case)

    switch ($Case) {
        'ok' {
            Add-Check -Name 'OS' -Ok $true -Detail 'sample'
            Add-Check -Name 'WEBVIEW2' -Ok $true -Detail 'sample'
            Add-Check -Name 'VCRUNTIME' -Ok $true -Detail 'sample'
            Add-Check -Name 'BROWSER' -Ok $true -Detail 'sample'
        }
        'no-vcruntime' {
            Add-Check -Name 'OS' -Ok $true -Detail 'sample'
            Add-Check -Name 'WEBVIEW2' -Ok $true -Detail 'sample'
            Add-Check -Name 'VCRUNTIME' -Ok $false -Detail 'missing: msvcp140.dll' -Hint 'sample hint'
            Add-Check -Name 'BROWSER' -Ok $true -Detail 'sample'
        }
        'no-webview2' {
            Add-Check -Name 'OS' -Ok $true -Detail 'sample'
            Add-Check -Name 'WEBVIEW2' -Ok $false -Detail 'not-detected' -Hint 'sample hint'
            Add-Check -Name 'VCRUNTIME' -Ok $true -Detail 'sample'
            Add-Check -Name 'BROWSER' -Ok $true -Detail 'sample'
        }
        default {
            Write-Out ("unknown self test case: " + $Case)
            return 3
        }
    }

    Write-Out ''
    return (Show-Report)
}

# ------------------------------------------------------------------------ main

$code = 3
try {
    if ($SelfTest -ne '') {
        $code = Invoke-SelfTest -Case $SelfTest
    } else {
        $code = Invoke-RealCheck
    }
} catch {
    Say ('preflight failed: ' + $_.Exception.Message)
    exit 3
}

# Keep a copy next to the script. The command window is easy to close before
# anyone copies the text, and this report is part of the evidence.
try {
    if ($OutDir -eq '') {
        $OutDir = Join-Path $PSScriptRoot 'out'
    }
    if (-not (Test-Path -LiteralPath $OutDir)) {
        New-Item -ItemType Directory -Path $OutDir -Force | Out-Null
    }
    if ($SelfTest -ne '') {
        $name = 'preflight-selftest-' + $SelfTest + '.txt'
    } else {
        $name = 'preflight.txt'
    }
    $target = Join-Path $OutDir $name
    Set-Content -LiteralPath $target -Value $script:Lines -Encoding UTF8
    Say ''
    Say ('saved: ' + $target)
} catch {
    Say ('could not write the report file: ' + $_.Exception.Message)
}

exit $code
