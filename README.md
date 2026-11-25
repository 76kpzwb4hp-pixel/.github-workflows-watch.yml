# Kereby watcher

Checks https://kereby.dk/bolig/ every 5 minutes and sends a push
notification (via ntfy) when a new apartment appears that is:

- under 14.000 kr./md.
- available (not reserved or rented out)
- in Nørrebro, Vesterbro or Frederiksberg

## How it works
- `kereby_watch.py` scrapes the page and compares it to `seen.json`
- `.github/workflows/watch.yml` runs it on a schedule
- Secrets (set under Settings > Secrets and variables > Actions):
  `NTFY_TOPIC`, and optionally `SMTP_USER`, `SMTP_PASS`, `EMAIL_TO`

## Changing the search
Edit `MAX_RENT` and `AREAS` at the top of `kereby_watch.py`.

## Run manually
Actions tab > Kereby watcher > Run workflow
