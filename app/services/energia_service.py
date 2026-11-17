"""
app/services/energia_service.py
Serviço de Energia — Cálculos tributários (ICMS, PIS, COFINS, COSIP, Bandeiras)
e processamento/retomada contínua de leituras do medidor DDS238-4.
"""

import sqlite3

from datetime import datetime, timedelta

from app.core.database import DB_PATH
from app.models import BANDEIRAS_TABELA


# --- CÁLCULO TRIBUTÁRIO ENEL SP (POR DENTRO + BANDEIRAS TARIFÁRIAS) ---

def calcular_custo(consumo_kwh: float, config: dict) -> dict:
    tarifa = float(config.get("tarifa", 0.63))
    
    # Bandeira Tarifária (ANEEL / Enel SP)
    bandeira_tipo = str(config.get("bandeira_tipo", "verde")).lower().strip()
    if bandeira_tipo not in BANDEIRAS_TABELA:
        bandeira_tipo = "verde"
    info_bandeira = BANDEIRAS_TABELA[bandeira_tipo]
    
    # Verifica se há tarifa personalizada configurada para esta bandeira
    chave_tarifa = f"tarifa_bandeira_{bandeira_tipo}"
    if chave_tarifa in config and config[chave_tarifa] is not None:
        tarifa_bandeira = float(config[chave_tarifa])
    elif "tarifa_bandeira" in config and config["tarifa_bandeira"] is not None:
        tarifa_bandeira = float(config["tarifa_bandeira"])
    else:
        tarifa_bandeira = float(info_bandeira["tarifa"])
        
    # Faixas progressivas de ICMS SP (RICMS/SP - Classe Residencial B1):
    f1_lim = float(config.get("icms_faixa1_limite", 90.0))
    f1_aliq = float(config.get("icms_faixa1_aliquota", 0.0))
    f2_lim = float(config.get("icms_faixa2_limite", 200.0))
    f2_aliq = float(config.get("icms_faixa2_aliquota", 12.0))
    f3_aliq = float(config.get("icms_faixa3_aliquota", config.get("icms", 18.0)))
    if f3_aliq <= 0:
        f3_aliq = 18.0

    if consumo_kwh <= f1_lim:
        icms_pct = f1_aliq
        icms_faixa = f"Isento (até {f1_lim:g} kWh)" if f1_aliq == 0 else f"{f1_aliq:.1f}% (até {f1_lim:g} kWh)"
    elif consumo_kwh <= f2_lim:
        icms_pct = f2_aliq
        icms_faixa = f"{f2_aliq:.1f}% ({f1_lim+1:g} a {f2_lim:g} kWh)"
    else:
        icms_pct = f3_aliq
        icms_faixa = f"{f3_aliq:.1f}% (> {f2_lim:g} kWh)"

    pis_pct = float(config.get("pis", 0.9))

    cofins_pct = float(config.get("cofins", 4.2))

    cosip = float(config.get("cosip", 12.50))

    icms = icms_pct / 100.0

    pis = pis_pct / 100.0

    cofins = cofins_pct / 100.0

    bruto_energia = consumo_kwh * tarifa

    bruto_bandeira = consumo_kwh * tarifa_bandeira

    bruto_total = bruto_energia + bruto_bandeira

    fator_impostos = 1.0 - (icms + pis + cofins)

    if fator_impostos > 0 and consumo_kwh > 0:

        base_calc = bruto_total / fator_impostos

        val_icms = base_calc * icms

        val_pis = base_calc * pis

        val_cofins = base_calc * cofins

        total = base_calc + cosip

        val_bandeira_com_impostos = (bruto_bandeira / fator_impostos) if bruto_bandeira > 0 else 0.0

    else:

        base_calc = bruto_total

        val_icms = 0.0

        val_pis = 0.0

        val_cofins = 0.0

        val_bandeira_com_impostos = 0.0

        total = cosip if consumo_kwh > 0 else 0.0

    return {

        "consumo_kwh": round(consumo_kwh, 2),

        "tarifa": round(tarifa, 4),

        "tarifa_bandeira": round(tarifa_bandeira, 5),

        "bandeira_tipo": bandeira_tipo,

        "bandeira_nome": info_bandeira["nome"],

        "bandeira_cor": info_bandeira["cor"],

        "icms_pct": icms_pct,

        "icms_faixa": icms_faixa,
        
        "bruto": round(bruto_total, 2),

        "bruto_energia": round(bruto_energia, 2),

        "bruto_bandeira": round(bruto_bandeira, 2),

        "val_bandeira_com_impostos": round(val_bandeira_com_impostos, 2),

        "base_calc": round(base_calc, 2),

        "val_icms": round(val_icms, 2),

        "val_pis": round(val_pis, 2),

        "val_cofins": round(val_cofins, 2),

        "cosip": round(cosip, 2),

        "total": round(total, 2)

    }


