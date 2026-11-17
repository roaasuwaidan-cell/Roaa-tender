# nupco-tender-tracker

Hourly GitHub Actions job that watches https://www.nupco.com/tenders/tenders-list/ and
sends a Telegram message for every **new** tender and every **updated** one
(status / submission deadline / bid-opening change).

Secrets required: `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID`.

Optional env: `NOTIFY_ON_FIRST_RUN=true` to receive every current tender on the very first run.

State lives in `state.json` (auto-committed). `last_seen.txt` is only read once, to migrate.
Run `python test_scraper.py` for the offline tests.

## v2.1 fixes
- NUPCO's own pages spell the label "Tendr Id"; parser now anchors on the ID pattern (NDP0846/26) and accepts every label spelling.
- Off-list tenders are fetched/parsed properly (page, REST content, or ID anywhere in text). If the ID still can't be found they are held 24h and retried, instead of being sent as "No ID yet".
- When a failed scrape happens, the Telegram health alert shows the real error and the received HTML is uploaded as the `scrape-debug` artifact.
