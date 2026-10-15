# Put the app's own keys into Azure Key Vault. Run it yourself:
#
#   powershell -ExecutionPolicy Bypass -File .\infra\set-secrets.ps1
#
# Copy each value from Vercel (Project -> Settings -> Environment Variables).
# Typing is hidden, nothing is printed or kept on disk, and pressing Enter on
# an empty prompt leaves that key as it is.

$ErrorActionPreference = "Stop"
$vault = "kv-zacon-za1"

$keys = [ordered]@{
    "anthropic-key-intel" = "ANTHROPIC_API_KEY_INTEL (chat, plan, supply write up)"
    "anthropic-key-docs"  = "ANTHROPIC_API_KEY_DOCS (document checker)"
    "anthropic-key"       = "ANTHROPIC_API_KEY (fallback for both)"
    "technofresh-key"     = "TECHNOFRESH_KEY (unlocks the stored TechnoFresh password)"
    "robot-email"         = "ZACON_ROBOT_EMAIL (the 08:00 pull's login)"
    "robot-password"      = "ZACON_ROBOT_PASSWORD"
}

$existing = az keyvault secret list --vault-name $vault --query "[].name" -o tsv
foreach ($name in $keys.Keys) {
    $state = if ($existing -contains $name) { "already set" } else { "not set" }
    $secure = Read-Host "$($keys[$name]) [$state]" -AsSecureString
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    $value = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
    if ([string]::IsNullOrWhiteSpace($value)) { Write-Host "  skipped"; continue }

    $tmp = [IO.Path]::GetTempFileName()
    try {
        [IO.File]::WriteAllText($tmp, $value.Trim(), (New-Object Text.UTF8Encoding $false))
        az keyvault secret set --vault-name $vault -n $name --file $tmp --encoding utf-8 -o none
        if ($LASTEXITCODE -ne 0) { throw "Saving $name failed." }
        Write-Host "  saved"
    }
    finally {
        Remove-Item $tmp -Force -ErrorAction SilentlyContinue
        $value = $null
    }
}
Write-Host ""
Write-Host "Done. Tell Claude, and it will hand the keys to the app."
