import smtplib
import secrets
import string
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from concurrent.futures import ThreadPoolExecutor
from werkzeug.security import generate_password_hash

# CONFIGURAÇÕES DE DISPARO DE E-MAIL (SMTP)
SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 587
SMTP_USER = "skynode.sistema@gmail.com"         # Seu e-mail de disparo
SMTP_PASSWORD = "eisb haql soxt opll"   # Sua senha de aplicativo

# Executor isolado para não pesar no Flask
email_executor = ThreadPoolExecutor(max_workers=2)

def gerar_senha_provisoria():
    """Gera uma senha aleatória segura de 8 caracteres."""
    caracteres = string.ascii_letters + string.digits
    return ''.join(secrets.choice(caracteres) for _ in range(8))

def _enviar_email_disparo(destino, nova_senha):
    """Função interna que faz o trabalho pesado de conexão com o servidor de e-mail."""
    try:
        msg = MIMEMultipart()
        msg['From'] = SMTP_USER
        msg['To'] = destino
        msg['Subject'] = "SkyNode - Recuperação de Senha"

        corpo = f"""
        <h2>Recuperação de Acesso - SkyNode</h2>
        <p>Olá,</p>
        <p>Sua nova senha provisória é: <strong>{nova_senha}</strong></p>
        <p>Por segurança, faça login e altere esta senha imediatamente no menu de Configurações.</p>
        <br>
        <small>Este é um e-mail automático, por favor não responda.</small>
        """
        msg.attach(MIMEText(corpo, 'html'))

        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.sendmail(SMTP_USER, destino, msg.as_string())
        print(f"📌 E-mail enviado para {destino}")
    except Exception as e:
        print(f"❌ Erro crítico no envio de e-mail: {e}")

def disparar_recuperacao(destino, nova_senha):
    """Envia o e-mail em segundo plano através do ThreadPool."""
    email_executor.submit(_enviar_email_disparo, destino, nova_senha)