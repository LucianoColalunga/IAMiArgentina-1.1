"""Núcleo multicanal: modelo de mensaje normalizado, contratos de los adaptadores y registro.

El núcleo no conoce ninguna plataforma. Cada integración (WhatsApp, Telegram, ...) implementa
los contratos de `contracts.py` y se registra; agregar un canal no toca el motor de IA ni el
gestor de conversaciones.
"""
