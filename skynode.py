import os
import io
import time
import json
import uuid
import socket
import sqlite3
import zipfile
import platform
import threading
import requests
import subprocess
import email_service
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

# Flask e suas extensões reunidos em blocos únicos
from flask import Flask, render_template, request, redirect, url_for, session, flash, send_from_directory, jsonify, send_file, abort
from flask_socketio import SocketIO, emit
from werkzeug.security import generate_password_hash, check_password_hash
from ping3 import ping

# 🚨 REGRAS DE ALERTA DO SKYNODE (Thresholds)

LIMITE_CPU = 50.0  # Se a CPU passar de 50%, dispara
LIMITE_RAM = 60.0  # Se a RAM passar de 60%, dispara

def verificar_limites_RMM(dispositivo):
    hostname = dispositivo.get("hostname", "Desconhecido")
    try:
        cpu = float(dispositivo.get("cpu", 0))
        ram = float(dispositivo.get("ram", 0))
    except (ValueError, TypeError):
        return

    print(f"DEBUG: Testando {hostname} -> CPU: {cpu}% | RAM: {ram}%")

    if cpu > LIMITE_CPU:
        print(f"\n🔥 [ALERTA CRÍTICO] - O dispositivo '{hostname}' está com uso de CPU alto: {cpu}%!")

    if ram > LIMITE_RAM:
        print(f"\n🧠 [ALERTA ATENÇÃO] - O dispositivo '{hostname}' está com uso de RAM alto: {ram}%!\n")

# =========================================
# DIAGNÓSTICO DE ALERTAS (OLLAMA REMOVIDO)
# =========================================
def ai_analyze_alert(message):
    # Retorno padrão limpo para manter a compatibilidade do sistema sem IA externa
    explanation = f"Alerta detectado no sistema: {message}."
    suggested_command = "echo 'Analise automatica desativada'"
    return explanation, suggested_command

# =========================================
# TELEGRAM CONFIG
# =========================================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

def send_telegram_alert(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message}
        response = requests.post(url, json=payload, timeout=10)
        print("📨 Telegram:", response.status_code)
    except Exception as e:
        print("❌ Telegram erro:", e)

# =========================================
# APP CONFIG
# =========================================
app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "skynode_secret")

# Mude async_mode para "eventlet" para alinhar com o Gunicorn da Render
socketio = SocketIO(
    app,
    cors_allowed_origins="*"
)

# =========================================
# PATHS E DIRETÓRIOS
# =========================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATABASE_FOLDER = os.path.join(BASE_DIR, "database")
DATABASE = os.path.join(DATABASE_FOLDER, "skynode.db")
SCREENSHOT_FOLDER = os.path.join(BASE_DIR, "static", "screenshots")

os.makedirs(DATABASE_FOLDER, exist_ok=True)
os.makedirs(SCREENSHOT_FOLDER, exist_ok=True)

# =========================================
# MEMÓRIA GLOBAL
# =========================================
devices = []
commands = {}
terminal_results = {}
alerts = []  
alert_cooldown = {}

# =========================================
# LÓGICA DE ALERTAS
# =========================================
def add_alert(hostname, message):
    timestamp = time.strftime("%H:%M:%S")
    alert_id = str(uuid.uuid4())[:8]
    
    ai_explanation, ai_command = ai_analyze_alert(message)
    
    alert_data = {
        "id": alert_id,
        "hostname": hostname,
        "text": f"[{timestamp}] {message}",
        "ai_analysis": ai_explanation,
        "ai_command": ai_command,
        "timestamp": timestamp
    }
    
    alerts.append(alert_data)
    if len(alerts) > 100:
        alerts.pop(0)

    print(f"🚨 Alerta: {alert_data['text']}")
    print(f"🛠️ DIAGNÓSTICO: {alert_data['ai_analysis']}")

    socketio.emit("new_alert", alert_data)
    send_telegram_alert(f"{alert_data['text']}\n\n🛠️ Diagnóstico: {ai_explanation}")

