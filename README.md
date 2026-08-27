> **Présentation → [docs/PRESENTATION.md](docs/PRESENTATION.md)** — à quoi ça sert, cas d'usages, usages futurs.

# ModelForge V1.06

ModelForge est un registre de modèles **orienté preuves**. Il conserve les versions et artefacts de
façon immuable, calcule les benchmarks depuis des observations brutes et rend un verdict explicable.
Cette V1.06 ajoute un dossier chronologique de dérive des performances, tout en conservant la
comparaison V1.01, la robustesse V1.02, la stabilité V1.03, la généralisation V1.04 et la disparité V1.05.
Elle ne contient volontairement aucun endpoint de déploiement, de promotion ou de passage en
production.

## Garanties V1.06

- une version créée ne peut être modifiée ni supprimée, y compris par SQL (triggers SQLite/PostgreSQL) ;
- le SHA-256 et la taille d'un artefact local sont calculés côté serveur ;
- un hash déclaré pour une URI externe reste `hash_verified=false` et ne suffit jamais à valider ;
- les observations brutes, résultats de gates et verdicts sont immuables ;
- les agrégats sont calculés côté serveur (`MEAN`, `MIN`, `MAX`, `P05`, `P95`) ;
- une suite doit couvrir `BIAS`, `SAFETY` et `REPRODUCIBILITY` ;
- une validation exige tous les gates au vert, un run terminé et des poids vérifiés localement ;
- un échec mesuré donne `REJECTED`, une preuve manquante donne `INSUFFICIENT` ;
- aucun `PASS`, verdict ou état de déploiement ne peut être fourni par le client.
- un rapport de comparaison est immuable, idempotent et scellé par trois SHA-256 ;
- les deltas sont calculés côté serveur dans le sens défini par chaque gate (`LTE` ou `GTE`) ;
- la garde de régression utilise une tolérance serveur nulle, non modifiable par le client ;
- `ACCEPTABLE` indique seulement l'absence de régression mesurée, jamais une promotion automatique.
- un dossier de robustesse accepte uniquement au moins deux IDs de sessions closes et immuables ;
- les métriques, gates, pires cas, dispersions et taux de réussite sont recalculés côté serveur ;
- le dataset lié, les hashes d'artefacts vérifiés et les résultats stockés sont contrôlés ;
- les dossiers sont immuables, idempotents, audités et scellés par trois SHA-256 ;
- `ROBUST` ne déclenche jamais un déploiement ou une promotion.
- un dossier temporel accepte uniquement une liste ordonnée d'au moins deux `evaluation_ids` uniques ;
- aucun score, métrique, résultat, gate ou verdict ne peut être fourni par le client ;
- chaque snapshot est rechargé et recalculé depuis les observations et la provenance immuables ;
- le serveur vérifie modèle/version, contrat métrique, dataset et hashes d'artefacts ;
- trajectoires, deltas successifs, amplitude, pire cas, tendance et volatilité sont calculés ;
- `STABLE` n'est ni une certification ni une autorisation de promotion ou de déploiement.
- chaque nouvelle évaluation peut être liée à un dataset d'évaluation par son SHA-256 immuable ;
- le dossier de généralisation accepte uniquement 2 à 50 `evaluation_ids` uniques ;
- une analyse exige la même version de modèle et au moins deux datasets d'évaluation distincts ;
- scores, métriques, dispersion, pire dataset, gates et taux de réussite sont recalculés côté serveur ;
- `GENERALIZES` ne constitue ni une certification ni une autorisation de mise en production.
- seuls les datasets liés ou segments explicitement stockés peuvent devenir des groupes d'analyse ;
- aucun attribut protégé, démographique ou social n'est deviné à partir des données ;
- le score par groupe, max–min, ratio pire/meilleur, dispersion et pire groupe sont calculés ;
- la garde de disparité est fixe à 10 % et ne peut pas être fournie par le client ;
- `BALANCED` décrit les groupes observés, jamais une preuve d'équité sociale.
- le dossier de dérive accepte uniquement 2 à 100 IDs persistés et uniques ;
- la requête est canonisée, puis le serveur impose l'ordre chronologique `created_at`, `id` ;
- deltas absolus/relatifs, tendances, ruptures, pire transition et groupes métriques sont recalculés ;
- les gardes fixes sont 5 % pour une tendance adverse et 10 % pour une rupture ;
- `STABLE` et `DRIFTING` restent descriptifs et n'autorisent aucune action.

## Verdicts

