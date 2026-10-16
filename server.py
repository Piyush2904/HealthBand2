"""
HealthBand WESAD Live Sensor Ingestion, Auth & Web Server
---------------------------------------------------------
Serves the CalmSignal HealthBand dashboard and provides endpoints:
- Static Web Server: http://localhost:8000
- Authentication:
    POST /api/auth/register
    POST /api/auth/login
    POST /api/auth/logout
    POST /api/auth/forgot-password
- User Profile & Data Isolation:
    GET  /api/user/data (requires token)
    POST /api/user/sync (requires token)
    POST /api/user/profile (requires token)
- Sensor Ingestion:
    POST /api/sensor-data
    GET  /api/sensor-data
    GET  /api/sensor-stream (Server-Sent Events)
- Evidence Knowledge Base:
    GET  /api/chat/evidence
"""

import os
import sys
import json
import time
import hashlib
import secrets
import threading
from http.server import HTTPServer, SimpleHTTPRequestHandler
import urllib.parse

PORT = 8000
HOST = "0.0.0.0"
DATA_DIR = os.path.dirname(os.path.abspath(__file__))
USERS_DB_FILE = os.path.join(DATA_DIR, "users_db.json")

# In-memory users and session cache (persisted to users_db.json)
db_lock = threading.Lock()