def check_alerts(device):
    hostname = device.get("hostname", "unknown")
    try:
        cpu = float(device.get("cpu", 0))
        ram = float(device.get("ram", 0))
        disk = float(device.get("disk", 0))
        ping_val = float(device.get("ping", 0))
    except:
        return
        
    status = get_device_status(device)
    now = time.time()
    cooldown = 300  

    def can_alert(key):
        last = alert_cooldown.get(key, 0)
        if now - last >= cooldown:
            alert_cooldown[key] = now
            return True
        return False

    if cpu >= 90 and can_alert(f"{hostname}_cpu"):
        add_alert(hostname, f"🔥 CPU alta: {cpu}%")

    if ram >= 85 and can_alert(f"{hostname}_ram"):
        add_alert(hostname, f"🧠 RAM alta: {ram}%")

    if disk >= 90 and can_alert(f"{hostname}_disk"):
        add_alert(hostname, f"💽 Disco cheio: {disk}%")

    if ping_val >= 150 and can_alert(f"{hostname}_ping"):
        add_alert(hostname, f"📶 Latência alta: {ping_val} ms")

    if status == "offline" and can_alert(f"{hostname}_offline"):
        add_alert(hostname, "⚫ Dispositivo Offline")

# =========================================
# BANCO DE DADOS (CENTRALIZADO - SQLITE CORRIGIDO)
# =========================================
def connect_db():
    conn = sqlite3.connect(DATABASE, check_same_thread=False)
    return conn

def create_database():
    conn = connect_db()
    cursor = conn.cursor()
    
    # 1. Cria a tabela de usuários se não existir
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'tecnico',
            email TEXT
        )
    """)
    conn.commit()
    
    try:
        cursor.execute("ALTER TABLE users ADD COLUMN email TEXT;")
        conn.commit()
    except Exception:
        pass

    # 2. Cria as outras tabelas do sistema

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS metrics (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        hostname TEXT,
        cpu REAL,
        ram REAL,
        disk REAL,
        ping REAL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS dispositivos (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        hostname TEXT UNIQUE NOT NULL,
        ip TEXT NOT NULL,
        status TEXT DEFAULT 'offline'
    )
    """)
    conn.commit()
    
    # 3. FORÇA BRUTA: Remove o admin antigo (se houver) para evitar hash corrompido
    cursor.execute("DELETE FROM users WHERE username=?", ("admin",))
    conn.commit()
    
    # 4. Insere o admin do zero com a criptografia perfeita do Werkzeug
    admin_password = generate_password_hash("admin123")
    cursor.execute("""
        INSERT INTO users (username, password, role, email) 
        VALUES (?, ?, ?, ?)
    """, ("admin", admin_password, "admin", "admin@skynode.com"))
    
    conn.commit()
    conn.close()
    print("✅ Banco de dados SQLite inicializado e ADMIN resetado com sucesso!")

    # =========================================
    #FIM DATA BASE
    # =========================================

def save_metrics(device):
    try:
        conn = connect_db()
        cursor = conn.cursor()
        cursor.execute("""
        INSERT INTO metrics (hostname, cpu, ram, disk, ping)
        VALUES (?, ?, ?, ?, ?)
        """, (
            device.get("hostname"),
            device.get("cpu", 0),
            device.get("ram", 0),
            device.get("disk", 0),
            device.get("ping", 0)
        ))
        conn.commit()
        conn.close()
    except Exception as e:
        print("❌ Erro save metrics:", e)

create_database()

# =========================================
# CONTROLE DE ESTADO DOS DISPOSITIVOS
# =========================================
def get_device_status(device):
    return "online" if (time.time() - device.get("last_seen", 0)) <= 120 else "offline"

def serialize_devices():
    updated_devices = []
    for device in devices:
        updated_devices.append({
            "hostname": device.get("hostname", "N/A"),
            "system": device.get("system", "N/A"),
            "cpu": device.get("cpu", 0),
            "ram": device.get("ram", 0),
            "disk": device.get("disk", 0),
            "ping": device.get("ping", 0),
            "local_ip": device.get("local_ip", "N/A"),
            "public_ip": device.get("public_ip", "N/A"),
            "status": get_device_status(device),
            "ip": device.get("ip", "")
        })
    return updated_devices

