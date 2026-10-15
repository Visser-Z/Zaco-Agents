$ErrorActionPreference = "Stop"

$project = "bixkmdvcxbwicccckodt"
$region = "eu-west-2"
$hosts = @("aws-0-$region.pooler.supabase.com", "aws-1-$region.pooler.supabase.com")

$outDir = Join-Path $env:USERPROFILE "Documents\Zacon-backups"
New-Item -ItemType Directory -Force -Path $outDir | Out-Null
$stamp = Get-Date -Format "yyyy-MM-dd_HHmm"
$file = "zaco-full-$stamp.dump"
$target = Join-Path $outDir $file

$local = Get-Command pg_dump -ErrorAction SilentlyContinue
if (-not $local) {
    $found = Get-ChildItem "C:\Program Files\PostgreSQL\*\bin\pg_dump.exe" -ErrorAction SilentlyContinue |
        Sort-Object FullName -Descending | Select-Object -First 1
    if ($found) { $local = $found.FullName }
}
if (-not $local) {
    docker version --format "{{.Server.Version}}" *> $null
    if ($LASTEXITCODE -ne 0) {
        throw "Neither pg_dump nor a running Docker engine was found. Start Docker Desktop and wait for 'Engine running', then try again."
    }
}

$secure = Read-Host "Database password" -AsSecureString
$bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
$env:PGPASSWORD = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
[Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)

$done = $false
try {
    foreach ($h in $hosts) {
        $uri = "postgresql://postgres.${project}@${h}:5432/postgres?sslmode=require"
        Write-Host "Trying $h ..."
        if ($local) {
            & $local $uri --format=custom --no-owner --no-privileges --file $target
        } else {
            docker run --rm -e PGPASSWORD -v "${outDir}:/backup" postgres:17 `
                pg_dump $uri --format=custom --no-owner --no-privileges --file "/backup/$file"
        }
        if ($LASTEXITCODE -eq 0) { $done = $true; break }
    }
}
finally {
    Remove-Item Env:PGPASSWORD -ErrorAction SilentlyContinue
}

if (-not $done) {
    Remove-Item $target -ErrorAction SilentlyContinue
    throw "The backup did not complete. If the message above says 'password authentication failed', the password is wrong; reset it in Supabase (Project Settings, Database) and run this again."
}

$size = [math]::Round((Get-Item $target).Length / 1MB, 2)
Write-Host ""
Write-Host "Backup done: $target ($size MB)"
Write-Host "Copy it somewhere off this laptop too (OneDrive, an external drive)."
