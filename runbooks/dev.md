# DEV

Profile `DEV`, target `dev`, catalog `dev_proj`. Run from the repository root in PowerShell.

## First deployment

1. Plan, then deploy (expected: 5 creates, no delete):

```powershell
Remove-Item Env:DATABRICKS_TOKEN -ErrorAction SilentlyContinue
databricks bundle plan -t dev --profile DEV
.\utils\deploy\deploy_latlang.ps1 -AppEnv dev -Infra
```

2. LibreOffice archive:

```powershell
databricks fs cp .\build\libreoffice\libreoffice-25.8.7-linux-x64.tar.gz dbfs:/Volumes/dev_proj/latlang/binaries/libreoffice-25.8.7-linux-x64.tar.gz --overwrite --profile DEV
```

3. Grants of the app's service principal. Print the statements, paste them in the SQL editor of the DEV workspace (needs
   `MANAGE` on `dev_proj`):

```powershell
$sp = (databricks apps get latlang --profile DEV -o json | ConvertFrom-Json).service_principal_client_id
$cat = 'dev_proj'
@"
GRANT USE CATALOG ON CATALOG $cat TO ``$sp``;
GRANT USE SCHEMA ON SCHEMA $cat.latlang TO ``$sp``;
GRANT READ VOLUME ON VOLUME $cat.latlang.binaries TO ``$sp``;
GRANT READ VOLUME, WRITE VOLUME ON VOLUME $cat.latlang.documents TO ``$sp``;
"@
```

Check:

```powershell
databricks grants get volume dev_proj.latlang.documents --profile DEV -o json | Select-String $sp
databricks grants get volume dev_proj.latlang.binaries --profile DEV -o json | Select-String $sp
```

4. Postgres role of the app, then restart:

```powershell
.\utils\deploy\ensure_app_access.ps1 -AppEnv dev
databricks apps stop latlang --profile DEV
databricks apps start latlang --profile DEV
```

5. Checks (both URLs in a browser: `ready`, then `"available": true`):

```powershell
$url = (databricks apps get latlang --profile DEV -o json | ConvertFrom-Json).url
"$url/api/health/ready"
"$url/api/translate/render-engine"
databricks apps logs latlang --profile DEV | Select-String -Pattern "Traceback|ERROR" -Context 0,3
```
