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
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

# Flask e suas extensões reunidos em blocos únicos
from flask import Flask, render_template, request, redirect, url_for, session, flash, send_from_directory, jsonify, send_file, abort
from flask_socketio import SocketIO, emit
from werkzeug.security import generate_password_hash, check_password_hash

# Bibliotecas de IA
import ollama

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
# INTEGRAÇÃO OLLAMA (IA LOCAL + AUTO-FIX)
# =========================================
def ai_analyze_alert(message):
    try:
        response = ollama.chat(
            model='phi3',
            messages=[
                {
                    "role": "system", 
                    "content": (
                        "Você é um analista SysAdmin e assistente automatizado do sistema RMM SkyNode. "
                        "Explique tecnicamente este alerta de forma muito curta (máximo 2 frases). "
                        "Depois, obrigatoriamente na ÚLTIMA LINHA, forneça um comando de terminal (Windows CMD) "
                        "REAL e GENÉRICO que possa mitigar ou resolver o problema, sem inventar variáveis fictícias como 'xyz' ou 'nome_do_processo'. "
                        "Se for CPU alta, sugira listar os processos que mais consomem ou limpar arquivos temporários. "
                        "Formato estrito da última linha: COMMAND: seu_comando_aqui"
                        "Exemplo de saída:\n"
                        "O uso de disco está crítico devido a arquivos temporários acumulados.\n"
                        "COMMAND: del /q /s %temp%\\*"
                    )
                },
                {
                    "role": "user", 
                    "content": f"Analise este alerta do agente: {message}"
                }
            ]
        )
        full_text = response['message']['content']
        explanation = []
        suggested_command = "echo 'Nenhuma acao automatica definida'"
        
        for line in full_text.split('\n'):
            if line.strip().startswith("COMMAND:"):
                suggested_command = line.replace("COMMAND:", "").strip()
            elif line.strip():
                explanation.append(line.strip())
                
        return " ".join(explanation), suggested_command
    except Exception as e:
        print("❌ OLLAMA ERRO (Verifique se o Ollama está aberto):", e)
        return "IA Local indisponível no momento.", "echo 'Erro na IA'"

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

