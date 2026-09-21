param(
    [string]$Title = "cxmon",
    [string]$Message = "",
    [int]$Seconds = 10
)
# Tray balloon notification.
#
# KEEP THIS FILE ASCII-ONLY.
# Windows PowerShell 5.1 reads .ps1 files using the system ANSI codepage.
# If a UTF-8 Chinese character sits right before a closing quote, the quote byte
# (0x22) is swallowed as a GBK trail byte -> unterminated string -> syntax error.
# Verified: this made the balloon channel fail with exit code 1.
# Chinese text is passed in from Python as arguments instead.
$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$icon = New-Object System.Windows.Forms.NotifyIcon
$icon.Icon = [System.Drawing.SystemIcons]::Warning
$icon.Visible = $true
$icon.BalloonTipTitle = $Title
$icon.BalloonTipText = $Message
$icon.BalloonTipIcon = [System.Windows.Forms.ToolTipIcon]::Warning
$icon.ShowBalloonTip($Seconds * 1000)
Start-Sleep -Seconds $Seconds
$icon.Visible = $false
$icon.Dispose()
