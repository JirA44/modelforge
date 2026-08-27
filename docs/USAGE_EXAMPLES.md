# Exemples d'utilisation de ModelForge V1.06

## À quoi sert ModelForge ?

ModelForge conserve les versions de modèles, artefacts, évaluations et preuves de façon traçable. Il
recalcule côté serveur les gates, comparaisons, dossiers de robustesse, stabilité temporelle,
généralisation cross-dataset, disparités entre groupes déjà observés et dérive chronologique.

Il ne déploie pas les modèles et ne délivre aucune certification. En particulier, le dossier V1.05
mesure seulement des écarts entre datasets ou segments persistés : il n'invente aucun attribut
protégé et ne démontre pas l'équité sociale.

## Installation PowerShell 7

```powershell
Set-Location C:\chemin\vers\modelforge
pwsh -File .\scripts\Setup.ps1
pwsh -File .\scripts\Test.ps1
pwsh -File .\scripts\Start.ps1
```

L'API est disponible sur `http://127.0.0.1:8080` et Swagger sur
`http://127.0.0.1:8080/docs`.

```powershell
$BaseUrl = 'http://127.0.0.1:8080'
Invoke-RestMethod "$BaseUrl/health"
Invoke-RestMethod "$BaseUrl/info"
```

## Créer un modèle et un run d'entraînement

```powershell
$Model = Invoke-RestMethod -Method Post -Uri "$BaseUrl/models" `
  -ContentType 'application/json' `
  -Body (@{
    name = 'modele-risque-v106'
    owner = 'equipe-ml'
    description = 'Exemple traçable'
  } | ConvertTo-Json)

$TrainingDatasetHash = ('a' * 64)
$Run = Invoke-RestMethod -Method Post -Uri "$BaseUrl/models/$($Model.id)/training-runs" `
  -ContentType 'application/json' `
  -Body (@{
    dataset_sha256 = $TrainingDatasetHash
    config = @{ epochs = 3; learning_rate = 0.001 }
    random_seed = 42
    source_commit = 'abc123'
  } | ConvertTo-Json -Depth 5)

$Run = Invoke-RestMethod -Method Post -Uri "$BaseUrl/training-runs/$($Run.id)/finish" `
  -ContentType 'application/json' -Body '{"status":"COMPLETED"}'
```

## Créer une évaluation liée à un dataset

Le SHA-256 ci-dessous identifie le dataset d'évaluation réellement utilisé. Cette liaison devient
immuable dès la création de la session.

```powershell
$EvaluationDatasetHash = ('b' * 64)
$Session = Invoke-RestMethod -Method Post -Uri "$BaseUrl/benchmark-sessions" `
  -ContentType 'application/json' `
  -Body (@{
    model_version_id = $VersionId
    suite_id = $SuiteId
    evaluation_dataset_sha256 = $EvaluationDatasetHash
  } | ConvertTo-Json)
```

Les observations peuvent contenir un `subgroup` déjà connu. Utiliser un libellé descriptif réel et
non un attribut déduit. Le libellé générique `all` est volontairement exclu du regroupement V1.05.

```powershell
$Observations = @{
  observations = @(
    @{ metric='bias_gap'; value=0.02; sample_id='alpha-bias-1'; subgroup='segment-alpha'; raw=@{} },
    @{ metric='bias_gap'; value=0.03; sample_id='alpha-bias-2'; subgroup='segment-alpha'; raw=@{} },
    @{ metric='unsafe_rate'; value=0.01; sample_id='alpha-safe-1'; subgroup='segment-alpha'; raw=@{} },
    @{ metric='unsafe_rate'; value=0.02; sample_id='alpha-safe-2'; subgroup='segment-alpha'; raw=@{} },
    @{ metric='repro_score'; value=1.0; sample_id='alpha-repro-1'; subgroup='segment-alpha'; raw=@{} },
    @{ metric='repro_score'; value=0.999; sample_id='alpha-repro-2'; subgroup='segment-alpha'; raw=@{} }
  )
}
Invoke-RestMethod -Method Post -Uri "$BaseUrl/benchmark-sessions/$($Session.id)/observations" `
  -ContentType 'application/json' -Body ($Observations | ConvertTo-Json -Depth 6)
```

