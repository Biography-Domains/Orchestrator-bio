# Orchestrator-bio

Bio-specific orchestrator for generating and maintaining biography assets:
- Static bio sites (1 repo per domain under `Biography-Domains/*-bio`)
- `hyperlink-bio` API (votes/comments + other interactive features)
- Domain + GitHub Pages + Cloudflare automation
- Scheduled refresh jobs (internal scheduler + worker)

## Quickstart (dev)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Orchestrator API
uvicorn services.orchestrator_bio_api.main:app --reload --port 8030

# hyperlink-bio API
uvicorn services.hyperlink_bio_api.main:app --reload --port 8020
```


