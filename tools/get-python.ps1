# Downloads the official Python installer from python.org, checks it is signed by the
# Python Software Foundation, and installs it for this Windows user only, so no
# administrator prompt appears. build.bat runs this when no Python 3.10+ is found.
# Add -WhatIf to download and check the installer without installing it.
[CmdletBinding(SupportsShouldProcess)]
param(
    [string]$Version = '3.12.10'
)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'  # The progress bar makes downloads many times slower.
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$arch = if ($env:PROCESSOR_ARCHITECTURE -eq 'ARM64') { 'arm64' } else { 'amd64' }
$url = "https://www.python.org/ftp/python/$Version/python-$Version-$arch.exe"
$file = Join-Path $env:TEMP "python-$Version-$arch.exe"

Write-Host "Downloading Python $Version from python.org..."
Invoke-WebRequest -Uri $url -OutFile $file -UseBasicParsing

$signature = Get-AuthenticodeSignature -FilePath $file
if ($signature.Status -ne 'Valid' -or $signature.SignerCertificate.Subject -notmatch 'O=Python Software Foundation') {
    Remove-Item $file -ErrorAction SilentlyContinue -WhatIf:$false
    throw "The download is not signed by the Python Software Foundation, so it was not run."
}
Write-Host "The installer is signed by the Python Software Foundation."

if ($PSCmdlet.ShouldProcess("Python $Version for this user", 'Install')) {
    Write-Host 'Installing Python for this user. This takes a minute...'
    $arguments = @('/quiet', 'InstallAllUsers=0', 'InstallLauncherAllUsers=0', 'Include_launcher=1', 'PrependPath=1', 'Include_test=0', 'Shortcuts=0')
    $process = Start-Process -FilePath $file -ArgumentList $arguments -Wait -PassThru
    if ($process.ExitCode -ne 0) {
        Remove-Item $file -ErrorAction SilentlyContinue -WhatIf:$false
        throw "The Python installer stopped with code $($process.ExitCode)."
    }
    Write-Host "Python $Version is installed."
}
Remove-Item $file -ErrorAction SilentlyContinue -WhatIf:$false
