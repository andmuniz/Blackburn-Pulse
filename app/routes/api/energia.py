"""
app/routes/api/energia.py
Camada Controller — Endpoints JSON de energia, configurações, faturas e geração de PDF.
Todas as URLs, respostas JSON, queries SQL e regras de negócio são idênticas ao main.py original.
"""

import sqlite3
import json
import os
import subprocess
import tempfile

from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Form, Query
from fastapi.responses import HTMLResponse, FileResponse

from app.core.database import DB_PATH, BASE_DIR
from app.models import BANDEIRAS_TABELA
from app.services.energia_service import calcular_custo
from app.services.auditoria_service import registrar_auditoria
from app.services.mqtt_service import esp_telemetria, mqtt_client

router = APIRouter()


# --- ROTA PRINCIPAL DE DADOS DO DASHBOARD ---

@router.get("/api/dados")

def get_dados():

    conn = sqlite3.connect(DB_PATH)

    cursor = conn.cursor()

    cursor.execute("SELECT chave, valor FROM configuracoes")
    config = dict(cursor.fetchall())
    cursor.execute("SELECT chave, valor FROM configuracoes_texto")
    cfg_txt = dict(cursor.fetchall()) if cursor else {}

    # Injeta bandeira tarifária na config antes do cálculo de custos
    bandeira_tipo = cfg_txt.get("bandeira_tipo", "verde").strip().lower()
    if bandeira_tipo not in BANDEIRAS_TABELA:
        bandeira_tipo = "verde"
    config["bandeira_tipo"] = bandeira_tipo

    cursor.execute("SELECT kwh, timestamp FROM historico_consumo ORDER BY id DESC LIMIT 1")

    row_last = cursor.fetchone()

    leitura_inicial = config.get("leitura_inicial", 2504.0)

    kwh_atual = row_last[0] if row_last else leitura_inicial

    data_hora = row_last[1] if row_last else datetime.now().strftime("%d/%m/%Y %H:%M:%S")

    # Consumo real do ciclo = Leitura Atual - Leitura Inicial do ciclo

    consumo_ciclo = max(0.0, kwh_atual - leitura_inicial)

    # Cálculo dos custos e tributos

    custos = calcular_custo(consumo_ciclo, config)

    # Pico de demanda hoje

    cursor.execute("""

        SELECT (MAX(kwh) - MIN(kwh)) as consumo

        FROM historico_consumo

        WHERE timestamp >= date('now', 'localtime', 'start of day')

        GROUP BY strftime('%H', timestamp)

        ORDER BY consumo DESC LIMIT 1

    """)

    row_pico = cursor.fetchone()

    pico_demanda = row_pico[0] if row_pico and row_pico[0] else 0.0

    # Carrega configurações de texto (Data de início, rede do ESP32, bandeira, etc.)

    cursor.execute("SELECT chave, valor FROM configuracoes_texto")

    cfg_txt = dict(cursor.fetchall()) if cursor else {}

    # Injeta bandeira tarifária na config para calcular_custo usá-la

    bandeira_tipo = cfg_txt.get("bandeira_tipo", "verde")

    config["bandeira_tipo"] = bandeira_tipo

    config["tarifa_bandeira"] = None  # será resolvido dentro de calcular_custo pela tabela

    # Data de início do ciclo atual: prioriza a data salva pelo usuário

    data_inicio_ciclo = None

    data_salva_txt = cfg_txt.get("data_inicio_ciclo")

    if data_salva_txt:

        try:

            data_inicio_ciclo = datetime.strptime(data_salva_txt, "%Y-%m-%d")

        except Exception:

            data_inicio_ciclo = None

    hoje = datetime.now()

    if not data_inicio_ciclo:

        dia_fechamento = int(config.get("dia_fechamento", 5))

        if hoje.day >= dia_fechamento:

            data_inicio_ciclo = hoje.replace(day=dia_fechamento, hour=0, minute=0, second=0)

        else:

            mes_anterior = (hoje.replace(day=1) - timedelta(days=1))

            data_inicio_ciclo = mes_anterior.replace(day=min(dia_fechamento, mes_anterior.day), hour=0, minute=0, second=0)

    

    dias_decorridos = max(1, (hoje - data_inicio_ciclo).days + 1)

    media_diaria = consumo_ciclo / dias_decorridos

    projecao_mensal_kwh = media_diaria * 30.0

    projecao_custo = calcular_custo(projecao_mensal_kwh, config)["total"]

    conn.close()

    # Status e conectividade do ESP32 em tempo real

    agora = datetime.now()

    ultimo_visto = esp_telemetria.get("ultimo_visto")

    if esp_telemetria.get("status") == "offline" or not ultimo_visto:

        is_online = False

        segundos_atras = int((agora - ultimo_visto).total_seconds()) if ultimo_visto else None

    else:

        segundos_atras = int((agora - ultimo_visto).total_seconds())

        # Considera online se o último batimento ou pulso ocorreu há menos de 45 segundos

        is_online = (segundos_atras <= 45)

    if not ultimo_visto:

        ultimo_visto_txt = "Aguardando 1º contato"

    elif segundos_atras < 5:

        ultimo_visto_txt = "Agora mesmo"

    elif segundos_atras < 60:

        ultimo_visto_txt = f"Há {segundos_atras}s"

    elif segundos_atras < 3600:

        ultimo_visto_txt = f"Há {segundos_atras // 60} min"

    else:

        ultimo_visto_txt = ultimo_visto.strftime("%d/%m %H:%M")

    info_esp = {

        "online": is_online,

        "ultimo_visto_txt": ultimo_visto_txt,

        "segundos_atras": segundos_atras,

        "ip": esp_telemetria.get("ip") or "--",

        "rssi": esp_telemetria.get("rssi"),

        "sd_status": esp_telemetria.get("sd_status", "DESCONHECIDO")

    }

    # Cálculo de Potência Instantânea e Custo Horário em Tempo Real
    pot_w = float(esp_telemetria.get("potencia_w") or 0.0)
    # Trava de segurança: rejeita picos espúrios de ruído acima de 15.000W
    if pot_w > 15000.0:
        pot_w = 15000.0

    ultimo_p_t = esp_telemetria.get("ultimo_pulso_tempo")
    if ultimo_p_t:
        seg_sem_pulso = max(0.5, (agora - ultimo_p_t).total_seconds())
        # Decaimento dinâmico imediato:
        # 1 pulso = 4.500 Joules (Ws). Se passaram 't' segundos sem novo pulso, a potência real atual máxima é 4.500 / t.
        # Isso faz os Watts baixarem suave e imediatamente quando um aparelho é desligado!
        teto_fisico = 4500.0 / seg_sem_pulso
        if teto_fisico < pot_w:
            pot_w = round(teto_fisico, 1)
    elif not is_online:
        pot_w = 0.0

    tarifa_efetiva = (custos.get("total", 0.0) / max(0.01, custos.get("consumo_kwh", 1.0))) if custos.get("consumo_kwh", 0) > 0 else (custos.get("tarifa", 0.7894) / 0.75)
    custo_hora = round((pot_w / 1000.0) * tarifa_efetiva, 2)

    if pot_w < 400:
        faixa_pot = "economico"
        faixa_pot_txt = "Standby"
        faixa_pot_cor = "emerald"
    elif pot_w < 1500:
        faixa_pot = "moderado"
        faixa_pot_txt = "Moderado"
        faixa_pot_cor = "cyan"
    elif pot_w < 3500:
        faixa_pot = "alerta"
        faixa_pot_txt = "Demanda Alta"
        faixa_pot_cor = "amber"
    else:
        faixa_pot = "critico"
        faixa_pot_txt = "Pico Extremo"
        faixa_pot_cor = "rose"

    info_esp["potencia_w"] = pot_w

    return {
        "potencia_ao_vivo": {
            "watts": pot_w,
            "kw": round(pot_w / 1000.0, 2),
            "custo_hora": custo_hora,
            "faixa": faixa_pot,
            "faixa_txt": faixa_pot_txt,
            "faixa_cor": faixa_pot_cor
        },
        "kwh_atual": round(kwh_atual, 3),

        "leitura_inicial": round(leitura_inicial, 2),

        "consumo_ciclo": round(consumo_ciclo, 3),

        "consumo_mes": round(consumo_ciclo, 3),

        "custo_mes": round(custos["total"], 2),

        "detalhes_custo": custos,

        "data_hora": data_hora,

        "data_inicio_ciclo": data_inicio_ciclo.strftime("%d/%m/%Y"),

        "data_inicio_ciclo_iso": data_inicio_ciclo.strftime("%Y-%m-%d"),

        "dias_decorridos": dias_decorridos,

        "pico_demanda": round(pico_demanda, 2),

        "media_diaria": round(media_diaria, 3),

        "projecao_kwh": round(projecao_mensal_kwh, 2),

        "projecao_custo": round(projecao_custo, 2),

        "config": config,

        "bandeiras_tabela": BANDEIRAS_TABELA,
        "bandeira_atual": bandeira_tipo,
        "bandeiras": {
            "atual": bandeira_tipo,
            "tarifas": {
                "verde": float(config.get("tarifa_bandeira_verde", 0.0)),
                "amarela": float(config.get("tarifa_bandeira_amarela", 0.01885)),
                "vermelha1": float(config.get("tarifa_bandeira_vermelha1", 0.04463)),
                "vermelha2": float(config.get("tarifa_bandeira_vermelha2", 0.07877)),
                "escassez": float(config.get("tarifa_bandeira_escassez", 0.14200))
            }
        },
        "icms_config": {
            "faixa1_limite": float(config.get("icms_faixa1_limite", 90.0)),
            "faixa1_aliquota": float(config.get("icms_faixa1_aliquota", 0.0)),
            "faixa2_limite": float(config.get("icms_faixa2_limite", 200.0)),
            "faixa2_aliquota": float(config.get("icms_faixa2_aliquota", 12.0)),
            "faixa3_aliquota": float(config.get("icms_faixa3_aliquota", config.get("icms", 18.0)))
        },

        "esp": info_esp,

        "esp_config": {

            "ssid": cfg_txt.get("wifi_ssid", ""),

            "mqtt_server": cfg_txt.get("mqtt_server", "")

        }

    }


