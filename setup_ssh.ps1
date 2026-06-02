# OpenSSH Server setup for Windows
# Run this as Administrator in PowerShell

$ErrorActionPreference = "Stop"

# 1. Install OpenSSH Server
Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0

# 2. Start the service and set it to auto-start
Start-Service sshd
Set-Service -Name sshd -StartupType 'Automatic'

# 3. Allow SSH through Windows Firewall
New-NetFirewallRule -Name sshd -DisplayName 'OpenSSH Server' `
    -Enabled True -Direction Inbound -Protocol TCP -Action Allow -LocalPort 22

# 4. Set up authorized_keys
$sshDir = "$env:USERPROFILE\.ssh"
$ak = "$sshDir\authorized_keys"

# Create .ssh directory if it doesn't exist
New-Item -ItemType Directory -Path $sshDir -Force | Out-Null

# Write your public key
"ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAINiyn/0XsLqSzrEoT6rrgZTYIzXOJOl5x6pKsK4UK75f 2513868362@qq.com" |
    Out-File -FilePath $ak -Encoding ASCII

# 5. Fix permissions — use & (call operator) so PowerShell doesn't
#    misinterpret icacls /flags as division operators
& icacls $ak /inheritance:r /grant "${env:USERNAME}:F"
& icacls $sshDir /inheritance:r /grant "${env:USERNAME}:F"

# 6. Show the IP address for connecting
Write-Host "`nDone. Connect via: ssh ${env:USERNAME}@<IP>" -ForegroundColor Green
Get-NetIPAddress -AddressFamily IPv4 |
    Where-Object { $_.InterfaceAlias -notlike "*Loopback*" } |
    Select-Object IPAddress, InterfaceAlias |
    Format-Table -AutoSize
