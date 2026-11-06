# UAT

Profile `UAT`, target `latlang-uat`, catalog `uat_proj`. The app runs; the bundle shows it as "not deployed".

## Before any `-Infra` deployment

```powershell
Remove-Item Env:DATABRICKS_TOKEN -ErrorAction SilentlyContinue
databricks apps get latlang --profile UAT -o json | ConvertFrom-Json | Select-Object name, app_status, service_principal_client_id
databricks bundle plan -t latlang-uat --profile UAT
```

Expected: no `create` for `apps.latlang`, no `delete`. The job `latlang-grant-volume-access-latlang-uat` is no longer
declared: the plan shows it as a `delete`, which is intended.

## Grants of the app's service principal

```powershell
$sp = (databricks apps get latlang --profile UAT -o json | ConvertFrom-Json).service_principal_client_id
$cat = 'uat_proj'
@"
GRANT USE CATALOG ON CATALOG $cat TO ``$sp``;
GRANT USE SCHEMA ON SCHEMA $cat.latlang TO ``$sp``;
GRANT READ VOLUME ON VOLUME $cat.latlang.binaries TO ``$sp``;
GRANT READ VOLUME, WRITE VOLUME ON VOLUME $cat.latlang.documents TO ``$sp``;
"@
```
