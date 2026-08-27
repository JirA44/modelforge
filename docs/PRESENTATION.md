# Modelforge — Présentation complète

## Présentation
modelforge est un registre immuable, hashé (SHA-256), auditable et rejouable.

## À quoi ça sert ? (problèmes réglés)
- **Benchmark non comparable (métriques différentes)** → résolu par un dossier déterministe, ordre-indépendant
- **Modèle promu sans preuve de comparabilité** → résolu par un dossier déterministe, ordre-indépendant
- **MLOps qui compare des pommes et des oranges** → résolu par un dossier déterministe, ordre-indépendant

## Cas d'utilisation concrets
- Registry LLM: prouver que 2 benchmarks sont comparables (même métrique, même split)
- Sélection modèle: dossier de couverture avant mise en prod
- Audit IA: tracer le champion par dataset

## Exemples d'utilisation (API)
```bash
curl -X POST http://localhost:8000/v1/benchmark-comparability-dossiers -d '{"benchmark_ids": [...] }'
# → { "qualification": "COMPLETE|GAPPED|INSUFFICIENT|INCOMPATIBLE", "coverage_ratio": 0.94, ... }
```

## À quoi ça pourrait servir (futur / possibilités)
- Gouvernance IA (AI Act)
- Leaderboard vérifiable
- Certification modèle avant déploiement

## Pour qui ?
Devs, auditeurs, ops, chercheurs — qui ont besoin d'une preuve opposable, pas d'un verdict déclaratif.