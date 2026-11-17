"""
app/core/database.py
Camada Core — Gerenciamento do banco de dados SQLite do Blackburn-Pulse.
Responsabilidades: definição de caminhos, criação de tabelas e valores padrão.
"""

import sqlite3

from pathlib import Path

# Diretório base do projeto (raiz, onde main.py está)
BASE_DIR = Path(__file__).resolve().parent.parent.parent

# Caminho absoluto do banco de dados SQLite — mantido na raiz do projeto
DB_PATH = BASE_DIR / "banco_energia.db"


def init_db():

    conn = sqlite3.connect(DB_PATH)

    cursor = conn.cursor()

    

    # Histórico de medições

    cursor.execute("""

        CREATE TABLE IF NOT EXISTS historico_consumo (

            id INTEGER PRIMARY KEY AUTOINCREMENT,

            timestamp TEXT,

            kwh REAL

        )

    """)

    

    # Histórico de faturas fechadas

    cursor.execute("""

        CREATE TABLE IF NOT EXISTS faturas_historico (

            id INTEGER PRIMARY KEY AUTOINCREMENT,

            mes_referencia TEXT UNIQUE,

            data_inicio TEXT,

            data_fim TEXT,

            leitura_inicial REAL,

            leitura_final REAL,

            consumo_kwh REAL,

            valor_total REAL,

            detalhes_json TEXT

        )

    """)

    # Tabela de Auditoria e Logs Gerenciais de Sistema e Quedas
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS auditoria_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            categoria TEXT,
            acao TEXT,
            detalhes TEXT,
            nivel TEXT DEFAULT 'INFO',
            usuario_origem TEXT DEFAULT 'Sistema'
        )
    """)

    # Configurações do sistema

    cursor.execute("""

        CREATE TABLE IF NOT EXISTS configuracoes (

            chave TEXT PRIMARY KEY,

            valor REAL

        )

    """)

    # Configurações de texto (Wi-Fi, MQTT e parâmetros de rede do ESP32)

    cursor.execute("""

        CREATE TABLE IF NOT EXISTS configuracoes_texto (

            chave TEXT PRIMARY KEY,

            valor TEXT

        )

    """)

    defaults_txt = {

        "wifi_ssid": "",

        "mqtt_server": "",

        "bandeira_tipo": "verde"

    }

    for k, v in defaults_txt.items():

        cursor.execute("INSERT OR IGNORE INTO configuracoes_texto (chave, valor) VALUES (?, ?)", (k, v))

    # Valores padrão Enel SP

    defaults = {

        "leitura_inicial": 0.0,

        "pulsos_iniciais_ciclo": -1.0,

        "ultimo_pulsos_esp": 0.0,

        "dia_fechamento": 5.0,

        "tarifa": 0.79,

        "tarifa_bandeira": 0.0,
        "tarifa_bandeira_verde": 0.0,
        "tarifa_bandeira_amarela": 0.01885,
        "tarifa_bandeira_vermelha1": 0.04463,
        "tarifa_bandeira_vermelha2": 0.07877,
        "tarifa_bandeira_escassez": 0.14200,

        "icms": 0.0,
        "icms_faixa1_limite": 90.0,
        "icms_faixa1_aliquota": 0.0,
        "icms_faixa2_limite": 200.0,
        "icms_faixa2_aliquota": 12.0,
        "icms_faixa3_aliquota": 18.0,

        "pis": 0.6,

        "cofins": 2.4,

        "cosip": 17.55

    }

    for k, v in defaults.items():

        cursor.execute("INSERT OR IGNORE INTO configuracoes (chave, valor) VALUES (?, ?)", (k, v))

    cursor.execute("SELECT COUNT(*) FROM historico_consumo")

    if cursor.fetchone()[0] == 0:

        cursor.execute("INSERT INTO historico_consumo (timestamp, kwh) VALUES (datetime('now', 'localtime'), ?)", (defaults["leitura_inicial"],))

    conn.commit()

    conn.close()


# Inicializa o banco ao importar o módulo (igual ao comportamento original)
init_db()

