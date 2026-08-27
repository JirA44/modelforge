# Changelog

## 1.0.6 — V1.06 — 2026-08-24

- Dossiers chronologiques de dérive pour 2 à 100 évaluations persistées compatibles.
- Requête limitée aux IDs, canonisée indépendamment de l'ordre puis triée côté serveur par date/ID.
- Recalcul des gates, verdicts, scores, datasets, contrats métriques et hashes de provenance.
- Deltas absolus/relatifs, tendances, ruptures, pire transition et groupes métriques concernés.
- Gardes fixes documentées : tendance adverse 5 %, rupture 10 % et passage `PASS` vers `FAIL`.
- Qualifications prudentes `STABLE`, `DRIFTING`, `INSUFFICIENT`, `INCOMPATIBLE`.
- API POST/GET/list, SQLite/PostgreSQL et OpenAPI 3.1 alignés sur 1.0.6.
- Snapshots immuables, idempotents, SHA-256 et audités ; aucune action automatique.

## 1.0.5 — V1.05 — 2026-08-24

- Dossiers de disparité de performance pour 2 à 50 évaluations persistées du même modèle/version.
- Groupes limités aux segments non génériques ou datasets déjà stockés ; aucun attribut protégé inféré.
- Scores de groupe bornés, max–min, ratio pire/meilleur, dispersion, pire groupe et cohérence des gates.
- Garde serveur fixe à 10 % et qualifications `BALANCED`, `DISPARATE`, `INSUFFICIENT`, `INCOMPATIBLE`.
- Snapshots ordre-indépendants, immuables, idempotents, hashés et audités.
- API POST/GET/list, SQLite/PostgreSQL et OpenAPI 3.1 alignés sur 1.0.5.
- Préparation Git/GitHub : guide de contribution, exemples français et workflow de tests.
- Aucun déploiement, aucune promotion, aucune certification et aucune preuve d'équité sociale.

## 1.0.4 — V1.04 — 2026-08-22

- Liaison SHA-256 immuable et facultative d'un dataset d'évaluation à chaque nouvelle session.
- Dossiers de généralisation pour 2 à 50 évaluations d'une même version sur au moins deux datasets.
- Entrée stricte limitée à `evaluation_ids` ; aucun score, métrique, gate, résultat ou qualification client.
- Recalcul serveur des évaluations et agrégation des scores, métriques, gates et taux par dataset.
- Dispersion, amplitude et pire dataset calculés avec une garde serveur fixe de 10 %.
- Qualifications `GENERALIZES`, `DATASET_SENSITIVE`, `INSUFFICIENT`, `INCOMPATIBLE`.
- API POST/GET/list, snapshots et rapports hashés, immuables, idempotents et audités.
- SQLite/PostgreSQL, OpenAPI statique/runtime, `/health`, `/info`, README et tests alignés sur 1.0.4.
- Aucun déploiement, aucune promotion et aucune certification.

## 1.0.3 — V1.03 — 2026-08-22

- Dossiers temporels pour des séquences ordonnées d'au moins deux évaluations closes et immuables.
- Entrée stricte limitée à `evaluation_ids` ; aucun score, métrique, gate, résultat ou verdict client.
- Rechargement et recalcul serveur des agrégats, gates, verdicts et scores depuis les preuves brutes.
- Vérification du modèle/version, du contrat canonique, du dataset et des hashes d'artefacts.
- Trajectoires métriques/globales, deltas successifs, amplitude, pire cas, direction et volatilité.
- Seuils serveur fixes documentés : tendance 5 %, écart-type 10 %, saut successif 20 %.
- Qualifications prudentes `STABLE`, `DEGRADING`, `VOLATILE`, `INSUFFICIENT`, `INCOMPATIBLE`.
- Snapshots et rapports hashés, immuables, idempotents, avec audit append-only.
- SQLite/PostgreSQL, OpenAPI statique/runtime, README et tests alignés sur 1.0.3.
- Aucun déploiement, aucune promotion et aucune certification.

## 1.0.2 — V1.02 — 2026-08-22

- Dossiers de robustesse déterministes pour au moins deux sessions closes d'une même version et suite.
- Recalcul serveur des agrégats, gates et verdicts depuis les observations brutes immuables.
- Dispersion, pire cas et taux de réussite calculés pour chaque métrique et pour l'ensemble des runs.
- Contrôles de cohérence du dataset, des hashes d'artefacts vérifiés et des résultats stockés.
- Qualifications prudentes `ROBUST`, `UNSTABLE` et `INSUFFICIENT`, sans valeur fournie par le client.
- Rapports immuables, idempotents, audités et scellés par hashes d'entrée, de preuves et de rapport.
- Schémas SQLite/PostgreSQL, OpenAPI statique/runtime, documentation et tests mis à jour.
- Toujours aucun endpoint de déploiement ou de promotion automatique.

## 1.0.1 — V1.01 — 2026-08-22

- Comparaison déterministe de deux sessions de benchmark closes pour deux versions d'un même modèle.
- Deltas calculés selon les opérateurs `LTE`/`GTE` et garde serveur de régression à tolérance nulle.
- Qualifications calculées `ACCEPTABLE`, `REGRESSED` et `INSUFFICIENT` ; aucune valeur client acceptée.
- Rapports immuables et idempotents avec hashes d'entrée, de preuves et de rapport.
- Schémas SQLite/PostgreSQL, contrat OpenAPI, documentation et tests de comparaison ajoutés.
- Toujours aucun endpoint de déploiement ou de promotion automatique.

## 1.0.0 — 2026-08-22

- Registre de modèles et versions immuables.
- Runs d'entraînement avec provenance et transitions terminales contrôlées.
- Artefacts locaux hashés côté serveur et artefacts externes explicitement non vérifiés.
- Benchmarks calculés uniquement depuis les observations brutes immuables.
- Gates biais, sécurité et reproductibilité.
- Verdicts `VALIDATED`, `REJECTED` et `INSUFFICIENT` sans déploiement automatique.
- API FastAPI, SQLite, schéma PostgreSQL, scripts PowerShell et tests.
