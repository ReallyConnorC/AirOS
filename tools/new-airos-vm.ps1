# Creates a Hyper-V virtual machine for testing Air OS, with the Debian installer attached.
# Run in PowerShell as Administrator, after Hyper-V is turned on:
#   powershell -ExecutionPolicy Bypass -File tools\new-airos-vm.ps1
$ErrorActionPreference = 'Stop'

$VmName   = 'AirOS'
$VmDir    = Join-Path $env:PUBLIC 'Documents\Hyper-V\AirOS'
$DebianDir = 'https://cdimage.debian.org/debian-cd/current/amd64/iso-cd/'

if (-not (Get-Command New-VM -ErrorAction SilentlyContinue)) {
    Write-Host 'Hyper-V is not turned on yet. Turn it on first (see the README), restart, then run this again.' -ForegroundColor Yellow
    exit 1
}
if (Get-VM -Name $VmName -ErrorAction SilentlyContinue) {
    Write-Host "A VM named '$VmName' already exists. Opening it."
    vmconnect.exe localhost $VmName
    exit 0
}
New-Item -ItemType Directory -Force $VmDir | Out-Null

# 1. Download the latest Debian netinst ISO and check it against Debian's published checksums
$sums = (New-Object System.Net.WebClient).DownloadString($DebianDir + 'SHA512SUMS')  # plain text, unlike Invoke-WebRequest which may return bytes
$line = ($sums -split "`r?`n") | Where-Object { $_ -match 'amd64-netinst\.iso\s*$' } | Select-Object -First 1
if (-not $line) { throw "Couldn't find the Debian installer in the list at $DebianDir. Check your internet connection and try again." }
$hash, $isoName = $line.Trim() -split '\s+', 2
$iso = Join-Path $VmDir $isoName.Trim()
if (-not (Test-Path $iso)) {
    Write-Host "Downloading $isoName (about 700 MB)..."
    $ProgressPreference = 'SilentlyContinue'   # the progress bar makes Invoke-WebRequest very slow
    Invoke-WebRequest -UseBasicParsing ($DebianDir + $isoName.Trim()) -OutFile $iso
}
if ((Get-FileHash $iso -Algorithm SHA512).Hash -ne $hash.ToUpper()) {
    Remove-Item $iso
    throw 'The downloaded ISO is corrupt (checksum mismatch). Run the script again.'
}

# 2. Create the VM: 4 GB RAM, 2 CPUs, 32 GB disk, internet through Hyper-V's Default Switch
New-VM -Name $VmName -Generation 2 -MemoryStartupBytes 4GB -SwitchName 'Default Switch' `
       -NewVHDPath (Join-Path $VmDir 'AirOS.vhdx') -NewVHDSizeBytes 32GB -Path $VmDir | Out-Null
Set-VMProcessor -VMName $VmName -Count 2
Set-VMMemory -VMName $VmName -DynamicMemoryEnabled $false
Set-VMFirmware -VMName $VmName -SecureBootTemplate 'MicrosoftUEFICertificateAuthority'  # lets Linux boot
$dvd = Add-VMDvdDrive -VMName $VmName -Path $iso -Passthru
Set-VMFirmware -VMName $VmName -FirstBootDevice $dvd
Set-VM -Name $VmName -AutomaticCheckpointsEnabled $false

# 3. Start it and open its screen
Start-VM -Name $VmName
vmconnect.exe localhost $VmName
Write-Host "`nThe '$VmName' VM is running. Follow the Debian installer in the window that opened." -ForegroundColor Green
