# Synthetic IT-access data

All invented. A second, unrelated workflow to show the engine is not specific to contracts.

| File | What it shows |
|---|---|
| `ticket_wiki_read.md` | Read access to the wiki: allowed without approval. |
| `ticket_prod_admin.md` | Admin access to the production database: needs a `security_admin` approver. |
| `iam_seed.json` | The mock identity system's users. |

Objectives to try (upload a ticket as the document):

1. `Process this access request: look up the requester and grant the requested access.`
2. `Grant sam.okafor@acme.example admin access to prod_db.` (needs approval)
3. `Grant someone@gmail.example read access to the wiki.` (denied: not a company account)