# ====================================================================
# ROTAS HTTP (APIs DO AGENTE)
# ====================================================================

@app.route("/api/status", methods=["GET", "POST"])
@app.route("/api/status/", methods=["GET", "POST"])
def api_receber_status():
    if request.method == "GET":
        return jsonify({"status": "sucesso", "message": "API operacional. Use POST para enviar dados."}), 200
        
    try:
        dados = request.get_json(force=True, silent=True)
        if not dados:
            return jsonify({"status": "erro", "message": "Sem dados ou JSON inválido"}), 400
            
        hostname = dados.get("hostname")
        if not hostname:
            return jsonify({"status": "erro", "message": "Hostname ausente"}), 400
            
        dados["last_seen"] = time.time()
        dados["ip"] = request.remote_addr
        dados["status"] = "online"
        
        global devices
        devices = [d for d in devices if d.get("hostname") != hostname]
        devices.append(dados)
        
        save_metrics(dados)
        check_alerts(dados)
        
        socketio.emit("devices_update", serialize_devices())
        return jsonify({"status": "sucesso"}), 200
    except Exception as e:
        return jsonify({"status": "erro", "message": str(e)}), 500

@app.route("/api/screenshot", methods=["GET", "POST"])
@app.route("/api/screenshot/", methods=["GET", "POST"])
def api_receber_screenshot():
    if request.method == "GET":
        return jsonify({"status": "erro", "message": "Use POST para upload de arquivos"}), 200
        
    try:
        hostname = request.form.get("hostname")
        file = request.files.get("screenshot")
        
        if hostname and file:
            caminho_foto = os.path.join(SCREENSHOT_FOLDER, f"{hostname}.png")
            file.save(caminho_foto)
            return jsonify({"status": "sucesso"}), 200
            
        return jsonify({"status": "erro", "message": "Dados ou arquivos incompletos"}), 400
    except Exception as e:
        return jsonify({"status": "erro", "message": str(e)}), 500

@app.route("/api/command", methods=["GET", "POST"])
@app.route("/api/command/", methods=["GET", "POST"])
def api_processar_comando():
    try:
        dados = request.get_json(force=True, silent=True)
        hostname = dados.get("hostname") if dados else request.args.get("hostname")
        
        if not hostname:
            return jsonify({"command": "echo 'Sem hostname'"}), 200
            
        if dados and "output" in dados:
            terminal_results[hostname] = dados.get("output")
            
        command = commands.get(hostname, "")
        if command:
            commands[hostname] = ""
            return jsonify({"command": command}), 200
            
        return jsonify({"command": "echo online"}), 200
    except Exception as e:
        return jsonify({"command": f"echo 'Erro no servidor: {str(e)}'"}), 200

# =========================================
# ROTAS FLASK (INTERFACE)
# =========================================

