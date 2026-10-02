# Commandes Databricks à lancer (Jules)

Claude n'a pas accès à Databricks : toutes les étapes Databricks sont ici,
prêtes à copier-coller. PowerShell, depuis `C:\Users\jugou\Desktop\Travail\transla`.

## Organisation Unity Catalog (une par environnement)

| Objet | Contenu | Droits de l'app |
|---|---|---|
| schéma `uat_proj.latlang` | tout ce qui suit | USE SCHEMA |
| volume `documents` | fichiers des jobs : `<job_id>/input_*.docx`, `output.docx`, PDF de preview | READ + WRITE |
| volume `binaries` | `libreoffice-25.8.7-linux-x64.tar.gz` | READ |
| tables Delta `errors`, `translation_*`, `glossary_*`, `dnt_rules` | copie nocturne de la base Lakebase `latlang` (job `D_0_latlang-lakebase-copy-latlang-uat`, 2 h) | aucun |

Schéma et volumes sont déclarés dans `databricks.yml`, et le job de copie dans
`resources/lakebase_copy.yml`. Les binaires et les documents sont séparés : vider
`documents` ne touche plus jamais LibreOffice.

## Restaurer `uat_proj.latlang` (supprimé le 2026-10-02)

L'archive LibreOffice est reconstruite localement dans
`build\libreoffice\libreoffice-25.8.7-linux-x64.tar.gz` (317 Mo, gitignoré) :

- la source est le `LibreOffice_25.8.7_Linux_x86-64_deb.tar.gz` officiel de TDF,
  SHA-256 vérifié (`7f4d7b2e36921eec5122c655249a24cc88935ee357e8261fd3bccd15aa1f7b9f`) ;
- le contenu a été vérifié : `program/soffice`, filtres Word et PDF, polices
  (Liberation, Carlito, Caladea, DejaVu, Noto, Amiri/Noto Arabic), et toutes les
  librairies système Ubuntu 22.04 nécessaires (seuls manquent les plugins GUI
  Qt/GTK/Java, jamais chargés en headless) ;
- pour la reconstruire : `uv run --no-project --with zstandard utils/soffice_packaging/package_libreoffice.py <bundle TDF> <sortie>`.

Les documents des anciens jobs de test sont perdus. Leurs previews et rebuilds
échoueront, alors que les nouveaux jobs fonctionnent.

### 0. Session

```powershell
Remove-Item Env:DATABRICKS_TOKEN -ErrorAction SilentlyContinue
```

### 1. Créer schéma, volumes et jobs, puis déployer le code

```powershell
.\utils\deploy\deploy_latlang.ps1 -AppEnv uat -Infra
```

### 2. Uploader l'archive LibreOffice

```powershell
databricks fs cp .\build\libreoffice\libreoffice-25.8.7-linux-x64.tar.gz dbfs:/Volumes/uat_proj/latlang/binaries/libreoffice-25.8.7-linux-x64.tar.gz --overwrite --profile UAT
```

### 3. Donner à l'app ses droits sur les volumes

Par défaut : READ sur `binaries`, READ + WRITE sur `documents`.

```powershell
$sp = (databricks apps get latlang --profile UAT -o json | ConvertFrom-Json).service_principal_client_id
databricks bundle run grant_volume_access --target latlang-uat --profile UAT --params "service_principal=$sp"
```

### 4. Préparer le job de copie Lakebase → UC

Il tourne sous `job-runner-sa-uat` (`3e5cd4e5-…`).

```powershell
databricks grants update schema uat_proj.latlang --json "@utils/databricks_ops/grants/lakebase_copy_schema.json" --profile UAT
databricks postgres list-roles projects/qualibot/branches/production --profile UAT -o json | Select-String 3e5cd4e5
```

Si la deuxième commande n'affiche rien, le service principal n'a pas encore de rôle
Postgres. Le créer :

```powershell
databricks postgres create-role projects/qualibot/branches/production --role-id 3e5cd4e5-f765-4760-b974-ee7715258b39 --json "@utils/databricks_ops/grants/lakebase_copy_pg_role.json" --profile UAT
```

### 5. Premier lancement de la copie

Le job tourne ensuite tout seul chaque nuit à 2 h.

```powershell
databricks bundle run lakebase_copy --target latlang-uat --profile UAT
databricks tables list uat_proj latlang --profile UAT -o json | ConvertFrom-Json | Select-Object name
```

### 6. Vérifier LibreOffice

```powershell
databricks fs ls dbfs:/Volumes/uat_proj/latlang/binaries --profile UAT
(databricks apps get latlang --profile UAT -o json | ConvertFrom-Json).url
```

Ouvrir `<url affichée>/api/translate/render-engine` dans le navigateur. Le premier
appel télécharge et extrait l'archive (environ 1 min) et doit renvoyer
`"available": true`.

## Ménage optionnel : restes de l'ancienne cible `latlang-uat-test`

Cette cible n'existe plus dans le bundle. Pour vérifier ce qui reste :

```powershell
databricks apps get latlang-uat-test --profile UAT
databricks postgres list-databases projects/qualibot/branches/production --profile UAT -o json | Select-String latlang
```

Si l'app existe encore : `databricks apps delete latlang-uat-test --profile UAT`.
La base Lakebase `latlang_test` contient le glossaire et les règles DNT historiques
(la base `latlang` est partie vide). Ne pas la supprimer avant de décider s'il faut
copier ces données dans `latlang`.
