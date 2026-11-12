# Base historique `data/history.db`

Guide pour un agent IA qui répond aux questions de l'utilisateur en interrogeant la base SQLite écrite par `db.py`.
La base garde **l'historique** des observations (lectures de sites, relevés d'offres d'emploi, scores) et les
**événements** déduits des changements entre deux observations.

## Ouvrir la base en lecture seule

Toujours en lecture seule, pour ne jamais modifier l'historique ni bloquer un run en cours :

```python
import sqlite3
conn = sqlite3.connect("file:data/history.db?mode=ro", uri=True)
conn.row_factory = sqlite3.Row
```

Conventions : dates en texte ISO (`'YYYY-MM-DD'`, ou `'YYYY-MM-DDTHH:MM:SS'` pour `scanned_at`, `started_at`,
`finished_at`) ; les listes sont stockées en JSON texte (lire avec `json_each(colonne)` ou `json_extract`).
Une entreprise est identifiée partout par son **domaine** (`acme.com`, sans `www.`). Version du schéma : `PRAGMA user_version` (= 1).

## Tables

| Table | Rôle |
|---|---|
| `runs` | Une ligne par exécution du pipeline. `options` (JSON) = options de la ligne de commande ; `warnings` (JSON) = liste d'avertissements ; `throttled` = nombre de réponses 429/503 reçues pendant le run (NULL pour les anciens runs). Un run d'options `{"import": "legacy_caches"}` correspond à l'import initial des anciens caches. |
| `companies` | Fiche de chaque entreprise (nom, taille, secteur, lieu). `province` = province canadienne ; `sources` = liste JSON des sources (`betakit`, `yc`, nom des exports) ; `excluded_reason` = raison d'exclusion (`Exclu : candidature CDI`…) ou NULL ; `first_seen` / `last_seen` = premier et dernier jour où l'entreprise était dans les sources. |
| `funding_rounds` | Levées de fonds connues (`announced_on`, `amount`, `currency`, `round`, `article_url`). |
| `site_scans` | **Chaque** lecture du site, réussie ou non. `ok` = 1 si le site a répondu. `hubspot` = verdict `Oui` / `Probable` / `Non détecté` / `Erreur`. `portal_ids`, `hubspot_signals`, `tools` (autres outils détectés), `saas_hints`, `ats_links` sont des listes JSON. `error` = message si `ok = 0`. |
| `stack` | État **actuel consolidé** des outils de chaque site, une ligne par (domaine, outil). Les outils incluent `HubSpot` (verdict Oui/Probable), chaque outil détecté (`Pipedrive`, `Calendly`, `Google Tag Manager`…) et `HubSpot portal <id>` pour chaque portal HubSpot. `status` = `present` ou `gone` ; `gone_since` = jour où l'outil a été déclaré disparu ; `absent_count` = nombre de lectures réussies consécutives où il manquait ; `checked_on` = dernier jour où la ligne a été évaluée. |
| `job_reads` | Chaque relevé d'un board d'offres (`ats` = `greenhouse:acme`, `lever:acme`… ; vide = recherche sans board : `ok` = 1 aucun board trouvé, `ok` = 0 recherche plantée). `ok` = 0 si la lecture a échoué (ex. lien vers le board sur le site mais API en panne) ; `postings` = nombre d'offres lues ; `sales_open` = offres sales ouvertes après ce relevé. |
| `job_postings` | Chaque offre d'emploi vue, une ligne par (domaine, ats, posting_id). `category` = `revops` (RevOps, Sales/Marketing Ops, CRM), `leadership` (VP / Head / Director sales-marketing-revenue, CRO, CMO), `sales` (AE, SDR, marketing, customer success) ou `other`. `published_at` = date de publication donnée par l'ATS (souvent vide) ; `first_seen` / `last_seen` = premier et dernier relevé où l'offre était présente ; `closed_on` = jour où elle a été déclarée fermée (NULL = encore ouverte). |
| `scores` | Score de chaque entreprise à chaque run : `tier` (`A`, `B`, `C` ou `Exclu : …`), `score` (points, NULL si exclue), `detail` (points gagnés), `angle` (angle d'approche suggéré). |
| `events` | Changements détectés, une ligne par événement : `day`, `domain`, `type`, `detail` (texte lisible : nom de l'outil, titre du poste, `C → A`…), `run_id`. `ref` = clé technique de dédoublonnage. |

### Types d'événements

| `type` | Signification |
|---|---|
| `tool_added` | Un outil apparaît sur le site (`detail` = outil). Aussi émis quand un outil disparu revient. |
| `tool_removed` | Un outil a disparu du site (confirmé, voir règles plus bas). |
| `second_portal` | Le site charge au moins deux portals HubSpot (migration, fusion, doublon de compte) : `detail` liste les IDs. |
| `posting_opened` | Nouvelle offre d'emploi (`detail` = titre). |
| `posting_closed` | Offre retirée du board (confirmé). |
| `posting_stale` | Offre RevOps ouverte depuis plus de 60 jours : le poste peine à être pourvu. Émis une fois par offre. |
| `leadership_hire` | Recrutement d'un poste de direction commerciale / marketing / revenu. Émis une fois par offre. |
| `hiring_spike` | Le nombre d'offres sales ouvertes augmente d'au moins 3 depuis le relevé précédent (`detail` = `6 → 9`). |
| `funding` | Nouvelle levée de fonds (`day` = date d'annonce). |
| `tier_up` / `tier_down` | Le tier change entre deux runs (`detail` = `C → A`). Les exclusions sont ignorées. |

## Vues prêtes à l'emploi

| Vue | Contenu |
|---|---|
| `v_latest_scores` | Dernier score de chaque domaine, avec `name`. |
| `v_current_stack` | Outils actuellement présents (`status = 'present'`), avec `name`. |
| `v_open_postings` | Offres encore ouvertes, avec `name` et `age_days` (jours depuis `published_at`, sinon `first_seen`). |
| `v_recent_events` | Événements des 30 derniers jours, avec `name`, du plus récent au plus ancien. |

## Règles à connaître pour bien interpréter

- **Anti-fausse alerte** : une lecture en échec (site injoignable, ATS en panne) est enregistrée dans `site_scans` /
  `job_reads` avec `ok = 0` mais **ne change rien** à `stack` ni à `job_postings` et n'émet aucun événement.
- Un outil ou une offre n'est déclaré disparu / fermé qu'après **2 lectures réussies consécutives** sans lui
  (`absent_count`). Au plus une lecture compte par jour. Un outil avec `absent_count = 1` est donc encore `present`.
- **Premier relevé** d'un domaine (ou d'un board) : l'état est rempli sans `tool_added` / `posting_opened`. Un
  `first_seen` égal au premier scan du domaine veut dire « déjà là quand on a commencé à regarder », pas « installé ce
  jour-là ». Pour « a installé HubSpot », utiliser les événements `tool_added`, pas `stack.first_seen`.
- `second_portal`, `leadership_hire` et `posting_stale` peuvent apparaître dès le premier relevé (ce sont des états).
- L'import initial des anciens caches (`detection.jsonl` seulement : la stack des sites) n'a émis **aucun** événement.
  Les offres ne sont pas importées : elles commencent au premier relevé réel du pipeline.
- Fréquence de relecture (`DEFAULT_REFRESH`) : dernier tier A/B chaque jour, sinon site tous les 30 jours et offres
  tous les 7 jours ; une lecture en échec (ou une recherche d'offres non concluante : 429, 5xx, timeout) est retentée
  le lendemain ; une recherche d'offres réussie sans aucun board trouvé n'est refaite que tous les 14 jours (`no_board`),
  tier A/B compris.
- Les requêtes web sont limitées par hôte (`common.py` : 4 simultanées, 0,25 s entre deux débuts) et retentées sur 429/503
  en respectant `Retry-After`.
- Les seuils (2 lectures, 60 jours, +3 offres) sont dans `DEFAULT_SIGNALS` en haut de `db.py`.

## Requêtes d'exemple

**Entreprises qui ont installé HubSpot ce mois-ci**
```sql
SELECT e.day, e.domain, c.name
FROM events e LEFT JOIN companies c USING (domain)
WHERE e.type = 'tool_added' AND e.detail = 'HubSpot' AND e.day >= date('now', 'start of month')
ORDER BY e.day DESC;
```

**Offres RevOps ouvertes depuis plus de 60 jours**
```sql
SELECT name, domain, title, age_days, url
FROM v_open_postings
WHERE category = 'revops' AND age_days > 60
ORDER BY age_days DESC;
```

**Évolution du score d'acme.com**
```sql
SELECT day, tier, score, detail FROM scores WHERE domain = 'acme.com' ORDER BY day, run_id;
```

**Entreprises avec deux portals HubSpot (ou plus)**
```sql
SELECT domain, name, group_concat(substr(tool, 16), ', ') AS portals
FROM v_current_stack
WHERE tool LIKE 'HubSpot portal %'
GROUP BY domain HAVING COUNT(*) >= 2;
```

**Nouveaux tiers A cette semaine**
```sql
SELECT e.day, e.domain, c.name, e.detail
FROM events e LEFT JOIN companies c USING (domain)
WHERE e.type = 'tier_up' AND e.detail LIKE '% → A' AND e.day >= date('now', '-7 days');
```

**Tiers A actuels, avec leur angle**
```sql
SELECT name, domain, score, angle FROM v_latest_scores WHERE tier = 'A' ORDER BY score DESC;
```

**Clients HubSpot qui utilisent aussi un CRM concurrent (consolidation)**
```sql
SELECT h.domain, h.name, group_concat(o.tool, ', ') AS other_crm
FROM v_current_stack h JOIN v_current_stack o ON o.domain = h.domain
WHERE h.tool = 'HubSpot'
  AND o.tool IN ('Salesforce Pardot', 'Salesforce Web-to-Lead', 'Marketo', 'Pipedrive', 'Zoho')
GROUP BY h.domain;
```

**Tout ce qui s'est passé chez une entreprise (fil d'actualité)**
```sql
SELECT day, type, detail FROM events WHERE domain = 'acme.com' ORDER BY day DESC, id DESC;
```

**Entreprises qui recrutent un poste de direction commerciale ce trimestre**
```sql
SELECT e.day, c.name, e.domain, e.detail
FROM events e LEFT JOIN companies c USING (domain)
WHERE e.type = 'leadership_hire' AND e.day >= date('now', '-90 days')
ORDER BY e.day DESC;
```

**Sites injoignables lors de leur dernière lecture**
```sql
SELECT s.domain, s.scanned_at, s.error
FROM site_scans s
WHERE s.ok = 0 AND s.id = (SELECT MAX(id) FROM site_scans WHERE domain = s.domain);
```

**Outils les plus répandus parmi les tiers A et B**
```sql
SELECT st.tool, COUNT(*) AS companies
FROM v_current_stack st JOIN v_latest_scores sc USING (domain)
WHERE sc.tier IN ('A', 'B') AND st.tool NOT LIKE 'HubSpot portal %'
GROUP BY st.tool ORDER BY companies DESC;
```
