# Signaux LinkedIn et news : état des lieux du marché

*Novembre 2026. Les prix viennent surtout de comparatifs écrits par les éditeurs eux-mêmes, et ils se contredisent souvent : ce sont des ordres de grandeur à confirmer sur la page de tarifs de chaque outil.*

## Contexte : pourquoi de nouveaux signaux

Constats sur le run du 2026-11-11 (215 entreprises scorées) :

- **Le tier B correspond surtout au profil de l'entreprise, pas à un besoin actuel.** 80 entreprises ont exactement 9 points : SaaS (+3), 20–200 salariés (+2), HubSpot (+2) et Ontario (+2). Rien ne départage ces 80 comptes.
- **Les signaux d'intention sont rares.** Offre RevOps ouverte : 2 entreprises. 3+ offres sales/marketing : 10. Autre CRM visible : 3. Job board trouvé : 38 sur 215. Il n'y a que 4 entreprises en A.
- **La base d'historique produit des événements que le score n'utilise pas** : `leadership_hire`, `posting_stale`, `tool_added`.
- **Le score ne tient pas compte de l'ancienneté d'un signal** : une levée d'il y a 2 semaines vaut autant qu'une levée d'il y a 11 mois.

Pistes retenues : l'**organigramme LinkedIn** des équipes revenue et les **news de l'entreprise**. On les intégrera ensuite dans un score en deux parties, « Profil » et « Timing ».

### Signaux visés

**Organigramme LinkedIn** (photos régulières des équipes Sales, Marketing, RevOps et direction, comparées entre elles) :

| Événement | Comment on le détecte |
|---|---|
| `leader_joined` : nouveau CRO, VP Sales/Marketing, Head of Revenue | Date de début du poste de moins de 90 jours |
| `revops_left` : la personne RevOps/Sales Ops part | Présente avant, absente de deux photos de suite |
| `sales_team_growth` | Effectif sales qui augmente de 30 % ou plus entre deux photos |
| `first_sales_hire` | Premier SDR ou AE dans une entreprise encore portée par ses fondateurs |
| `no_revops_coverage` : manque structurel | 10 commerciaux ou plus et personne en RevOps ou Ops |

**News** : acquisition (achetée ou acheteuse), expansion (lancement aux États-Unis, nouveau bureau), nouveau produit ou passage à une offre entreprise, partenariats, nominations, licenciements.

État des données : 92 des 98 comptes A et B ont déjà leur page LinkedIn entreprise dans le CSV (trouvée par `find_linkedin.py`).

## 1. Données LinkedIn « brutes » : on fabrique le signal soi-même

| Outil | Modèle | Prix indicatif | Pour l'organigramme |
|---|---|---|---|
| **HarvestAPI (via Apify)** | Lecture en direct à la demande | Environ $4 pour 1 000 profils (Short), $8 (Full), + $0,02 par entreprise | ✅ Filtre par poste, date de début du poste, historique d'expérience |
| **Bright Data** | Lecture en direct ou bases déjà constituées | Environ $1,50 pour 1 000 enregistrements ; bases à partir d'environ $250 | ✅ Mais plus lourd à intégrer |
| **Coresignal** | Base déjà constituée, avec historique | À partir d'environ $49/mois | ✅✅ Historique des postes, utile pour voir les mouvements passés |
| **People Data Labs** | Base agrégée | Plan Pro environ $98/mois | ✅ Recherche par entreprise et par poste, avec date de début ; données moins fraîches |
| **Enrich Layer, ScrapingDog, Piloterr** | Remplaçants de Proxycurl | Variable | Plutôt pour enrichir un profil à la fois |

C'est la solution la moins chère et la plus souple. En contrepartie, c'est nous qui calculons les changements et produisons les événements. La base d'historique (`data/history.db`) fait déjà exactement ça.

### Détail HarvestAPI : acteur `harvestapi/linkedin-company-employees`

