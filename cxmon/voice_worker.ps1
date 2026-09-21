param(
    [string]$Voice = "",
    [int]$Rate = 1
)
# Resident TTS worker: reads one line at a time from stdin and speaks it.
# Staying resident means each alert costs tens of milliseconds instead of
# restarting PowerShell (~1 second) every time.
# Parent exits -> stdin closes -> ReadLine returns $null -> loop ends.
#
# KEEP THIS FILE ASCII-ONLY (same reason as toast.ps1): Windows PowerShell 5.1
# reads .ps1 files using the system ANSI codepage, and UTF-8 Chinese next to a
# quote can swallow that quote and break parsing.
$ErrorActionPreference = "Stop"
[Console]::InputEncoding = New-Object System.Text.UTF8Encoding $false
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false
Add-Type -AssemblyName System.Speech
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
try { $synth.Rate = $Rate } catch { }
if ($Voice) {
    try { $synth.SelectVoice($Voice) } catch { }
}
while ($true) {
    $line = [Console]::In.ReadLine()
    if ($null -eq $line) { break }
    $line = $line.Trim()
    if ($line -eq "") { continue }
    if ($line -eq "__QUIT__") { break }
    try { $synth.Speak($line) } catch { }
}
try { $synth.Dispose() } catch { }
