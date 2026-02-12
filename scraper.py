import requests
from bs4 import BeautifulSoup
import os

# Configuration
URL = "https://www.nupco.com/en/tenders/"
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
FILE_NAME = "last_seen.txt"

def send_telegram_message(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
    requests.post(url, json=payload)

def scrape_tenders():
    headers = {'User-Agent': 'Mozilla/5.0'}
    response = requests.get(URL, headers=headers)
    soup = BeautifulSoup(response.text, 'html.parser')
    
    found_ids = []
    # This looks specifically for the Tender ID text in the NUPCO table
    for element in soup.find_all(string=lambda t: "Tender ID" in t):
        tender_id_val = element.find_next().text.strip() if element.find_next() else ""
        if tender_id_val and len(tender_id_val) > 3:
            found_ids.append(tender_id_val)
    
    return list(set(found_ids))

# 1. Load previous IDs
if os.path.exists(FILE_NAME):
    with open(FILE_NAME, "r") as f:
        content = f.read().splitlines()
        seen_ids = set(content)
else:
    seen_ids = set()

# 2. Get current IDs
current_ids = scrape_tenders()

# 3. Find new ones
new_tenders = [tid for tid in current_ids if tid not in seen_ids]

# FORCE RUN: If file only contains '0', treat everything as new for the first time
if '0' in seen_ids:
    new_tenders = current_ids

if new_tenders:
    message = "🚨 *New NUPCO Tenders Found!* 🚨\n\n" + "\n".join([f"- {tid}" for tid in new_tenders])
    send_telegram_message(message)
    
    with open(FILE_NAME, "w") as f:
        f.write("\n".join(current_ids))
    print(f"Sent {len(new_tenders)} tenders to Telegram.")
else:
    print("No new tenders found.")

