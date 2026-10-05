# Audit de l'application de traduction — 2026-10-05

Branche : `audit/translation` (depuis `main`, base `432586c`). Périmètre : tout le
parcours d'un document — extraction, segmentation, appels LLM et reprises,
glossaire/DNT, contrôles qualité, reconstruction, stockage, API, panneau de
relecture. Les numéros de ligne sont ceux de `432586c` (avant corrections).

## En bref

- **20 défauts corrigés** (partie A), chacun avec un commit dédié, poussé sur
  `audit/translation` ; **14 points laissés à décider** (partie B).
- Le plus grave : **du texte pouvait rester dans la langue source sans aucun
  signalement** — segments dont l'identifiant en doublon était abandonné à
  l'insertion (A1), texte jamais extrait (cellule à contrôle de contenu, zone de
  texte VML, tableau dans une zone de texte — A2), traduction que la
  reconstruction sautait en silence (A3).
- Les corrections d'**affichage trompeur** : prévisualisation de l'ancienne
  version après « Rebuild again » (A5), fichier d'un job `failed` servi comme s'il
  était bon (A5), message d'erreur effacé (A4).
- **Vérifié** : `pytest` — 145 tests passent (64 avant l'audit, dont 1 en échec
  dès le départ, corrigé : A20). Chaque correction a un test qui échoue sans elle
  (sauf mention contraire dans la fiche).
