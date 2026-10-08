# Catalogue proposé dans le sélecteur de modèle. Le PREMIER modèle de
# chaque fournisseur est celui qu'on obtient en cliquant sur le fournisseur :
# c'est le choix recommandé. Toute autre référence passe par « Custom model ».
#
# Relevé le 2026-10-08 sur la doc officielle de chaque fournisseur
# (platform.claude.com, developers.openai.com, docs.mistral.ai,
# ai.google.dev, api-docs.deepseek.com, console.groq.com). Retirés ce jour-là :
# Claude 4.x, o3/o4-mini/GPT-4.1 (absents de la doc OpenAI), Devstral Small et
# Mistral Nemo (dépréciés), deepseek-chat/-reasoner (alias éteints le
# 2026-07-24), Llama 4, Qwen 3 32B et Llama 3.3 70B (arrêtés chez Groq).
# Gemini : gemini-3-pro, gemini-2.0-flash et gemini-2.0-pro avaient déjà été
# retirés le 2026-09-25 après des 404 sur agent-dev.
MODELS = [
    {
        "id": "anthropic/claude-opus-5-5",
        "name": "Claude Opus 5.5",
        "provider": "anthropic",
        "tag": "Recommended",
    },
    {
        "id": "anthropic/claude-sonnet-5-5",
        "name": "Claude Sonnet 5.5",
        "provider": "anthropic",
        "tag": "Balanced",
    },
    {
        "id": "anthropic/claude-haiku-5-5",
        "name": "Claude Haiku 5.5",
        "provider": "anthropic",
        "tag": "Fast",
    },
    {
        "id": "anthropic/claude-fable-5-1",
        "name": "Claude Fable 5.1",
        "provider": "anthropic",
        "tag": "Most capable",
    },
    {
        "id": "openai/gpt-6.1-sol",
        "name": "GPT-6.1 Sol",
        "provider": "openai",
        "tag": "Recommended",
    },
    {
        "id": "openai/gpt-6-astra",
        "name": "GPT-6 Astra",
        "provider": "openai",
        "tag": "Most capable",
    },
    {
        "id": "openai/gpt-6-luna",
        "name": "GPT-6 Luna",
        "provider": "openai",
        "tag": "Fast",
    },
    # Alias `-latest` : Mistral les fait suivre la dernière version stable,
    # d'où des noms sans numéro de version.
    {
        "id": "mistral/mistral-large-latest",
        "name": "Mistral Large",
        "provider": "mistral",
        "tag": "Recommended",
    },
    {
        "id": "mistral/mistral-medium-latest",
        "name": "Mistral Medium",
        "provider": "mistral",
        "tag": "Balanced",
    },
    {
        "id": "mistral/mistral-small-latest",
        "name": "Mistral Small",
        "provider": "mistral",
        "tag": "Fast",
    },
    {
        "id": "mistral/codestral-latest",
        "name": "Codestral",
        "provider": "mistral",
        "tag": "Code",
    },
    # Google : 3.8 Flash est le modèle stable recommandé ; 3.1 Pro n'existe
    # qu'en preview.
    {
        "id": "gemini/gemini-3.8-flash",
        "name": "Gemini 3.8 Flash",
        "provider": "gemini",
        "tag": "Recommended",
    },
    {
        "id": "gemini/gemini-3.1-pro-preview",
        "name": "Gemini 3.1 Pro",
        "provider": "gemini",
        "tag": "Preview",
    },
    {
        "id": "gemini/gemini-3.5-flash-lite",
        "name": "Gemini 3.5 Flash-Lite",
        "provider": "gemini",
        "tag": "Fastest",
    },
    {
        "id": "deepseek/deepseek-flash",
        "name": "DeepSeek V4.1 Flash",
        "provider": "deepseek",
        "tag": "Recommended",
    },
    {
        "id": "deepseek/deepseek-v4-pro",
        "name": "DeepSeek V4 Pro",
        "provider": "deepseek",
        "tag": "Powerful",
    },
    {
        "id": "groq/openai/gpt-oss-120b",
        "name": "GPT-OSS 120B",
        "provider": "groq",
        "tag": "Recommended",
    },
    {
        "id": "groq/openai/gpt-oss-20b",
        "name": "GPT-OSS 20B",
        "provider": "groq",
        "tag": "Fast",
    },
    {
        "id": "groq/llama-3.1-8b-instant",
        "name": "Llama 3.1 8B",
        "provider": "groq",
        "tag": "Fastest",
    },
]
