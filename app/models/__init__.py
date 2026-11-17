# Camada Models — Constantes de domínio e tabelas ANEEL

# --- TABELA DE BANDEIRAS TARIFÁRIAS (ANEEL / ENEL SP) ---

BANDEIRAS_TABELA = {

    "verde": {

        "nome": "Bandeira Verde",

        "tarifa": 0.0,

        "descricao": "Condições favoráveis de geração. Sem acréscimo tarifário.",

        "cor": "emerald",

    },

    "amarela": {

        "nome": "Bandeira Amarela",

        "tarifa": 0.01885,

        "descricao": "Condições de geração menos favoráveis (+ R$ 0,01885 / kWh).",

        "cor": "amber",

    },

    "vermelha1": {

        "nome": "Bandeira Vermelha - Patamar 1",

        "tarifa": 0.04463,

        "descricao": "Condições mais custosas de geração (+ R$ 0,04463 / kWh).",

        "cor": "rose",

    },

    "vermelha2": {

        "nome": "Bandeira Vermelha - Patamar 2",

        "tarifa": 0.07877,

        "descricao": "Condições ainda mais onerosas (+ R$ 0,07877 / kWh).",

        "cor": "red",

    },

    "escassez": {

        "nome": "Escassez Hídrica",

        "tarifa": 0.14200,

        "descricao": "Cenário hídrico severo e crítico (+ R$ 0,14200 / kWh).",

        "cor": "purple",

    }

}