| Verdict | Condition calculée |
|---|---|
| `VALIDATED` | Tous les gates passent, le run lié est `COMPLETED` et au moins un artefact `WEIGHTS` est vérifié depuis ses octets. |
| `REJECTED` | Au moins un gate disposant d'assez d'observations échoue. |
| `INSUFFICIENT` | Aucun échec établi, mais observations ou provenance insuffisantes. |

`VALIDATED` signifie uniquement « conforme à cette suite et à ces preuves ». Ce n'est ni une
certification universelle ni une autorisation de déploiement.

## Comparaison V1.01

`POST /version-comparisons` accepte uniquement `baseline_session_id` et `candidate_session_id`.
Les deux sessions doivent être closes, utiliser la même version de suite, viser deux versions
distinctes d'un même modèle et reposer sur les résultats immuables déjà calculés.

| Qualification | Condition calculée côté serveur |
|---|---|
| `ACCEPTABLE` | Les deux benchmarks sont `VALIDATED` et aucun agrégat candidat ne se dégrade. |
| `REGRESSED` | Au moins un delta est défavorable selon l'opérateur du gate. |
| `INSUFFICIENT` | Un agrégat comparable manque ou les deux benchmarks ne sont pas tous deux validés. |

Le rapport contient `input_sha256`, `evidence_sha256`, `report_sha256`, les valeurs source, chaque
delta, sa direction et les raisons. Rejouer exactement la même comparaison retourne le même rapport.

## Robustesse V1.02

`POST /robustness-dossiers` accepte uniquement `session_ids` (entre 2 et 100 IDs uniques). Toutes
les sessions doivent être closes, viser exactement la même version de modèle et utiliser la même
version de suite. L'ordre des IDs ne change pas l'identité du dossier.

Le serveur repart des observations brutes pour recalculer chaque agrégat et chaque gate. Il produit
pour chaque métrique la moyenne inter-évaluations, le minimum, le maximum, l'écart-type population,
l'étendue, le pire cas selon l'opérateur `LTE`/`GTE` et le taux de réussite. Il vérifie également le
hash du dataset d'entraînement, les hashes d'artefacts vérifiés et la concordance entre verdicts
stockés et verdicts recalculés.

La garde de stabilité est fixée côté serveur : le pire cas est projeté d'une étendue observée
supplémentaire dans le sens défavorable. Si cette projection franchit le seuil du gate, le dossier
est `UNSTABLE`, même si toutes les évaluations prises séparément passent encore.

| Qualification | Condition calculée côté serveur |
|---|---|
| `ROBUST` | Toutes les évaluations sont complètes, cohérentes et repassent tous les gates. |
| `UNSTABLE` | Les preuves sont complètes mais au moins une évaluation échoue. |
| `INSUFFICIENT` | Une évaluation, un hash, un dataset ou une concordance de résultat manque. |

Le dossier contient les sources recalculées, `success_rate`, `metric_summary`, les contrôles de
cohérence, les raisons et trois hashes de scellement. Il ne contient aucune autorisation de mise en
production.

## Stabilité temporelle V1.03

`POST /temporal-stability-dossiers` accepte un unique champ `evaluation_ids`, contenant entre 2 et
100 IDs uniques dans l'ordre à analyser. L'ordre fait partie de l'identité du dossier : inverser deux
évaluations produit une autre trajectoire et un autre hash d'entrée. Les évaluations doivent être
closes ; une session encore ouverte est refusée car elle n'est pas immuable.

Pour chaque évaluation, le serveur recharge les observations brutes, recalcule les agrégats, les
gates et le verdict, puis vérifie leur concordance avec les résultats stockés. Il scelle un snapshot
incluant le modèle, sa version, le contrat des gates, le dataset d'entraînement, les hashes
d'artefacts vérifiés et un `snapshot_sha256`.

La comparaison temporelle exige le même modèle, exactement la même version, un contrat métrique
identique et un dataset comparable. Des versions de suite différentes restent comparables uniquement
si leur contrat canonique — catégories, métriques, agrégations, opérateurs, seuils et minimums
d'observations — est strictement identique.

### Politique fixe du serveur

Les seuils ne figurent pas dans la requête et ne sont pas modifiables par le client :

| Règle | Seuil fixe |
|---|---:|
| Direction dégradante/améliorante | variation nette normalisée strictement supérieure à `5 %` dans le sens concerné |
| Volatilité de métrique | écart-type population normalisé strictement supérieur à `10 %` |
| Saut volatil | delta successif normalisé strictement supérieur à `20 %` |
| Priorité de qualification | `INCOMPATIBLE` → `INSUFFICIENT` → `DEGRADING` → `VOLATILE` → `STABLE` |