# --- ROTAS DE BANDEIRA TARIFÁRIA (SELEÇÃO E EDIÇÃO DINÂMICA) ---

@router.post("/api/bandeira/selecionar")
def api_selecionar_bandeira(tipo: str = Form(...)):
    tipo_limpo = tipo.strip().lower()
    if tipo_limpo not in BANDEIRAS_TABELA:
        tipo_limpo = "verde"
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO configuracoes_texto (chave, valor) VALUES ('bandeira_tipo', ?)", (tipo_limpo,))
    conn.commit()
    conn.close()
    info_b = BANDEIRAS_TABELA.get(tipo_limpo, {})
    registrar_auditoria("BANDEIRA", "Mudança de Bandeira Tarifária", f"Bandeira alterada para {info_b.get('nome', tipo_limpo)} (Tarifa: R$ {info_b.get('tarifa', 0.0):.5f}/kWh)", "INFO", "Painel Web")
    return {"status": "sucesso", "bandeira_tipo": tipo_limpo}


@router.post("/api/bandeira/editar")
def api_editar_tarifa_bandeira(tipo: str = Form(...), valor: float = Form(...)):
    tipo_limpo = tipo.strip().lower()
    if tipo_limpo not in BANDEIRAS_TABELA:
        return {"status": "erro", "mensagem": "Bandeira inválida"}
    chave = f"tarifa_bandeira_{tipo_limpo}"
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO configuracoes (chave, valor) VALUES (?, ?)", (chave, float(valor)))
    conn.commit()
    conn.close()
    registrar_auditoria("BANDEIRA", "Edição de Tarifa da Bandeira", f"Tarifa da bandeira {tipo_limpo.upper()} alterada para R$ {float(valor):.5f}/kWh", "AVISO", "Painel Web")
    return {"status": "sucesso", "tipo": tipo_limpo, "novo_valor": float(valor)}


