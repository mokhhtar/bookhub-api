import urllib.request
import urllib.error
import json

req = urllib.request.Request(
    'https://bookhub-api-hnv7.onrender.com/summary',
    data=json.dumps({'title': 'Atomic Habits', 'author': 'James Clear', 'depth': 'quick'}).encode(),
    headers={'Content-Type': 'application/json'}
)

try:
    response = urllib.request.urlopen(req)
    print("Success:", response.read().decode())
except urllib.error.HTTPError as e:
    print("Error code:", e.code)
    print("Body:", e.read().decode())