socketio = SocketIO(
    app,
    cors_allowed_origins="*",
    async_mode="threading"
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
    print(f"🤖 IA LOCAL: {alert_data['ai_analysis']}")
    print(f"🛠️ COMANDO SUGERIDO: {alert_data['ai_command']}")

    socketio.emit("new_alert", alert_data)
    send_telegram_alert(f"{alert_data['text']}\n\n🤖 IA Diagnóstico: {ai_explanation}")

def check_alerts(device):
    hostname = device.get("hostname", "unknown")
    try:
        cpu = float(device.get("cpu", 0))
        ram = float(device.get("ram", 0))
        disk = float(device.get("disk", 0))
        ping = float(device.get("ping", 0))
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

    if ping >= 150 and can_alert(f"{hostname}_ping"):
        add_alert(hostname, f"📶 Latência alta: {ping} ms")

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
    
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password TEXT NOT NULL,
        role TEXT NOT NULL,
        email TEXT
    )
    """)
    
    try:
        cursor.execute("ALTER TABLE users ADD COLUMN email TEXT;")
        conn.commit()
    except Exception:
        pass

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
    
    cursor.execute("SELECT * FROM users WHERE username=?", ("admin",))
    if not cursor.fetchone():
        admin_password = generate_password_hash("admin123")
        cursor.execute("INSERT INTO users (username, password, role, email) VALUES (?, ?, ?, ?)", 
                       ("admin", admin_password, "admin", "admin@skynode.com"))
    
    conn.commit()
    conn.close()
    print("✅ Banco de dados SQLite inicializado com sucesso!")

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

# Executa inicialização única
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
# 🚨 NOVAS ROTAS HTTP COMPLETAS PARA TRATAR O FORMATO DO AGENTE (ANTI-405)
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
            
        # Verifica se há resultado de comando executado vindo do agente
        if dados and "output" in dados:
            terminal_results[hostname] = dados.get("output")
            
        command = commands.get(hostname, "")
        if command:
            commands[hostname] = "" # Limpa após entregar
            return jsonify({"command": command}), 200
            
        return jsonify({"command": "echo online"}), 200
    except Exception as e:
        return jsonify({"command": f"echo 'Erro no servidor: {str(e)}'"}), 200

# =========================================
# ROTAS FLASK (DASHBOARD E INTERFACE)
# =========================================
@app.route("/", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form["username"]
        password = request.form["password"]
        
        conn = connect_db()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM users WHERE username=?", (username,))
        user = cursor.fetchone()
        conn.close()

        if user and check_password_hash(user[2], password):
            session["user"] = username
            session["role"] = user[3]
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
            # 💡 FIX SEGURO CONTRA CRASH: Converte o Row em dicionário manipulável do Python
            dado_dict = dict(dado)
            if dado_dict.get('created_at'):
                try:
                    # Se vier como String do banco, valida o tipo antes de tratar
                    if isinstance(dado_dict['created_at'], str):
                        pass 
                except:
                    dado_dict['created_at'] = str(dado_dict['created_at'])
            return jsonify(dado_dict)
            
        return jsonify({"cpu": 0, "ram": 0, "disk": 0, "ping": 0})
    except Exception as e:
        print(f"❌ Erro ao buscar métricas para o card: {e}")
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
        
    username = request.form.get('username')
    email = request.form.get('email')
    password = request.form.get('password')
    role = request.form.get('role')

    if username and password:
        hashed_password = generate_password_hash(password)
        conn = connect_db()
        cursor = conn.cursor()
        try:
            cursor.execute(
                "INSERT INTO users (username, email, password, role) VALUES (?, ?, ?, ?)",
                (username, email, hashed_password, role)
            )
            conn.commit()
        except sqlite3.Error as e:
            print(f"Erro crítico ao inserir no SQLite: {e}")
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
        
        # Deleta o admin antigo para não dar conflito de chave única
        cursor.execute("DELETE FROM users WHERE username=?", ("admin",))
        
        # Gera o hash novinho em folha da senha 'admin123'
        nova_senha_hash = generate_password_hash("admin123")
        
        # Insere o admin com a senha resetada
        cursor.execute(
            "INSERT INTO users (username, password, role, email) VALUES (?, ?, ?, ?)",
            ("admin", nova_senha_hash, "admin", "admin@skynode.com")
        )
        
        conn.commit()
        conn.close()
        return "GATILHO EXECUTADO: O usuario 'admin' foi resetado para a senha 'admin123' com sucesso!", 200
    except Exception as e:
        return f"Erro ao resetar: {str(e)}", 500
    
    

@app.route('/download-agent')
def download_agent():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    downloads_path = os.path.join(base_dir, 'downloads')
    
    agent_path = os.path.join(downloads_path, 'agent.exe')
    config_path = os.path.join(downloads_path, 'config.ini')
    
    if not os.path.exists(agent_path) or not os.path.exists(config_path):
        arquivos_reais = os.listdir(downloads_path) if os.path.exists(downloads_path) else "Pasta nao existe"
        return f"Erro: Arquivos nao encontrados. Conteudo da pasta: {arquivos_reais}", 500

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
    sistema = platform.system().lower()
    comando = ["ping", "-n", "1", "-w", "400", ip] if sistema == "windows" else ["ping", "-c", "1", "-W", "1", ip]
    try:
        resultado = subprocess.run(comando, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=1.5)
        if resultado.returncode == 0:
            return ip
    except:
        pass
    return None

@app.route("/api/descoberta/scan")
def api_descoberta_scan():
    if "user" not in session:
        return jsonify({"error": "unauthorized"}), 401

    subrede = request.args.get("subrede", "").strip()
    if not subrede:
        return jsonify({"error": "Sub-rede ausente"}), 400

    hosts_validos = []
    lista_ips = [f"{subrede}.{i}" for i in range(1, 255)]

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
    
    data = request.json
    hostname = data.get("hostname")
    ip = data.get("ip")
    
    try:
        conn = connect_db()
        cursor = conn.cursor()
        cursor.execute("INSERT INTO dispositivos (hostname, ip, status) VALUES (?, ?, 'online')", (hostname, ip))
        conn.commit()
        conn.close()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

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

# =========================================
# FUNÇÃO DE INICIALIZAÇÃO SEGURA DO APP
# =========================================
if __name__ == "__main__":
    # Mantido em escopo local para evitar colisões com o gunicorn no ambiente da Render
    port = int(os.environ.get("PORT", 5000))
    socketio.run(app, host="0.0.0.0", port=port, allow_unsafe_werkzeug=True)