@app.route("/", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username") or request.form.get("user") or request.form.get("login")
        password = request.form.get("password") or request.form.get("senha")
        
        if not username or not password:
            flash("Preencha todos os campos!")
            return render_template("login.html")
            
        username = username.strip()
        
        conn = connect_db()
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM users WHERE username=?", (username,))
        user = cursor.fetchone()
        conn.close()

        if user:
            user_dict = dict(user)
            if check_password_hash(user_dict["password"], password):
                session["user"] = user_dict["username"]
                session["role"] = user_dict["role"]
                return redirect(url_for("dashboard"))
            
        flash("Usuário ou senha inválidos!")
    return render_template("login.html")

@app.route("/dashboard")
def dashboard():
    if "user" not in session:
        return redirect(url_for("login"))
    return render_template("dashboard.html", user=session["user"], role=session["role"], devices=serialize_devices())

@app.route("/api/devices")
def api_devices():
    return jsonify(serialize_devices())

@app.route("/api/alerts")
def api_alerts():
    return jsonify(alerts[-20:])

@app.route("/api/execute_ai_command", methods=["POST"])
def execute_ai_command():
    if "user" not in session:
        return jsonify({"error": "Não autorizado"}), 401
        
    data = request.json or {}
    alert_id = data.get("alert_id")
    hostname = data.get("hostname")
    
    alert = next((a for a in alerts if a["id"] == alert_id), None)
    if not alert:
        return jsonify({"error": "Alerta não encontrado"}), 404
        
    command_to_run = alert.get("ai_command")
    terminal_results[hostname] = ""
    commands[hostname] = command_to_run
    
    return jsonify({
        "status": "success", 
        "message": f"Comando de correção enviado para {hostname}!",
        "command": command_to_run
    })

@app.route("/api/metrics/<hostname>")
def obter_metricas_dispositivo(hostname):
    try:
        conn = connect_db()
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT cpu, ram, disk, ping, created_at 
            FROM metrics 
            WHERE hostname = ? 
            ORDER BY id DESC LIMIT 1
        """, (hostname,))
        
        dado = cursor.fetchone()
        conn.close()
        
        if dado:
            dado_dict = dict(dado)
            if dado_dict.get('created_at'):
                try:
                    if isinstance(dado_dict['created_at'], str):
                        pass 
                except:
                    dado_dict['created_at'] = str(dado_dict['created_at'])
            return jsonify(dado_dict)
            
        return jsonify({"cpu": 0, "ram": 0, "disk": 0, "ping": 0})
    except Exception as e:
        print(f"❌ Erro ao buscar métricas: {e}")
        return jsonify({"cpu": 0, "ram": 0, "disk": 0, "ping": 0}), 500

@app.route("/screenshots/<filename>")
def screenshots(filename):
    return send_from_directory(SCREENSHOT_FOLDER, filename)

@app.route("/device/<hostname>")
def device_details(hostname):
    if "user" not in session:
        return redirect(url_for("login"))
    
    selected_device = next((d for d in devices if d.get("hostname") == hostname), None)
    if not selected_device:
        return "Dispositivo não encontrado", 404
        
    return render_template("device_details.html", device=selected_device, screenshot=f"{hostname}.png", user=session["user"], role=session["role"])

@app.route("/monitor/<hostname>")
def monitor(hostname):
    if "user" not in session:
        return redirect(url_for("login"))
    selected_device = next((d for d in devices if d.get("hostname") == hostname), None)
    if not selected_device:
        return "Dispositivo não encontrado", 404
    return render_template("monitor.html", device=selected_device, user=session["user"], role=session["role"])

@app.route("/terminal/<hostname>")
def terminal(hostname):
    if "user" not in session:
        return redirect(url_for("login"))
    return render_template("terminal.html", hostname=hostname, user=session["user"], role=session["role"])

@app.route("/viewer/<hostname>")
def viewer(hostname):
    if "user" not in session:
        return redirect(url_for("login"))
    return render_template("viewer.html", hostname=hostname, user=session["user"], role=session["role"])

@app.route("/api/terminal/<hostname>", methods=["POST"])
def api_terminal(hostname):
    if "user" not in session:
        return jsonify({"error": "unauthorized"}), 401
    
    command = request.json.get("command", "").strip()
    if not command:
        return jsonify({"output": "Comando vazio"})

    terminal_results[hostname] = ""
    commands[hostname] = command

    timeout, start = 15, time.time()
    while time.time() - start < timeout:
        if terminal_results.get(hostname):
            break
        time.sleep(0.5)

    return jsonify({"output": terminal_results.get(hostname, "Sem resposta do agente")})

@app.route("/remote/<hostname>")
def remote(hostname):
    if "user" not in session:
        return redirect(url_for("login"))
    
    selected_device = next((d for d in devices if d.get("hostname") == hostname), None)
    if not selected_device:
        return "Dispositivo não encontrado", 404

    ip = selected_device.get("local_ip")
    rustdesk_path = r"C:\Program Files\RustDesk\rustdesk.exe"
    
    if os.path.exists(rustdesk_path):
        try:
            subprocess.Popen(["cmd.exe", "/c", "start", "", rustdesk_path, ip])
        except Exception as e:
            print("❌ Erro RustDesk:", e)
    return redirect(url_for("device_details", hostname=hostname))

@app.route("/send_command/<hostname>/<command>")
def send_command(hostname, command):
    if "user" not in session:
        return jsonify({"error": "unauthorized"}), 401

    terminal_results[hostname] = ""
    commands[hostname] = command

    timeout, start = 15, time.time()
    while time.time() - start < timeout:
        if terminal_results.get(hostname):
            break
        time.sleep(0.5)

    return jsonify({
        "hostname": hostname,
        "command": command,
        "output": terminal_results.get(hostname, "Sem resposta")
    })

@app.route('/users')
def lista_usuarios():
    if "user" not in session:
        return redirect(url_for("login"))
    
    conn = connect_db()
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT username, email, role FROM users")
    usuarios_do_banco = cursor.fetchall()
    conn.close()

    lista_formatada = []
    for u in usuarios_do_banco:
        lista_formatada.append({
            "username": u["username"],
            "email": u["email"],
            "role": u["role"]
        })
    return render_template('users.html', user=session["user"], role=session["role"], users_list=lista_formatada)

@app.route('/add_user', methods=['POST'])
def add_user():
    if "user" not in session:
        return redirect(url_for("login"))
        
    username = request.form.get('username') or request.form.get('user')
    email = request.form.get('email')
    password = request.form.get('password') or request.form.get('senha')
    role = request.form.get('role') or 'tecnico'

    if not username or not password:
        flash("Erro: Campos obrigatórios ausentes!")
        return redirect('/users')

    username = username.strip()
    hashed_password = generate_password_hash(password)
    
    conn = connect_db()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "INSERT INTO users (username, email, password, role) VALUES (?, ?, ?, ?)",
            (username, email, hashed_password, role)
        )
        conn.commit()
    except Exception as e:
        print(f"Erro ao inserir usuario: {e}")
    finally:
        conn.close()
        
    return redirect('/users')

@app.route('/delete_user/<username>')
def delete_user(username):
    if "user" not in session:
        return redirect(url_for("login"))
        
    if username == "admin":
        return redirect('/users')
    try:
        with connect_db() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM users WHERE username=?", (username,))
            conn.commit()
        return redirect('/users')
    except Exception as e:
        return f"<h3>Erro ao excluir usuário:</h3><p>{e}</p><a href='/users'>Voltar</a>", 500

@app.route("/notepad")
def notepad():
    if "user" not in session:
        return redirect(url_for("login"))
    return render_template("notepad.html", user=session["user"], role=session["role"])

@app.route("/alerts")
def alerts_page():
    if "user" not in session:
        return redirect(url_for("login"))
    return render_template("alerts.html", alerts=alerts, user=session["user"], role=session["role"])

@app.route("/change_password", methods=["GET", "POST"])
def change_password():
    if "user" not in session:
        return redirect(url_for("login"))

    if request.method == "POST":
        current_password = request.form["current_password"]
        new_password = request.form["new_password"]
        confirm_password = request.form["confirm_password"]

        conn = connect_db()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM users WHERE username=?", (session["user"],))
        user = cursor.fetchone()

        if not user or not check_password_hash(user[2], current_password):
            flash("Senha atual incorreta!")
        elif new_password != confirm_password:
            flash("As senhas não coincidem!")
        else:
            cursor.execute("UPDATE users SET password=? WHERE username=?", (generate_password_hash(new_password), session["user"]))
            conn.commit()
            flash("Senha alterada com sucesso!")
            conn.close()
            return redirect(url_for("dashboard"))
        conn.close()

    return render_template("change_password.html", user=session["user"], role=session["role"])

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

@app.route("/secret-reset-admin")
def secret_reset_admin():
    try:
        conn = connect_db()
        cursor = conn.cursor()
        cursor.execute("DROP TABLE IF EXISTS users")
        cursor.execute("""
            CREATE TABLE users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password TEXT NOT NULL,
                role TEXT NOT NULL,
                email TEXT
            )
        """)
        nova_senha_hash = generate_password_hash("admin123")
        cursor.execute(
            "INSERT INTO users (username, password, role, email) VALUES (?, ?, ?, ?)",
            ("admin", nova_senha_hash, "admin", "admin@skynode.com")
        )
        conn.commit()
        conn.close()
        return "SUCESSO: Usuário 'admin' resetado para 'admin123'!", 200
    except Exception as e:
        return f"Erro ao resetar: {str(e)}", 500
    
@app.route('/download-agent')
def download_agent():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    downloads_path = os.path.join(base_dir, 'downloads')
    
    # Cria a pasta em tempo de execução se ela não existir, impedindo o crash
    os.makedirs(downloads_path, exist_ok=True)
    
    agent_path = os.path.join(downloads_path, 'agent.exe')
    config_path = os.path.join(downloads_path, 'config.ini')
    
    # Se os arquivos não existirem, avisa sem derrubar o servidor python
    if not os.path.exists(agent_path) or not os.path.exists(config_path):
        try:
            arquivos_reais = os.listdir(downloads_path)
        except Exception:
            arquivos_reais = "Erro ao listar diretório"
        return f"Erro: Arquivos do agente (agent.exe / config.ini) nao foram encontrados na pasta downloads. Arquivos na pasta: {arquivos_reais}", 404

    try:
        memory_file = io.BytesIO()
        with zipfile.ZipFile(memory_file, 'w', zipfile.ZIP_DEFLATED) as zipf:
            zipf.write(agent_path, 'agent.exe')
            zipf.write(config_path, 'config.ini')
                
        memory_file.seek(0)
        return send_file(
            memory_file,
            mimetype='application/zip',
            as_attachment=True,
            download_name='SkyNode_Agent.zip'
        )
    except Exception as e:
        return f"Erro ao gerar o pacote ZIP: {str(e)}", 500
    
@app.route("/mapa")
def pagina_mapa():
    if "user" not in session:
        return redirect("/login")
    return render_template("mapa_rede.html", user=session["user"], role=session["role"])

@app.route("/api/mapa/dados")
def api_mapa_dados():
    if "user" not in session:
        return jsonify({"error": "unauthorized"}), 401
        
    dispositivos_cadastrados = []
    try:
        conn = connect_db()
        cursor = conn.cursor()
        cursor.execute("SELECT hostname, ip, status FROM dispositivos")
        for linha in cursor.fetchall():
            dispositivos_cadastrados.append({
                "hostname": linha[0],
                "ip": linha[1],
                "status": str(linha[2]).lower()
            })
        conn.close()
    except Exception as e:
        print(f"❌ Erro ao ler banco no Mapa: {e}")
        dispositivos_cadastrados = [{"hostname": "Sem Agentes Cadastrados", "ip": "127.0.0.1", "status": "offline"}]

    return jsonify({"dispositivos": dispositivos_cadastrados})

def checar_ip_individual(ip):
    try:
        res = ping(ip, timeout=0.2)
        if res is not None and res is not False:
            return {"ip": ip, "hostname": f"Dispositivo {ip.split('.')[-1]}"}
    except Exception:
        pass
    return None

@app.route("/api/descoberta/scan")
def api_descoberta_scan():
    if "user" not in session:
        return jsonify({"error": "unauthorized"}), 401

    subrede = request.args.get("subrede", "").strip()
    if not subrede:
        return jsonify({"error": "Sub-rede ausente"}), 400

    subrede = subrede.rstrip('.')
    lista_ips = [f"{subrede}.{i}" for i in range(1, 255)]
    hosts_validos = []

    with ThreadPoolExecutor(max_workers=50) as executor:
        resultados = executor.map(checar_ip_individual, lista_ips)
        for ip_respondido in resultados:
            if ip_respondido:
                hosts_validos.append(ip_respondido)

    return jsonify({"hosts_encontrados": hosts_validos})

@app.route("/api/dispositivos/adicionar", methods=["POST"])
def api_adicionar_descoberto():
    if "user" not in session:
        return jsonify({"error": "unauthorized"}), 401
    
    data = request.json or {}
    hostname = data.get("hostname")
    ip = data.get("ip")
    
    if not hostname or not ip:
        return jsonify({"error": "Dados incompletos"}), 400
    
    conn = None
    try:
        conn = connect_db()
        cursor = conn.cursor()
        
        cursor.execute("SELECT id FROM dispositivos WHERE ip = ?", (ip,))
        if cursor.fetchone():
            return jsonify({"success": False, "error": "Este dispositivo já está cadastrado!"}), 400
            
        cursor.execute(
            "INSERT INTO dispositivos (hostname, ip, status) VALUES (?, ?, 'online')", 
            (hostname, ip)
        )
        conn.commit()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": f"Erro no banco de dados: {str(e)}"}), 500
    finally:
        if conn:
            conn.close()

@app.route('/<pagina>')
def carregar_pagina_coringa(pagina):
    if "user" not in session:
        return redirect(url_for("login"))

    paginas_menu = ['dispositivos', 'dispositivo', 'monitoramento', 'configuracoes', 'configuracao', 'mapa']
    if pagina in paginas_menu:
        pasta_templates = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'templates')
        if os.path.exists(pasta_templates):
            arquivos_reais = os.listdir(pasta_templates)
            if f"{pagina}.html" in arquivos_reais:
                return render_template(f"{pagina}.html", user=session["user"], role=session["role"], devices=serialize_devices())
    return "Rota não mapeada no menu", 404

@app.route("/api/processos/<hostname>", methods=["GET"])
def api_processos(hostname):
    if "user" not in session:
        return jsonify({"error": "unauthorized"}), 401

    terminal_results[hostname] = ""
    commands[hostname] = "get_processes"

    timeout, start = 15, time.time()
    while time.time() - start < timeout:
        if terminal_results.get(hostname):
            break
        time.sleep(0.5)
    return jsonify({"processos": terminal_results.get(hostname, "Sem resposta do agente")})

@app.after_request
def add_header(response):
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response

@app.route('/forgot_password', methods=['GET', 'POST'])
def forgot_password():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        
        conn = connect_db()
        cursor = conn.cursor()
        cursor.execute("SELECT email FROM users WHERE username=?", (username,))
        row = cursor.fetchone()
        
        if row and row[0]:
            email_usuario = row[0]
            
            # Utiliza as funções do módulo externo de forma limpa
            senha_provisoria = email_service.gerar_senha_provisoria()
            senha_hash = generate_password_hash(senha_provisoria)
            
            # Atualiza o banco de dados local
            cursor.execute("UPDATE users SET password=? WHERE username=?", (senha_hash, username))
            conn.commit()
            conn.close()
            
            # Dispara o e-mail delegando para o arquivo externo
            email_service.disparar_recuperacao(email_usuario, senha_provisoria)
            
            flash("Se o usuário existir e possuir um e-mail cadastrado, uma nova senha foi enviada!")
            return redirect(url_for('login'))
        else:
            conn.close()
            flash("Usuário não encontrado ou sem e-mail associado.")
            
    return render_template('forgot_password.html')


@app.route('/recuperar_senha', methods=['GET', 'POST'])
def recuperar_senha():
    if request.method == 'POST':
        email = request.form.get('email')
        
        # 1. Conecta ao banco de dados SQLite
        conn = sqlite3.connect('database.db') # Certifique-se de que o nome do arquivo .db está correto
        cursor = conn.cursor()
        
        # 2. Verifica se o usuário com esse e-mail existe
        cursor.execute("SELECT id FROM users WHERE email = ?", (email,))
        user = cursor.fetchone()
        
        if user:
            # 3. Usa o email_service para gerar a senha provisória de 8 dígitos
            nova_senha_texto = email_service.gerar_senha_provisoria()
            
            # 4. Criptografa a senha gerada (usando o generate_password_hash que você importou)
            senha_criptografada = generate_password_hash(nova_senha_texto)
            
            # 5. Atualiza a senha do usuário no banco de dados
            cursor.execute("UPDATE users SET password = ? WHERE email = ?", (senha_criptografada, email))
            conn.commit()
            
            # 6. Dispara o e-mail real em segundo plano usando a thread do email_service
            email_service.disparar_recuperacao(email, nova_senha_texto)
            
            flash('Se o e-mail estiver cadastrado, você receberá uma senha provisória em instantes.', 'success')
        else:
            # Mensagem idêntica por segurança para evitar mapeamento de e-mails existentes
            flash('Se o e-mail estiver cadastrado, você receberá uma senha provisória em instantes.', 'success')
            
        conn.close()
        return redirect(url_for('login'))
        
    return render_template('recuperar_senha.html')

if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
