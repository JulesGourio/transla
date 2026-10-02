# Commandes Databricks à lancer (Jules)

Claude n'a pas accès à Databricks : toutes les étapes Databricks sont ici,
prêtes à copier-coller. PowerShell, depuis `C:\Users\jugou\Desktop\Travail\transla`.

## Restaurer `uat_proj.latlang` après sa suppression (2026-10-02)

Contenu du volume perdu : documents des jobs de test (non restaurés, sans
importance) et l'archive LibreOffice, reconstruite à l'identique en local :

- Fichier : `build\libreoffice\libreoffice-25.8.7-linux-x64-v1.tar.gz` (317 Mo, gitignoré)
- Source : TDF `LibreOffice_25.8.7_Linux_x86-64_deb.tar.gz`, SHA-256 vérifié
  `7f4d7b2e36921eec5122c655249a24cc88935ee357e8261fd3bccd15aa1f7b9f`
- SHA-256 de l'archive : `E1836FA61766FFAF9A0A89FB105985C384FB40A70A1CE70CF4DC006B50DD9BEF`
- Contenu vérifié : `program/soffice`, filtres Word + PDF, polices (Liberation,
  Carlito, Caladea, DejaVu, Noto, Amiri/Noto Arabic), les librairies système
  Ubuntu 22.04 (toutes les dépendances hors plugins GUI Qt/GTK/Java).
- Trop gros pour le repo : GitHub refuse les fichiers > 100 Mo, et Databricks Apps
  refuse les gros fichiers dans le code source de l'app. Il doit aller dans le volume.

### 0. Session

```powershell
Remove-Item Env:DATABRICKS_TOKEN -ErrorAction SilentlyContinue
```

### 1. Recréer schéma + volume et déployer le code actuel

```powershell
.\utils\deploy\deploy_latlang.ps1 -AppEnv uat -Infra
```

Si l'étape `bundle deploy` échoue sur le schéma ou le volume, les créer à la main
puis relancer la commande ci-dessus :

```powershell
databricks schemas create latlang uat_proj --profile UAT
databricks volumes create uat_proj latlang latlang MANAGED --profile UAT
```

### 2. Uploader l'archive LibreOffice

```powershell
databricks fs cp .\build\libreoffice\libreoffice-25.8.7-linux-x64-v1.tar.gz dbfs:/Volumes/uat_proj/latlang/latlang/libreoffice/libreoffice-25.8.7-linux-x64-v1.tar.gz --overwrite --profile UAT
```

### 3. Redonner à l'app les droits sur le volume (perdus avec le schéma)

```powershell
$sp = (databricks apps get latlang --profile UAT -o json | ConvertFrom-Json).service_principal_client_id
databricks bundle run grant_volume_access --target latlang-uat --profile UAT --params "service_principal=$sp"
```

### 4. Vérifier

```powershell
databricks fs ls dbfs:/Volumes/uat_proj/latlang/latlang/libreoffice --profile UAT
(databricks apps get latlang --profile UAT -o json | ConvertFrom-Json).url
```

Ouvrir dans le navigateur `<url affichée>/api/translate/render-engine`. Le premier
appel télécharge et extrait l'archive (environ 1 min) et doit renvoyer
`"available": true` avec `LibreOffice 25.8.7...`.