# --- ROTA DE CONFIGURAÇÃO DE FAIXAS DE ICMS PROGRESSIVO ---

@router.post("/api/icms/editar")
def api_editar_icms(
    faixa1_limite: float = Form(90.0),
    faixa1_aliquota: float = Form(0.0),
    faixa2_limite: float = Form(200.0),
    faixa2_aliquota: float = Form(12.0),
    faixa3_aliquota: float = Form(18.0)
):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO configuracoes (chave, valor) VALUES ('icms_faixa1_limite', ?)", (float(faixa1_limite),))
    cursor.execute("INSERT OR REPLACE INTO configuracoes (chave, valor) VALUES ('icms_faixa1_aliquota', ?)", (float(faixa1_aliquota),))
    cursor.execute("INSERT OR REPLACE INTO configuracoes (chave, valor) VALUES ('icms_faixa2_limite', ?)", (float(faixa2_limite),))
    cursor.execute("INSERT OR REPLACE INTO configuracoes (chave, valor) VALUES ('icms_faixa2_aliquota', ?)", (float(faixa2_aliquota),))
    cursor.execute("INSERT OR REPLACE INTO configuracoes (chave, valor) VALUES ('icms_faixa3_aliquota', ?)", (float(faixa3_aliquota),))
    cursor.execute("INSERT OR REPLACE INTO configuracoes (chave, valor) VALUES ('icms', ?)", (float(faixa3_aliquota),))
    conn.commit()
    conn.close()
    registrar_auditoria("ICMS", "Atualização das Faixas de ICMS SP", f"Faixa 1 (até {faixa1_limite:g} kWh): {faixa1_aliquota}% | Faixa 2 (até {faixa2_limite:g} kWh): {faixa2_aliquota}% | Faixa 3 (> {faixa2_limite:g} kWh): {faixa3_aliquota}%", "INFO", "Painel Web")
    return {
        "status": "sucesso",
        "faixa1_limite": float(faixa1_limite),
        "faixa1_aliquota": float(faixa1_aliquota),
        "faixa2_limite": float(faixa2_limite),
        "faixa2_aliquota": float(faixa2_aliquota),
        "faixa3_aliquota": float(faixa3_aliquota)
    }


@router.post("/api/config")

