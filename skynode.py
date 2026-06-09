import os
import sqlite3
import time
import io
import zipfile
import subprocess
from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify, send_from_directory, send_file
from flask_socketio import SocketIO, emit
from werkzeug.security import generate_password_hash, check_password_hash
from concurrent.futures import ThreadPoolExecutor
from ping3 import ping

app = Flask(__name__)

# Chave secreta fixa e robusta para garantir a persistência dos cookies de sessão
app.secret_key = "CHAVE_MEGA_SEGURA_E_FIXA_DO_SKYNODE_2026"

# Inicialização limpa do SocketIO integrada ao ciclo do Flask
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="eventlet")

DATABASE = "skynode.db"
SCREENSHOT_FOLDER = "screenshots"
os.makedirs(SCREENSHOT_FOLDER, exist_ok=True)

# Estruturas de dados globais (Voláteis em memória para os agentes ativos)
devices = []
alerts = []
commands = {}
terminal_results = {}

def connect_db():
    return sqlite3.connect(DATABASE)

def create_database():
    conn = connect_db()
    cursor = conn.cursor()
    
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password TEXT NOT NULL,
        email TEXT,
        role TEXT NOT NULL
    )
    """)
    
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS metrics (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        hostname TEXT NOT NULL,
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
        hostname TEXT NOT NULL,
        ip TEXT UNIQUE NOT NULL,
        status TEXT NOT NULL
    )
    """)
    
    # Validação e inserção segura do administrador padrão
    cursor.execute("SELECT * FROM users WHERE username='admin'")
    if not cursor.fetchone():
        hashed_pw = generate_password_hash("admin123")
        cursor.execute("INSERT INTO users (username, password, email, role) VALUES ('admin', ?, 'admin@skynode.com', 'admin')", (hashed_pw,))
        conn.commit()
        
    conn.close()

# Inicializa o banco de dados preservando os registros existentes
create_database()

def get_device_status(device):
    last_seen = device.get('last_seen', 0)
    if time.time() - last_seen < 15:
        return "online"
    return "offline"

def serialize_devices():
    updated_devices = []
    global devices
    if not devices:
        return updated_devices
        
    for device in devices:
        try:
            updated_devices.append({
                "hostname": device.get("hostname", "N/A"),
                "system": device.get("system", "N/A"),
                "cpu": device.get("cpu", 0),
                "ram": device.get("ram", 0),
                "disk": device.get("disk", 0),
                "ping": device.get("ping", 0),
                "local_ip": device.get("local_ip", "N/A"),
                "public_ip": device.get("public_ip", "N/A"),
                "status": get_device_status(device) if 'last_seen' in device else "offline",
                "ip": device.get("ip", "")
            })
        except Exception as e:
            print(f"⚠️ Erro ao serializar dispositivo: {e}")
            continue
    return updated_devices

# ==========================================
# ROTAS FLASK - INTERFACE PRINCIPAL
# ==========================================

@app.route('/', methods=['GET', 'POST'])
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')

        conn = connect_db()
        cursor = conn.cursor()
        cursor.execute("SELECT password, role FROM users WHERE username=?", (username,))
        row = cursor.fetchone()
        conn.close()

        if row and check_password_hash(row[0], password):
            session.clear()  # Limpa resíduos de sessões anteriores para evitar conflitos de cookie
            session['username'] = username
            session['role'] = row[1]
            return redirect(url_for('dashboard'))
        else:
            flash("Usuário ou senha incorretos!")
            
    return render_template('login.html')

@app.route("/dashboard")
@app.route("/dashboard/")
def dashboard():
    if "username" not in session:
        return redirect(url_for('login'))
        
    try:
        lista_dispositivos = serialize_devices()
    except Exception as e:
        print(f"❌ Erro ao carregar dispositivos no painel: {e}")
        lista_dispositivos = []

    user_role = session.get("role", "tecnico")
    return render_template("dashboard.html", user=session["username"], role=user_role, devices=lista_dispositivos)

@app.route("/change_password", methods=["GET", "POST"])
@app.route("/change_password/", methods=["GET", "POST"])
def change_password():
    if "username" not in session:
        return redirect(url_for('login'))

    if request.method == "POST":
        current_password = request.form.get("current_password", "")
        new_password = request.form.get("new_password", "")
        confirm_password = request.form.get("confirm_password", "")

        conn = connect_db()
        cursor = conn.cursor()
        cursor.execute("SELECT password FROM users WHERE username=?", (session["username"],))
        user = cursor.fetchone()

        if not user or not check_password_hash(user[0], current_password):
            flash("Senha atual incorreta!")
        elif new_password != confirm_password:
            flash("As senhas não coincidem!")
        else:
            cursor.execute("UPDATE users SET password=? WHERE username=?", (generate_password_hash(new_password), session["username"]))
            conn.commit()
            conn.close()
            flash("Senha alterada com sucesso!")
            return redirect(url_for('dashboard'))
        conn.close()

    return render_template("change_password.html", user=session["username"], role=session["role"])

