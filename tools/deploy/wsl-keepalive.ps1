# Keep the WSL distro that runs the multiplayer services (tools/deploy/install-services.sh)
# running from Windows sign-in on, so the page and the relay are there without a
# terminal open.
#
#   powershell -File tools\deploy\wsl-keepalive.ps1 [-Distro Ubuntu-24.04]           # add, and start now
#   powershell -File tools\deploy\wsl-keepalive.ps1 -Remove                          # take it out again
#
# It puts a shortcut in the user's Startup folder (no administrator rights needed). The
# shortcut runs `wsl.exe -d <distro> --exec sleep infinity` without a window
# (conhost --headless). That process holds the distro open, and systemd starts the
# enabled services. The VM itself also needs vmIdleTimeout=-1 under [wsl2] in
# %USERPROFILE%\.wslconfig, or WSL may still shut it down when idle.
param([string]$Distro = 'Ubuntu-24.04', [switch]$Remove)

$startup = [Environment]::GetFolderPath('Startup')
$lnk = Join-Path $startup "aoe2-on-browser ($Distro).lnk"

if ($Remove) {
    Remove-Item -LiteralPath $lnk -ErrorAction SilentlyContinue
    Get-CimInstance Win32_Process -Filter "Name = 'wsl.exe'" |
        Where-Object { $_.CommandLine -like "*-d $Distro --exec sleep infinity*" } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
    "removed $lnk"
    return
}

$sys = Join-Path $env:SystemRoot 'System32'
$shell = New-Object -ComObject WScript.Shell
$s = $shell.CreateShortcut($lnk)
$s.TargetPath = Join-Path $sys 'conhost.exe'
$s.Arguments = "--headless `"$(Join-Path $sys 'wsl.exe')`" -d $Distro --exec sleep infinity"
$s.WorkingDirectory = $sys
$s.WindowStyle = 7
$s.Description = 'Keeps WSL running for the aoe2-on-browser page and relay'
$s.Save()
"added $lnk"

$running = Get-CimInstance Win32_Process -Filter "Name = 'wsl.exe'" |
    Where-Object { $_.CommandLine -like "*-d $Distro --exec sleep infinity*" }
if (-not $running) { Start-Process -FilePath $s.TargetPath -ArgumentList $s.Arguments -WindowStyle Hidden; 'started it now' }
else { 'already running' }
