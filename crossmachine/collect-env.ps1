#Requires -Version 5.1
<#
Collects the environment fingerprint of the target machine. This is the context
needed to interpret frame-rate numbers.

Why it matters: 55fps on an i5-8250U with integrated graphics at 150% scaling and
power-saver mode means something completely different from 55fps on an RTX 3060 at
100% scaling. A bare fps number cannot distinguish "the code is slow" from
"this machine is just like that".

IMPORTANT - this file must stay pure ASCII.
Windows PowerShell 5.1 decodes a UTF-8 script WITHOUT a BOM as ANSI (code page 936
on a Chinese system). Non-ASCII bytes get mangled, and a mangled multi-byte
sequence can swallow the newline that follows it, merging the next statement into
the comment. That failure mode is silent: the script still runs, but assignments
quietly disappear. Keeping the file ASCII removes the whole class of problem
regardless of the machine's code page.

Read-only. Changes nothing on the system. Output goes to out\ next to this script,
UTF-8 without BOM.
#>

$ErrorActionPreference = 'Stop'

function Get-Safe {
    param([scriptblock]$Block, $Fallback = $null)
    try { & $Block } catch { $Fallback }
}

function Get-BrowserInfo {
    param([string[]]$Candidates)
    foreach ($path in $Candidates) {
        if (Test-Path -LiteralPath $path) {
            $info = (Get-Item -LiteralPath $path).VersionInfo
            return [ordered]@{
                found   = $true
                path    = $path
                version = $info.FileVersion
            }
        }
    }
    return [ordered]@{ found = $false; path = $null; version = $null }
}

function Get-WebView2Version {
    # Evergreen WebView2 Runtime uses a fixed product GUID across machines
    $guid = '{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}'
    $keys = @(
        "HKLM:\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\$guid",
        "HKLM:\SOFTWARE\Microsoft\EdgeUpdate\Clients\$guid",
        "HKCU:\SOFTWARE\Microsoft\EdgeUpdate\Clients\$guid"
    )
    foreach ($key in $keys) {
        if (Test-Path -LiteralPath $key) {
            $props = Get-ItemProperty -LiteralPath $key -ErrorAction SilentlyContinue
            if ($null -ne $props -and $null -ne $props.pv) {
                return [pscustomobject]@{ version = "$($props.pv)"; source = 'registry' }
            }
        }
    }
    # Fallback: the registry entry can be missing or unreadable even when the
    # runtime is present. Look for the files instead.
    # Reporting null from a single probe used to be read as "not installed",
    # which is a stronger claim than "could not detect". Keep them separate.
    $roots = @(
        "${env:ProgramFiles(x86)}\Microsoft\EdgeWebView\Application",
        "$env:ProgramFiles\Microsoft\EdgeWebView\Application"
    )
    foreach ($root in $roots) {
        if (-not (Test-Path -LiteralPath $root)) { continue }
        $exe = Get-ChildItem -LiteralPath $root -Filter 'msedgewebview2.exe' -Recurse -ErrorAction SilentlyContinue |
            Sort-Object { $_.Directory.Name } -Descending | Select-Object -First 1
        if ($null -ne $exe) {
            return [pscustomobject]@{ version = "$($exe.Directory.Name)"; source = 'files' }
        }
    }
    return [pscustomobject]@{ version = $null; source = 'not-detected' }
}

$os = Get-CimInstance Win32_OperatingSystem
$cpu = Get-CimInstance Win32_Processor | Select-Object -First 1
$cs = Get-CimInstance Win32_ComputerSystem
$video = Get-CimInstance Win32_VideoController | Select-Object -First 1

$gpus = @(
    Get-CimInstance Win32_VideoController | ForEach-Object {
        $dt = $_.DriverDate
        $ram = $_.AdapterRAM
        [ordered]@{
            name          = $_.Name
            driverVersion = $_.DriverVersion
            # Serializing a raw DateTime yields /Date(1781654400000)/ which is unreadable
            driverDate    = $(if ($null -ne $dt) { $dt.ToString('yyyy-MM-dd') } else { $null })
            adapterRamMB  = $(if ($null -ne $ram) { [math]::Round($ram / 1MB) } else { $null })
            isVirtual     = ("$($_.Name)" -match 'Virtual|ToDesk|Parsec|Sunlogin|AnyDesk|RustDesk|Mirror')
        }
    }
)

# Scaling ratio: LogPixels 96=100% 120=125% 144=150% 168=175% 192=200%
$logPixels = Get-Safe { (Get-ItemProperty -LiteralPath 'HKCU:\Control Panel\Desktop' -Name LogPixels).LogPixels } 96
if ($null -eq $logPixels -or $logPixels -le 0) { $logPixels = 96 }

$scheme = Get-Safe { (powercfg /getactivescheme) -join ' ' } ''

