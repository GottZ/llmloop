import requests
import json
from db import get_active_backend, get_model_config

class LLMError(Exception):
    def __init__(self, message, code, status_code=None, raw_response=None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status_code = status_code
        self.raw_response = raw_response

def list_models(backend):
    url = f"{backend['base_url'].rstrip('/')}/models"
    headers = {"Authorization": f"Bearer {backend['api_key']}"}
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code != 200:
             raise LLMError(f"Failed to list models: {resp.text}", "model_list_error", resp.status_code, resp.text)
        data = resp.json()
        return data.get("data", [])
    except requests.RequestException as e:
        raise LLMError(f"Network error listing models: {str(e)}", "network_error")

def call_llm(role, messages, tool_choice="none"):
    backend = get_active_backend()
    if not backend:
        raise LLMError("No active LLM backend configured", "no_backend")
    
    config = get_model_config(backend['id'], role)
    if not config:
        raise LLMError(f"No model configuration for role '{role}'", "no_config")

    url = f"{backend['base_url'].rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {backend['api_key']}",
        "Content-Type": "application/json"
    }
    
    params = config['parameters'] or {}
    payload = {
        "model": config['model_name'],
        "messages": messages,
        "temperature": params.get("temperature", 0.8),
        "max_tokens": params.get("max_tokens", 65536),
        "top_p": params.get("top_p", 0.9),
    }
    
    # Merge extra params if they exist
    if "extra_body" in params:
        payload.update(params["extra_body"])

    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=60)
        if resp.status_code != 200:
            raise LLMError(f"LLM API Error: {resp.text[:200]}...", "api_error", resp.status_code, resp.text)
        
        data = resp.json()
        return data['choices'][0]['message']['content']
    except requests.RequestException as e:
        raise LLMError(f"Network error calling LLM: {str(e)}", "network_error")
    except (KeyError, IndexError, json.JSONDecodeError) as e:
        raise LLMError(f"Malformed response from LLM: {str(e)}", "bad_response")
