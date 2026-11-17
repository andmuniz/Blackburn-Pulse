# app/routes/api/analytics.py
# Rota: GET /api/analytics/dados
# Transferido integralmente de main.py (linhas 1594–1871)

import sqlite3
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Query

from app.core.database import DB_PATH
from app.services.energia_service import calcular_custo

router = APIRouter()


@router.get("/api/analytics/dados")
def api_analytics_dados(
    periodo: str = Query("30d"),
    data_inicio: Optional[str] = Query(None),
    data_fim: Optional[str] = Query(None)
):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT chave, valor FROM configuracoes")
    config = dict(cursor.fetchall())
    cursor.execute("SELECT chave, valor FROM configuracoes_texto")
    cfg_txt = dict(cursor.fetchall()) if cursor else {}

    bandeira_tipo = cfg_txt.get("bandeira_tipo", "verde").strip().lower()
    config["bandeira_tipo"] = bandeira_tipo
    tarifa = float(config.get("tarifa", 0.7894))

    agora = datetime.now()
    filtro_sql = ""
    params = []

    if periodo == "24h":
        # Pega a partir de 00:00:00 do dia de hoje para não misturar
        # as mesmas horas de ontem com as de hoje
        filtro_sql = "WHERE timestamp >= date('now', 'localtime', 'start of day')"
    elif periodo == "7d":
        filtro_sql = "WHERE timestamp >= date('now', 'localtime', '-7 days')"
    elif periodo == "30d":
        filtro_sql = "WHERE timestamp >= date('now', 'localtime', '-30 days')"
    elif periodo == "mes_atual":
        filtro_sql = "WHERE timestamp >= date('now', 'localtime', 'start of month')"
    elif periodo == "ano":
        filtro_sql = "WHERE timestamp >= date('now', 'localtime', 'start of year')"
    elif periodo == "custom" and data_inicio and data_fim:
        filtro_sql = "WHERE date(timestamp) >= date(?) AND date(timestamp) <= date(?)"
        params = [data_inicio, data_fim]
    else:
        filtro_sql = "WHERE timestamp >= date('now', 'localtime', '-30 days')"

    # Curva Temporal Principal
    labels_tempo = []
    kwh_tempo = []
    custos_tempo = []

    if periodo == "24h":
        cursor.execute(f"""
            SELECT strftime('%H:00', timestamp) as hora, (MAX(kwh) - MIN(kwh)) as consumo
            FROM historico_consumo
            {filtro_sql}
            GROUP BY strftime('%H', timestamp)
            ORDER BY hora ASC
        """, params)
        rows_tempo = cursor.fetchall()
        mapa_h = {r[0]: round(r[1] or 0.0, 3) for r in rows_tempo}
        for h in range(24):
            h_str = f"{h:02d}:00"
            labels_tempo.append(h_str)
            c_h = mapa_h.get(h_str, 0.0)
            kwh_tempo.append(c_h)
            custos_tempo.append(round(calcular_custo(c_h, config)["total"] - config.get("cosip", 12.50), 2) if c_h > 0 else 0.0)
    elif periodo == "ano":
        cursor.execute(f"""
            SELECT strftime('%Y-%m', timestamp) as mes, (MAX(kwh) - MIN(kwh)) as consumo
            FROM historico_consumo
            {filtro_sql}
            GROUP BY strftime('%Y-%m', timestamp)
            ORDER BY mes ASC
        """, params)
        rows_tempo = cursor.fetchall()
        meses_nomes = {'01':'Jan', '02':'Fev', '03':'Mar', '04':'Abr', '05':'Mai', '06':'Jun', '07':'Jul', '08':'Ago', '09':'Set', '10':'Out', '11':'Nov', '12':'Dez'}
        for r in rows_tempo:
            partes = r[0].split('-') if r[0] else ['2026', '01']
            nome_mes = meses_nomes.get(partes[1], partes[1])
            labels_tempo.append(f"{nome_mes}/{partes[0][2:]}")
            c_m = round(r[1] or 0.0, 2)
            kwh_tempo.append(c_m)
            custos_tempo.append(calcular_custo(c_m, config)["total"])
    else:
        cursor.execute(f"""
            SELECT date(timestamp) as dia, (MAX(kwh) - MIN(kwh)) as consumo
            FROM historico_consumo
            {filtro_sql}
            GROUP BY date(timestamp)
            ORDER BY dia ASC
        """, params)
        rows_tempo = cursor.fetchall()
        for r in rows_tempo:
            try:
                dt_obj = datetime.strptime(r[0], "%Y-%m-%d")
                labels_tempo.append(dt_obj.strftime("%d/%m"))
            except Exception:
                labels_tempo.append(str(r[0]))
            c_d = round(r[1] or 0.0, 3)
            kwh_tempo.append(c_d)
            custos_tempo.append(round(calcular_custo(c_d, config)["total"] - config.get("cosip", 12.50), 2) if c_d > 0 else 0.0)

    # Perfil das 24 horas do dia (2-level aggregation)
    # Nível 1: consumo por dia+hora (MAX-MIN dentro de cada hora de cada dia)
    # Nível 2: média por hora-do-dia entre todos os dias do período
    # Isso evita o bug de MAX-MIN cruzar dias diferentes (inflando a barra)
    cursor.execute(f"""
        SELECT sub.hora, AVG(sub.consumo_hora) as consumo
        FROM (
            SELECT strftime('%Y-%m-%d', timestamp) as dia,
                   strftime('%H', timestamp) as hora,
                   (MAX(kwh) - MIN(kwh)) as consumo_hora
            FROM historico_consumo
            {filtro_sql}
            GROUP BY strftime('%Y-%m-%d', timestamp), strftime('%H', timestamp)
        ) sub
        GROUP BY sub.hora
        ORDER BY sub.hora ASC
    """, params)
    rows_h24 = cursor.fetchall()
    mapa_h24 = {int(r[0]): round(r[1] or 0.0, 3) for r in rows_h24 if r[0] is not None}
    horas_24_labels = [f"{h:02d}h" for h in range(24)]
    horas_24_kwh = [mapa_h24.get(h, 0.0) for h in range(24)]

    pico_val = max(horas_24_kwh) if horas_24_kwh else 0.0
    pico_hora_idx = horas_24_kwh.index(pico_val) if pico_val > 0 else None
    pico_hora_str = f"{pico_hora_idx:02d}:00 - {(pico_hora_idx+1)%24:02d}:00 ({horas_24_kwh[pico_hora_idx]:.2f} kWh)" if pico_hora_idx is not None else "Sem pico registrado"

    # Consumo por Dia da Semana (2-level aggregation)
    # Nível 1: consumo por dia calendário (MAX-MIN diário correto)
    # Nível 2: média por dia-da-semana para múltiplas semanas no período
    dias_semana_nomes = ["Domingo", "Segunda", "Terça", "Quarta", "Quinta", "Sexta", "Sábado"]
    cursor.execute(f"""
        SELECT sub.dia_sem, AVG(sub.consumo_dia) as consumo
        FROM (
            SELECT date(timestamp) as dia,
                   strftime('%w', timestamp) as dia_sem,
                   (MAX(kwh) - MIN(kwh)) as consumo_dia
            FROM historico_consumo
            {filtro_sql}
            GROUP BY date(timestamp)
        ) sub
        GROUP BY sub.dia_sem
    """, params)
    rows_sem = cursor.fetchall()
    mapa_sem = {int(r[0]): round(r[1] or 0.0, 3) for r in rows_sem if r[0] is not None}
    dias_semana_kwh = [mapa_sem.get(d, 0.0) for d in range(7)]

    sem_val = max(dias_semana_kwh) if dias_semana_kwh else 0.0
    dia_semana_pico_idx = dias_semana_kwh.index(sem_val) if sem_val > 0 else None
    dia_semana_pico_str = f"{dias_semana_nomes[dia_semana_pico_idx]} ({dias_semana_kwh[dia_semana_pico_idx]:.2f} kWh)" if dia_semana_pico_idx is not None else "Sem registro"

    # Comparativo dos Últimos 12 Meses
    cursor.execute("""
        SELECT strftime('%Y-%m', timestamp) as mes, (MAX(kwh) - MIN(kwh)) as consumo
        FROM historico_consumo
        WHERE timestamp >= date('now', 'localtime', '-12 months')
        GROUP BY strftime('%Y-%m', timestamp)
        ORDER BY mes ASC
    """)
    rows_meses = cursor.fetchall()
    meses_comparativo_labels = []
    meses_comparativo_kwh = []
    meses_comparativo_custo = []
    meses_map_pt = {'01':'Jan', '02':'Fev', '03':'Mar', '04':'Abr', '05':'Mai', '06':'Jun', '07':'Jul', '08':'Ago', '09':'Set', '10':'Out', '11':'Nov', '12':'Dez'}
    for r in rows_meses:
        parts = r[0].split('-') if r[0] else ['2026', '01']
        meses_comparativo_labels.append(f"{meses_map_pt.get(parts[1], parts[1])}/{parts[0][2:]}")
        c_k = round(r[1] or 0.0, 2)
        meses_comparativo_kwh.append(c_k)
        calc_mes = calcular_custo(c_k, config)
        meses_comparativo_custo.append(calc_mes["total"])

    # Totais e KPIs do Período
    # Usa MAX-MIN único sobre todo o período para ser consistente com o dashboard principal
    # (evita acúmulo de erro da soma de MAX-MIN por dia nos cruzamentos de meia-noite)
    cursor.execute(f"""
        SELECT (MAX(kwh) - MIN(kwh)) as consumo_total
        FROM historico_consumo
        {filtro_sql}
    """, params)
    row_total = cursor.fetchone()
    consumo_total_periodo = round(row_total[0] if row_total and row_total[0] else sum(kwh_tempo), 2)

    custos_periodo = calcular_custo(consumo_total_periodo, config)
    dias_totais = max(1, len(labels_tempo) if periodo != "24h" else 1)
    media_diaria_kwh = round(consumo_total_periodo / dias_totais, 2)
    media_diaria_rs = round(custos_periodo["total"] / dias_totais, 2)

    # Relatório Consolidado de Faturas
    import json
    cursor.execute("SELECT id, mes_referencia, data_inicio, data_fim, leitura_inicial, leitura_final, consumo_kwh, valor_total, detalhes_json FROM faturas_historico ORDER BY id DESC")
    faturas_rows = cursor.fetchall()

    tabela_faturas = []
    for f in faturas_rows:
        det = json.loads(f[8]) if f[8] else {}
        tabela_faturas.append({
            "id": f[0],
            "tipo": "fechada",
            "mes_referencia": f[1],
            "periodo": f"{f[2]} a {f[3]}",
            "leitura_inicial": f[4],
            "leitura_final": f[5],
            "consumo_kwh": f[6],
            "bruto_energia": det.get("bruto_energia", 0.0),
            "bruto_bandeira": det.get("bruto_bandeira", 0.0),
            "icms": det.get("val_icms", 0.0),
            "pis_cofins": round(det.get("val_pis", 0.0) + det.get("val_cofins", 0.0), 2),
            "cosip": det.get("cosip", 0.0),
            "valor_total": f[7],
            "bandeira_nome": det.get("bandeira_nome", "Bandeira Verde"),
            "pdf_url": f"/gerar-pdf?fatura_id={f[0]}"
        })

    # Adiciona fatura do ciclo em aberto no topo
    cursor.execute("SELECT kwh FROM historico_consumo ORDER BY id DESC LIMIT 1")
    last_row = cursor.fetchone()
    leitura_ini_atual = config.get("leitura_inicial", 2504.0)
    leitura_fim_atual = last_row[0] if last_row else leitura_ini_atual
    consumo_aberto = max(0.0, leitura_fim_atual - leitura_ini_atual)
    custos_aberto = calcular_custo(consumo_aberto, config)

    cursor.execute("SELECT valor FROM configuracoes_texto WHERE chave='data_inicio_ciclo'")
    row_dt = cursor.fetchone()
    dt_ini_aberto = row_dt[0] if row_dt and row_dt[0] else agora.strftime("%Y-%m-%d")
    try:
        dt_ini_formatada = datetime.strptime(dt_ini_aberto, "%Y-%m-%d").strftime("%d/%m/%Y")
    except Exception:
        dt_ini_formatada = dt_ini_aberto

    fatura_aberto = {
        "id": None,
        "tipo": "aberto",
        "mes_referencia": f"{agora.strftime('%m/%Y')} (Em Aberto)",
        "periodo": f"{dt_ini_formatada} até Agora",
        "leitura_inicial": leitura_ini_atual,
        "leitura_final": leitura_fim_atual,
        "consumo_kwh": round(consumo_aberto, 2),
        "bruto_energia": custos_aberto.get("bruto_energia", 0.0),
        "bruto_bandeira": custos_aberto.get("bruto_bandeira", 0.0),
        "icms": custos_aberto.get("val_icms", 0.0),
        "pis_cofins": round(custos_aberto.get("val_pis", 0.0) + custos_aberto.get("val_cofins", 0.0), 2),
        "cosip": custos_aberto.get("cosip", 0.0),
        "valor_total": custos_aberto.get("total", 0.0),
        "bandeira_nome": custos_aberto.get("bandeira_nome", "Bandeira Verde"),
        "pdf_url": "/gerar-pdf"
    }
    tabela_faturas.insert(0, fatura_aberto)

    conn.close()

    return {
        "periodo": periodo,
        "kpis": {
            "consumo_total_kwh": consumo_total_periodo,
            "custo_total_rs": custos_periodo["total"],
            "media_diaria_kwh": media_diaria_kwh,
            "media_diaria_rs": media_diaria_rs,
            "pico_hora": pico_hora_str,
            "dia_semana_pico": dia_semana_pico_str,
            "icms_faixa": custos_periodo.get("icms_faixa", "Isento"),
            "icms_pct": custos_periodo.get("icms_pct", 0.0),
            "bandeira_nome": custos_periodo.get("bandeira_nome", "Bandeira Verde")
        },
        "grafico_tempo": {
            "labels": labels_tempo,
            "kwh": kwh_tempo,
            "custos": custos_tempo
        },
        "perfil_horario": {
            "labels": horas_24_labels,
            "kwh": horas_24_kwh
        },
        "dias_semana": {
            "labels": dias_semana_nomes,
            "kwh": dias_semana_kwh
        },
        "comparativo_mensal": {
            "labels": meses_comparativo_labels,
            "kwh": meses_comparativo_kwh,
            "custos": meses_comparativo_custo
        },
        "faturas": tabela_faturas
    }