- **Entrées utiles** : `companies` (URL des pages LinkedIn entreprise), `jobTitles` (filtre sur le poste actuel), `excludeCurrentJobTitles`, `seniorityLevelIds`, `functionIds`, `locations`, `maxItemsPerCompany`, `recentlyChangedJobs`, `profileScraperMode` (Short ou Full), `companyBatchMode` (« all at once », limité à 10 entreprises, ou « one by one », jusqu'à 1 000).
- **Sorties utiles (mode Full)** : `linkedinUrl` et `headline`. Le poste actuel se trouve dans `experience[]`, c'est l'entrée dont `endDate` vaut « Present ». Chaque entrée a un `position` et un `startDate {month, year}`.
- **Inconnue** : la doc ne dit pas si le mode Short donne la date de début du poste. Si oui, le coût est divisé par deux.
- **Estimation** : 92 comptes à environ 15 profils, en Full, ça fait à peu près $13 par photo. Une photo par mois, plus une photo par semaine limitée aux postes de direction, reste sous $20 par mois.
- **Limites** : 2 500 profils maximum par recherche. Le plan gratuit d'Apify est limité à 10 exécutions au total.
- **Risques** : ces services passent outre les conditions d'utilisation de LinkedIn. Comme ils fonctionnent sans cookies, notre propre compte n'est pas exposé, mais la source peut casser du jour au lendemain. Côté données personnelles (LPRPDE), on garde le strict minimum : poste, date de début, URL du profil.

## 2. Outils « signaux clés en main » : le signal est déjà calculé

| Outil | Points forts | Prix indicatif |
|---|---|---|
| **Trigify** | Signaux LinkedIn : engagement sur des posts, changements de poste, posts de dirigeants | **Environ $40/mois** pour l'offre Starter, $199 pour la plus haute |
| **Clay** | On assemble sa propre chaîne d'enrichissement et ses signaux (offres d'emploi, nouvelles embauches, news) | Gratuit en usage limité ; environ $185–495/mois |
| **Common Room** | Signaux communautaires et sur le site, plus le côté personnes | Environ $1 000–2 500/mois |
| **UserGems** | Champion du suivi des changements de poste | Environ **$40 000/an** + mise en place à partir de $3 000 |

Ces outils sont faits pour des équipes commerciales. Seuls Trigify et Clay sont à notre portée, et ni l'un ni l'autre ne se branche simplement sur un pipeline Python.

## 3. News sur les entreprises

| Outil | Points forts | Prix indicatif |
|---|---|---|
| **PredictLeads** ⭐ | Transforme les news en **événements typés** (levée, expansion, nomination, acquisition, lancement), plus les offres d'emploi et les technologies utilisées | **100 crédits gratuits par mois**, puis environ $0,04 par crédit |
| **GDELT** | Gratuit, usage commercial autorisé (avec citation) | $0, mais données brutes : beaucoup de bruit, pas de support |
| **Google News RSS** | Gratuit, simple à lire | $0, mais tout le travail de désambiguïsation reste à faire |
| **Event Registry (NewsAPI.ai)** | Regroupe les articles et identifie les entités nommées | $90 à $500+/mois |
| **NewsAPI.org** | Généraliste | $449/mois en production : trop cher pour ce que ça apporte |

PredictLeads règle le problème des noms ambigus comme « Beacon » ou « CoLab » : ses événements sont rattachés au domaine de l'entreprise, pas à son nom.

Autres sources gratuites à envisager : le flux RSS du blog ou de la page presse sur le site de l'entreprise, et une recherche élargie sur BetaKit (déjà branché pour les levées).

## Recommandation (solo, environ 100 comptes A et B, pipeline Python sans dépendance)

