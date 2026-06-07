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
# AGORA O CÓDIGO BUSCA DO SISTEMA, SEM EXPOR NADA NO GITHUB!
# =========================================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

def send_telegram_alert(message):
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
SCREENSHOT_FOLDER = os.path.join(BASE_DIR, "screenshots")

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
    cpu = float(device.get("cpu", 0))
    ram = float(device.get("ram", 0))
    disk = float(device.get("disk", 0))
    ping = float(device.get("ping", 0))
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
    # Cria e conecta ao arquivo de banco de dados local na Render
    conn = sqlite3.connect('skynode.db', check_same_thread=False)
    return conn

def create_database():
    conn = connect_db()
    cursor = conn.cursor()
    
    # 1. Cria a tabela de usuários adaptada para SQLite (AUTOINCREMENT)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password TEXT NOT NULL,
        role TEXT NOT NULL,
        email TEXT
    )
    """)
    
    # 2. Tentativa de adicionar a coluna email caso a tabela já exista
    try:
        cursor.execute("ALTER TABLE users ADD COLUMN email TEXT;")
        conn.commit()
        print("🎉 Coluna 'email' adicionada com sucesso à tabela existente!")
    except Exception:
        # No SQLite, se a coluna já existir, ele gera um erro que podemos ignorar
        pass

    # 3. Cria a tabela de métricas adaptada para SQLite
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

    # 4. Cria a tabela de dispositivos adaptada para SQLite
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS dispositivos (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        hostname TEXT UNIQUE NOT NULL,
        ip TEXT NOT NULL,
        status TEXT DEFAULT 'offline'
    )
    """)
    
    # 5. Criação do Admin padrão adaptada para a sintaxe do SQLite (?)
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
        # No SQLite usamos '?' em vez de '?'
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

def save_metrics(device):
    try:
        conn = connect_db()
        cursor = conn.cursor()
        # --- ALTERADO DE '?' PARA '?' ---
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

# =========================================
# ROTAS FLASK (HTTP/API)
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

        # user[2] mapeia para o campo password
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

  # Garanta que este import está no topo do arquivo junto com os outros

