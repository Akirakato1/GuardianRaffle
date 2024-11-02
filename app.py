
import json
from flask import Flask, redirect, request, session, url_for, render_template, jsonify, send_file
import requests
import os
from flask_socketio import SocketIO, emit
import tempfile
from rethinkdb import RethinkDB
import threading
import time

app = Flask(__name__)
app.secret_key = os.urandom(24)
#socketio = SocketIO(app)  # Initialize SocketIO for real-time updates
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="eventlet")

CLIENT_ID = os.getenv("CLIENT_ID")
CLIENT_SECRET = os.getenv("CLIENT_SECRET")
REDIRECT_URI = "https://guardian-raffle-c0b2ceda6634.herokuapp.com/callback"
document_id=""

API_BASE_URL = "https://discord.com/api"
AUTHORIZATION_BASE_URL = "https://discord.com/api/oauth2/authorize"
TOKEN_URL = "https://discord.com/api/oauth2/token"

DATA_FILE = "user_data.json"
MAX_SELECTIONS = 10  # Maximum cells a user can select

r = RethinkDB()  # Add this line if using the legacy driver
conn = r.connect(
                host=os.getenv('RETHINKDB_HOST'),
                port=int(os.getenv('RETHINKDB_PORT')),
                db=os.getenv('RETHINKDB_NAME'),
                user=os.getenv('RETHINKDB_USERNAME'),
                password=os.getenv('RETHINKDB_PASSWORD')
            )

#def load_data():
#    try:
#        with open(DATA_FILE, "r") as file:
#            return json.load(file)
#    except (FileNotFoundError, json.JSONDecodeError):
#        return {}

#def save_data(data):
#    with open(DATA_FILE, "w") as file:
#        json.dump(data, file, indent=4)
    
def reconnect():
    global conn
    if conn is None or not conn.is_open():
        print("Reconnecting to RethinkDB...")
        conn = r.connect(
            host=os.getenv('RETHINKDB_HOST'),
            port=int(os.getenv('RETHINKDB_PORT')),
            db=os.getenv('RETHINKDB_NAME'),
            user=os.getenv('RETHINKDB_USERNAME'),
            password=os.getenv('RETHINKDB_PASSWORD')
        )
        print("Reconnected to RethinkDB")

# Function to maintain the connection
def maintain_connection():
    global conn
    while True:
        try:
            if conn is None or not conn.is_open():
                print("Reconnecting to RethinkDB...")
                reconnect()
            else:
                # Optional: Ping the server to keep the connection active
                r.now().run(conn)
        except Exception as e:
            print("Error maintaining connection:", e)
        time.sleep(60)  # Wait for 1 minute before checking again

threading.Thread(target=maintain_connection, daemon=True).start()

def load_data():
    # Convert the cursor to a list to make it JSON-serializable
    global conn
    cursor = r.table('user_data').run(conn)
    data = list(cursor)[0]  # Now `data` is a JSON-serializable Python list
    global document_id
    document_id=data["id"]
    data.pop('id', None)
    return data  # This can be returned to the route or processed further

def save_data(data):
    global document_id
    global conn
    # Insert or update each top-level key as a document in RethinkDB
    r.table('user_data').get(document_id).update(data).run(conn)


@app.route('/verify-data')
def verify_import():
    global conn
    data = list(r.table('user_data').run(conn))
    return jsonify(data)

@app.route("/")
def loading_page():
    reconnect()
    return render_template('loading.html') 
    
@app.route("/home")
def home():
    user = session.get('user')
    user_id = user["id"] if user else None
    user_data = load_data()
    current_user_cells = []
    other_selected_cells = []
    for uid, info in user_data.items():
        for cell in info["cells"]:
            if user and uid == user_id:
                current_user_cells.append(cell)
            else:
                other_selected_cells.append(cell)

    user_selected_count = len(current_user_cells)
    total_selected_count = sum(len(info["cells"]) for info in user_data.values())

    return render_template(
        "index.html",
        user=user,
        user_selected_count=user_selected_count,
        max_selections=MAX_SELECTIONS,
        total_selected_count=total_selected_count,
        current_user_cells=current_user_cells,
        other_selected_cells=other_selected_cells
    )

@app.route("/login")
def login():
    return redirect(
        f"{AUTHORIZATION_BASE_URL}?client_id={CLIENT_ID}&redirect_uri={REDIRECT_URI}&response_type=code&scope=identify"
    )

@app.route("/search_owner")
def search_owner():
    user_data = load_data()
    
    cell_number = int(request.args.get("cell_number"))

    # Convert cell number to row and column
    row = (cell_number - 1) // 100
    col = (cell_number - 1) % 100
    cell_coordinates = [row, col]

    # Search for the owner in user_data
    for user_id, info in user_data.items():
        if cell_coordinates in info["cells"]:
            return jsonify({"owner": info["username"]})

    # If no owner found, return unselected
    return jsonify({"owner": None})

@app.route("/callback")
def callback():
    code = request.args.get("code")
    data = {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
        "scope": "identify",
    }
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    response = requests.post(TOKEN_URL, data=data, headers=headers)
    response.raise_for_status()
    token = response.json()["access_token"]

    user_info = requests.get(
        f"{API_BASE_URL}/users/@me", headers={"Authorization": f"Bearer {token}"}
    ).json()
    session['user'] = user_info

    user_data = load_data()

    user_id = user_info["id"]
    if user_id not in user_data:
        user_data[user_id] = {"username": user_info["username"], "cells": []}
        save_data(user_data)

    return redirect(url_for("home"))

# Handle cell selection through socketio to broadcast the update
@socketio.on('select_cell')
def handle_select_cell(data):
    user_data = load_data()
    if 'user' not in session:
        emit('error', {"error": "User not logged in"})
        return

    user_id = session['user']['id']
    row, col = data['row'], data['col']
    selected_cells = user_data[user_id]["cells"]
    
    # Toggle cell selection
    if [row, col] in selected_cells:
        selected_cells.remove([row, col])
        cell_selected = False
    elif len(selected_cells) < MAX_SELECTIONS:
        selected_cells.append([row, col])
        cell_selected = True
    else:
        emit('error', {"error": "Selection limit reached"})
        return

    user_data[user_id]["cells"] = selected_cells
    save_data(user_data)

    # Update the counters
    total_selected_count = sum(len(info["cells"]) for info in user_data.values())

    # Broadcast the update to all clients
    emit('update_cell', {
        "row": row,
        "col": col,
        "cell_selected": cell_selected,
        "user_id": user_id,
        "user_selected_count": len(selected_cells),
        "total_selected_count": total_selected_count
    }, broadcast=True)

if __name__ == "__main__":
    socketio.run(app, debug=True)