def save_config(

    leitura_inicial: float = Form(...),

    dia_fechamento: float = Form(5.0),

    data_inicio_ciclo: Optional[str] = Form(None),

    bandeira_tipo: Optional[str] = Form("verde"),

    tarifa: float = Form(...),

    icms: Optional[float] = Form(18.0),

    pis: float = Form(...),

    cofins: float = Form(...),

    cosip: float = Form(...)

):

    conn = sqlite3.connect(DB_PATH)

    cursor = conn.cursor()

    # Verifica se a Leitura Inicial Física foi de fato alterada

    cursor.execute("SELECT valor FROM configuracoes WHERE chave='leitura_inicial'")

    row_ant = cursor.fetchone()

    leitura_antiga = row_ant[0] if row_ant else None

    # Só recalibra o ponto zero dos pulsos se a leitura inicial física foi realmente modificada pelo usuário

    if leitura_antiga is None or abs(leitura_antiga - leitura_inicial) > 0.001:

        cursor.execute("SELECT valor FROM configuracoes WHERE chave='ultimo_pulsos_esp'")

        row_last_p = cursor.fetchone()

        ultimo_p = row_last_p[0] if row_last_p else 0.0

        cursor.execute("UPDATE configuracoes SET valor=? WHERE chave='leitura_inicial'", (leitura_inicial,))

        cursor.execute("INSERT OR REPLACE INTO configuracoes (chave, valor) VALUES ('pulsos_iniciais_ciclo', ?)", (ultimo_p,))

        cursor.execute("INSERT INTO historico_consumo (timestamp, kwh) VALUES (datetime('now', 'localtime'), ?)", (leitura_inicial,))

    # Atualiza as demais configurações sem zerar o ciclo

    cursor.execute("UPDATE configuracoes SET valor=? WHERE chave='dia_fechamento'", (dia_fechamento,))

    cursor.execute("UPDATE configuracoes SET valor=? WHERE chave='tarifa'", (tarifa,))

    cursor.execute("UPDATE configuracoes SET valor=? WHERE chave='icms'", (icms,))

    cursor.execute("UPDATE configuracoes SET valor=? WHERE chave='pis'", (pis,))

    cursor.execute("UPDATE configuracoes SET valor=? WHERE chave='cofins'", (cofins,))

    cursor.execute("UPDATE configuracoes SET valor=? WHERE chave='cosip'", (cosip,))

    # Salva a data de início exata do ciclo se fornecida

    if data_inicio_ciclo and data_inicio_ciclo.strip():

        cursor.execute("INSERT OR REPLACE INTO configuracoes_texto (chave, valor) VALUES ('data_inicio_ciclo', ?)", (data_inicio_ciclo.strip(),))

    # Salva a bandeira tarifária selecionada

    bandeira_tipo_limpa = (bandeira_tipo or "verde").strip().lower()

    if bandeira_tipo_limpa not in BANDEIRAS_TABELA:

        bandeira_tipo_limpa = "verde"

    cursor.execute("INSERT OR REPLACE INTO configuracoes_texto (chave, valor) VALUES ('bandeira_tipo', ?)", (bandeira_tipo_limpa,))

    conn.commit()

    conn.close()

    registrar_auditoria("CONFIG", "Alteração de Parâmetros e Tributos", f"Tarifa Base: R$ {tarifa:.4f} | ICMS: {icms}% | PIS: {pis}% | COFINS: {cofins}% | COSIP: R$ {cosip:.2f} | Leitura Inicial: {leitura_inicial:.2f} kWh | Dia Fechamento: {dia_fechamento:g}", "INFO", "Painel Web")
    return {"status": "ok"}


@router.post("/api/restaurar-ciclo")

def restaurar_ciclo():

    """Recupera o consumo do ciclo anterior caso tenha sido zerado acidentalmente."""

    conn = sqlite3.connect(DB_PATH)

    cursor = conn.cursor()

    

    cursor.execute("SELECT valor FROM configuracoes WHERE chave='leitura_inicial'")

    row_l = cursor.fetchone()

    leitura_ini = row_l[0] if row_l else 1958.50

    cursor.execute("SELECT valor FROM configuracoes WHERE chave='ultimo_pulsos_esp'")

    row_p = cursor.fetchone()

    ultimo_p = row_p[0] if row_p else 0.0

    # Busca o maior kWh gravado no histórico antes do zeramento acidental

    cursor.execute("SELECT MAX(kwh) FROM historico_consumo WHERE kwh > ?", (leitura_ini,))

    row_max = cursor.fetchone()

    kwh_max = row_max[0] if row_max and row_max[0] else None

    if kwh_max and kwh_max > leitura_ini:

        kwh_consumido = kwh_max - leitura_ini

        pulsos_consumidos = kwh_consumido * 800.0

        pulsos_iniciais = max(0.0, ultimo_p - pulsos_consumidos)

        cursor.execute("INSERT OR REPLACE INTO configuracoes (chave, valor) VALUES ('pulsos_iniciais_ciclo', ?)", (pulsos_iniciais,))

        

        # Insere a leitura máxima recuperada no histórico

        agora_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        cursor.execute("INSERT INTO historico_consumo (timestamp, kwh) VALUES (?, ?)", (agora_str, round(kwh_max, 3)))

        conn.commit()

        conn.close()

        return {

            "status": "sucesso", 

            "mensagem": f"Ciclo restaurado com sucesso! Consumo recuperado: {kwh_consumido:.2f} kWh (Leitura atual: {kwh_max:.2f} kWh)."

        }

    else:

        conn.close()

        return {

            "status": "erro",

            "mensagem": "Nenhum histórico anterior com consumo acima da leitura inicial foi encontrado para restauração automática."

        }


@router.post("/api/esp32/config")

def save_esp32_config(

    ssid: str = Form(...),

    password: str = Form(""),

    mqtt_server: str = Form(...)

):

    """Envia novas configurações de Wi-Fi e Broker MQTT para o ESP32 salvar na memória permanente."""

    ssid = ssid.strip()

    password = password.strip()

    mqtt_server = mqtt_server.strip()

    if not ssid:

        return {"status": "erro", "mensagem": "O nome da rede Wi-Fi (SSID) não pode ser vazio."}

    if not mqtt_server:

        return {"status": "erro", "mensagem": "O IP do servidor MQTT não pode ser vazio."}

    # Salva no banco de dados local para persistência e preenchimento da tela

    conn = sqlite3.connect(DB_PATH)

    cursor = conn.cursor()

    cursor.execute("INSERT OR REPLACE INTO configuracoes_texto (chave, valor) VALUES ('wifi_ssid', ?)", (ssid,))

    cursor.execute("INSERT OR REPLACE INTO configuracoes_texto (chave, valor) VALUES ('mqtt_server', ?)", (mqtt_server,))

    conn.commit()

    conn.close()

    # Prepara payload JSON para o ESP32

    payload = json.dumps({

        "ssid": ssid,

        "password": password,

        "mqtt_server": mqtt_server

    })

    try:

        # Publica no tópico que o ESP32 escuta

        mqtt_client.publish("home/energia/config/set", payload, qos=1)

        return {

            "status": "sucesso",

            "mensagem": f"Configurações enviadas ao ESP32 via MQTT! O microcontrolador salvará na memória permanente e reiniciará conectado à rede '{ssid}'."

        }

    except Exception as e:

        return {"status": "erro", "mensagem": f"Erro ao enviar comando via MQTT: {e}"}