- **Non vérifié** : rien n'a tourné contre Databricks, un vrai LLM, Lakebase
  (Postgres) ni LibreOffice (absent de cette machine) ; aucun document réel,
  uniquement des `.docx` fabriqués à la main. Les requêtes SQL modifiées sont
  testées sur un faux pool **sqlite** (`tests/fakedb.py`), pas sur Postgres.
  Aucune modification du client : `bun` n'est pas installé ici, ni `tsc`/`vite
  build` lancés (rien à reconstruire). Étapes de test dans `OPS_COMMANDS.md`.

### Outils sur cette machine

| Outil | État |
|---|---|
| Python 3.12 + `.venv` | présent, mais sans `pytest`/`python-docx` : installés avec `uv pip install` |
| `pytest` | 9.1.1 installé pour l'audit — 145 passent |
| `bun` | **absent** (le client n'a pas été reconstruit) |
| LibreOffice (`soffice`) | **absent** (aucune conversion PDF testée ; `pages.py` testé avec des PDF PyMuPDF) |
| Databricks CLI / LLM / Lakebase | pas d'accès (règle du projet) |

## A. Corrigé sur la branche

Gravité : 🔴 résultat faux, contenu perdu ou faille · 🟠 gêne réelle · 🟡 mineur.

### A1 🔴 Deux segments au même identifiant : le second n'était jamais traduit

- **Où.** `extract.py` L260-310 (`extract_drawings_in`), L347 (`nested_id`), L355-386
  (`extract_sdt`) ; `translate.py` L479 (`ON CONFLICT (job_id, seg_id) DO NOTHING`).
- **Scénario.** Deux zones de texte dans deux cellules d'un même tableau, deux
  tableaux dans un même contrôle de contenu, un contrôle de contenu imbriqué (sa
  numérotation de paragraphes repart de 0), deux sous-tableaux dans une cellule :
  mêmes `seg_id`. Reproduit sur des `.docx` fabriqués.
- **Conséquence.** À l'insertion en base, le second segment était ignoré sans
  bruit : jamais envoyé au LLM, jamais traduit, jamais signalé (la vérification
  d'intégrité compare des ensembles d'identifiants, donc ne voit rien). Le même
  `seg_id` pouvait aussi faire apparaître « Still reads as BG » sur le mauvais
  segment. L'appariement des zones de texte regroupait en plus des zones de
  cellules différentes.
- **Correction.** Les identifiants en collision reçoivent un suffixe `~N` (ceux
  qui ne collisionnaient pas sont inchangés) ; l'appariement des zones de texte
  utilise la position XML de la zone. Commit `9d5e316`.
- **Tests.** `tests/test_extract.py` (5 tests dont l'appariement).

### A2 🔴 Texte jamais extrait, donc jamais traduit

- **Où.** `extract.py` L313-352 (cellule : seuls `w:p` et `w:tbl` sont lus) et
  L260-310 (zones de texte : seulement sous `mc:AlternateContent`, paragraphes
  seulement).
- **Scénario.** (a) Un contrôle de contenu (`w:sdt`) dans une cellule de
  tableau — fréquent dans les formulaires ; (b) une zone de texte VML
  (`w:pict`) ou un dessin sans `AlternateContent` — documents anciens,
  filigranes, en-têtes ; (c) un tableau placé dans une zone de texte.
- **Conséquence.** Le texte restait dans la langue source dans le document
  livré, sans signalement ; pas même un segment « kept » dans le panneau.
- **Correction.** Les trois cas sont extraits ; pour (c) le chemin de la copie
  `mc:Fallback` est déduit du chemin relatif, et la traduction est écrite dans les
  deux copies. Commit `9d5e316` (même que A1).
- **Tests.** `test_content_control_inside_a_table_cell_is_extracted`,
  `test_vml_text_box_without_alternate_content_is_extracted`,
  `test_table_inside_a_text_box_is_extracted_and_translated_in_both_copies`.

### A3 🔴 Une traduction que la reconstruction ne pouvait pas écrire passait inaperçue

- **Où.** `rebuild.py` L474-476 / L516-518 (`walk_path` renvoie `None` → `continue`),
  L486 / L535 (`applied += 1` compté quand même).
- **Scénario.** Un chemin XML stocké qui ne se résout plus, ou un paragraphe sans
  `<w:t>` où écrire.
- **Conséquence.** Le paragraphe restait dans la langue source ; le journal
  disait « applied n/m » sans compter la perte ; aucun signalement à l'écran.
- **Correction.** Après chaque reconstruction, le document produit est relu et
  chaque segment traduit comparé à ce qui devait y être écrit
  (`find_unplaced_translations`). Un écart est signalé « Translation could not be
  placed in the document » (effacé à la reconstruction suivante), le job finit
  `done_with_warnings`. Commit `167937e`.
- **Tests.** `test_a_translation_that_could_not_be_written_into_the_document_is_flagged`
  (+ cas sans faux positif).
- **Limite.** Pour un segment « bilingue au format » (seule la portion source est
  traduite), la comparaison est une inclusion, pas une égalité.

### A4 🔴 Un job en échec perdait son message d'erreur

- **Où.** `translate.py` L175-176 (`error_type = $4, error_msg = $5` sans condition),
  appelé par `_upload_input_to_volume` (L515-521) sans statut.
- **Scénario.** Fichier corrompu : l'extraction échoue en quelques millisecondes,
  puis l'envoi en arrière-plan vers le volume se termine et remet les deux
  colonnes à NULL.
- **Conséquence.** Job `failed` sans aucune explication.
- **Correction.** Les colonnes d'erreur ne changent qu'avec un statut. Commit
  `3f3de3f`. Ajoute `tests/fakedb.py` (faux pool sqlite) pour tester le SQL du
  routeur.
- **Tests.** `test_background_upload_does_not_wipe_the_error_of_a_failed_job`.

### A5 🔴 Prévisualisation périmée ou d'un fichier invalide

- **Où.** `translate.py` L2368, L2444 (`Cache-Control: max-age=600`), L2255-2345
  (aucun contrôle du statut), L2613 (vue partagée), `_run_rebuild_stage` (ne
  supprimait pas les anciens PDF).
- **Scénario.** (1) Modifier un segment, « Rebuild again » : les PDF de la version
  précédente restent dans le volume plusieurs secondes après `done`, et le
  navigateur garde le PDF « après » dix minutes. (2) Une validation structurelle en
  échec (`failed`) : `output.docx` est quand même envoyé et reste servi par la
  prévisualisation et le lien de partage.
- **Conséquence.** L'utilisateur relit l'ancien document en croyant voir la
  nouvelle traduction ; ou ouvre un fichier que le pipeline a lui-même jugé
  cassé (« a broken file must never reach the user »).
- **Correction.** Les PDF de preview sont supprimés au début d'une reconstruction
  ou d'un redémarrage ; les prévisualisations et la vue partagée ne servent que
  les jobs `done`/`done_with_warnings` ; les PDF/diffs sont revalidés par ETag au
  lieu d'être mis en cache dix minutes. Le client n'affiche déjà la preview
  que pour ces statuts : rien ne change à l'écran. Commit `967d9bd`.
- **Tests.** 5 tests dans `tests/test_router_jobs.py`.

### A6 🔴 « Translate » sur un segment mi-source, mi-cible dupliquait du texte

- **Où.** `translate.py` L1050-1108 (`retranslate_segment`).
- **Scénario.** Segment « bilingue en ligne » (`X / Y`, ou deux mises en forme dans
  un paragraphe) : le bouton envoyait tout le texte source au LLM et stockait la
  réponse telle quelle.
- **Conséquence.** Pour un découpage par format, le texte complet était écrit
  dans les runs du côté source alors que le côté conservé restait dans les siens :
  le texte conservé apparaissait deux fois dans le document. Pour `X / Y`, le
  côté à conserver était traduit aussi.
- **Correction.** Même planification que l'étape de traduction (côté source
  seulement, recomposé avec l'autre côté). Commit `d1e32e5`.
- **Tests.** 3 tests (découpage `/`, découpage par format, segment simple inchangé).

### A7 🟠 Une écriture perdue laissait un segment sans traduction, puis plantait la reconstruction

- **Où.** `translate.py` L413 (`applied.update(new_items)` avant l'écriture en
  base), L1958 (`translated_text` `None` transmis à la reconstruction).
- **Scénario.** Coupure de connexion pendant l'écriture d'un lot : la transaction
  est annulée mais les chaînes sont déjà comptées « appliquées » ; la reprise les
  saute.
- **Conséquence.** Segment sans traduction ni signalement ; depuis l'état
  `translated`, la reconstruction plantait en `TypeError`. Le 2e essai de lot
  renvoyait aussi tout le lot au lieu des seules chaînes manquantes (coût).
- **Correction.** « Appliqué » seulement après écriture réussie ; repli par
  chaîne qui échoue → marquée échec (source gardée, signalée) ; reprise
  limitée aux manquantes ; une traduction manquante à la reconstruction garde sa
  source et est signalée « Translation missing » au lieu de planter. Commits
  `d08a037` et `8d4b39a`.
- **Tests.** `tests/test_translation_batches.py` (3) et un test de bout en bout de
  l'étape de reconstruction.

### A8 🟠 40 longs paragraphes dans un appel dépassaient la limite de réponse

- **Où.** `translate.py` L75 (`_TRANSLATE_BATCH_SIZE = 40`), `llm.py` L65
  (`max_tokens=8192`).
- **Scénario.** Document à paragraphes longs : la réponse JSON est coupée.
- **Conséquence.** Réparation demandée au modèle (impossible), puis tout le
  lot refait, puis une requête par chaîne : jusqu'à 4× le coût et la durée, sur
  chaque lot du document. Une réponse tronquée n'était même pas reconnue comme
  telle.
- **Correction.** Lots limités aussi à 6000 caractères source
  (`TRANSLATE_BATCH_MAX_CHARS`), une chaîne plus longue part seule ; une réponse
  coupée à `max_tokens` est signalée comme telle sans passer par la réparation ;
  compteur de progression en chaînes réelles. Commit `685cf10` (+ `de67350`).
- **Tests.** 4 dans `tests/test_translation_batches.py`, 1 dans `test_llm_retries.py`.

### A9 🟠 Une limite de débit transformait tout le document en « traduction échouée »

- **Où.** `llm.py` L80-82 (`_post`), `translate.py` L400-446 (reprises immédiates).
- **Scénario.** L'endpoint répond 429/502/503/504 ou expire.
- **Conséquence.** Deux essais de lot + deux essais par chaîne, tous dans la même
  seconde : chaque chaîne échoue, le document sort entièrement en langue source
  (signalé segment par segment, mais inutilisable).
- **Correction.** Attentes de 2 s / 6 s / 15 s (`Retry-After` respecté) sur 429,
  5xx, délais dépassés et coupures de connexion ; 400/401/404 ne sont pas
  réessayés. Commit `de67350`.
- **Tests.** `tests/test_llm_retries.py` (5).

### A10 🟠 Filtre de pages : filtre silencieusement désactivé, en-têtes non traduits

- **Où.** `pages.py` L25-41 (`parse_page_spec`), L57 et L44-75 (`assign_pages`).
- **Scénario.** (a) `5-3` donne un ensemble vide, donc « pas de filtre » : tout le
  document est traduit (et facturé) alors que l'utilisateur croit n'avoir demandé
  que les pages 3 à 5. (b) `1-999999999999` construit un ensemble de cette taille.
  (c) Les en-têtes, pieds de page et notes sont appariés par un curseur qui avance
  seulement : arrivés après le corps, ils sont rattachés à la dernière page, donc
  `1-3` les laisse tous dans la langue source. (d) Une phrase coupée sur deux
  lignes du PDF ne contient pas l'espace du texte du segment : non placée.
- **Correction.** Plages inversées, nulles ou > 5000 refusées (HTTP 400) ;
  en-têtes/pieds/notes/commentaires jamais rattachés à une page (donc
  traduits) ; comparaison insensible aux retours à la ligne. Commit `473b4b7`.
- **Tests.** `tests/test_pages.py` (10).
- **Limite.** Voir B9 (le curseur peut encore dériver sur du texte répété).

### A11 🟠 Une traduction tronquée ou gonflée n'était jamais signalée

- **Où.** `translate.py` L1461-1500 (`_apply_resolved_segment`) : seul contrôle de
  la réponse = mots source restants.
- **Scénario.** Le modèle traduit la première moitié d'un long paragraphe et
  oublie la suite, ou répond par un résumé d'un mot, ou ajoute un commentaire.
- **Conséquence.** Paragraphe lu comme « traduit » par un lecteur qui ne connaît
  pas la langue source.
- **Correction.** Source ≥ 60 caractères traduite en < 30 % de sa longueur, ou
  source ≥ 40 caractères traduite en > 3× : signalé « Translation looks
  incomplete / too long ». Marges larges (les langues diffèrent de ±40 %) pour ne
  signaler que ce qui est faux dans n'importe quel couple. Commit `83909f1`.
- **Tests.** 4 dans `tests/test_router_jobs.py`.
- **Risque.** Faux positifs possibles sur des segments atypiques (liste de
  codes) : un signalement de trop se règle d'un clic, un oubli non. À surveiller.

### A12 🟠 Signalements périmés après une retraduction réussie

- **Où.** `translate.py` L1494-1495 et L1617 (`COALESCE(conflict_detail, …)`).
- **Scénario.** Segment échoué (« Translation failed after all retries »), puis
  retraduit avec succès (reprise de l'étape, ou re-passe automatique) : le
  drapeau restait ; idem pour « DNT token lost » d'une traduction précédente.
- **Conséquence.** Même famille que « Still reads as BG » corrigé le 02/10 :
  segment bon, signalé faux, job bloqué en `done_with_warnings`.
- **Correction.** Les détails écrits par cette fonction (ou par le chemin d'échec)
  sur la traduction précédente sont réinitialisés quand une nouvelle est
  appliquée ; ceux d'autres origines sont conservés. Commits `8d4b39a`, `83909f1`.
- **Tests.** 2.

### A13 🟠 Traits d'union insécables : numéro de pièce altéré

- **Où.** `extract.py` L182-186 (rien n'était lu pour `<w:noBreakHyphen/>`),
  `rebuild.py` (l'élément restait après le texte réécrit).
- **Scénario.** « NAS1726‑4D » saisi avec Ctrl+Maj+- (trait d'union insécable).
- **Conséquence.** Le LLM recevait « NAS17264D » ; la sortie contenait un trait
  d'union en trop en fin de paragraphe.
- **Correction.** Lu comme U+2011 ; supprimé à la réécriture (le caractère
  voyage dans le texte) ; reconnu comme `-` par la détection DNT et son contrôle.
  Commits `cbfcf60`, `521e078`.
- **Tests.** 4.

### A14 🟠 Un caractère de contrôle dans une réponse faisait planter toute la reconstruction

- **Où.** `rebuild.py` L253 (`_set_t`) : lxml refuse `\x00-\x08`, `\x0b`, `\x0c`…
- **Scénario.** Un caractère de contrôle dans la réponse du modèle.
- **Conséquence.** Job `failed` « All strings must be XML compatible », tout le
  document perdu pour un caractère.
- **Correction.** Caractères non valides en XML 1.0 supprimés avant écriture.
  Commit `cbfcf60`. **Test.** 1.

### A15 🟠 Original non stocké : l'échec arrivait après la traduction payante

- **Où.** `translate.py` L515-528 (envoi en arrière-plan, échec seulement journalisé),
  L681 / L855 / L915 / L1717 / L2172 / L2251 (`asyncio.create_task` sans
  référence), `_sanitize_filename` L110.
- **Scénario.** Droit d'écriture refusé sur le volume, nom de fichier très long.
- **Conséquence.** Questions, traduction complète (coût LLM), puis échec à la
  reconstruction sur « no input_volume_path ». Les tâches de fond n'étaient
  référencées que faiblement (asyncio peut les collecter en cours d'exécution).
- **Correction.** La première étape attend l'envoi et échoue tout de suite avec
  un message clair ; tâches gardées dans un ensemble ; nom limité à 100
  caractères en gardant l'extension. Commit `92853e7`.
- **Tests.** 5.

### A16 🟠 Une règle DNT « * » laissait tout le document non traduit

- **Où.** `translate.py` L2735 (`add_dnt_rule`), `glossary_io.py` (`DntMatcher`).
- **Scénario.** Règle `prefix` ou `glob` « * », ou regex `.*` : correspond à tous
  les segments ; une regex invalide est ignorée sans un mot alors que le panneau
  la liste comme active.
- **Conséquence.** Document entièrement non traduit, pour tous les utilisateurs
  (la table est partagée), sans signalement.
- **Correction.** Validation à l'ajout (motif non vide, mode connu, regex
  compilable, pas uniquement des jokers, regex ne reconnaissant pas une phrase
  ordinaire) → HTTP 422 avec la raison. Commit `0c84928`. **Tests.** 4 groupes.
- **Non fait.** Les règles déjà en base ne sont pas revalidées (voir B-ops dans
  `OPS_COMMANDS.md` : requête de contrôle).

### A17 🟠 Fichier « .docx » non valide ou bombe de décompression

- **Où.** `translate.py` L118-124 (`_validate_file` : extension seulement).
- **Scénario.** `.doc` renommé, fichier protégé par mot de passe, zip sans
  `word/document.xml`, petit zip qui se décompresse en plusieurs Go.
- **Conséquence.** Job qui échoue plus tard sur « File is not a zip file » ; pour
  la bombe, chaque partie est lue entièrement en mémoire, plusieurs fois par job.
- **Correction.** Refus immédiat (HTTP 400, message clair) d'un non-zip, d'un zip
  sans document Word, ou de plus de 800 Mo décompressés
  (`MAX_TRANSLATE_UNZIPPED_MB`). Commit `b780987`. **Tests.** 4.
- **Vérifié sans problème** : XXE — lxml ≥ 5 ne résout que les entités internes
  par défaut (testé avec une entité externe `file://` : refusée).

