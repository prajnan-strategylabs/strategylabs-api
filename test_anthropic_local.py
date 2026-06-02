import urllib.request
import os
import json
import urllib.error

url = 'https://api.anthropic.com/v1/messages'
key = os.environ.get('AI_API_KEY', '').strip().strip('"').strip("'")

print("Target URL:", url)
print("Key length:", len(key))
print("Key starts with sk-ant:", key.startswith("sk-ant"))
print("Key prefix:", key[:25])
print("Key suffix:", key[-10:])


models_to_test = [
    'claude-3-5-sonnet-latest',
    'claude-3-5-sonnet-20241022',
    'claude-3-5-haiku-20241022',
    'claude-3-haiku-20240307',
    'claude-3-opus-20240229'
]

for model in models_to_test:
    print(f"\n--- Testing model: {model} ---")
    headers = {
        'x-api-key': key,
        'anthropic-version': '2023-06-01',
        'content-type': 'application/json'
    }
    payload = {
        'model': model,
        'max_tokens': 100,
        'messages': [{'role': 'user', 'content': 'Say hello.'}]
    }
    
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=headers)
    try:
        with urllib.request.urlopen(req) as res:
            print(f"SUCCESS with {model}!")
            print(res.read().decode())
    except urllib.error.HTTPError as e:
        print(f"HTTP Error code for {model}:", e.code)
        body = e.read().decode()
        print(f"Body: {body}")
    except Exception as e:
        print(f"System error for {model}:", e)