def load_users_db():
    if os.path.exists(USERS_DB_FILE):
        try:
            with open(USERS_DB_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print("[DB Load Error]", e)
    return {"users": {}, "sessions": {}, "user_data": {}}

def save_users_db(db):
    try:
        with open(USERS_DB_FILE, "w", encoding="utf-8") as f:
            json.dump(db, f, indent=2)
    except Exception as e:
        print("[DB Save Error]", e)

def hash_password(password, salt=None):
    if not salt:
        salt = secrets.token_hex(16)
    hashed = hashlib.sha256((salt + password).encode('utf-8')).hexdigest()
    return hashed, salt

# Global state for latest live sensor reading
latest_sensor_data = {
    "source": "Initial standby",
    "timestamp": time.time(),
    "hr": 74,
    "hrv": 58,
    "eda": 1.85,
    "st": 33.2,
    "sp": 98,
    "act": "Resting",
    "mv": "Low",
    "amb": 26.5,
    "hum": 55,
    "packets_received": 0
}

data_lock = threading.Lock()
sse_clients = []
sse_lock = threading.Lock()

class HealthBandRequestHandler(SimpleHTTPRequestHandler):
    def end_headers(self):
        # Enable CORS for local network and ESP32 hardware
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS, PUT, DELETE')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type, Authorization')
        self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(200)
        self.end_headers()

    def send_json(self, status_code, data):
        body = json.dumps(data).encode('utf-8')
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def get_auth_token(self):
        auth_header = self.headers.get('Authorization', '')
        if auth_header.startswith('Bearer '):
            return auth_header[7:].strip()
        # Fallback to query param
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)
        if 'token' in qs:
            return qs['token'][0]
        return None

    def get_authenticated_user(self, db):
        token = self.get_auth_token()
        if not token or token not in db["sessions"]:
            return None, None
        user_id = db["sessions"][token]["user_id"]
        user = db["users"].get(user_id)
        return user_id, user

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path == "/" or path == "":
            self.send_response(302)
            self.send_header("Location", "/Health%20Band.html")
            self.end_headers()
            return

        if path == "/api/sensor-data":
            with data_lock:
                copy_data = dict(latest_sensor_data)
            self.send_json(200, copy_data)
            return

        if path == "/api/chat/evidence":
            evidence_file = os.path.join(DATA_DIR, "evidence_base.js")
            if os.path.exists(evidence_file):
                self.send_response(200)
                self.send_header("Content-Type", "application/javascript")
                self.end_headers()
                with open(evidence_file, "rb") as f:
                    self.wfile.write(f.read())
            else:
                self.send_json(404, {"error": "Evidence base file missing"})
            return

        if path == "/api/user/data":
            with db_lock:
                db = load_users_db()
                user_id, user = self.get_authenticated_user(db)
                if not user_id or not user:
                    self.send_json(401, {"error": "Unauthorized session. Please log in."})
                    return
                user_data = db["user_data"].get(user_id, {})
                user_safe = {
                    "id": user_id,
                    "email": user["email"],
                    "name": user["name"],
                    "age": user["age"],
                    "weight": user["weight"],
                    "height": user["height"],
                    "created_at": user.get("created_at")
                }
                self.send_json(200, {"user": user_safe, "data": user_data})
            return

        if path == "/api/sensor-stream":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "keep-alive")
            self.end_headers()

            with sse_lock:
                sse_clients.append(self.wfile)

            with data_lock:
                init_msg = f"data: {json.dumps(latest_sensor_data)}\n\n".encode('utf-8')
            try:
                self.wfile.write(init_msg)
                self.wfile.flush()
                while True:
                    time.sleep(1.0)
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
            except (ConnectionResetError, BrokenPipeError):
                pass
            finally:
                with sse_lock:
                    if self.wfile in sse_clients:
                        sse_clients.remove(self.wfile)
            return

        # Default static file handler
        return super().do_GET()

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        content_length = int(self.headers.get('Content-Length', 0))
        post_body = self.rfile.read(content_length).decode('utf-8') if content_length > 0 else "{}"

        try:
            req_data = json.loads(post_body)
        except Exception as e:
            self.send_json(400, {"error": "Invalid JSON format", "details": str(e)})
            return

        # 1. User Registration
        if path == "/api/auth/register":
            name = str(req_data.get("name", "")).strip()
            email = str(req_data.get("email", "")).strip().lower()
            password = str(req_data.get("password", ""))
            age = req_data.get("age")
            weight = req_data.get("weight")
            height = req_data.get("height")

            if len(name) < 2:
                self.send_json(400, {"error": "Please enter a valid full name (at least 2 characters)."})
                return
            if not email or "@" not in email or "." not in email:
                self.send_json(400, {"error": "Please enter a valid email address."})
                return
            if len(password) < 6:
                self.send_json(400, {"error": "Password must be at least 6 characters."})
                return

            try:
                age_val = int(age)
                if age_val < 5 or age_val > 120:
                    raise ValueError()
            except:
                self.send_json(400, {"error": "Age must be a valid number between 5 and 120."})
                return

            try:
                wt_val = float(str(weight).replace(",", "."))
                ht_val = float(str(height).replace(",", "."))
                if wt_val < 20 or wt_val > 300 or ht_val < 50 or ht_val > 250:
                    raise ValueError()
            except:
                self.send_json(400, {"error": "Please provide realistic weight (20-300 kg) and height (50-250 cm)."})
                return

            with db_lock:
                db = load_users_db()
                for uid, u in db["users"].items():
                    if u.get("email") == email:
                        self.send_json(400, {"error": "An account with this email already exists. Please log in."})
                        return

                user_id = "usr_" + secrets.token_hex(6)
                pwd_hash, salt = hash_password(password)

                user_record = {
                    "id": user_id,
                    "email": email,
                    "name": name,
                    "age": age_val,
                    "weight": wt_val,
                    "height": ht_val,
                    "pwd_hash": pwd_hash,
                    "salt": salt,
                    "created_at": time.time()
                }

                token = "tok_" + secrets.token_hex(16)
                db["users"][user_id] = user_record
                db["sessions"][token] = {"user_id": user_id, "created_at": time.time()}
                db["user_data"][user_id] = {
                    "profile": {
                        "name": name, "email": email, "age": age_val, "wt": wt_val, "ht": ht_val
                    },
                    "contacts": [],
                    "journal": [],
                    "events": [],
                    "sessions": [],
                    "base": {
                        "days": 0,
                        "n": {"hr": 0, "hrv": 0, "eda": 0, "st": 0, "sp": 0},
                        "m": {"hr": 78.5, "hrv": 62.0, "eda": 1.85, "st": 33.0, "sp": 98},
                        "v": {"hr": 25.0, "hrv": 120.0, "eda": 0.8, "st": 0.3, "sp": 0.5}
                    },
                    "chat_history": []
                }
                save_users_db(db)

                user_safe = {
                    "id": user_id, "email": email, "name": name,
                    "age": age_val, "weight": wt_val, "height": ht_val
                }
                self.send_json(200, {
                    "status": "success",
                    "message": "Account created successfully.",
                    "user": user_safe,
                    "token": token
                })
                return

        # 2. User Login
        if path == "/api/auth/login":
            email = str(req_data.get("email", "")).strip().lower()
            password = str(req_data.get("password", ""))

            with db_lock:
                db = load_users_db()
                matched_user = None
                matched_uid = None
                for uid, u in db["users"].items():
                    if u.get("email") == email:
                        matched_user = u
                        matched_uid = uid
                        break

                if not matched_user:
                    self.send_json(401, {"error": "Invalid email or password."})
                    return

                check_hash, _ = hash_password(password, matched_user["salt"])
                if check_hash != matched_user["pwd_hash"]:
                    self.send_json(401, {"error": "Invalid email or password."})
                    return

                token = "tok_" + secrets.token_hex(16)
                db["sessions"][token] = {"user_id": matched_uid, "created_at": time.time()}
                save_users_db(db)

                user_safe = {
                    "id": matched_uid,
                    "email": matched_user["email"],
                    "name": matched_user["name"],
                    "age": matched_user["age"],
                    "weight": matched_user["weight"],
                    "height": matched_user["height"]
                }
                user_data = db["user_data"].get(matched_uid, {})
                self.send_json(200, {
                    "status": "success",
                    "user": user_safe,
                    "token": token,
                    "data": user_data
                })
                return

        # 3. User Logout
        if path == "/api/auth/logout":
            token = self.get_auth_token()
            with db_lock:
                db = load_users_db()
                if token and token in db["sessions"]:
                    del db["sessions"][token]
                    save_users_db(db)
            self.send_json(200, {"status": "success", "message": "Logged out successfully."})
            return

        # 4. Forgot Password Flow
        if path == "/api/auth/forgot-password":
            email = str(req_data.get("email", "")).strip().lower()
            new_password = str(req_data.get("new_password", ""))
            with db_lock:
                db = load_users_db()
                matched_user = None
                for uid, u in db["users"].items():
                    if u.get("email") == email:
                        matched_user = u
                        break

                if not matched_user:
                    # Generic response for privacy
                    self.send_json(200, {"status": "info", "message": "If an account matches this email, reset instructions have been applied."})
                    return

                if new_password:
                    if len(new_password) < 6:
                        self.send_json(400, {"error": "New password must be at least 6 characters."})
                        return
                    pwd_hash, salt = hash_password(new_password)
                    matched_user["pwd_hash"] = pwd_hash
                    matched_user["salt"] = salt
                    save_users_db(db)
                    self.send_json(200, {"status": "success", "message": "Password has been successfully updated. You can now log in."})
                    return
                else:
                    self.send_json(200, {"status": "verified", "message": "Account verified. Please enter your new password."})
                    return

        # 5. User Profile Update
        if path == "/api/user/profile":
            with db_lock:
                db = load_users_db()
                user_id, user = self.get_authenticated_user(db)
                if not user_id or not user:
                    self.send_json(401, {"error": "Unauthorized"})
                    return

                if "name" in req_data and len(str(req_data["name"]).strip()) >= 2:
                    user["name"] = str(req_data["name"]).strip()
                if "age" in req_data:
                    try: user["age"] = int(req_data["age"])
                    except: pass
                if "weight" in req_data:
                    try: user["weight"] = float(str(req_data["weight"]).replace(",", "."))
                    except: pass
                if "height" in req_data:
                    try: user["height"] = float(str(req_data["height"]).replace(",", "."))
                    except: pass

                # Sync to user_data profile as well
                if user_id in db["user_data"]:
                    p = db["user_data"][user_id].get("profile", {})
                    p["name"] = user["name"]
                    p["age"] = user["age"]
                    p["wt"] = user["weight"]
                    p["ht"] = user["height"]
                    db["user_data"][user_id]["profile"] = p

                save_users_db(db)
                user_safe = {
                    "id": user_id, "email": user["email"], "name": user["name"],
                    "age": user["age"], "weight": user["weight"], "height": user["height"]
                }
                self.send_json(200, {"status": "success", "user": user_safe})
                return

        # 6. User Data Sync (contacts, journal, sessions, events, baseline)
        if path == "/api/user/sync":
            with db_lock:
                db = load_users_db()
                user_id, user = self.get_authenticated_user(db)
                if not user_id or not user:
                    self.send_json(401, {"error": "Unauthorized"})
                    return

                user_store = db["user_data"].get(user_id, {})
                for k in ["contacts", "journal", "events", "sessions", "base", "chat_history", "set"]:
                    if k in req_data:
                        user_store[k] = req_data[k]
                db["user_data"][user_id] = user_store
                save_users_db(db)
                self.send_json(200, {"status": "synced", "timestamp": time.time()})
                return

        # 7. Hardware Sensor Ingestion
        if path == "/api/sensor-data":
            with data_lock:
                latest_sensor_data["timestamp"] = time.time()
                latest_sensor_data["source"] = req_data.get("source", "Hardware Sensor")
                latest_sensor_data["packets_received"] += 1
                for k in ["hr", "hrv", "eda", "st", "sp", "act", "mv", "amb", "hum"]:
                    if k in req_data:
                        latest_sensor_data[k] = req_data[k]
                data_copy = dict(latest_sensor_data)

            msg = f"data: {json.dumps(data_copy)}\n\n".encode('utf-8')
            dead_clients = []
            with sse_lock:
                for client in sse_clients:
                    try:
                        client.write(msg)
                        client.flush()
                    except:
                        dead_clients.append(client)
                for d in dead_clients:
                    if d in sse_clients:
                        sse_clients.remove(d)

            res_body = {"status": "received", "packet": latest_sensor_data["packets_received"]}
            self.send_json(200, res_body)
            return

        self.send_json(404, {"error": "Endpoint not found"})

def run_server():
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    server = HTTPServer((HOST, PORT), HealthBandRequestHandler)
    print(f"=======================================================")
    print(f"  HealthBand WESAD Server Running on http://localhost:{PORT}")
    print(f"  - App: http://localhost:{PORT}/Health%20Band.html")
    print(f"  - Auth API: /api/auth/register, /api/auth/login")
    print(f"  - Sensor API: /api/sensor-data, /api/sensor-stream")
    print(f"  - Evidence API: /api/chat/evidence")
    print(f"=======================================================")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down server...")
        server.server_close()

if __name__ == "__main__":
    run_server()
