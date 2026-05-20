import json
import os
import threading

DATA_FILE = "/root/sniper/clients.json"
# Fallback for local development if the Linux VPS path does not exist
if not os.path.exists(os.path.dirname(DATA_FILE)):
    DATA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "clients.json")

db_lock = threading.Lock()

def load_clients():
    """Loads all clients from the database file."""
    with db_lock:
        if not os.path.exists(DATA_FILE):
            return {}
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"Error loading database: {e}")
            return {}

def save_clients(clients):
    """Saves the entire clients dictionary to the database file atomically."""
    with db_lock:
        try:
            os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)
            temp_file = DATA_FILE + ".tmp"
            with open(temp_file, "w", encoding="utf-8") as f:
                json.dump(clients, f, indent=2, ensure_ascii=False)
            # On Windows, os.rename fails if target already exists, so remove it first
            if os.name == "nt" and os.path.exists(DATA_FILE):
                os.remove(DATA_FILE)
            os.rename(temp_file, DATA_FILE)
            return True
        except Exception as e:
            print(f"Error saving database: {e}")
            if os.path.exists(temp_file):
                try:
                    os.remove(temp_file)
                except:
                    pass
            return False

def save_client(client_id, client_obj):
    """Saves a single client object by updating it in the dictionary."""
    clients = load_clients()
    clients[client_id] = client_obj
    return save_clients(clients)

def delete_client(client_id):
    """Deletes a client from the database by ID."""
    clients = load_clients()
    if client_id in clients:
        del clients[client_id]
        return save_clients(clients)
    return False