### A18 🟡 Modification manuelle : vide accepté, écrasée pendant une étape

- **Où.** `translate.py` L1182-1211 (`update_segment_translation`), L1050
  (`retranslate_segment`).
- **Conséquence.** Une traduction réduite à des espaces vidait le paragraphe tout
  en s'affichant « confirmée par un humain » ; une modification pendant la
  traduction/reconstruction était écrasée par l'écriture par lot de l'étape, ou
  manquait la reconstruction déjà lancée.
- **Correction.** 422 pour un texte vide ; 409 « job en cours » pendant une étape
  active. Commit `0993509`. **Tests.** 3.

### A19 🟡 Candidat de glossaire approuvé deux fois

- **Où.** `translate.py` L2810-2845, L2905.
- **Scénario.** Double-clic, ou deux relecteurs : le terme était inséré deux
  fois ; rejeter un candidat approuvé changeait son statut alors que le terme
  restait.
- **Correction.** Le candidat est « réservé » (`pending` → `approved`/`rejected`)
  avant toute écriture, 409 sinon. Commit `f23f1a0`. **Test.** 1.

### A20 🟡 Test périmé en échec depuis avant l'audit ; pagination de l'historique

- `test_inject_comments_places_marker_and_preserves_segment_text` échouait : il
  comparait tous les segments alors que l'extraction lit `comments.xml` depuis le
  support des notes/commentaires (la vérification de production, elle, l'exclut).
  Commit `67428be`.
- `GET /translate/jobs?limit=-1` renvoyait 500 ; `limit` énorme lisait tout
  l'historique. Borné à 1-200. Commit `84c2a92`.

## B. À décider

Rien ici n'a été modifié : soit le changement est visible, soit il demande un
choix de votre part.

| # | Point | Où | Recommandation |
|---|---|---|---|
| B1 | **Les traductions sont réécrites automatiquement** par des règles d'abréviation (`Monter la rondelle` → `Monter rdl.`…) quand elles dépassent un seuil de longueur (en-têtes, pieds, zones de texte, tableaux). Le texte du document diffère alors de celui du panneau Segments, et cela touche aussi une traduction corrigée à la main. | `translate.py` L1964 ; `length_adapt.py` L32 | Ne pas l'appliquer aux segments modifiés par un humain et enregistrer le texte abrégé dans la base (le panneau montre ce qui est dans le document), ou le désactiver. |
| B2 | **Mise en forme mixte perdue** : toute la traduction va dans le premier run. « **ATTENTION :** ne pas serrer… » devient entièrement en gras ; un premier run en exposant/indice contamine tout le paragraphe (essai : `<w:b/>` du 1er run appliqué à toute la phrase). `replace_proportional` existe mais n'est jamais appelé. | `rebuild.py` L474-485, L534 | Écrire la traduction par morceaux proportionnels aux runs d'origine (la fonction existe) pour les paragraphes à plusieurs mises en forme. À valider sur des documents réels. |
| B3 | **Champs Word (REF, PAGEREF) au milieu d'un paragraphe** : le résultat du champ est vidé et son texte déplacé dans le premier run ; à la mise à jour des champs (F9) Word réinsère le résultat → texte en double. | `rebuild.py` `_write_runs_text` | Protéger les runs de résultat de champ (ne pas les vider) ; demande un choix de conception. |
| B4 | **Intitulés en majuscules avec trait d'union ou apostrophe jamais traduits** : `SOUS-ENSEMBLE`, `PRE-ASSEMBLAGE`, `L'OUTILLAGE`, `CONTROLE-FINAL` sont classés « nom propre » (DNT) comme `BERNARD-SENTAURENS`. Mesuré. | `langdetect.py` L507 | Soit supprimer la règle (les noms propres passent par le LLM, qui les laisse tels quels), soit la limiter aux mots absents du glossaire. Changement visible : à valider. |
| B5 | **Alerte permanente sur les en-têtes/pieds** : toute traduction plus longue que la source y est « CRITICAL » (seuil 100 %), donc quasiment chaque document finit `done_with_warnings` ; et le drapeau est posé avant le raccourcissement automatique (B1). | `fit_check.py` THRESHOLDS ; `translate.py` L2095 | Relever le seuil des en-têtes (≈120 %) ou ne signaler qu'après raccourcissement. |
| B6 | **Arabe** : aucun `w:bidi`/`w:rtl` posé à la traduction vers l'arabe (alignement et ponctuation LTR) ; `w:lang` garde la langue source (correcteur orthographique et césure faux) pour toutes les langues. | `rebuild.py` | Poser `w:lang` = langue cible, et `w:bidi`+`w:rtl` pour `ar`. Déjà noté dans `CLAUDE.md` comme risque ouvert. |
| B7 | **OCR d'images : échec silencieux** (le job continue sans, l'utilisateur ne le sait pas) ; les `.gif`/`.bmp`/`.tiff` sont envoyés avec le type MIME `image/jpeg`. | `translate.py` L541-582 ; `docx_images.py` L166 | Convertir en PNG avec Pillow, et remonter « N images sur M non lues » dans `stage_progress`. |
| B8 | **Battement de cœur seulement aux changements d'étape** : une étape longue (lots de plus de 2 min) affiche « may have stalled » ; surtout, avec **plus d'une instance** de l'app, le rattrapage au démarrage de l'une peut marquer `failed` un job vivant de l'autre. | `app.py` L35-57 | Une tâche de battement de cœur pendant les étapes, ou rester sur une instance. |
| B9 | **Filtre de pages : dérive du curseur** sur du texte répété (un « Oui » trouvé page 30 déplace le curseur et classe les segments suivants hors plage → non traduits). | `pages.py` | Mesurer sur un vrai document avec LibreOffice (absent ici) ; limiter le saut du curseur. |
| B10 | **Parties du fichier jamais lues** : graphiques, SmartArt, propriétés (titre), texte alternatif des images. Aucun signalement à l'utilisateur. | `extract.py` L64-97 | Lister à l'envoi les parties non traduites. |
| B11 | **Prévisualisation : course entre deux reconstructions rapprochées** — l'ancien envoi des PDF peut finir après le nouveau et le remplacer. | `translate.py` `_generate_preview_pdfs` | Nommer les PDF par empreinte du `.docx`. Rare. |
| B12 | **Identité** : sans en-tête `x-forwarded-user`, `user_id` vaut `''` (tous les jobs sans identité sont partagés) et `ENV` non défini = mode développement. Derrière le proxy Databricks l'en-tête est toujours posé. | `user.py` L76-90 | Refuser (401) en production quand l'en-tête manque. |
| B13 | **Lien de partage** : donne texte source + traduction + fichier à tout utilisateur authentifié ayant le lien, sans expiration ni révocation. | `translate.py` L2549-2641 | Ajouter révocation/expiration si les documents sont sensibles. |
| B14 | **Commentaires Word** : les segments d'en-tête/pied/note ne sont jamais ancrés (le chemin est cherché dans `document.xml`) et leur commentaire est abandonné sans trace. | `comments.py` L83-125 | Ancrer dans la partie du segment. |
| B15 | **LibreOffice : délai dépassé** — `subprocess.run(timeout=…)` tue le lanceur mais pas `soffice.bin` (processus orphelins). | `soffice.py` | Lancer dans un groupe de processus et le tuer en entier. |

## C. Parcours vérifiés sans problème

- Injection SQL : toutes les requêtes sont paramétrées ; les deux noms de colonnes
  construits en texte (`_propose_glossary_candidates`) sont validés contre
  `_LANG_NAMES`.
- Propriété des jobs : chaque endpoint de job filtre sur `user_id`.
- Chemins de fichiers : `_sanitize_filename` + identifiant numérique de job ; pas
  de traversée possible.
- Verrous de concurrence : pas de double semaphore imbriqué (pas d'interblocage) ;
  `translate`/`rebuild` sont « réservés » atomiquement (pas de double lancement).
- Reprise après plantage de l'étape de traduction : les écritures par lot sont
  bien reprises (cf. A7 pour le cas de l'écriture perdue).

## D. Ce que cet audit n'a pas pu faire

- Aucun document réel (le dossier `Translator/` est absent de cette machine) :
  les cas ci-dessus sont reproduits sur des `.docx` fabriqués à la main.
- Aucun appel LLM réel : le comportement du modèle (réponses tronquées, caractères
  de contrôle, rejet de `temperature`) est simulé.
- SQL testé sur sqlite : les formes `CASE`/`COALESCE`/`LIKE` modifiées sont du SQL
  standard, mais la **première exécution sur Postgres** reste à observer (typage
  des paramètres asyncpg `$3`/`$5`).
- `langdetect.py` (détection de langue) et `glossary_extract.py` /
  `glossary_verify.py` : lus en partie, pas audités à fond.
- Le client n'a pas été reconstruit (pas de changement, pas de `bun`).