La normalisation utilise la valeur absolue du seuil du gate, avec un plancher technique de
`1e-12`. Pour un gate `LTE`, une hausse est défavorable ; pour un gate `GTE`, une baisse est
défavorable. Le score global est la moyenne des marges normalisées calculées côté serveur.

| Qualification | Condition calculée côté serveur |
|---|---|
| `STABLE` | Trajectoire complète, compatible, sans tendance défavorable ni garde de volatilité franchie. |
| `DEGRADING` | Au moins une métrique, un passage `PASS→FAIL` ou le score global se dégrade. |
| `VOLATILE` | Écart-type/saut excessif ou changements de statut de gate sans dégradation prioritaire. |
| `INSUFFICIENT` | Agrégats, provenance, hashes ou concordance avec les résultats stockés insuffisants. |
| `INCOMPATIBLE` | Modèle/version, contrat métrique ou dataset non comparable. |

`STABLE` décrit seulement la dynamique temporelle sous ces règles. Un modèle peut rester stable à
un niveau médiocre ou échouer des gates de façon constante : ce résultat ne constitue donc jamais
une validation scientifique, une certification ou une décision de mise en production.

## Généralisation cross-dataset V1.04

Lors de `POST /benchmark-sessions`, le champ facultatif `evaluation_dataset_sha256` lie la session
à la version immuable du dataset réellement utilisée pour l'évaluation. Cette valeur est validée,
normalisée en minuscules, enregistrée dans une table append-only et ne peut plus être changée. Les
sessions historiques sans liaison restent lisibles mais ne suffisent pas à un dossier cross-dataset.

`POST /generalization-dossiers` accepte un unique champ `evaluation_ids`, contenant 2 à 50 IDs
uniques. Leur ordre n'affecte pas le résultat : un dossier porte sur l'ensemble sélectionné. Les
évaluations doivent être closes, cibler exactement le même modèle et la même version, et présenter
un contrat canonique identique. Au moins deux hashes de datasets d'évaluation distincts sont requis.

Le serveur recharge les observations, recalcule agrégats, gates, verdicts et score de marge
normalisé pour chaque évaluation, puis regroupe les résultats par dataset. Le rapport contient :

- le score, les métriques, les gates et le taux de réussite par dataset ;
- la dispersion et l'amplitude cross-dataset de chaque métrique ;
- le pire dataset selon le score serveur et selon chaque opérateur `LTE`/`GTE` ;
- le taux de réussite global et les contrôles de cohérence des contrats, versions et artefacts ;
- un hash par snapshot et les hashes d'entrée, de contrat, de preuves et de rapport.

### Politique fixe de dispersion

La garde serveur vaut **10 %** et n'est jamais fournie par le client. Pour chaque métrique, la
dispersion est l'amplitude entre les moyennes par dataset, divisée par la valeur absolue du seuil du
gate (plancher technique `1e-12`). Le score global étant déjà normalisé, son amplitude est comparée
directement à `0.10`.

| Qualification | Condition calculée côté serveur |
|---|---|
| `GENERALIZES` | Tous les datasets passent et toutes les dispersions restent au plus à 10 %. |
| `DATASET_SENSITIVE` | Échec selon un dataset, statut de gate incohérent ou dispersion supérieure à 10 %. |
| `INSUFFICIENT` | Dataset non lié/non distinct, mesures, provenance, hashes ou scores incomplets. |
| `INCOMPATIBLE` | Modèle/version ou contrat métrique incompatible. |

La qualification décrit uniquement les datasets fournis et la suite choisie. Elle ne prouve pas une
généralisation universelle et n'autorise ni déploiement, ni promotion, ni certification.

## Disparité de performance observée V1.05

`POST /performance-disparity-dossiers` accepte uniquement 2 à 50 `evaluation_ids` uniques. Les
évaluations doivent être closes, appartenir au même modèle et à exactement la même version, avec un
contrat métrique canonique identique. L'ordre des IDs est ignoré afin que le snapshot soit
idempotent pour un même ensemble.

Le serveur ne crée jamais de catégorie humaine ou d'attribut protégé. Il utilise exclusivement :

1. les valeurs `subgroup` non vides et différentes du libellé générique `all`, si au moins deux
   segments distincts sont déjà persistés ;
2. sinon, les SHA-256 de datasets d'évaluation liés, si au moins deux existent ;
3. sinon, le dossier est `INSUFFICIENT`.

