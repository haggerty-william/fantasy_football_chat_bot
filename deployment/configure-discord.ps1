#Requires -Version 5.1
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][ValidatePattern('^[1-9][0-9]*$')][string]$GuildId,
    [ValidatePattern('^[1-9][0-9]*$')][string]$ChannelId,
    [string]$Profile = 'minikube',
    [string]$Namespace = 'default'
)
$ErrorActionPreference = 'Stop'
$secureToken = Read-Host 'Discord BOT token (input hidden)' -AsSecureString
$tokenPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureToken)
try {
    $botToken = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($tokenPointer)
    if ([string]::IsNullOrWhiteSpace($botToken)) { throw 'A bot token is required.' }
    $secret = @{
        apiVersion = 'v1'; kind = 'Secret'
        metadata = @{ name = 'fantasy-football-discord-app'; namespace = $Namespace }
        type = 'Opaque'
        stringData = @{
            DISCORD_BOT_TOKEN = $botToken.Trim()
            DISCORD_GUILD_ID = $GuildId
            DISCORD_COMMAND_CHANNEL_ID = "$ChannelId"
        }
    }
    # Send via stdin, never command arguments or a credentials file.
    $secret | ConvertTo-Json -Depth 5 | & kubectl --context $Profile --namespace $Namespace apply --server-side --field-manager=discord-setup -f -
    if ($LASTEXITCODE -ne 0) { throw 'Discord Secret update failed.' }
} finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($tokenPointer)
    $botToken = $null
    $secret = $null
    $secureToken.Dispose()
}
& kubectl --context $Profile --namespace $Namespace rollout restart deployment/fantasy-football-bot
if ($LASTEXITCODE -ne 0) { throw 'Bot restart failed.' }
& kubectl --context $Profile --namespace $Namespace rollout status deployment/fantasy-football-bot --timeout=180s
if ($LASTEXITCODE -ne 0) { throw 'Bot rollout failed.' }
Write-Host 'Check bot logs for: Discord interactions ready'