@app.route("/api/metrics/<hostname>")
def obter_metricas_dispositivo(hostname):
    try:
        conn = connect_db()
        # 💡 O NOVO PULO DO GATO: No SQLite, usamos o row_factory para retornar como dicionário
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT cpu, ram, disk, ping, created_at 
            FROM metrics 
            WHERE hostname =? 
            ORDER BY id DESC LIMIT 1
        """, (hostname,))
        
        dado = cursor.fetchone()
        conn.close()
        
        if dado:
            # Converte a data para string para o JSON não quebrar
            if 'created_at' in dado and dado['created_at']:
                dado['created_at'] = dado['created_at'].strftime('%Y-%m-%d %H:%M:%S')
            return jsonify(dado)
            
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
            print(f"🖥 Abrindo RustDesk com segurança para {ip}")
        except Exception as e:
            print("❌ Erro RustDesk:", e)
    else:
        print("❌ RustDesk não encontrado no caminho especificado.")

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
        # Criptografa a senha para manter a compatibilidade com o sistema de login
        hashed_password = generate_password_hash(password)
        conn = connect_db()
        cursor = conn.cursor()
        try:
            cursor.execute(
                "INSERT INTO users (username, email, password, role) VALUES (?, ?, ?, ?)",
                (username, email, hashed_password, role)
            )
            conn.commit()
            print(f"👤 Usuário '{username}' cadastrado com sucesso!")
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

# ... (mantenha o restante do seu código Flask)

@app.route('/download-agent')
def download_agent():
    # 1. Descobre o caminho da pasta 'downloads' na raiz do projeto
    base_dir = os.path.dirname(os.path.abspath(__file__))
    downloads_path = os.path.join(base_dir, 'downloads')
    
    # Caminho completo dos dois arquivos
    agent_path = os.path.join(downloads_path, 'agent.exe')
    config_path = os.path.join(downloads_path, 'config.ini')
    
    # 2. Verifica se a pasta ou os arquivos estão faltando no servidor
    if not os.path.exists(agent_path) or not os.path.exists(config_path):
        # Se falhar, ele mostra o que o servidor realmente tem na pasta para sabermos o motivo
        arquivos_reais = os.listdir(downloads_path) if os.path.exists(downloads_path) else "Pasta nao existe"
        return f"Erro: Arquivos nao encontrados. Conteudo da pasta: {arquivos_reais}", 500

    # 3. Cria o ZIP na memória incluindo os dois arquivos existentes
    memory_file = io.BytesIO()
    with zipfile.ZipFile(memory_file, 'w', zipfile.ZIP_DEFLATED) as zipf:
        zipf.write(agent_path, 'agent.exe')
        zipf.write(config_path, 'config.ini')
            
    memory_file.seek(0)
    
    # 4. Envia o ZIP pronto para o usuário
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
        cursor = conn.cursor() # PyMySQL não usa row_factory
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

# ====================================================================
# ROTAS DE NAVEGAÇÃO DOS MENUS (CORRIGIDO PROTEÇÃO DE SESSÃO)
# ====================================================================
# ====================================================================
# ROTA INTELIGENTE ANTIBUG (DIAGNÓSTICO AUTOMÁTICO DE ARQUIVOS)
# ====================================================================
@app.route('/<pagina>')
def carregar_pagina_coringa(pagina):
    if "user" not in session:
        return redirect(url_for("login"))

    paginas_menu = ['dispositivos', 'dispositivo', 'monitoramento', 'configuracoes', 'configuracao']
    
    if pagina in paginas_menu:
        pasta_templates = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'templates')
        
        if os.path.exists(pasta_templates):
            arquivos_reais = os.listdir(pasta_templates)
            
            if f"{pagina}.html" in arquivos_reais:
                return render_template(f"{pagina}.html", user=session["user"], role=session["role"], devices=serialize_devices())
            
            if pagina == 'dispositivos' and 'dispositivo.html' in arquivos_reais:
                return render_template('dispositivo.html', user=session["user"], role=session["role"], devices=serialize_devices())
            if pagina == 'configuracoes' and 'configuracao.html' in arquivos_reais:
                return render_template('configuracao.html', user=session["user"], role=session["role"])
                
            if pagina == 'dispositivo' and 'dispositivos.html' in arquivos_reais:
                return render_template('dispositivos.html', user=session["user"], role=session["role"], devices=serialize_devices())
            if pagina == 'configuracao' and 'configuracoes.html' in arquivos_reais:
                return render_template('configuracoes.html', user=session["user"], role=session["role"])

            print(f"⚠️ Erro de Arquivo: O navegador pediu '/{pagina}', mas o arquivo correspondente não existe.")
            return f"""
            <div style="font-family: sans-serif; padding: 20px;">
                <h2 style="color: red;">🚨 Arquivo HTML Não Encontrado!</h2>
                <p>O Flask tentou abrir a tela do menu, mas ela não existe na pasta <b>templates</b>.</p>
                <p><b>O que foi solicitado:</b> {pagina}.html</p>
                <hr>
                <h3>📋 Arquivos detectados na sua pasta 'templates':</h3>
                <ul>
                    {"".join([f"<li>{arq}</li>" for arq in arquivos_reais if arq.endswith('.html')])}
                </ul>
            </div>
            """, 404

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

# 🛡️ LIMPEZA DE CACHE DINÂMICO CONTRA ERROS 404 RESIDUAIS
@app.after_request
def add_header(response):
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response

# =========================================
# SERVIDORES TCP (SOCKETS DE AGENTES)
# =========================================
def socket_server():
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("0.0.0.0", 6001))
    server.listen(100)
    print("✅ Socket de métricas na porta 6001")

    while True:
        try:
            client, addr = server.accept()
            data = client.recv(4096).decode()
            if not data:
                client.close()
                continue

            device = json.loads(data)
            device["ip"] = addr[0]
            device["last_seen"] = time.time()
            device["status"] = "online"

            global devices
            devices = [d for d in devices if d.get("hostname") != device.get("hostname")]
            devices.append(device)

            save_metrics(device)
            check_alerts(device)

            socketio.emit("devices_update", serialize_devices())
            client.close()
        except Exception as e:
            print("❌ Erro socket métricas:", e)

def screenshot_server():
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("0.0.0.0", 6002))
    server.listen(20)
    print("✅ Socket de screenshot na porta 6002")

    while True:
        try:
            client, _ = server.accept()
            hostname_size = int.from_bytes(client.recv(4), "big")
            hostname = client.recv(hostname_size).decode()
            filename = os.path.join(SCREENSHOT_FOLDER, f"{hostname}.png")

            with open(filename, "wb") as file:
                while True:
                    data = client.recv(4096)
                    if not data:
                        break
                    file.write(data)
            client.close()
        except Exception as e:
            print("❌ Erro socket screenshot:", e)

def command_server():
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("0.0.0.0", 6003))
    server.listen(50)
    print("✅ Socket de comandos na porta 6003")

    while True:
        try:
            client, _ = server.accept()
            client.settimeout(15)
            hostname = client.recv(4096).decode().strip()

            command = commands.get(hostname, "")
            if not command:
                command = "echo online"

            client.sendall(command.encode())
            result = client.recv(65535).decode(errors="ignore")
            terminal_results[hostname] = result
            commands[hostname] = ""
            client.close()
        except Exception as e:
            print("❌ Erro socket comandos:", e)

def monitor_devices():
    while True:
        try:
            socketio.emit("devices_update", serialize_devices())
            time.sleep(3)
        except Exception as e:
            print("❌ Erro loop monitor:", e)

@socketio.on('atualizar_status')
def handle_agent_data(payload):
    dados = json.loads(payload) if isinstance(payload, str) else payload
    verificar_limites_RMM(dados)

# ==============================================================================
# CONTROLE VIA HTTP - ROTAS PARA O AGENTE DA RENDER
# ==============================================================================

@app.route('/api/status', methods=['POST'])
def api_receber_status():
    try:
        dados = request.get_json()
        if not dados:
            return jsonify({"status": "erro", "message": "Sem dados"}), 400
            
        hostname = dados.get('hostname')
        
        conn = connect_db()
        cursor = conn.cursor()
        
        # Garante que a tabela exista no SQLite
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS dispositivos (
                hostname TEXT PRIMARY KEY,
                system TEXT,
                cpu REAL,
                ram REAL,
                disk REAL,
                ping REAL,
                local_ip TEXT,
                public_ip TEXT,
                last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # Atualiza ou Insere o status do PC
        cursor.execute('''
            INSERT INTO dispositivos (hostname, system, cpu, ram, disk, ping, local_ip, public_ip, last_seen)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(hostname) DO UPDATE SET
                system=excluded.system,
                cpu=excluded.cpu,
                ram=excluded.ram,
                disk=excluded.disk,
                ping=excluded.ping,
                local_ip=excluded.local_ip,
                public_ip=excluded.public_ip,
                last_seen=CURRENT_TIMESTAMP
        ''')
        conn.commit()
        conn.close()
        
        return jsonify({"status": "sucesso"}), 200
    except Exception as e:
        print(f"Erro na API de Status: {e}")
        return jsonify({"status": "erro", "message": str(e)}), 500


@app.route('/api/screenshot', methods=['POST'])
def api_receber_screenshot():
    try:
        hostname = request.form.get('hostname')
        file = request.files.get('screenshot')
        
        if hostname and file:
            # Salva a foto na pasta static do seu painel para o HTML carregar
            caminho_dir = os.path.join('static', 'screenshots')
            os.makedirs(caminho_dir, exist_ok=True)
            
            caminho_foto = os.path.join(caminho_dir, f"{hostname}.jpg")
            file.save(caminho_foto)
            return jsonify({"status": "sucesso"}), 200
            
        return jsonify({"status": "erro", "message": "Dados incompletos"}), 400
    except Exception as e:
        print(f"Erro na API de Screenshot: {e}")
        return jsonify({"status": "erro", "message": str(e)}), 500


@app.route('/api/command', methods=['POST'])
def api_processar_comando():
    try:
        dados = request.get_json()
        hostname = dados.get('hostname')
        resultado_anterior = dados.get('result', '')
        
        conn = connect_db()
        cursor = conn.cursor()
        
        # Alteração preventiva: cria colunas de comando se não existirem na tabela dispositivos
        try:
            cursor.execute("ALTER TABLE dispositivos ADD COLUMN log_comando TEXT")
            cursor.execute("ALTER TABLE dispositivos ADD COLUMN comando_pendente TEXT")
        except Exception:
            pass # As colunas já existem

        # 1. Se o agente mandou a resposta de um comando anterior, salva no banco
        if resultado_anterior:
            cursor.execute("UPDATE dispositivos SET log_comando = ? WHERE hostname = ?", (resultado_anterior, hostname))
            
        # 2. Busca se você clicou em algum comando no painel web para enviar para esse PC
        cursor.execute("SELECT comando_pendente FROM dispositivos WHERE hostname = ?", (hostname,))
        row = cursor.fetchone()
        
        comando_para_executar = ""
        if row and row[0]:
            comando_para_executar = row[0]
            # Limpa o comando do banco para ele não rodar repetidamente em loop
            cursor.execute("UPDATE dispositivos SET comando_pendente = NULL WHERE hostname = ?", (hostname,))
            
        conn.commit()
        conn.close()
        
        return jsonify({"command": comando_para_executar}), 200
    except Exception as e:
        print(f"Erro na API de Comando: {e}")
        return jsonify({"command": ""}), 500


# =========================================
# INICIALIZAÇÃO SEGURA DO ECOSSISTEMA
# =========================================
if __name__ == "__main__":
    print("\n🔍 [SkyNode] Iniciando modo de diagnóstico seguro...")
    try:
        print("🚀 Ativando servidores de comunicação TCP...")
        threading.Thread(target=socket_server, daemon=True).start()
        threading.Thread(target=screenshot_server, daemon=True).start()
        threading.Thread(target=command_server, daemon=True).start()
        threading.Thread(target=monitor_devices, daemon=True).start()

        print("🔥 Servidor Web online em: http://0.0.0.0:5000")
        # Correção aplicada: adicionado allow_unsafe_werkzeug=True para rodar na Render
        socketio.run(app, host="0.0.0.0", port=5000, debug=False, allow_unsafe_werkzeug=True)

    except Exception as erro_fatal:
        print("\n❌ --------------------------------------------------")
        print(f"🚨 ERRO CRÍTICO NA INICIALIZAÇÃO: {erro_fatal}")
        print("-------------------------------------------------- ❌\n")
        # O input() foi removido daqui para evitar o erro EOFError no servidor automatizado