param(
    [string]$AppEnv = 'dev'   # dev | uat
)

# Idempotent first-deployment setup of the app's Postgres role on the shared Lakebase project.
# The bundle does not own that project, so the role of the app's service principal is created here.

$ErrorActionPreference = 'Stop'

$Profiles = @{ dev = 'DEV'; uat = 'UAT' }
if (-not $Profiles.ContainsKey($AppEnv)) {
    Write-Host "Unknown environment '$AppEnv'. Use: dev | uat" -ForegroundColor Red
    exit 1
}
$Profile = $Profiles[$AppEnv]
$Branch = 'projects/qualibot/branches/production'

$sp = (databricks apps get latlang --profile $Profile -o json | ConvertFrom-Json).service_principal_client_id
if (-not $sp) { throw "App 'latlang' not found on profile $Profile: deploy the bundle first." }

$existing = databricks postgres list-roles $Branch --profile $Profile -o json | Select-String $sp
if ($existing) {
    Write-Host "Postgres role of $sp already exists on $Branch." -ForegroundColor DarkGray
    exit 0
}

$role = '{"spec": {"identity_type": "SERVICE_PRINCIPAL", "postgres_role": "' + $sp + '", "auth_method": "LAKEBASE_OAUTH_V1", "membership_roles": ["DATABRICKS_SUPERUSER"], "attributes": {"createdb": true}}}'
$file = Join-Path $env:TEMP 'latlang_app_role.json'
Set-Content -Path $file -Value $role -Encoding ascii
databricks postgres create-role $Branch --role-id $sp --json "@$file" --profile $Profile
if ($LASTEXITCODE -ne 0) { throw "create-role failed (exit $LASTEXITCODE)" }
Write-Host "Role created. Restart the app: databricks apps stop latlang --profile $Profile ; databricks apps start latlang --profile $Profile" -ForegroundColor Green
