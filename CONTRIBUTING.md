# Contribuer à ModelForge

Merci de contribuer à ModelForge. Le projet privilégie la traçabilité, les preuves immuables et les
qualifications prudentes. Une qualification calculée ne doit jamais devenir implicitement une
autorisation de déploiement, de promotion ou une certification.

## Préparer l'environnement sous Windows

Prérequis : Python 3.10 ou plus récent et PowerShell 7.

```powershell
Set-Location C:\chemin\vers\modelforge
pwsh -File .\scripts\Setup.ps1
pwsh -File .\scripts\Test.ps1
```

## Règles de contribution

- conserver toutes les fonctions et migrations historiques lors d'une évolution cumulative ;
- utiliser les modèles Pydantic stricts avec `extra="forbid"` pour les entrées sensibles ;
- recalculer métriques, gates, scores et qualifications côté serveur depuis les preuves brutes ;
- ne jamais accepter un `PASS`, score, verdict, résultat ou qualification fourni par le client ;
- protéger les rapports, snapshots, observations et audits par l'immutabilité SQL ;
- rendre les créations idempotentes à partir d'une entrée canonique hashée ;
- ne jamais inventer un attribut protégé ou présenter une disparité observée comme une preuve
  d'équité sociale ;
- ne pas ajouter de route de déploiement, promotion ou certification dans ce starter ;
- mettre à jour `VERSION`, `pyproject.toml`, le package, FastAPI, `/health`, `/info`, OpenAPI,
  README et CHANGELOG à chaque version.

## Validation locale obligatoire

```powershell
pwsh -File .\scripts\Test.ps1
.\.venv\Scripts\python.exe -m compileall -q modelforge tests
```

Vérifier également que `contracts/openapi.yaml` est un document OpenAPI 3.1 valide, que l'OpenAPI
runtime expose les mêmes routes et qu'aucun fichier temporaire (`*.db`, cache, environnement,
`egg-info`) n'est ajouté au commit.

## Tests attendus

Toute capacité doit couvrir au minimum : résultat favorable, résultat défavorable, données
insuffisantes, incompatibilité, idempotence, immutabilité SQL, entrée stricte et OpenAPI runtime.
Les tests existants restent obligatoires. Aucun faux `PASS` ou test neutralisé n'est accepté.

## Proposition de changement

1. créer une branche courte et descriptive ;
2. limiter les changements au sujet annoncé ;
3. ajouter les tests et la documentation dans le même commit logique ;
4. exécuter la suite complète ;
5. ouvrir une pull request décrivant les garanties, limites et résultats exacts des tests.

Le dépôt est préparé pour GitHub mais aucun dépôt distant n'est initialisé ou poussé automatiquement.
