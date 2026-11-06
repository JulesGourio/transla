# LatLang : passation

Complète le [README](README.md), qui décrit le code. Ici : ce qu'il faut savoir pour **opérer, déployer et faire évoluer**
l'application depuis un poste **Windows** (PowerShell, navigateur Databricks). Les noms de fichiers sont ceux du dépôt.

**Sommaire** : [1 Vue d'ensemble](#1-vue-densemble) · [2 Environnements](#2-environnements) · [3 Première mise en place](#3-première-mise-en-place-dun-environnement) · [4 Déployer une modification](#4-déployer-une-modification) · [5 Vérifier après un déploiement](#5-vérifier-après-un-déploiement) · [6 Jobs](#6-jobs-databricks) · [7 Exploitation](#7-exploitation-courante) · [8 Évolutions](#8-faire-évoluer-lapplication) · [9 Pannes](#9-pannes-connues-et-diagnostic) · [10 Pièges](#10-pièges-de-déploiement) · [11 Limites et points ouverts](#11-limites-connues-et-points-ouverts)

## 1. Vue d'ensemble

LatLang traduit des documents Word (`.docx`) de documentation aéronautique, souvent bilingues, en gardant la mise en page.
Le déroulé complet d'un traitement est dans [docs/pipeline.md](docs/pipeline.md).

- **Application** : Databricks App `latlang`. Backend FastAPI ([server/](server/README.md)) et frontend React
  ([client/](client/README.md)), servis par le même processus.
- **Données** : base Lakebase `latlang` (tâches, segments, glossaire, retours utilisateurs), décrite dans
  [docs/lakebase_schema.md](docs/lakebase_schema.md). Le projet Lakebase (`qualibot`) est partagé avec d'autres
  applications pour réutiliser son calcul ; seule la base `latlang` est à cette application.
- **Fichiers** : volume `documents` (originaux, documents traduits, PDF d'aperçu) et volume `binaries` (archive LibreOffice).
- **Modèle** : un endpoint Model Serving (`TRANSLATE_ENDPOINT`, `databricks-gpt-5-6-luna` par défaut) pour la traduction et
  l'OCR des images.
- **Aperçu PDF** : LibreOffice portable, extrait de l'archive du volume `binaries` au premier usage.
- **Droits d'accès à l'application** : posés sur la ressource Databricks App (groupes dans [databricks.yml](databricks.yml)),
  pas dans le code. Chaque tâche appartient à l'utilisateur qui l'a lancée ; le glossaire est commun.

Il n'y a pas de file d'attente : les étapes tournent dans le processus de l'application (voir
[docs/pipeline.md](docs/pipeline.md#background-work)). Un redémarrage de l'application interrompt les étapes en cours : les
tâches concernées passent en `failed` (`OrphanedJob`) et se relancent depuis le bouton de l'interface. Une traduction en
cours reprend là où elle s'était arrêtée.

## 2. Environnements

| Env | Cible du bundle | Workspace | Catalogue.schéma | Profil CLI | Service principal des jobs |
|---|---|---|---|---|---|
| DEV | `dev` | `dbc-c623749d-731b` | `dev_proj.latlang` | `DEV` | non défini (pas de job en DEV) |
| UAT | `latlang-uat` | `dbc-3a17bfce-9e88` | `uat_proj.latlang` | `UAT` | `3e5cd4e5-f765-4760-b974-ee7715258b39` (job-runner-sa-uat) |
| PROD | `latlang-prod` | **non renseigné** dans `databricks.yml` | `prod_proj.latlang` | `qualibot-prod` | non défini |

- Profils CLI : fichier `%USERPROFILE%\.databrickscfg`.
- Groupes de la ressource app : CoreAdmin et CoreDev (`CAN_MANAGE`), groupe d'utilisateurs finaux (`CAN_USE`, variable
  `end_users_group`). Le groupe prévu pour cette application, `Role-Project-LEAP-End-users-LatLang`, n'existe pas encore :
  la variable pointe sur le groupe d'une application voisine en attendant (voir [§11](#11-limites-connues-et-points-ouverts)).
- Ne jamais lancer `bundle deploy` ni `bundle destroy` sur `latlang-uat` ou `latlang-prod` sans l'accord du propriétaire.
- Sur un poste où l'extension Databricks de l'éditeur est active, la variable `DATABRICKS_TOKEN` prend le pas sur le
  profil. Avant toute commande `databricks` :

```powershell
Remove-Item Env:DATABRICKS_TOKEN -ErrorAction SilentlyContinue
```

## 3. Première mise en place d'un environnement

À faire une fois par environnement (UAT donné en exemple ; pour PROD, renseigner d'abord l'hôte du workspace dans
`databricks.yml`, cible `latlang-prod`, et un profil CLI). Depuis `C:\Users\jugou\Desktop\Travail\transla`.

### 3.1 Construire l'archive LibreOffice (une fois, sur le poste)

L'archive fait environ 317 Mo : elle n'est pas dans Git et se reconstruit. Télécharger depuis la Document Foundation le
paquet « LibreOffice 25.8.7, Linux x86-64 (deb) », vérifier son empreinte SHA-256 publiée
(`7f4d7b2e36921eec5122c655249a24cc88935ee357e8261fd3bccd15aa1f7b9f` pour la version 25.8.7), puis :

```powershell
uv run --no-project --with zstandard utils/soffice_packaging/package_libreoffice.py "C:\chemin\LibreOffice_25.8.7_Linux_x86-64_deb.tar.gz" ".\build\libreoffice\libreoffice-25.8.7-linux-x64.tar.gz"
```

Détail du contenu et du fonctionnement : [utils/soffice_packaging](utils/soffice_packaging/README.md).

### 3.2 Créer le schéma, les volumes, les jobs et déployer le code

```powershell
Remove-Item Env:DATABRICKS_TOKEN -ErrorAction SilentlyContinue
.\utils\deploy\deploy_latlang.ps1 -AppEnv uat -Infra
```

Le premier déploiement d'une cible demande `MANAGE` sur le catalogue. Il crée le schéma, les volumes `documents` et
`binaries`, l'application, ses permissions et les jobs.

### 3.3 Envoyer l'archive dans le volume `binaries`

```powershell
databricks fs cp .\build\libreoffice\libreoffice-25.8.7-linux-x64.tar.gz dbfs:/Volumes/uat_proj/latlang/binaries/libreoffice-25.8.7-linux-x64.tar.gz --overwrite --profile UAT
```

Le nom du fichier doit être celui de `SOFFICE_ARCHIVE_VOLUME_PATH` dans [utils/deploy/target_env.json](utils/deploy/target_env.json).

### 3.4 Donner à l'application ses droits sur les volumes

L'application s'exécute avec son propre service principal. Il lui faut `READ` sur `binaries` et `READ`+`WRITE` sur
`documents` (le job donne aussi `USE CATALOG` et `USE SCHEMA`) :

```powershell
$sp = (databricks apps get latlang --profile UAT -o json | ConvertFrom-Json).service_principal_client_id
databricks bundle run grant_volume_access --target latlang-uat --profile UAT --params "service_principal=$sp"
```

Utiliser bien `service_principal_client_id` (c'est aussi le champ `id` de `apps get`), et non l'identifiant
`oauth2_app_client_id`, qui désigne une autre intégration.

### 3.5 Créer le rôle Postgres de l'application

Sans ce rôle, l'application échoue au démarrage avec `password authentication failed`. `createdb` doit être posé dès la
création : `DATABRICKS_SUPERUSER` seul ne le donne pas, et l'application a besoin de créer sa base `latlang` au premier
démarrage.

```powershell
$sp = (databricks apps get latlang --profile UAT -o json | ConvertFrom-Json).service_principal_client_id
$role = '{"spec": {"identity_type": "SERVICE_PRINCIPAL", "postgres_role": "' + $sp + '", "auth_method": "LAKEBASE_OAUTH_V1", "membership_roles": ["DATABRICKS_SUPERUSER"], "attributes": {"createdb": true}}}'
Set-Content -Path "$env:TEMP\latlang_app_role.json" -Value $role -Encoding ascii
databricks postgres create-role projects/qualibot/branches/production --role-id $sp --json "@$env:TEMP\latlang_app_role.json" --profile UAT
```

Redémarrer ensuite l'application : `databricks apps stop latlang --profile UAT` puis `databricks apps start latlang --profile UAT`.
Au démarrage elle crée la base `latlang` et toutes les tables.

### 3.6 Préparer le job de copie Lakebase vers Unity Catalog

Le job tourne sous `job-runner-sa-uat`. Il lui faut les droits sur le schéma et un rôle Postgres :

```powershell
databricks grants update schema uat_proj.latlang --json "@utils/databricks_ops/grants/lakebase_copy_schema.json" --profile UAT
databricks postgres list-roles projects/qualibot/branches/production --profile UAT -o json | Select-String 3e5cd4e5
```

Si la seconde commande n'affiche rien, créer le rôle :

```powershell
databricks postgres create-role projects/qualibot/branches/production --role-id 3e5cd4e5-f765-4760-b974-ee7715258b39 --json "@utils/databricks_ops/grants/lakebase_copy_pg_role.json" --profile UAT
```

Premier lancement, puis vérification (ensuite le job tourne seul chaque nuit à 02:00) :

```powershell
databricks bundle run lakebase_copy --target latlang-uat --profile UAT
databricks tables list uat_proj latlang --profile UAT -o json | ConvertFrom-Json | Select-Object name
```

### 3.7 Vérifier que LibreOffice démarre

```powershell
databricks fs ls dbfs:/Volumes/uat_proj/latlang/binaries --profile UAT
(databricks apps get latlang --profile UAT -o json | ConvertFrom-Json).url
```

Ouvrir `<url affichée>/api/translate/render-engine` dans le navigateur. Le premier appel télécharge et extrait l'archive
(environ une minute) et doit répondre `"available": true`.

## 4. Déployer une modification

Une modification de code Python seul :

```powershell
Remove-Item Env:DATABRICKS_TOKEN -ErrorAction SilentlyContinue
.\utils\deploy\deploy_latlang.ps1 -AppEnv uat -SkipBuild
```

Une modification du frontend : même commande **sans** `-SkipBuild` (le script lance `bun install` et `bun run build`).

Une modification de `databricks.yml`, de `resources/` (jobs, permissions, volumes) : ajouter `-Infra`.

Un nouveau réglage propre à un environnement : l'ajouter dans `utils/deploy/target_env.json` seulement, puis redéployer.

Voir les erreurs après un déploiement :

```powershell
databricks apps logs latlang --profile UAT | Select-String -Pattern "Traceback|ERROR" -Context 0,3
```

## 5. Vérifier après un déploiement

À la main, dans l'application :

1. Envoyer un `.docx` qui contient un formulaire (contrôle de contenu dans une cellule de tableau) et une zone de texte :
   leur texte doit apparaître dans le tableau des segments et être traduit.
2. Envoyer un fichier `.doc` renommé en `.docx` : message immédiat « not a valid .docx », aucune tâche créée.
3. Traduire, ouvrir l'aperçu, modifier un segment, cliquer « Rebuild » : l'aperçu montre la modification tout de suite.
4. Filtre de pages : `5-3` répond « pages must look like… » ; `1-3` traduit aussi les en-têtes et pieds de page.
5. Une tâche complète va jusqu'à `done` sans erreur dans `databricks apps logs latlang --profile UAT`.

Règles DNT déjà en base : le contrôle (règle qui correspond à tout ou à rien) s'applique à l'ajout seulement. Pour repérer
une règle ancienne dangereuse, dans l'éditeur SQL de la base `latlang` (hôte donné par
`databricks postgres list-endpoints projects/qualibot/branches/production --profile UAT -o json`) :

```sql
SELECT id, pattern, match_mode FROM dnt_rules
WHERE trim(both '*?[]' from pattern) = '' OR (match_mode = 'regex' AND pattern IN ('.*', '.+', '^.*$'));
```

Supprimer la règle trouvée depuis le panneau Glossaire de l'application.

## 6. Jobs Databricks

| Job | Rôle | Planning | Où |
|---|---|---|---|
| `D_0_latlang-lakebase-copy-latlang-uat` | Copie chaque table de la base Lakebase `latlang` vers des tables Delta du schéma | tous les jours 02:00 (Europe/Paris) | UAT |
| `WD_0_latlang_start_app_latlang-uat` | Démarre l'application | lundi au vendredi 08:00 (Europe/Paris) | UAT |
| `WD_1_latlang_stop_app_latlang-uat` | Arrête l'application | lundi au vendredi 19:00 (Europe/Paris) | UAT |
| `latlang-grant-volume-access-latlang-uat` | Donne au service principal de l'application ses droits sur les volumes | manuel | UAT |

L'application est donc arrêtée la nuit et le week-end en UAT. Pour la démarrer à la main :
`databricks apps start latlang --profile UAT`.

Description des notebooks : [utils/databricks_ops](utils/databricks_ops/README.md). Déclarations :
[resources/](resources/README.md) et [databricks.yml](databricks.yml).

## 7. Exploitation courante

- **Coût** : chaque tâche affiche son nombre d'appels, ses jetons et son coût estimé. Les prix par endpoint sont dans
  `server/services/llm.py` ; un endpoint absent de la liste est facturé comme un grand modèle (coût surestimé).
- **Changer de modèle** : modifier `TRANSLATE_ENDPOINT` dans `app.yaml`, ajouter le prix de l'endpoint dans `llm.py`,
  déployer. L'endpoint doit accepter des réponses JSON ; un endpoint qui refuse `temperature` est géré.
- **Tâches bloquées** : une tâche qui n'a plus de signe de vie depuis 2 minutes est marquée « stalled » dans l'interface ;
  au redémarrage de l'application elle passe en `failed`. Le bouton « Restart from scratch » la refait entièrement.
- **Sauvegarde** : la copie nocturne Delta contient l'état de la base de la veille. Pour sauvegarder avant une opération
  risquée, lancer `databricks bundle run lakebase_copy --target latlang-uat --profile UAT`.
- **Glossaire** : se gère dans l'application (bouton Glossary). Les termes proposés après la validation d'une traduction
  arrivent dans l'onglet des candidats à relire.
- **Espace disque des volumes** : les fichiers d'une tâche sont dans `documents/<id>/`. Aucun nettoyage automatique n'existe ;
  supprimer un dossier le rend impossible à reconstruire ou à prévisualiser.

## 8. Faire évoluer l'application

### Ajouter une langue

Toucher dans l'ordre : `langdetect.py` (profil, `SUPPORTED_LANGS`), `routers/translate.py` (noms de langues, alias de
réponses, modèles Pydantic du glossaire, requêtes SQL), `glossary_verify.py` (noms), `lakebase.py` (colonnes du glossaire,
avec `ALTER TABLE … ADD COLUMN IF NOT EXISTS`), puis le client (`TranslateView.tsx`, `SegmentsPanel.tsx`, `GlossaryPanel.tsx`,
`Flag.tsx`). Ajouter des tests dans `tests/test_translate.py`.

### Mettre à jour LibreOffice

Reconstruire l'archive avec la nouvelle version ([§3.1](#31-construire-larchive-libreoffice-une-fois-sur-le-poste)), la
téléverser **sous un nouveau nom de fichier**, changer `SOFFICE_ARCHIVE_VOLUME_PATH` dans `utils/deploy/target_env.json`,
redéployer. Le dossier d'extraction porte le nom de l'archive : un nouveau nom évite de réutiliser l'ancienne version.

### Modifier le comportement d'une étape

Chaque étape est décrite dans [docs/pipeline.md](docs/pipeline.md) et son code dans
[server/services/translation](server/services/translation/README.md). Lancer `pytest` avant de déployer.

## 9. Pannes connues et diagnostic

| Symptôme | Cause probable | Action |
|---|---|---|
| L'application démarre mais `/` répond `{"detail":"Not Found"}`, `/api/*` répond | `client/out` n'a pas été envoyé | Redéployer **sans** `-SkipBuild` |
| `password authentication failed` dans les logs au démarrage | Pas de rôle Postgres pour le service principal de l'application | [§3.5](#35-créer-le-rôle-postgres-de-lapplication) |
| `permission denied to create database` dans les logs | Le rôle existe sans `createdb` | Supprimer le rôle (`databricks postgres delete-role`) et le recréer avec `createdb` |
| `User does not have READ VOLUME on Volume …` | Droits du volume manquants (ou posés avant la dernière version du job) | Relancer le job de droits ([§3.4](#34-donner-à-lapplication-ses-droits-sur-les-volumes)) ; une tâche créée avant la correction garde un chemin d'original vide et doit être recréée |
| Preview : « Exact PDF rendering unavailable » (501) | LibreOffice indisponible | Ouvrir `/api/translate/render-engine` pour lire la cause (archive absente, mauvais nom, droits) |
| Preview : « PDF conversion failed » (502) | LibreOffice a échoué sur ce document | Lire les logs ; tester le document dans Word |
| Une tâche reste en `failed` avec `OrphanedJob` | L'application a redémarré pendant l'étape | Relancer l'étape depuis l'interface |
| Un nom de base corrompu (`latlang\r`) | `target_config.env` écrit avec des fins de ligne Windows | Le script les écrit en LF ; ne pas éditer ce fichier à la main |
| Le job de copie échoue | Rôle Postgres ou droits du schéma manquants ([§3.6](#36-préparer-le-job-de-copie-lakebase-vers-unity-catalog)) | Refaire §3.6 |
| Document sans aucun segment, tâche « done » | Le fichier ne contient que des images (PDF scanné converti en Word) | Voir [§11](#11-limites-connues-et-points-ouverts) |

## 10. Pièges de déploiement

- **`requirements.txt`** doit exister à la racine : Databricks Apps installe depuis ce fichier, pas depuis
  `pyproject.toml`. Ajouter une dépendance aux deux.
- **Ne pas déclarer `resources` (`uc_securable`) sur l'application dans `databricks.yml`** : Terraform tenterait de donner
  `USE CATALOG` à tous les utilisateurs du compte et échouerait sans `MANAGE` sur le catalogue. Les droits de l'application
  passent par le job de droits.
- **Ne pas déclarer dans ce bundle une ressource déjà gérée par un autre bundle** (schéma, projet Lakebase) : le déploiement
  échoue avec « already exists ». Le projet Lakebase n'est référencé que par son identifiant.
- **`client/out` est ignoré par Git** : `databricks.yml` le force dans la synchronisation (`sync.include`), le script de
  déploiement aussi. Sans lui, l'application n'a pas de page d'accueil.
- **`LAKEBASE_DATABASE`** n'a pas de valeur par défaut sûre par environnement : elle est posée par
  `target_env.json` pour chaque cible. Un nouveau réglage par environnement s'ajoute là, en vérifiant la base visée.
- **Fins de ligne de `target_config.env`** : toujours LF (le script s'en charge).
- **Un déploiement vers une application arrêtée est refusé** : le script la démarre avant.
- **Supprimer et recréer des ressources efface les droits associés** : refaire les droits du §3.4 et §3.6.

## 11. Limites connues et points ouverts

À reprendre par la personne qui reçoit le projet.

**À créer ou décider**

- Le groupe `Role-Project-LEAP-End-users-LatLang` n'existe pas : le créer (demande à l'équipe annuaire) ou en choisir un
  autre, puis changer `end_users_group` dans `databricks.yml` et redéployer avec `-Infra`.
- `latlang-prod` n'a jamais été déployé : l'hôte du workspace est vide dans `databricks.yml`, le service principal des jobs
  et le profil CLI sont à définir, puis suivre le [§3](#3-première-mise-en-place-dun-environnement).
- Le glossaire et les règles DNT historiques sont dans la base `latlang_test` d'une ancienne cible (si elle existe encore).
  Les copier dans `latlang` avec le notebook `utils/databricks_ops/lakebase_sync/migrate_translation_data_from_doccompare.py`
  (il refuse `latlang` comme cible tant que le paramètre n'est pas changé exprès) ou repartir d'un glossaire vide. Vérifier
  d'abord ce qui reste :

```powershell
databricks apps get latlang-uat-test --profile UAT
databricks postgres list-databases projects/qualibot/branches/production --profile UAT -o json | Select-String latlang
```

**Non vérifié de bout en bout** (le code et les tests existent, pas de passage sur un vrai document déployé à ce jour)

- Le portugais et l'arabe sur un vrai document. Pour l'arabe : le rendu LibreOffice (écriture de droite à gauche, formes
  des lettres) n'a pas été contrôlé, et la vue côte à côte du panneau de relecture n'a pas de mise en page de droite à gauche.
- La traduction du texte des images avec un vrai appel au modèle de vision.
- Le filtre de pages avec LibreOffice réel.

**Limites assumées**

- Seuls les `.docx` sont acceptés. Un PDF se convertit d'abord dans Word (« Fichier > Enregistrer sous > .docx »), qui
  reconstruit les tableaux et la mise en page bien mieux que ne le ferait un traitement direct du PDF.
- Le texte des images n'est jamais réécrit dans l'image ni dans le document : la traduction n'apparaît que dans le panneau
  des segments (catégorie « Images »).
- Un document dont tout le contenu est une image (page scannée convertie en Word) n'a aucun segment de texte : la tâche se
  termine sans rien traduire, sans message. Utiliser l'option de traduction des images.
- Un redémarrage interrompt le lot de traduction en cours ; l'étape reprend ensuite sans refaire ce qui est déjà enregistré.
- Les prix de l'estimation de coût sont saisis à la main dans `server/services/llm.py`.
- Les scripts de `utils/glossary/` ont servi à une importation ponctuelle depuis un autre corpus de documents ; ils
  supposent des ressources qui ne sont pas dans ce dépôt ([utils/glossary](utils/glossary/README.md)).

## Documentation

| Sujet | Où |
|---|---|
| Vue d'ensemble, configuration, développement local | [README.md](README.md) |
| Déroulé d'une tâche, états, règles de traduction | [docs/pipeline.md](docs/pipeline.md) |
| Tables de la base | [docs/lakebase_schema.md](docs/lakebase_schema.md) |
| Routes | [server/routers](server/routers/README.md) |
| Moteur de traduction | [server/services/translation](server/services/translation/README.md) |
| Écrans | [client/src/components/translate](client/src/components/translate/README.md) |
| Déploiement, jobs, droits | [utils](utils/README.md), [resources](resources/README.md) |
| Tests | [tests](tests/README.md) |
