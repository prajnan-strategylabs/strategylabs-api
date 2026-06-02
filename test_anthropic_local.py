import urllib.request
import os
import json
import urllib.error

key = os.environ.get('AI_API_KEY', '').strip().strip('"').strip("'")

print("--- DIAGNOSTICS: KEY CHECK ---")
print("Key length:", len(key))
print("Key starts with sk-ant:", key.startswith("sk-ant"))
print("Key prefix:", key[:25])
print("Key suffix:", key[-10:])

# 1. Query available models
models_url = 'https://api.anthropic.com/v1/models'
headers = {
    'x-api-key': key,
    'anthropic-version': '2023-06-01'
}

req = urllib.request.Request(models_url, headers=headers)
try:
    print("\n--- DIAGNOSTICS: GET MODELS LIST ---")
    with urllib.request.urlopen(req) as res:
        print("Success response:")
        models_data = json.loads(res.read().decode())
        print(json.dumps(models_data, indent=2))
except urllib.error.HTTPError as e:
    print("HTTP Error code:", e.code)
    print("HTTP Error body:")
    print(e.read().decode())
except Exception as e:
    print("System error:", e)

# 2. Test query models individually in messages
messages_url = 'https://api.anthropic.com/v1/messages'
models_to_test = [
    'claude-sonnet-4-6',
    'claude-opus-4-7',
    'claude-3-5-sonnet-latest',
    'claude-3-5-sonnet-20241022'
]


for model in models_to_test:
    print(f"\n--- Testing model messages endpoint: {model} ---")
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
    
    req = urllib.request.Request(messages_url, data=json.dumps(payload).encode(), headers=headers)
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

