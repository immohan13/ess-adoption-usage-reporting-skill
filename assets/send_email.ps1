<#
.SYNOPSIS
  Open the agent-adoption-report email in Outlook for review (NEVER auto-sends by default).

.DESCRIPTION
  Reads recipients + subject from the YAML config and the HTML body from
  <output_dir>/email.html (where output_dir comes from the config or
  defaults to ./out next to the config file). Uses the local installed
  Outlook profile via COM -- no Graph tokens, no SMTP.

.PARAMETER Config
  Path to the YAML config file used by the pipeline.

.PARAMETER Send
  If provided, sends immediately. Default behaviour is .Display() so the
  user can review and click Send manually. Per skill safety rules, the
  default invocation must always require the user to confirm in Outlook.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)] [string] $Config,
    [switch] $Send
)

$ErrorActionPreference = 'Stop'

$cfgPath = (Resolve-Path -Path $Config).Path
$cfgDir  = Split-Path -Parent $cfgPath

# Parse a few fields out of the YAML without a YAML module dependency.
# Walk line by line -- Select-String on -Raw treats the whole file as one
# string, which means ^ / $ won't match per line and the regexes silently fail.
$yamlLines = Get-Content -Path $cfgPath
$subject = $null
$outputDir = $null
foreach ($ln in $yamlLines) {
    if (-not $subject   -and $ln -match '^\s*subject:\s*"?(.+?)"?\s*$')    { $subject   = $matches[1].Trim('"') }
    if (-not $outputDir -and $ln -match '^\s*output_dir:\s*"?(.+?)"?\s*$') { $outputDir = $matches[1].Trim('"') }
}
if (-not $subject)   { $subject = "Agent adoption report" }
if (-not $outputDir) { $outputDir = 'out' }
if (-not [System.IO.Path]::IsPathRooted($outputDir)) {
    $outputDir = Join-Path $cfgDir $outputDir
}

$bodyFile = Join-Path $outputDir 'email.html'
if (-not (Test-Path $bodyFile)) {
    throw "email body not found: $bodyFile  (run the pipeline first)"
}

# Recipients: collect all `- foo@bar.com` lines after the `recipients:` key.
$inRecip = $false
$recipients = @()
foreach ($ln in $yamlLines) {
    if ($ln -match '^\s*recipients:\s*$')           { $inRecip = $true; continue }
    if ($inRecip -and $ln -match '^\s*-\s*(.+?)\s*$') { $recipients += $matches[1].Trim('"'); continue }
    # Stop at the next non-indented or non-list-item key.
    if ($inRecip -and $ln -notmatch '^\s*-\s' -and $ln.Trim() -ne '') { break }
}
if (-not $recipients) { throw "no recipients found in config" }

$to = ($recipients -join '; ')
Write-Host "Subject:    $subject"    -ForegroundColor Cyan
Write-Host "Recipients: $to"         -ForegroundColor Cyan
Write-Host "Body:       $bodyFile"   -ForegroundColor Cyan

Write-Host "Launching Outlook COM..." -ForegroundColor Cyan
$ol = New-Object -ComObject Outlook.Application
$mail = $ol.CreateItem(0)
$mail.To       = $to
$mail.Subject  = $subject
$mail.HTMLBody = (Get-Content -Raw -Path $bodyFile)

if ($Send.IsPresent) {
    Write-Host "Sending..." -ForegroundColor Yellow
    $mail.Send()
    Write-Host "Sent." -ForegroundColor Green
} else {
    Write-Host "Opening in Outlook for review (no Send yet)..." -ForegroundColor Yellow
    $mail.Display($false)
    Write-Host "Outlook compose window open. Review and click Send when ready." -ForegroundColor Green
}