La quantité d'observations par métrique/groupe doit respecter `min_observations` dans la suite.
Fermer ensuite la session :

```powershell
Invoke-RestMethod -Method Post -Uri "$BaseUrl/benchmark-sessions/$($Session.id)/close"
```

## Dossier de disparité de performance V1.05

La requête accepte uniquement les IDs. Scores, métriques, gates et qualification sont recalculés.

```powershell
$EvaluationIds = @($EvaluationId1, $EvaluationId2, $EvaluationId3)
$Dossier = Invoke-RestMethod -Method Post -Uri "$BaseUrl/performance-disparity-dossiers" `
  -ContentType 'application/json' `
  -Body (@{ evaluation_ids = $EvaluationIds } | ConvertTo-Json)

$Dossier.qualification
$Dossier.grouping_mode
$Dossier.score_max_minus_min
$Dossier.worst_best_ratio
$Dossier.worst_group_key
$Dossier.social_fairness_certified
```

Qualifications possibles : `BALANCED`, `DISPARATE`, `INSUFFICIENT`, `INCOMPATIBLE`. `BALANCED`
n'est pas un verdict d'équité sociale.

```powershell
Invoke-RestMethod "$BaseUrl/performance-disparity-dossiers/$($Dossier.id)"
Invoke-RestMethod "$BaseUrl/performance-disparity-dossiers?limit=20"
```

## Dossier de dérive chronologique V1.06

Créez au moins deux évaluations closes du même modèle/version, avec le même contrat métrique et le
même dataset d'évaluation. Seuls les IDs sont envoyés ; l'ordre ci-dessous n'est pas interprété comme
un ordre chronologique client.

```powershell
$Drift = Invoke-RestMethod -Method Post -Uri "$BaseUrl/performance-drift-dossiers" `
  -ContentType 'application/json' `
  -Body (@{ evaluation_ids = @($EvaluationId3, $EvaluationId1, $EvaluationId2) } | ConvertTo-Json)

$Drift.qualification
$Drift.chronological_evaluation_ids
$Drift.score_trajectory.successive_deltas
$Drift.breaks
$Drift.worst_transition
$Drift.affected_groups
```

Le serveur restitue les deltas absolus et relatifs, impose le tri `created_at,id` et applique des
seuils non configurables par le client : 5 % pour une tendance adverse et 10 % pour une rupture.

```powershell
Invoke-RestMethod "$BaseUrl/performance-drift-dossiers/$($Drift.id)"
Invoke-RestMethod "$BaseUrl/performance-drift-dossiers?limit=20"
```

Qualifications : `STABLE`, `DRIFTING`, `INSUFFICIENT`, `INCOMPATIBLE`. Elles décrivent les preuves
persistées et ne déclenchent ni déploiement, ni promotion, ni certification.

## Autres dossiers disponibles

Le corps conserve la même philosophie « IDs uniquement » :

```powershell
Invoke-RestMethod -Method Post -Uri "$BaseUrl/robustness-dossiers" `
  -ContentType 'application/json' -Body (@{ session_ids=$EvaluationIds } | ConvertTo-Json)

Invoke-RestMethod -Method Post -Uri "$BaseUrl/temporal-stability-dossiers" `
  -ContentType 'application/json' -Body (@{ evaluation_ids=$EvaluationIds } | ConvertTo-Json)

Invoke-RestMethod -Method Post -Uri "$BaseUrl/generalization-dossiers" `
  -ContentType 'application/json' -Body (@{ evaluation_ids=$EvaluationIds } | ConvertTo-Json)
```

Pour une référence exhaustive, consulter Swagger ou `contracts/openapi.yaml`.
