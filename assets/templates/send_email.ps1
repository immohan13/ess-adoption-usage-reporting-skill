<#
.SYNOPSIS
  Send (or preview in Outlook) the agent adoption & usage email via Outlook COM.

.DESCRIPTION
  Uses the local installed Outlook (your signed-in profile) -- no Graph tokens,
  no MCP, no SMTP. Reads the HTML body from .\queries\email-usage-trend-slim.html
  (chart is inline base64). Default action is .Display() so you get a final
  preview window in Outlook itself; pass -Send to skip preview and send.

.PARAMETER Send
  When provided, sends the mail immediately. Otherwise opens it in Outlook
  in a Display window for manual review/Send.

.PARAMETER BodyFile
  Optional override of the HTML body file. Defaults to
  .\queries\email-usage-trend-slim.html relative to the script.

.EXAMPLE
  .\queries\Send-UsageEmail.ps1            # opens in Outlook for preview/Send
  .\queries\Send-UsageEmail.ps1 -Send      # sends immediately
#>
[CmdletBinding()]
param(
    [switch] $Send,
    [string] $BodyFile,
    [string] $To,
    [string] $Subject
)

$ErrorActionPreference = 'Stop'

# Resolve script dir robustly
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $BodyFile) { $BodyFile = Join-Path $scriptDir 'email-3scopes.html' }

if (-not (Test-Path $BodyFile)) {
    throw "Body file not found: $BodyFile"
}

# Recipients + subject are NOT hard-coded so no distribution list lives in source
# control. Resolution order (first non-empty wins):
#   1. -To / -Subject parameters
#   2. $env:ADOPTION_REPORT_RECIPIENTS / $env:ADOPTION_REPORT_SUBJECT
#   3. recipients.local.json next to this script: { "to": "...", "subject": "..." }
$localCfgPath = Join-Path $scriptDir 'recipients.local.json'
$localCfg = $null
if (Test-Path $localCfgPath) {
    $localCfg = Get-Content -Raw -Path $localCfgPath | ConvertFrom-Json
}

if (-not $To)      { $To      = $env:ADOPTION_REPORT_RECIPIENTS }
if (-not $To -and $localCfg) { $To = $localCfg.to }

if (-not $Subject) { $Subject = $env:ADOPTION_REPORT_SUBJECT }
if (-not $Subject -and $localCfg) { $Subject = $localCfg.subject }
if (-not $Subject) {
    $Subject = 'Agent Adoption & Usage report -- WoW trend, top customers, new customers & models in use'
}

if (-not $To) {
    throw "No recipients. Pass -To 'a@x.com; b@x.com', set `$env:ADOPTION_REPORT_RECIPIENTS, or create recipients.local.json (see recipients.example.json)."
}

$to      = $To
$subject = $Subject

Write-Host "Reading body from: $BodyFile" -ForegroundColor Cyan
$html = Get-Content -Raw -Path $BodyFile

Write-Host "Launching Outlook COM..." -ForegroundColor Cyan
$ol = New-Object -ComObject Outlook.Application

# 0 = olMailItem
$mail = $ol.CreateItem(0)
$mail.To       = $to
$mail.Subject  = $subject
$mail.HTMLBody = $html

if ($Send.IsPresent) {
    Write-Host "Sending mail to: $to" -ForegroundColor Yellow
    $mail.Send()
    Write-Host "Sent." -ForegroundColor Green
}
else {
    Write-Host "Opening in Outlook for review (no Send yet)..." -ForegroundColor Yellow
    # $true => modal-ish; pass $false so the PowerShell prompt returns
    $mail.Display($false)
    Write-Host "Outlook compose window open. Review and click Send when ready." -ForegroundColor Green
}
