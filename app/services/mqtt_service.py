"""
app/services/mqtt_service.py
Serviço MQTT — Estado global do ESP32, processamento de mensagens e cliente Mosquitto.

Estado compartilhado:
  - esp_telemetria: dict mutado em tempo de execução pelo on_message() (thread do loop MQTT).
    Importar sempre por referência (nunca copiar) para garantir leitura atualizada nas rotas.
  - esp_queda_tracker: rastreia quedas e reconexões para auditoria.
"""

import json

from datetime import datetime

import paho.mqtt.client as mqtt

from app.core.database import DB_PATH
from app.services.auditoria_service import registrar_auditoria
from app.services.energia_service import processar_leitura

# --- TELEMETRIA E STATUS DO ESP32 EM TEMPO REAL ---

esp_telemetria = {
    "status": "desconhecido",
    "ultimo_visto": None,
    "ip": None,
    "rssi": None,
    "uptime": None,
    "potencia_w": 0.0,
    "ultimo_pulso_tempo": None,
    "sd_status": "DESCONHECIDO"   # "OK", "FALHA" ou "DESCONHECIDO"
}

# Rastreador de conexão/quedas do ESP32 para auditoria
esp_queda_tracker = {
    "ultimo_status": "desconhecido",
    "momento_desconexao": None,
    "ultimo_ip": None
}


# --- RECEPTOR MQTT EM BACKGROUND ---

