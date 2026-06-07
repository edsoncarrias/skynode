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
        'server_ip': 'skynode-k6nw.onrender.com',
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
    # Nota: PORT, SCREENSHOT_PORT e COMMAND_PORT não são mais usados diretamente nos sockets,
    # pois agora trafegamos tudo via HTTP na porta padrão da nuvem (80/443).
except Exception as e:
    print("❌ Erro ao ler o arquivo config.ini. Usando padrões de emergência.", e)
    SERVER = "skynode-k6nw.onrender.com"

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
# LOOP PRINCIPAL (COMPATÍVEL COM A RENDER)
# =========================================
# Garante que o endereço use o protocolo HTTPS seguro da nuvem
URL_BASE = f"https://{SERVER.replace('https://', '').replace('http://', '')}"
print(f"🚀 SkyNode Agent Iniciado apontando para: {URL_BASE}")

# Armazena o relatório da última execução de comando para enviar ao painel
ultimo_resultado = ""

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

        # -----------------------------------------
        # 1. ENVIO DOS DADOS DE STATUS (MÁQUINA ONLINE)
        # -----------------------------------------
        try:
            requests.post(f"{URL_BASE}/api/status", json=data, timeout=5)
        except Exception as e:
            print("❌ Erro ao sincronizar dados de status com o servidor:", e)

        # -----------------------------------------
        # 2. CAPTURA E ENVIO DE SCREENSHOT
        # -----------------------------------------
        screenshot_path = "temp_screen.jpg"
        try:
            with mss.mss() as sct:
                monitor = sct.monitors[1]
                screenshot = sct.grab(monitor)
                img = Image.frombytes("RGB", screenshot.size, screenshot.rgb)
                img = img.resize((1024, 576))
                img.save(screenshot_path, "JPEG", quality=20, optimize=True)

            with open(screenshot_path, "rb") as f:
                arquivos = {'screenshot': f}
                dados_form = {'hostname': HOSTNAME_GLOBAL}
                requests.post(f"{URL_BASE}/api/screenshot", data=dados_form, files=arquivos, timeout=10)
        except Exception as e:
            print("❌ Erro ao enviar captura de tela:", e)
        finally:
            if os.path.exists(screenshot_path):
                os.remove(screenshot_path)

        # -----------------------------------------
        # 3. VERIFICAÇÃO E EXECUÇÃO DE COMANDOS PENDENTES
        # -----------------------------------------
        command = ""
        try:
            cmd_data = {"hostname": HOSTNAME_GLOBAL, "result": ultimo_resultado}
            response = requests.post(f"{URL_BASE}/api/command", json=cmd_data, timeout=10)
            
            if response.status_code == 200:
                command = response.json().get("command", "").strip()
            # Limpa o resultado anterior após o envio com sucesso
            ultimo_resultado = ""
        except Exception as e:
            print("❌ Erro ao consultar fila de comandos do painel:", e)
            command = ""

        # Processamento das ações recebidas do painel
        if command:
            try:
                if command.startswith("mouse_move"):
                    _, x, y = command.split("|")
                    pyautogui.moveTo(int(x), int(y))
                    ultimo_resultado = "Mouse movido"
                elif command == "mouse_click":
                    pyautogui.click()
                    ultimo_resultado = "Click executado"
                elif command == "double_click":
                    pyautogui.doubleClick()
                    ultimo_resultado = "Duplo click"
                elif command == "right_click":
                    pyautogui.rightClick()
                    ultimo_resultado = "Click direito"
                elif command.startswith("keyboard"):
                    _, text = command.split("|", 1)
                    pyautogui.write(text, interval=0.03)
                    ultimo_resultado = "Texto digitado"
                elif command == "press_enter":
                    pyautogui.press("enter")
                    ultimo_resultado = "ENTER pressionado"
                elif command == "press_backspace":
                    pyautogui.press("backspace")
                    ultimo_resultado = "BACKSPACE pressionado"
                elif command == "get_processes":
                    processos = obter_processos_ativos()
                    ultimo_resultado = "PROCESS_LIST:" + json.dumps(processos)
                else:
                    ultimo_resultado = os.popen(command).read()
                    if not ultimo_resultado.strip():
                        ultimo_resultado = "Comando executado."
            except Exception as e:
                ultimo_resultado = str(e)

    except Exception as e:
        print("❌ ERRO INTERNO NO LOOP:", e)

    # Intervalo regulado em 5 segundos para manter a consistência e estabilidade na Render Grátis
    time.sleep(5)