Pour chaque groupe observé, le serveur recharge les observations, recalcule les métriques/gates et
transforme chaque marge normalisée en composante bornée entre 0 et 1 par une fonction logistique.
Le score de groupe est la moyenne de ces composantes. Le rapport calcule ensuite :

- l'écart `max − min` entre scores ;
- le ratio `pire / meilleur` ;
- la dispersion (écart-type population) ;
- le pire groupe observé ;
- les amplitudes métriques normalisées et la cohérence des statuts de gates.

| Qualification | Condition serveur |
|---|---|
| `BALANCED` | Max–min ≤ 10 %, ratio pire/meilleur ≥ 90 %, métriques et gates cohérents. |
| `DISPARATE` | Écart supérieur à 10 %, ratio inférieur à 90 % ou statuts de gates différents. |
| `INSUFFICIENT` | Moins de deux groupes observés ou preuves/hashes/mesures incomplets. |
| `INCOMPATIBLE` | Modèle/version ou contrat métrique incompatible. |

`BALANCED` signifie seulement « pas de disparité supérieure à la garde dans ces groupes persistés et
ces métriques ». Cela ne démontre pas l'équité sociale, l'absence de discrimination ni la conformité
réglementaire. Le rapport expose donc toujours `social_fairness_certified=false` et
`observed_group_disparity_only=true`.

## Dérive chronologique des performances V1.06

`POST /performance-drift-dossiers` accepte uniquement 2 à 100 `evaluation_ids` uniques. Leur ordre
dans la requête est ignoré pour l'idempotence ; le serveur recharge les évaluations closes et les
ordonne par date de création/fermeture immuable, puis par ID.

La comparaison exige le même modèle, exactement la même version, un contrat métrique identique et
un même dataset effectif. Le serveur recalcule les gates, verdicts, hashes d'artefacts et scores avant
de produire, pour chaque métrique :

- les deltas successifs absolus et relatifs ;
- le progrès normalisé selon `LTE` ou `GTE` ;
- la tendance nette et les passages `PASS` vers `FAIL` ;
- les ruptures de magnitude au moins égale à 10 % du seuil ;
- la pire transition et les catégories `BIAS`, `SAFETY`, `REPRODUCIBILITY` concernées.

Le score serveur est la moyenne des marges normalisées. Une tendance adverse supérieure à 5 %, une
rupture adverse supérieure à 10 % ou un passage `PASS` vers `FAIL` produit `DRIFTING`.

| Qualification | Condition serveur |
|---|---|
| `STABLE` | Aucune garde fixe de dérive adverse n'est franchie. |
| `DRIFTING` | Tendance, rupture ou changement de gate adverse établi. |
| `INSUFFICIENT` | Preuves, provenance, hashes ou trajectoire incomplets/incohérents. |
| `INCOMPATIBLE` | Modèle/version, dataset ou contrat métrique non comparable. |

Le dossier est immuable, hashé, idempotent et audité. Il ne déploie, ne promeut et ne certifie rien.

## Démarrage Windows / PowerShell 7

Prérequis : Python 3.10 ou plus récent et PowerShell 7.

```powershell
Set-Location C:\chemin\vers\modelforge
pwsh -File .\scripts\Setup.ps1
pwsh -File .\scripts\Test.ps1
pwsh -File .\scripts\Start.ps1
```

L'API écoute sur `http://127.0.0.1:8080`. Documentation interactive :
`http://127.0.0.1:8080/docs`. Le fichier SQLite par défaut est `data/modelforge.db` ; définir
`MODELFORGE_DB` pour changer son emplacement.

## Cycle de preuve

1. `POST /models` crée le modèle logique.
2. `POST /models/{id}/training-runs` enregistre dataset, configuration, seed et commit.
3. `POST /training-runs/{id}/finish` ferme le run une seule fois.
4. `POST /models/{id}/versions` crée une version immuable liée au run terminé.
5. `POST /versions/{id}/artifacts` envoie les poids en Base64 pour vérification locale.
6. `POST /benchmark-suites` fixe les gates immuables.
7. `POST /benchmark-sessions`, puis `/observations`, ajoute les mesures brutes.
8. `POST /benchmark-sessions/{id}/close` calcule les agrégats, les gates et le verdict.
9. `POST /version-comparisons` compare deux sessions closes et scelle le rapport de non-régression.
10. `POST /robustness-dossiers` agrège plusieurs évaluations de la même version et scelle le dossier.
11. `POST /temporal-stability-dossiers` analyse une séquence ordonnée et scelle ses snapshots.
12. `POST /generalization-dossiers` compare au moins deux datasets et scelle le dossier.
13. `POST /performance-disparity-dossiers` mesure les écarts entre groupes observés.
14. `POST /performance-drift-dossiers` mesure la dérive chronologique recalculée.