@router.post("/api/sincronizar-zerar-ciclo")

def sincronizar_zerar_ciclo():

    """Zera o consumo do ciclo atual a partir do instante atual, mantendo a leitura inicial."""

    conn = sqlite3.connect(DB_PATH)

    cursor = conn.cursor()

    cursor.execute("SELECT valor FROM configuracoes WHERE chave='ultimo_pulsos_esp'")

    row_p = cursor.fetchone()

    ultimo_p = row_p[0] if row_p else 0.0

    cursor.execute("SELECT valor FROM configuracoes WHERE chave='leitura_inicial'")

    row_l = cursor.fetchone()

    leitura_ini = row_l[0] if row_l else 2504.0

    # Define o offset de pulsos para o valor atual do ESP32

    cursor.execute("INSERT OR REPLACE INTO configuracoes (chave, valor) VALUES ('pulsos_iniciais_ciclo', ?)", (ultimo_p,))

    cursor.execute("INSERT INTO historico_consumo (timestamp, kwh) VALUES (datetime('now', 'localtime'), ?)", (leitura_ini,))

    conn.commit()

    conn.close()

    registrar_auditoria("CICLO", "Sincronização / Ponto Zero do Ciclo", f"Offset recalibrado para {int(ultimo_p)} pulsos do ESP32 (Leitura: {leitura_ini:.2f} kWh)", "AVISO", "Painel Web")
    return {"status": "sucesso", "mensagem": f"Ponto de partida sincronizado com {int(ultimo_p)} pulsos do ESP32."}


@router.get("/api/historico")

def get_historico(

    tipo: str = Query("diario"),

    data_inicio: Optional[str] = Query(None),

    data_fim: Optional[str] = Query(None)

):

    conn = sqlite3.connect(DB_PATH)

    cursor = conn.cursor()

    cursor.execute("SELECT chave, valor FROM configuracoes")

    config = dict(cursor.fetchall())

    tarifa = config.get("tarifa", 0.63)

    fator_impostos = 1.0 - ((config.get("icms", 18.0) + config.get("pis", 0.9) + config.get("cofins", 4.2)) / 100.0)

    labels = []

    valores_kwh = []

    valores_rs = []

    if tipo == "custom" and data_inicio and data_fim:

        cursor.execute("""

            SELECT date(timestamp) as dia, (MAX(kwh) - MIN(kwh)) as consumo

            FROM historico_consumo

            WHERE date(timestamp) >= date(?) AND date(timestamp) <= date(?)

            GROUP BY date(timestamp)

            ORDER BY date(timestamp) ASC

        """, (data_inicio, data_fim))

        rows = cursor.fetchall()

        for r in rows:

            labels.append(datetime.strptime(r[0], "%Y-%m-%d").strftime("%d/%m"))

            c = round(r[1] or 0.0, 3)

            valores_kwh.append(c)

            custo = (c * tarifa / fator_impostos) if fator_impostos > 0 else (c * tarifa)

            valores_rs.append(round(custo, 2))

    elif tipo == "diario":

        cursor.execute("""

            SELECT strftime('%H:00', timestamp) as hora, (MAX(kwh) - MIN(kwh)) as consumo

            FROM historico_consumo

            WHERE timestamp >= date('now', 'localtime', 'start of day')

            GROUP BY strftime('%H', timestamp)

            ORDER BY hora ASC

        """)

        rows = cursor.fetchall()

        mapa_horas = {r[0]: round(r[1] or 0.0, 3) for r in rows}

        for h in range(24):

            hora_str = f"{h:02d}:00"

            labels.append(hora_str)

            c = mapa_horas.get(hora_str, 0.0)

            valores_kwh.append(c)

            custo = (c * tarifa / fator_impostos) if fator_impostos > 0 else (c * tarifa)

            valores_rs.append(round(custo, 2))

    elif tipo == "semanal":

        cursor.execute("""

            SELECT date(timestamp) as dia, strftime('%w', timestamp) as dia_semana, (MAX(kwh) - MIN(kwh)) as consumo

            FROM historico_consumo

            WHERE timestamp >= date('now', 'localtime', '-6 days')

            GROUP BY date(timestamp)

            ORDER BY date(timestamp) ASC

        """)

        rows = cursor.fetchall()

        dias_semana_map = ['Dom', 'Seg', 'Ter', 'Qua', 'Qui', 'Sex', 'Sáb']

        for r in rows:

            dia_str = datetime.strptime(r[0], "%Y-%m-%d").strftime("%d/%m")

            dia_nome = dias_semana_map[int(r[1])]

            labels.append(f"{dia_nome} ({dia_str})")

            c = round(r[2] or 0.0, 3)

            valores_kwh.append(c)

            custo = (c * tarifa / fator_impostos) if fator_impostos > 0 else (c * tarifa)

            valores_rs.append(round(custo, 2))

    elif tipo == "mensal":

        cursor.execute("""

            SELECT date(timestamp) as dia, (MAX(kwh) - MIN(kwh)) as consumo

            FROM historico_consumo

            WHERE timestamp >= date('now', 'localtime', '-29 days')

            GROUP BY date(timestamp)

            ORDER BY date(timestamp) ASC

        """)

        rows = cursor.fetchall()

        for r in rows:

            labels.append(datetime.strptime(r[0], "%Y-%m-%d").strftime("%d/%m"))

            c = round(r[1] or 0.0, 3)

            valores_kwh.append(c)

            custo = (c * tarifa / fator_impostos) if fator_impostos > 0 else (c * tarifa)

            valores_rs.append(round(custo, 2))

    elif tipo == "anual":

        cursor.execute("""

            SELECT strftime('%Y-%m', timestamp) as mes, (MAX(kwh) - MIN(kwh)) as consumo

            FROM historico_consumo

            WHERE timestamp >= date('now', 'localtime', '-11 months', 'start of month')

            GROUP BY strftime('%Y-%m', timestamp)

            ORDER BY mes ASC

        """)

        rows = cursor.fetchall()

        meses_map = {'01':'Jan', '02':'Fev', '03':'Mar', '04':'Abr', '05':'Mai', '06':'Jun', '07':'Jul', '08':'Ago', '09':'Set', '10':'Out', '11':'Nov', '12':'Dez'}

        for r in rows:

            ano, m = r[0].split('-')

            labels.append(f"{meses_map.get(m, m)}/{ano[2:]}")

            c = round(r[1] or 0.0, 3)

            valores_kwh.append(c)

            custo = (c * tarifa / fator_impostos) if fator_impostos > 0 else (c * tarifa)

            valores_rs.append(round(custo, 2))

    conn.close()

    return {

        "tipo": tipo,

        "labels": labels,

        "kwh": valores_kwh,

        "rs": valores_rs

    }


