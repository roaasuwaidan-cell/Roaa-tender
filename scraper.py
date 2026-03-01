def scrape_tenders():
    headers = {'User-Agent': 'Mozilla/5.0'}
    response = requests.get(URL, headers=headers)
    soup = BeautifulSoup(response.text, 'html.parser')
    
    found_ids = []
    # Search for any text containing "Tender" to be safe
    elements = soup.find_all(string=lambda t: "Tender" in t)
    
    for element in elements:
        # NUPCO often puts the ID in a <span> or <td> following the label
        parent = element.find_parent()
        if parent:
            text = parent.get_text(strip=True)
            # Simple logic to grab digits/ID formats
            if any(char.isdigit() for char in text):
                found_ids.append(text)
    
    print(f"Debug: Found these potential IDs: {found_ids}") # This helps you debug in GitHub logs
    return list(set(found_ids))
