# El Diario Cripto

Bot que cada 15 minutos revisa noticias cripto (Trump en Truth Social, listings de Binance, CoinDesk, Cointelegraph, The Block, Decrypt, Blockworks), las puntúa y:

- manda a Telegram lo importante: 🔥 **A seguir**, 👀 **Échale un ojo**, ⚠️ **Ya se ha movido**
- publica un periódico en GitHub Pages con todas las noticias de las últimas 48 h

## Qué incluye el periódico
- **📅 Agenda**: eventos macro (Fed, IPC, empleo) en hora de Alemania. Se editan en `agenda.json`; puedes añadir unlocks, listings o reuniones del BCE (usa `"tz": "Europe/Berlin"` si la hora es alemana).
- **Portada y secciones** 🔥 A seguir / 👀 Échale un ojo / ⚠️ Ya se ha movido, con categoría (Regulación, Institucional, Exchange, Seguridad…).
- **Verificación**: ✔ fuente oficial (Binance, Trump), ✔ confirmado (2+ fuentes distintas) o SIN CONFIRMAR.
- **Reglas editoriales**: las predicciones de precio y los "top 5 altcoins para comprar" se descartan; los rumores restan puntos.
- **📊 Marcador de aciertos** a 24 h y 7 días.
- **Telegram solo con lo destacado**: al momento solo llegan las 🔥 *A seguir* (cambia `TELEGRAM_LABELS` si quieres más).
- **☀️ Edición de la mañana** por Telegram a partir de las 08:00 (Alemania): agenda de hoy y de dentro de 3 días + las 5 noticias top.

## Puesta en marcha
1. Settings → Secrets and variables → Actions → añadir `TELEGRAM_TOKEN` y `TELEGRAM_CHAT_ID`.
2. Settings → Pages → Source: *Deploy from a branch* → rama `main`, carpeta `/docs`.
3. Actions → *Diario Cripto* → **Run workflow** (la primera pasada carga las noticias actuales sin avisar por Telegram).

## Ajustes
Todo está arriba del todo en `crypto_news_alert.py`: umbrales (`ALERT_THRESHOLD`, `HOT_THRESHOLD`), cuándo se considera "ya se ha movido" (`MOVED_1H_PCT`, `MOVED_24H_PCT`), la watchlist y las palabras clave (`CATALYSTS`).

El periódico incluye un **marcador de aciertos**: cada alerta se mide a las 24 h y a los 7 días para saber si la puntuación funciona antes de arriesgar dinero.

Prueba local sin Telegram: `python crypto_news_alert.py --dry`

## Icono en el móvil
Abre el enlace de GitHub Pages en el móvil:
- **Android (Chrome)**: menú ⋮ → *Añadir a pantalla de inicio* / *Instalar aplicación*.
- **iPhone (Safari)**: botón compartir → *Añadir a pantalla de inicio*.

Se abre a pantalla completa, sin barra del navegador, como una app.
