import requests
import json

# Test the API endpoint
url = 'http://127.0.0.1:8001/api/v1/user-tools/completed-reports/139bc18c-b7b7-4491-a676-6563165fb8a0/latest-html?source=system'

# Use a dummy token (won't work but we just want to see the response structure)
headers = {'Authorization': 'Bearer dummy'}

response = requests.get(url, headers=headers, timeout=5)
print(f"Status: {response.status_code}")

if response.status_code == 200:
    data = response.json()
    html = data.get('report_html', '')

    if 'MEDICAL CLAIM REPORT' in html:
        print("✅ API is returning the NEW auto-generated report!")
    elif 'HEALTH CLAIM' in html:
        print("❌ API is still returning the OLD VerifAI report")
    else:
        print("? Unknown report format")

    print(f"\nReport starts with: {html[:100]}")
else:
    print(f"Error: {response.text[:200]}")