# --- PROCESSAMENTO E RETOMADA CONTÍNUA DE LEITURAS ---

def processar_leitura(pulsos: float):

    conn = sqlite3.connect(DB_PATH)

    cursor = conn.cursor()

    try:

        # Guarda o último número bruto de pulsos enviado pelo ESP32

        cursor.execute("INSERT OR REPLACE INTO configuracoes (chave, valor) VALUES ('ultimo_pulsos_esp', ?)", (pulsos,))

        # Busca a referência de pulsos no início do ciclo

        cursor.execute("SELECT valor FROM configuracoes WHERE chave='pulsos_iniciais_ciclo'")

        row_p_ini = cursor.fetchone()

        pulsos_iniciais = row_p_ini[0] if row_p_ini else -1.0

        # Se é a primeira leitura ou se o ESP32 resetou para menor que a base:

        if pulsos_iniciais < 0 or pulsos < pulsos_iniciais:

            pulsos_iniciais = pulsos

            cursor.execute("INSERT OR REPLACE INTO configuracoes (chave, valor) VALUES ('pulsos_iniciais_ciclo', ?)", (pulsos_iniciais,))

        # Busca a Leitura Inicial Física do medidor

        cursor.execute("SELECT valor FROM configuracoes WHERE chave='leitura_inicial'")

        row_l_ini = cursor.fetchone()

        leitura_inicial = row_l_ini[0] if row_l_ini else 2504.0

        # Consumo acumulado no ciclo ATUAL = (Pulsos Atuais - Pulsos no Início do Ciclo) / 800 (DDS238-4 com 800 imp/kWh)

        pulsos_novos = max(0.0, pulsos - pulsos_iniciais)

        consumo_kwh_ciclo = pulsos_novos / 800.0

        # Leitura Física Real Atual no Visor = Leitura Inicial + Consumo do Ciclo

        kwh_total = round(leitura_inicial + consumo_kwh_ciclo, 3)

        # Verifica último registro gravado no histórico

        cursor.execute("SELECT timestamp, kwh FROM historico_consumo ORDER BY id DESC LIMIT 1")

        row_last = cursor.fetchone()

        agora = datetime.now()

        agora_str = agora.strftime("%Y-%m-%d %H:%M:%S")

        if row_last:

            try:

                ultimo_tempo = datetime.strptime(row_last[0], "%Y-%m-%d %H:%M:%S")

            except Exception:

                try:

                    ultimo_tempo = datetime.fromisoformat(row_last[0])

                except Exception:

                    ultimo_tempo = agora

            ultimo_kwh = float(row_last[1])

            delta_tempo = (agora - ultimo_tempo).total_seconds()

            delta_kwh = kwh_total - ultimo_kwh

            # RETOMADA INTELIGENTE:

            # Se o App ou o ESP32 ficaram offline (> 10 minutos) e acumularam consumo durante a queda:

            # Preenche o intervalo interpolando suavemente a cada 30 minutos para que o histórico

            # e os gráficos diários/horários reflitam o consumo contínuo ocorrido durante a ausência.

            if delta_tempo > 600 and delta_kwh > 0.001:

                passos = max(1, int(delta_tempo // 1800))

                if passos > 1:

                    kwh_passo = delta_kwh / passos

                    seg_passo = delta_tempo / passos

                    for i in range(1, passos):

                        t_interp = ultimo_tempo + timedelta(seconds=i * seg_passo)

                        k_interp = round(ultimo_kwh + i * kwh_passo, 3)

                        cursor.execute("INSERT INTO historico_consumo (timestamp, kwh) VALUES (?, ?)", 

                                       (t_interp.strftime("%Y-%m-%d %H:%M:%S"), k_interp))

            # Evita poluir o banco gravando registros redundantes a cada heartbeat se o consumo não mudou

            if delta_kwh <= 0.0001 and delta_tempo < 300:

                conn.commit()

                return

        cursor.execute("INSERT INTO historico_consumo (timestamp, kwh) VALUES (?, ?)", (agora_str, kwh_total))

        conn.commit()

    except Exception as e:

        print(f"Erro no processamento da leitura: {e}")

    finally:

        conn.close()