Un exemple de corps de requêtes se trouve dans [examples/evidence-flow.json](examples/evidence-flow.json).

## Principaux endpoints

| Méthode | Route | Rôle |
|---|---|---|
| `POST` | `/models` | Créer un modèle. |
| `POST` | `/models/{id}/training-runs` | Démarrer un run traçable. |
| `POST` | `/training-runs/{id}/finish` | Terminer ou échouer le run. |
| `POST` | `/models/{id}/versions` | Créer une version immuable. |
| `POST` | `/versions/{id}/artifacts` | Attacher et hasher un artefact. |
| `POST` | `/benchmark-suites` | Définir les gates. |
| `POST` | `/benchmark-sessions` | Ouvrir une évaluation. |
| `POST` | `/benchmark-sessions/{id}/observations` | Ajouter les preuves brutes. |
| `POST` | `/benchmark-sessions/{id}/close` | Calculer gates et verdict. |
| `POST` | `/version-comparisons` | Calculer/sceller une comparaison idempotente. |
| `GET` | `/version-comparisons/{id}` | Relire un rapport immuable. |
| `POST` | `/robustness-dossiers` | Recalculer et sceller la robustesse multi-évaluations. |
| `GET` | `/robustness-dossiers/{id}` | Relire un dossier de robustesse immuable. |
| `POST` | `/temporal-stability-dossiers` | Calculer/sceller une trajectoire temporelle. |
| `GET` | `/temporal-stability-dossiers/{id}` | Relire le dossier temporel immuable. |
| `POST` | `/generalization-dossiers` | Calculer/sceller la généralisation cross-dataset. |
| `GET` | `/generalization-dossiers` | Lister les dossiers scellés. |
| `GET` | `/generalization-dossiers/{id}` | Relire un dossier scellé. |
| `POST` | `/performance-disparity-dossiers` | Calculer/sceller les disparités observées. |
| `GET` | `/performance-disparity-dossiers` | Lister les dossiers scellés. |
| `GET` | `/performance-disparity-dossiers/{id}` | Relire un dossier immuable. |
| `POST` | `/performance-drift-dossiers` | Calculer/sceller la dérive chronologique. |
| `GET` | `/performance-drift-dossiers` | Lister les dossiers de dérive scellés. |
| `GET` | `/performance-drift-dossiers/{id}` | Relire un dossier de dérive immuable. |
| `GET` | `/audit-events` | Consulter la piste d'audit append-only. |

Contrat : [contracts/openapi.yaml](contracts/openapi.yaml). Schéma PostgreSQL :
[database/postgresql.sql](database/postgresql.sql).

## Limites de ce starter

- Les petits artefacts locaux sont stockés dans SQLite. En production, conserver les poids dans un
  object store immuable et ajouter un worker qui télécharge puis vérifie réellement les octets.
- Il n'y a pas d'authentification dans ce starter : placer l'API derrière une passerelle OIDC/mTLS.
- Une suite de benchmark doit être revue et versionnée par l'organisation ; ModelForge garantit le
  calcul, pas la pertinence scientifique du seuil choisi.
- SQLite convient au développement local. Utiliser le schéma PostgreSQL pour une exploitation
  multi-utilisateur et ajouter sauvegarde, réplication et contrôle d'accès.

## Tests

```powershell
pwsh -File .\scripts\Test.ps1
```

Les tests vérifient notamment l'immutabilité SQL, les hashes, les verdicts et qualifications, les
prérequis de provenance, le rejet des métriques inconnues/doublons, les améliorations/régressions,
les dossiers robustes/instables/insuffisants, les trajectoires stables/dégradantes/volatiles,
les données temporelles insuffisantes, les incompatibilités de version/contrat, l'ordre,
l'idempotence, la généralisation/sensibilité cross-dataset, les liaisons de dataset immuables,
les listes de dossiers, `extra=forbid`, le refus de résultats client et l'absence de route de
déploiement.

Des exemples complets en français sont disponibles dans
[docs/USAGE_EXAMPLES.md](docs/USAGE_EXAMPLES.md). Les contributions sont décrites dans
[CONTRIBUTING.md](CONTRIBUTING.md).