# --- GERENCIAMENTO PERMANENTE DE FATURAS FECHADAS ---

@router.get("/api/faturas")

def get_faturas():

    conn = sqlite3.connect(DB_PATH)

    cursor = conn.cursor()

    cursor.execute("""

        SELECT id, mes_referencia, data_inicio, data_fim, leitura_inicial, leitura_final, consumo_kwh, valor_total, detalhes_json

        FROM faturas_historico

        ORDER BY id DESC

    """)

    rows = cursor.fetchall()

    conn.close()

    faturas = []

    for r in rows:

        faturas.append({

            "id": r[0],

            "mes_referencia": r[1],

            "data_inicio": r[2],

            "data_fim": r[3],

            "leitura_inicial": r[4],

            "leitura_final": r[5],

            "consumo_kwh": r[6],

            "valor_total": r[7],

            "detalhes": json.loads(r[8]) if r[8] else {}

        })

    return faturas


@router.post("/api/faturas/fechar")

def fechar_fatura():

    conn = sqlite3.connect(DB_PATH)

    cursor = conn.cursor()

    cursor.execute("SELECT chave, valor FROM configuracoes")

    config = dict(cursor.fetchall())

    # Adiciona bandeira à config para o cálculo correto

    cursor.execute("SELECT valor FROM configuracoes_texto WHERE chave='bandeira_tipo'")

    row_bnd = cursor.fetchone()

    config["bandeira_tipo"] = row_bnd[0] if row_bnd else "verde"

    config["tarifa_bandeira"] = None

    cursor.execute("SELECT kwh, timestamp FROM historico_consumo ORDER BY id DESC LIMIT 1")

    row_last = cursor.fetchone()

    

    leitura_inicial = config.get("leitura_inicial", 2504.0)

    kwh_atual = row_last[0] if row_last else leitura_inicial

    consumo = max(0.0, kwh_atual - leitura_inicial)

    custos = calcular_custo(consumo, config)

    agora = datetime.now()

    mes_ref = agora.strftime("%m/%Y")

    data_fim = agora.strftime("%d/%m/%Y %H:%M")

    

    cursor.execute("SELECT valor FROM configuracoes_texto WHERE chave='data_inicio_ciclo'")
    row_dt_ini = cursor.fetchone()
    data_inicio_custom = None
    if row_dt_ini and row_dt_ini[0]:
        try:
            data_inicio_custom = datetime.strptime(row_dt_ini[0].strip(), "%Y-%m-%d")
        except Exception:
            pass

    if data_inicio_custom:
        data_inicio = data_inicio_custom.strftime("%d/%m/%Y")
    else:
        dia_fechamento = int(config.get("dia_fechamento", 5))
        if agora.day >= dia_fechamento:
            data_ini_dt = agora.replace(day=dia_fechamento, hour=0, minute=0, second=0)
        else:
            mes_ant = agora.replace(day=1) - timedelta(days=1)
            data_ini_dt = mes_ant.replace(day=min(dia_fechamento, mes_ant.day), hour=0, minute=0, second=0)
        data_inicio = data_ini_dt.strftime("%d/%m/%Y")

    cursor.execute("""
        INSERT OR REPLACE INTO faturas_historico 
        (mes_referencia, data_inicio, data_fim, leitura_inicial, leitura_final, consumo_kwh, valor_total, detalhes_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (mes_ref, data_inicio, data_fim, leitura_inicial, kwh_atual, consumo, custos["total"], json.dumps(custos)))

    # Pega os pulsos atuais do ESP e define como nova base do ciclo
    cursor.execute("SELECT valor FROM configuracoes WHERE chave='ultimo_pulsos_esp'")
    row_p = cursor.fetchone()
    ultimo_p = row_p[0] if row_p else 0.0

    cursor.execute("UPDATE configuracoes SET valor=? WHERE chave='leitura_inicial'", (kwh_atual,))
    cursor.execute("INSERT OR REPLACE INTO configuracoes (chave, valor) VALUES ('pulsos_iniciais_ciclo', ?)", (ultimo_p,))
    cursor.execute("INSERT OR REPLACE INTO configuracoes_texto (chave, valor) VALUES ('data_inicio_ciclo', ?)", (agora.strftime("%Y-%m-%d"),))

    conn.commit()

    conn.close()

    registrar_auditoria("CICLO", f"Fechamento de Fatura ({mes_ref})", f"Fatura de {mes_ref} fechada com consumo de {consumo:.2f} kWh e valor total de R$ {custos['total']:.2f}. Nova leitura inicial: {kwh_atual:.2f} kWh.", "INFO", "Painel Web")
    return {

        "status": "sucesso",

        "mensagem": f"Fatura de {mes_ref} fechada e arquivada com sucesso!",

        "nova_leitura_inicial": kwh_atual

    }


# --- ROTA PARA RESETAR MEDIÇÕES ---

@router.post("/api/resetar-dados")

def resetar_dados():

    conn = sqlite3.connect(DB_PATH)

    cursor = conn.cursor()

    cursor.execute("SELECT valor FROM configuracoes WHERE chave='leitura_inicial'")

    row = cursor.fetchone()

    leitura_inicial = row[0] if row else 2504.0

    # Reseta o offset dos pulsos para os pulsos atuais do ESP

    cursor.execute("SELECT valor FROM configuracoes WHERE chave='ultimo_pulsos_esp'")

    row_p = cursor.fetchone()

    ultimo_p = row_p[0] if row_p else 0.0

    cursor.execute("INSERT OR REPLACE INTO configuracoes (chave, valor) VALUES ('pulsos_iniciais_ciclo', ?)", (ultimo_p,))

    cursor.execute("DELETE FROM historico_consumo")

    cursor.execute("INSERT INTO historico_consumo (timestamp, kwh) VALUES (datetime('now', 'localtime'), ?)", (leitura_inicial,))

    conn.commit()

    conn.close()

    return {"status": "sucesso", "mensagem": "Medições em tempo real resetadas (as faturas fechadas foram preservadas)."}


# --- GERADOR DE FATURA PDF ---

@router.get("/gerar-pdf")
@router.get("/gerar-pdf/{fatura_id}")

def gerar_pdf(fatura_id: Optional[int] = None):

    conn = sqlite3.connect(DB_PATH)

    cursor = conn.cursor()

    cursor.execute("SELECT chave, valor FROM configuracoes")

    config = dict(cursor.fetchall())

    # Adiciona bandeira para cálculo correto

    cursor.execute("SELECT valor FROM configuracoes_texto WHERE chave='bandeira_tipo'")

    row_bnd = cursor.fetchone()

    config["bandeira_tipo"] = row_bnd[0] if row_bnd else "verde"

    config["tarifa_bandeira"] = None

    if fatura_id:

        cursor.execute("SELECT mes_referencia, data_inicio, data_fim, leitura_inicial, leitura_final, consumo_kwh, valor_total, detalhes_json FROM faturas_historico WHERE id=?", (fatura_id,))

        row_fat = cursor.fetchone()

        if not row_fat:

            conn.close()

            return HTMLResponse("<h1>Fatura não encontrada</h1>")

        mes_ref, dt_ini, dt_fim, leitura_ini, leitura_fim, consumo, total_val, det_json = row_fat

        custos = json.loads(det_json) if det_json else calcular_custo(consumo, config)

        titulo_fatura = f"FATURA DE ENERGIA - REF. {mes_ref}"

        periodo_str = f"Período: {dt_ini} a {dt_fim}"

    else:

        cursor.execute("SELECT kwh, timestamp FROM historico_consumo ORDER BY id DESC LIMIT 1")

        row_last = cursor.fetchone()

        leitura_ini = config.get("leitura_inicial", 2504.0)

        leitura_fim = row_last[0] if row_last else leitura_ini

        consumo = max(0.0, leitura_fim - leitura_ini)

        custos = calcular_custo(consumo, config)

        agora = datetime.now().strftime("%d/%m/%Y %H:%M:%S")

        titulo_fatura = "DEMONSTRATIVO DE FATURA ATUAL (EM ABERTO)"

        periodo_str = f"Emissão em Tempo Real: {agora}"

    conn.close()

    band_nome = custos.get("bandeira_nome", "Bandeira Verde")
    band_tarifa = custos.get("tarifa_bandeira", 0.0)
    band_val_bruto = custos.get("bruto_bandeira", 0.0)

    if band_tarifa > 0:
        linha_bandeira = f"<tr><td><b>Adicional {band_nome}</b></td><td>{consumo:.2f} kWh x R$ {band_tarifa:.5f}</td><td>R$ {band_val_bruto:.2f}</td></tr>"
    else:
        linha_bandeira = f"<tr><td><b>{band_nome}</b></td><td>Sem acréscimo tarifário (R$ 0,00/kWh)</td><td>R$ 0,00</td></tr>"

    html = f"""

    <!DOCTYPE html><html><head><meta charset='utf-8'>

    <title>Fatura Blackburn-Pulse</title>

    <style>

      body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; padding: 40px; color: #1e293b; background: #fff; }}

      .box {{ border: 2px solid #059669; padding: 30px; border-radius: 12px; max-width: 800px; margin: 0 auto; }}

      .header {{ border-bottom: 2px solid #e2e8f0; padding-bottom: 15px; margin-bottom: 20px; }}

      .title {{ color: #059669; font-size: 24px; font-weight: 800; margin: 0; }}

      .subtitle {{ color: #64748b; font-size: 13px; margin: 4px 0 0 0; }}

      .kpi-grid {{ display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 15px; background: #f8fafc; padding: 15px; border-radius: 8px; margin-bottom: 25px; }}

      .kpi-item {{ font-size: 12px; color: #64748b; }}

      .kpi-val {{ font-size: 18px; font-weight: 700; color: #0f172a; margin-top: 4px; }}

      table {{ width: 100%; border-collapse: collapse; margin-top: 15px; font-size: 14px; }}

      th {{ background: #f1f5f9; text-align: left; padding: 10px; border-bottom: 2px solid #cbd5e1; font-size: 12px; color: #475569; }}

      td {{ padding: 10px; border-bottom: 1px solid #e2e8f0; }}

      .total-row {{ font-size: 18px; font-weight: 800; color: #059669; background: #ecfdf5; }}

    </style>

    </head>

    <body>

      <div class='box'>

        <div class='header'>

          <div class='title'>&#9889; BLACKBURN-PULSE</div>

          <div class='subtitle'>{titulo_fatura}</div>

          <p style='font-size:12px;color:#64748b;margin:6px 0 0 0;'><b>{periodo_str}</b> | <b>Unidade Consumidora:</b> #48291 | <b>Bandeira:</b> {band_nome}</p>

        </div>

        <div class='kpi-grid'>

          <div class='kpi-item'>LEITURA ANTERIOR (INICIAL)<div class='kpi-val'>{leitura_ini:.2f} kWh</div></div>

          <div class='kpi-item'>LEITURA ATUAL (FINAL)<div class='kpi-val'>{leitura_fim:.2f} kWh</div></div>

          <div class='kpi-item'>CONSUMO FATURADO<div class='kpi-val' style='color:#059669;'>{consumo:.2f} kWh</div></div>

        </div>

        <h3>Detalhamento do Faturamento e Tributos</h3>

        <table>

          <thead>

            <tr><th>Item / Descrição</th><th>Base / Alíquota</th><th>Valor (R$)</th></tr>

          </thead>

          <tbody>

            <tr><td>Consumo Ativo (kWh)</td><td>{consumo:.2f} kWh x R$ {custos.get('tarifa', 0.63):.4f}</td><td>R$ {custos.get('bruto_energia', custos.get('bruto', 0.0)):.2f}</td></tr>

            {linha_bandeira}

            <tr><td>Base de Cálculo Tributária (Impostos "Por Dentro")</td><td>1 - ({custos.get('icms_pct', config.get('icms', 18.0)):.1f}% + {config.get('pis', 0.9):.2f}% + {config.get('cofins', 4.2):.2f}%)</td><td>R$ {custos.get('base_calc', 0.0):.2f}</td></tr>

            <tr><td>ICMS Estadual</td><td>{custos.get('icms_pct', 18.0):.1f}% ({custos.get('icms_faixa', 'Padrão SP')})</td><td>R$ {custos.get('val_icms', 0.0):.2f}</td></tr>

            <tr><td>PIS / COFINS Federal</td><td>{config.get('pis', 0.9):.2f}% + {config.get('cofins', 4.2):.2f}%</td><td>R$ {(custos.get('val_pis', 0.0) + custos.get('val_cofins', 0.0)):.2f}</td></tr>

            <tr><td>COSIP / CIP (Iluminação Pública)</td><td>Tarifa Fixa Municipal</td><td>R$ {custos.get('cosip', 0.0):.2f}</td></tr>

            <tr class='total-row'><td><b>VALOR TOTAL A PAGAR</b></td><td></td><td><b>R$ {custos.get('total', 0.0):.2f}</b></td></tr>

          </tbody>

        </table>

        </div>

    </body></html>

    """

    temp_dir = tempfile.gettempdir()

    html_file = os.path.join(temp_dir, f"fatura_{fatura_id or 'atual'}.html")

    pdf_file = os.path.join(temp_dir, f"fatura_{fatura_id or 'atual'}.pdf")

    with open(html_file, "w", encoding="utf-8") as f:

        f.write(html)

    try:

        subprocess.run(["wkhtmltopdf", html_file, pdf_file], check=True)

        return FileResponse(pdf_file, media_type="application/pdf", filename=f"fatura_blackburn_pulse_{fatura_id or 'atual'}.pdf")

    except Exception:

        return HTMLResponse(content=html)