1. **News : PredictLeads.** Le quota gratuit suffit peut-être pour les comptes A et B. Les événements se rattachent par domaine, comme le reste de la base. Il recoupe aussi ce qu'on détecte déjà (offres d'emploi, technologies), ce qui permet de contrôler notre propre détection.
2. **Organigramme : HarvestAPI, ou People Data Labs si on préfère un abonnement fixe.** HarvestAPI coûte environ $15–20 par mois avec des données fraîches. PDL donne un coût fixe, mais des données moins à jour, ce qui gêne pour repérer une arrivée de moins de 90 jours.
3. **Écartés pour l'instant :** UserGems et Common Room (prix d'entreprise) et NewsAPI.org (trop cher). Clay ne vaut le coup que si on veut construire ses chaînes d'enrichissement dans un outil visuel plutôt qu'en code.

### Prochaines étapes

1. Ouvrir les comptes gratuits PredictLeads et Apify.
2. Essai sur 10 comptes A pour juger la qualité réelle des données (filtre par poste, mode Short ou Full, coût réel).
3. Table `people` et nouveaux types d'événements dans `history.db`.
4. Refonte du score Profil / Timing, qui intègre ces événements.

## Sources

- [HarvestAPI – LinkedIn Company Employees (Apify)](https://apify.com/harvestapi/linkedin-company-employees/pricing)
- [Unipile – Best LinkedIn API 2026](https://www.unipile.com/best-linkedin-api/)
- [SocialCrawl – LinkedIn data APIs pricing](https://www.socialcrawl.dev/blog/best-linkedin-data-apis-2026)
- [LinkFinderAI – 12 best LinkedIn APIs](https://linkfinderai.com/best-linkedin-api)
- [Datacircle – LinkedIn company data API](https://datacircle.dev/best-linkedin-company-data-api)
- [Datacircle – Best LinkedIn API pricing](https://datacircle.dev/best-linkedin-api)
- [APISerpent – LinkedIn data APIs](https://apiserpent.com/blog/best-linkedin-data-apis-2026)
- [SocialFetch – partner vs public](https://www.socialfetch.dev/blog/best-linkedin-data-apis-2026)
- [InsightSocial – LinkedIn APIs pricing](https://www.insightsocial.app/blog/best-linkedin-data-apis-2026)
- [Overloop – Buying signals software](https://overloop.com/blog/buying-signals-software)
- [Overloop – Trigify alternatives](https://overloop.com/blog/trigify-alternatives)
- [Overloop – Common Room alternatives](https://overloop.com/blog/common-room-alternatives)
- [Leexi – Signal-based selling tools](https://www.leexi.ai/en/sales-enablement/prospecting/best-signal-based-selling-tools/)
- [UserGems – AI sales signal tools](https://www.usergems.com/blog/best-ai-sales-signal-tools-in-2026)
- [Satellyte – Trigify alternatives](https://satellyte.ai/blog/trigify-alternatives-2026)
- [ModernInbound – Common Room vs UserGems](https://moderninbound.com/blog/common-room-vs-usergems)
- [Avina – Buying signals tools](https://www.avina.io/blog/best-buying-signals-tools-2026)
- [GetAOB – Buying signal tools](https://getaob.com/buying-signal-tools)
- [Autobound – B2B news APIs](https://www.autobound.ai/blog/best-b2b-news-apis)
- [Newscatcher – Best news APIs](https://www.newscatcherapi.com/blog-posts/best-news-apis)
- [Geekflare – Global news APIs](https://geekflare.com/guides/global-news-api/)
- [Forage – News APIs compared](https://forage.ai/blog/best-news-api/)
- [PredictLeads – Company news data providers](https://predictleads.com/blog/company-news-data-providers-2026/)
- [Currents – News APIs guide](https://currentsapi.services/en/blog/best-news-apis)
- [Perigon – Best news APIs](https://perigon.io/blog/best-news-apis-in-2026)
- [MarketIntelligenceTools – News API pricing](https://marketintelligencetools.com/reports/news-api-pricing/)
- [DataResearchTools – NewsAPI vs Mediastack vs GDELT](https://dataresearchtools.com/best-news-apis-comparison/)