def on_message(client, userdata, msg):

    global esp_telemetria

    topic = msg.topic

    payload_raw = msg.payload.decode(errors="ignore").strip()

    # 1. Notificação LWT do Broker MQTT ("online" ao conectar ou "offline" se desconectar)
    if topic == "home/energia/status":
        esp_telemetria["status"] = payload_raw
        if payload_raw == "online":
            esp_telemetria["ultimo_visto"] = datetime.now()
            if esp_queda_tracker["ultimo_status"] == "offline":
                duracao_txt = ""
                if esp_queda_tracker["momento_desconexao"]:
                    seg = int((datetime.now() - esp_queda_tracker["momento_desconexao"]).total_seconds())
                    if seg < 60:
                        duracao_txt = f" (Ficou offline por {seg}s)"
                    else:
                        duracao_txt = f" (Ficou offline por {seg // 60}m {seg % 60}s)"
                registrar_auditoria("ESP32_CONEXAO", "ESP32 Reconectado", f"Dispositivo restabeleceu conexao com o Broker MQTT.{duracao_txt}", "INFO", "Broker MQTT")
                esp_queda_tracker["ultimo_status"] = "online"
                esp_queda_tracker["momento_desconexao"] = None
        elif payload_raw == "offline":
            if esp_queda_tracker["ultimo_status"] != "offline":
                esp_queda_tracker["ultimo_status"] = "offline"
                esp_queda_tracker["momento_desconexao"] = datetime.now()
                ip_info = f" Último IP: {esp_telemetria.get('ip')}" if esp_telemetria.get('ip') else ""
                registrar_auditoria("ESP32_CONEXAO", "ESP32 Desconectado (Queda)", f"Sinal LWT recebido do Broker. Conexao perdida.{ip_info}", "AVISO", "Broker MQTT")
        return

    # 2. Heartbeat de telemetria periódico (a cada 20s)

    if topic == "home/energia/heartbeat":

        try:

            data = json.loads(payload_raw)

            esp_telemetria["status"] = "online"

            esp_telemetria["ultimo_visto"] = datetime.now()

            esp_telemetria["ip"] = data.get("ip")

            esp_telemetria["rssi"] = data.get("rssi")

            esp_telemetria["uptime"] = data.get("uptime")

            if esp_queda_tracker["ultimo_status"] == "offline":
                duracao_txt = ""
                if esp_queda_tracker["momento_desconexao"]:
                    seg = int((datetime.now() - esp_queda_tracker["momento_desconexao"]).total_seconds())
                    if seg < 60:
                        duracao_txt = f" (Ficou offline por {seg}s)"
                    else:
                        duracao_txt = f" (Ficou offline por {seg // 60}m {seg % 60}s)"
                registrar_auditoria("ESP32_CONEXAO", "ESP32 Reconectado", f"Heartbeat recebido. IP: {data.get('ip')}.{duracao_txt}", "INFO", "ESP32 IoT")
                esp_queda_tracker["ultimo_status"] = "online"
                esp_queda_tracker["momento_desconexao"] = None

            # Monitora o status do Cartão Micro SD e gera alerta na auditoria em caso de falha
            sd_status_novo = data.get("sd_status", "DESCONHECIDO")
            sd_status_anterior = esp_telemetria.get("sd_status", "DESCONHECIDO")
            esp_telemetria["sd_status"] = sd_status_novo
            if sd_status_novo == "FALHA" and sd_status_anterior != "FALHA":
                ip_sd = data.get("ip", "--")
                registrar_auditoria(
                    "SD_FALHA",
                    "Falha no Cartão Micro SD",
                    f"O ESP32 ({ip_sd}) reportou indisponibilidade do Cartão Micro SD. "
                    "O sistema ativou o buffer de emergência (RAM/NVS). "
                    "Verifique a integridade física do cartão e use 'Recuperar para SD' após a substituição.",
                    "CRITICO",
                    "ESP32 IoT"
                )
                print(f"[ALERTA CRÍTICO] Cartão Micro SD do ESP32 ({ip_sd}) reportou FALHA via heartbeat MQTT!")
            elif sd_status_novo == "OK" and sd_status_anterior == "FALHA":
                ip_sd = data.get("ip", "--")
                registrar_auditoria(
                    "SD_FALHA",
                    "Cartão Micro SD Restaurado",
                    f"O ESP32 ({ip_sd}) confirmou que o Cartão Micro SD voltou a operar normalmente.",
                    "INFO",
                    "ESP32 IoT"
                )
                print(f"[INFO] Cartão Micro SD do ESP32 ({ip_sd}) restaurado e operando normalmente.")

            # Se o heartbeat contiver dados de pulsos, sincroniza a leitura imediatamente

            if "potencia_w" in data and data["potencia_w"] is not None:
                esp_telemetria["potencia_w"] = float(data["potencia_w"])

            if "pulsos" in data:
                processar_leitura(float(data["pulsos"]))

        except Exception as e:

            print(f"Erro ao ler heartbeat: {e}")

        return

    # 3. Registro do Pulso de Energia do Medidor DDS238

    if topic == "home/energia/dds238":

        esp_telemetria["status"] = "online"

        esp_telemetria["ultimo_visto"] = datetime.now()

        try:
            data = json.loads(payload_raw)
            pulsos = float(data.get("pulsos", 0))
            agora_pulso = datetime.now()
            
            # Captura ou calcula a potência ativa em Watts instantânea
            if "potencia_w" in data and data["potencia_w"] is not None:
                esp_telemetria["potencia_w"] = float(data["potencia_w"])
            else:
                if esp_telemetria.get("ultimo_pulso_tempo"):
                    delta_s = (agora_pulso - esp_telemetria["ultimo_pulso_tempo"]).total_seconds()
                    if delta_s > 0.15:
                        esp_telemetria["potencia_w"] = round(4500.0 / delta_s, 1)
            esp_telemetria["ultimo_pulso_tempo"] = agora_pulso

            processar_leitura(pulsos)
        except Exception as e:
            print(f"Erro no processamento MQTT: {e}")


# --- INICIALIZAÇÃO DO CLIENTE MQTT ---

mqtt_client = mqtt.Client()

mqtt_client.on_message = on_message

try:

    mqtt_client.connect("localhost", 1883, 60)

    mqtt_client.subscribe([

        ("home/energia/dds238", 0),

        ("home/energia/status", 0),

        ("home/energia/heartbeat", 0)

    ])

    mqtt_client.loop_start()

except Exception as e:

    print(f"Erro ao conectar no MQTT: {e}")

