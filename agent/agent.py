import socket
import json
import platform
import psutil
import time
import requests
import os
import pyautogui
from ping3 import ping
import mss
from PIL import Image
import configparser  # Biblioteca nativa para ler arquivos .ini

# =========================================
# CARREGAMENTO DINÂMICO DE CONFIGURAÇÃO
# =========================================

CONFIG_FILE = "config.ini"
config = configparser.ConfigParser()

# Configurações padrão caso o arquivo não exista
DEFAULT_CONFIG = {
    'SERVER_CONFIG': {
        'server_ip': '127.0.0.1',
        'port': '6001',
        'screenshot_port': '6002',
        'command_port': '6003'
    }
}

if not os.path.exists(CONFIG_FILE):
    config.read_dict(DEFAULT_CONFIG)
    with open(CONFIG_FILE, 'w') as configfile:
        config.write(configfile)
    print(f"⚠️ Arquivo {CONFIG_FILE} não encontrado. Criado um modelo padrão.")
else:
    config.read(CONFIG_FILE)

# Definição das variáveis baseadas no arquivo CONFIG.INI do usuário
try:
    SERVER = config.get('SERVER_CONFIG', 'server_ip')
    PORT = config.getint('SERVER_CONFIG', 'port')
    SCREENSHOT_PORT = config.getint('SERVER_CONFIG', 'screenshot_port')
    COMMAND_PORT = config.getint('SERVER_CONFIG', 'command_port')
except Exception as e:
    print("❌ Erro ao ler o arquivo config.ini. Usando padrões de emergência.", e)
    SERVER = "127.0.0.1"
    PORT = 6001
    SCREENSHOT_PORT = 6002
    COMMAND_PORT = 6003

# Captura o hostname real da máquina globalmente uma única vez
HOSTNAME_GLOBAL = socket.gethostname()

# =========================================
# FAILSAFE
# =========================================
pyautogui.FAILSAFE = False

# =========================================
# IP LOCAL E PÚBLICO
# =========================================
def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "Indisponível"

def get_public_ip():
    try:
        return requests.get("https://api.ipify.org", timeout=5).text
    except Exception:
        return "Indisponível"

# ==============================================================================
# FUNÇÃO PARA COLETAR PROCESSOS ATIVOS
# ==============================================================================
def obter_processos_ativos():
    lista_processos = []
    for proc in sorted(psutil.process_iter(['pid', 'name', 'memory_percent', 'cpu_percent']), 
                       key=lambda x: x.info['memory_percent'] or 0, 
                       reverse=True)[:20]:
        try:
            lista_processos.append({
                'pid': proc.info['pid'],
                'name': proc.info['name'],
                'cpu': round(proc.info['cpu_percent'] or 0, 1),
                'ram': round(proc.info['memory_percent'] or 0, 1)
            })
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            pass
    return lista_processos

# =========================================
# LOOP PRINCIPAL
# =========================================
print(f"🚀 SkyNode Agent Iniciado apontando para: {SERVER}")

while True:
    try:
        system = platform.system()
        cpu = psutil.cpu_percent(interval=None) 
        ram = psutil.virtual_memory().percent
        disk = psutil.disk_usage('/').percent
        
        try:
            latency = ping(SERVER, timeout=2)
            latency = round(latency * 1000, 1) if latency else 999
        except Exception:
            latency = 999
            
        local_ip = get_local_ip()
        public_ip = get_public_ip()

        data = {
            "hostname": HOSTNAME_GLOBAL,
            "system": system,
            "cpu": cpu,
            "ram": ram,
            "disk": disk,
            "ping": latency,
            "local_ip": local_ip,
            "public_ip": public_ip
        }

        # Conexão de Status
        client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        client.connect((SERVER, PORT))
        client.sendall(json.dumps(data).encode())
        client.close()

        # Screenshot
        screenshot_path = "temp_screen.jpg"
        with mss.mss() as sct:
            monitor = sct.monitors[1]
            screenshot = sct.grab(monitor)
            img = Image.frombytes("RGB", screenshot.size, screenshot.rgb)
            img = img.resize((1024, 576))
            img.save(screenshot_path, "JPEG", quality=20, optimize=True)

        screenshot_client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        screenshot_client.connect((SERVER, SCREENSHOT_PORT))

        hostname_bytes = HOSTNAME_GLOBAL.encode()
        hostname_size = len(hostname_bytes)

        screenshot_client.sendall(hostname_size.to_bytes(4, "big"))
        screenshot_client.sendall(hostname_bytes)

        with open(screenshot_path, "rb") as file:
            while True:
                chunk = file.read(4096)
                if not chunk:
                    break
                screenshot_client.sendall(chunk)

        screenshot_client.close()

        if os.path.exists(screenshot_path):
            os.remove(screenshot_path)

        # Socket Comandos
        cmd_client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        cmd_client.settimeout(15)
        cmd_client.connect((SERVER, COMMAND_PORT))

        cmd_client.sendall(HOSTNAME_GLOBAL.encode())
        command = cmd_client.recv(4096).decode().strip()
        
        result = ""

        try:
            if command.startswith("mouse_move"):
                _, x, y = command.split("|")
                pyautogui.moveTo(int(x), int(y))
                result = "Mouse movido"
            elif command == "mouse_click":
                pyautogui.click()
                result = "Click executado"
            elif command == "double_click":
                pyautogui.doubleClick()
                result = "Duplo click"
            elif command == "right_click":
                pyautogui.rightClick()
                result = "Click direito"
            elif command.startswith("keyboard"):
                _, text = command.split("|", 1)
                pyautogui.write(text, interval=0.03)
                result = "Texto digitado"
            elif command == "press_enter":
                pyautogui.press("enter")
                result = "ENTER pressionado"
            elif command == "press_backspace":
                pyautogui.press("backspace")
                result = "BACKSPACE pressionado"
            elif command == "get_processes":
                processos = obter_processos_ativos()
                result = "PROCESS_LIST:" + json.dumps(processos)
            elif command:
                result = os.popen(command).read()
                if not result.strip():
                    result = "Comando executado."
            else:
                result = "Nenhum comando."
        except Exception as e:
            result = str(e)

        cmd_client.sendall(result.encode(errors="ignore"))
        cmd_client.close()

    except Exception as e:
        print("❌ ERRO NO LOOP:", e)

    time.sleep(3)