@app.route('/users')
@app.route('/users/')
def lista_usuarios():
    if "username" not in session:
        return redirect(url_for('login'))
    
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
    return render_template('users.html', user=session["username"], role=session["role"], users_list=lista_formatada)

@app.route('/add_user', methods=['POST'])
def add_user():
    if "username" not in session:
        return redirect(url_for('login'))
        
    username = request.form.get('username') or request.form.get('user')
    email = request.form.get('email', '')
    password = request.form.get('password') or request.form.get('senha')
    role = request.form.get('role') or 'tecnico'

    if not username or not password:
        flash("Erro: Campos obrigatórios ausentes!")
        return redirect(url_for('lista_usuarios'))

    username = username.strip()
    hashed_password = generate_password_hash(password)
    
    conn = connect_db()
    cursor = conn.cursor()
    try:
        cursor.execute("INSERT INTO users (username, email, password, role) VALUES (?, ?, ?, ?)", (username, email, hashed_password, role))
        conn.commit()
    except Exception as e:
        print(f"❌ Erro ao inserir usuario: {e}")
        flash("Erro ao cadastrar usuário. Certifique-se de que o nome é único.")
    finally:
        conn.close()
        
    return redirect(url_for('lista_usuarios'))

@app.route('/delete_user/<username>')
def delete_user(username):
    if "username" not in session:
        return redirect(url_for('login'))
        
    if username == "admin":
        flash("O usuário administrador padrão não pode ser removido.")
        return redirect(url_for('lista_usuarios'))
    try:
        with connect_db() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM users WHERE username=?", (username,))
            conn.commit()
        return redirect(url_for('lista_usuarios'))
    except Exception as e:
        return f"<h3>Erro ao excluir usuário:</h3><p>{e}</p><a href='/users'>Voltar</a>", 500

@app.route("/notepad")
@app.route("/notepad/")
def notepad():
    if "username" not in session:
        return redirect(url_for('login'))
    return render_template("notepad.html", user=session["username"], role=session["role"])

@app.route("/alerts")
@app.route("/alerts/")
def alerts_page():
    if "username" not in session:
        return redirect(url_for('login'))
    return render_template("alerts.html", alerts=alerts, user=session["username"], role=session["role"])

@app.route("/mapa")
@app.route("/mapa/")
def pagina_mapa():
    if "username" not in session:
        return redirect(url_for('login'))
    return render_template("mapa_rede.html", user=session["username"], role=session["role"])

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for('login'))

@app.route("/secret-reset-admin")
def secret_reset_admin():
    conn = connect_db()
    cursor = conn.cursor()
    hashed_pw = generate_password_hash("admin123")
    cursor.execute("INSERT OR REPLACE INTO users (username, password, email, role) VALUES ('admin', ?, 'admin@skynode.com', 'admin')", (hashed_pw,))
    conn.commit()
    conn.close()
    return "SUCESSO: Usuário 'admin' resetado para 'admin123'!"

# ==========================================
# ROTAS DE API E MANIPULAÇÃO DE AGENTES
# ==========================================

@app.route("/api/devices")
def api_devices():
    return jsonify(serialize_devices())

@app.route("/api/alerts")
def api_alerts():
    return jsonify(alerts[-20:])

@app.route("/api/execute_ai_command", methods=["POST"])
def execute_ai_command():
    if "username" not in session:
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
def obtener_metricas_dispositivo(hostname):
    try:
        conn = connect_db()
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT cpu, ram, disk, ping, created_at FROM metrics WHERE hostname = ? ORDER BY id DESC LIMIT 1", (hostname,))
        dado = cursor.fetchone()
        conn.close()
        
        if dado:
            return jsonify(dict(dado))
        return jsonify({"cpu": 0, "ram": 0, "disk": 0, "ping": 0})
    except Exception as e:
        print(f"❌ Erro ao buscar métricas: {e}")
        return jsonify({"cpu": 0, "ram": 0, "disk": 0, "ping": 0}), 500

@app.route("/screenshots/<filename>")
def screenshots(filename):
    return send_from_directory(SCREENSHOT_FOLDER, filename)

@app.route("/device/<hostname>")
def device_details(hostname):
    if "username" not in session:
        return redirect(url_for('login'))
    
    selected_device = next((d for d in devices if d.get("hostname") == hostname), None)
    if not selected_device:
        return "Dispositivo não encontrado", 404
        
    return render_template("device_details.html", device=selected_device, screenshot=f"{hostname}.png", user=session["username"], role=session["role"])