$disks = @(
    Get-Safe {
        Get-PhysicalDisk | ForEach-Object {
            [ordered]@{
                name      = $_.FriendlyName
                mediaType = "$($_.MediaType)"
                sizeGB    = [math]::Round($_.Size / 1GB)
            }
        }
    } @()
)

$wv = Get-WebView2Version

$result = [ordered]@{
    schema       = 'env-v1'
    tookAt       = (Get-Date).ToString('o')
    host         = $env:COMPUTERNAME
    os           = [ordered]@{
        caption = $os.Caption
        version = $os.Version
        build   = $os.BuildNumber
        arch    = $os.OSArchitecture
    }
    cpu          = [ordered]@{
        name        = $(if ($null -ne $cpu) { $cpu.Name } else { $null })
        cores       = $(if ($null -ne $cpu) { $cpu.NumberOfCores } else { $null })
        threads     = $(if ($null -ne $cpu) { $cpu.NumberOfLogicalProcessors } else { $null })
        maxClockMHz = $(if ($null -ne $cpu) { $cpu.MaxClockSpeed } else { $null })
    }
    memoryGB     = $(if ($null -ne $cs) { [math]::Round($cs.TotalPhysicalMemory / 1GB, 1) } else { $null })
    manufacturer = $(if ($null -ne $cs) { "$($cs.Manufacturer) $($cs.Model)" } else { $null })
    gpus         = $gpus
    display      = [ordered]@{
        scalePercent = [math]::Round($logPixels / 96 * 100)
        logPixels    = $logPixels
        width        = $(if ($null -ne $video) { $video.CurrentHorizontalResolution } else { $null })
        height       = $(if ($null -ne $video) { $video.CurrentVerticalResolution } else { $null })
        refreshHz    = $(if ($null -ne $video) { $video.CurrentRefreshRate } else { $null })
    }
    power        = [ordered]@{
        activeScheme = $scheme
        # Power saver drags the refresh rate down; those numbers are not comparable
        hint         = $(if ($scheme -match 'Power saver') { 'power-saver' }
                        elseif ($scheme -match 'High performance') { 'high-performance' }
                        elseif ($scheme -match 'Ultimate') { 'ultimate' }
                        else { 'balanced-or-other' })
    }
    browsers     = [ordered]@{
        chrome = Get-BrowserInfo @(
            'C:\Program Files\Google\Chrome\Application\chrome.exe',
            'C:\Program Files (x86)\Google\Chrome\Application\chrome.exe',
            "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe"
        )
        edge   = Get-BrowserInfo @(
            'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe',
            'C:\Program Files\Microsoft\Edge\Application\msedge.exe'
        )
    }
    webview2Version = $wv.version
    # 'registry' | 'files' | 'not-detected'. null version with source
    # 'not-detected' means the probe failed, NOT that the runtime is absent.
    webview2Source  = $wv.source
    disks           = $disks
    powershell      = $PSVersionTable.PSVersion.ToString()
}

$remoteHints = @('ToDesk', 'SunloginClient', 'AnyDesk', 'Parsec', 'RustDesk', 'TeamViewer')
$remoteRunning = @(
    Get-Safe {
        Get-Process | Where-Object { $remoteHints -contains $_.ProcessName } |
            ForEach-Object { $_.ProcessName } | Sort-Object -Unique
    } @()
)
$virtualDisplay = @($gpus | Where-Object { $_.isVirtual } | ForEach-Object { $_.name })

$isRdp = ($env:SESSIONNAME -like 'RDP-*')

# Graded caveat. Having a remote-control tool installed is not the same as being
# remote-controlled right now, so "virtual display present" is only a caution,
# while an actual RDP session invalidates the numbers.
$caveat = 'clean'
if ($isRdp) {
    $caveat = 'remote-session'
} elseif ($remoteRunning.Count -gt 0) {
    $caveat = 'remote-tool-running'
} elseif ($virtualDisplay.Count -gt 0) {
    $caveat = 'virtual-display-present'
}

$result['virtualization'] = [ordered]@{
    isVirtualMachine       = ($null -ne $cs -and "$($cs.Model)" -match 'Virtual|VMware|VirtualBox|KVM|Hyper-V')
    sessionName            = $env:SESSIONNAME
    isRemoteSession        = $isRdp
    remoteToolsRunning     = $remoteRunning
    virtualDisplayAdapters = $virtualDisplay
    caveat                 = $caveat
}

$outDir = Join-Path $PSScriptRoot 'out'
New-Item -ItemType Directory -Force -Path $outDir | Out-Null
$outFile = Join-Path $outDir ("env_" + $env:COMPUTERNAME + ".json")

$json = $result | ConvertTo-Json -Depth 8
$utf8 = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText($outFile, $json, $utf8)

Write-Output $json
Write-Output ''
Write-Output "written to $outFile"