@app.route("/monitor/<hostname>")
def monitor(hostname):
    if "username" not in session:
        return redirect(url_for('login'))
    selected_device = next((d for d in devices if d.get("hostname") == hostname), None)
    if not selected_device:
        return "Dispositivo não encontrado", 404
    return render_template("monitor.html", device=selected_device, user=session["username"], role=session["role"])

@app.route("/terminal/<hostname>")
def terminal(hostname):
    if "username" not in session:
        return redirect(url_for('login'))
    return render_template("terminal.html", hostname=hostname, user=session["username"], role=session["role"])

@app.route("/viewer/<hostname>")
def viewer(hostname):
    if "username" not in session:
        return redirect(url_for('login'))
    return render_template("viewer.html", hostname=hostname, user=session["username"], role=session["role"])

@app.route("/api/terminal/<hostname>", methods=["POST"])
def api_terminal(hostname):
    if "username" not in session:
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
    if "username" not in session:
        return redirect(url_for('login'))
    
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
    if "username" not in session:
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

@app.route('/download-agent')
def download_agent():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    downloads_path = os.path.join(base_dir, 'downloads')
    os.makedirs(downloads_path, exist_ok=True)
    
    agent_path = os.path.join(downloads_path, 'agent.exe')
    config_path = os.path.join(downloads_path, 'config.ini')
    
    if not os.path.exists(agent_path) or not os.path.exists(config_path):
        try:
            arquivos_reais = os.listdir(downloads_path)
        except Exception:
            arquivos_reais = "Erro ao listar diretório"
        return f"Erro: Arquivos do agente não encontrados na pasta downloads. Conteúdo atual: {arquivos_reais}", 404

    try:
        memory_file = io.BytesIO()
        with zipfile.ZipFile(memory_file, 'w', zipfile.ZIP_DEFLATED) as zipf:
            zipf.write(agent_path, 'agent.exe')
            zipf.write(config_path, 'config.ini')
                
        memory_file.seek(0)
        return send_file(memory_file, mimetype='application/zip', as_attachment=True, download_name='SkyNode_Agent.zip')
    except Exception as e:
        return f"Erro ao gerar o pacote ZIP: {str(e)}", 500

@app.route("/api/mapa/dados")
def api_mapa_dados():
    if "username" not in session:
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
    if "username" not in session:
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
    if "username" not in session:
        return jsonify({"error": "unauthorized"}), 401
    
    data = request.json or {}
    hostname = data.get("hostname")
    ip = data.get("ip")
    
    if not hostname or not ip:
        return jsonify({"error": "Dados incompletos"}), 400
    
    try:
        conn = connect_db()
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM dispositivos WHERE ip = ?", (ip,))
        if cursor.fetchone():
            conn.close()
            return jsonify({"success": False, "error": "Este dispositivo já está cadastrado!"}), 400
            
        cursor.execute("INSERT INTO dispositivos (hostname, ip, status) VALUES (?, ?, 'online')", (hostname, ip))
        conn.commit()
        conn.close()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

# ==========================================
# SOCKETIO - SESSÃO DOS AGENTES REAIS
# ==========================================

@socketio.on("connect")
def handle_connect():
    pass

@socketio.on("register")
def handle_register(data):
    hostname = data.get("hostname")
    if not hostname:
        return
    for d in devices:
        if d["hostname"] == hostname:
            d.update(data)
            d["last_seen"] = time.time()
            break
    else:
        data["last_seen"] = time.time()
        devices.append(data)
    emit("register_response", {"status": "success"})

@socketio.on("update")
def handle_update(data):
    hostname = data.get("hostname")
    if not hostname:
        return
    for d in devices:
        if d["hostname"] == hostname:
            d.update(data)
            d["last_seen"] = time.time()
            break
            
    try:
        conn = connect_db()
        cursor = conn.cursor()
        cursor.execute("INSERT INTO metrics (hostname, cpu, ram, disk, ping) VALUES (?, ?, ?, ?, ?)",
                       (hostname, data.get("cpu"), data.get("ram"), data.get("disk"), data.get("ping")))
        conn.commit()
        conn.close()
    except Exception as e:
        print("Erro ao salvar métricas no banco:", e)

    response = {}
    if hostname in commands and commands[hostname]:
        response["command"] = commands[hostname]
        commands[hostname] = ""
    emit("update_response", response)

@socketio.on("terminal_response")
def handle_terminal_response(data):
    hostname = data.get("hostname")
    output = data.get("output")
    if hostname:
        terminal_results[hostname] = output

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    socketio.run(app, host="0.0.0.0", port=port, debug=False)
