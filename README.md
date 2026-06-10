# Kalshi Trading Bot / Bot de Trading para Kalshi

A simple prediction-market trading bot for [Kalshi](https://kalshi.com) with a
Flask dashboard. **Configured for the demo (paper trading) environment by
default — no real money.**

Un bot simple de trading para mercados de predicción de
[Kalshi](https://kalshi.com) con un panel en Flask. **Configurado por defecto
para el entorno demo (dinero ficticio) — sin dinero real.**

---

## English

### How it works

The bot watches one market and trades the YES side:

- **Buys** `BOT_ORDER_COUNT` YES contracts when the ask price drops below
  `BOT_BUY_THRESHOLD` (default 30¢ ≈ 30% implied probability).
- **Sells** the whole position when the bid reaches `BOT_SELL_THRESHOLD`
  (default 70¢ ≈ 70%).
- After selling it keeps watching and re-enters if the price falls below the
  buy threshold again.

Orders are limit orders placed at the current ask/bid, so they fill
immediately when liquidity exists but never at a worse price than observed.

### Setup

1. **Create a demo account and API key**
   - Sign up at <https://demo.kalshi.co>.
   - Go to *Account → API Keys*, create a key, and download the private key
     `.pem` file. Note the **Key ID**.

2. **Install dependencies** (Python 3.11)

   ```powershell
   python -m venv .venv
   .venv\Scripts\activate
   pip install -r requirements.txt
   ```

3. **Configure environment**

   ```powershell
   copy .env.example .env
   ```

   Edit `.env` and set:
   - `KALSHI_KEY_ID` — your API key ID
   - `KALSHI_PRIVATE_KEY_PATH` — path to the downloaded `.pem` file
   - `BOT_MARKET_TICKER` — the market to trade (e.g. `KXBTCD-25JUN10-...`).
     List markets with `GET /api/markets` once the server is running.

4. **Run the server**

   ```powershell
   python app.py
   ```

   Open <http://127.0.0.1:5000> for the dashboard. Click **Start bot** to
   begin trading; **Stop bot** to halt it. You can also run the bot without
   Flask: `python bot_strategy.py`.

### API endpoints

| Method | Endpoint          | Description                                  |
|--------|-------------------|----------------------------------------------|
| GET    | `/api/balance`    | Account balance (cents)                       |
| GET    | `/api/markets`    | List markets (`?status=open&limit=20`, etc.)  |
| GET    | `/api/positions`  | Current positions (`?ticker=...`)             |
| GET    | `/api/orders`     | List orders (`?ticker=...&status=resting`)    |
| POST   | `/api/orders`     | Place an order (JSON body, see below)         |
| GET    | `/api/bot/status` | Bot state, last price, trade log              |
| POST   | `/api/bot/start`  | Start the strategy loop                       |
| POST   | `/api/bot/stop`   | Stop the strategy loop                        |

Example order:

```powershell
curl -X POST http://127.0.0.1:5000/api/orders -H "Content-Type: application/json" `
  -d '{"ticker":"EXAMPLE-TICKER","side":"yes","action":"buy","count":1,"type":"limit","yes_price":25}'
```

### ⚠️ Disclaimer

This is educational software for the **demo** environment. If you point it at
the production API you are trading real money entirely at your own risk.

---

## Español

### Cómo funciona

El bot vigila un solo mercado y opera el lado YES (Sí):

- **Compra** `BOT_ORDER_COUNT` contratos YES cuando el precio de venta (ask)
  baja de `BOT_BUY_THRESHOLD` (30¢ por defecto ≈ 30% de probabilidad).
- **Vende** toda la posición cuando el precio de compra (bid) alcanza
  `BOT_SELL_THRESHOLD` (70¢ por defecto ≈ 70%).
- Después de vender sigue vigilando y vuelve a comprar si el precio cae otra
  vez por debajo del umbral de compra.

Las órdenes son órdenes límite al precio actual de ask/bid: se ejecutan de
inmediato si hay liquidez, pero nunca a un precio peor que el observado.

### Instalación

1. **Crear cuenta demo y clave API**
   - Regístrate en <https://demo.kalshi.co>.
   - Ve a *Account → API Keys*, crea una clave y descarga el archivo `.pem`
     con la clave privada. Anota el **Key ID**.

2. **Instalar dependencias** (Python 3.11)

   ```powershell
   python -m venv .venv
   .venv\Scripts\activate
   pip install -r requirements.txt
   ```

3. **Configurar variables de entorno**

   ```powershell
   copy .env.example .env
   ```

   Edita `.env` y define:
   - `KALSHI_KEY_ID` — el ID de tu clave API
   - `KALSHI_PRIVATE_KEY_PATH` — ruta al archivo `.pem` descargado
   - `BOT_MARKET_TICKER` — el mercado a operar. Puedes listar mercados con
     `GET /api/markets` cuando el servidor esté corriendo.

4. **Iniciar el servidor**

   ```powershell
   python app.py
   ```

   Abre <http://127.0.0.1:5000> para ver el panel. Pulsa **Start bot** para
   empezar a operar y **Stop bot** para detenerlo. También puedes ejecutar el
   bot sin Flask: `python bot_strategy.py`.

### Endpoints de la API

| Método | Endpoint          | Descripción                                    |
|--------|-------------------|------------------------------------------------|
| GET    | `/api/balance`    | Saldo de la cuenta (en centavos)                |
| GET    | `/api/markets`    | Lista de mercados (`?status=open&limit=20`)     |
| GET    | `/api/positions`  | Posiciones actuales (`?ticker=...`)             |
| GET    | `/api/orders`     | Lista de órdenes (`?ticker=...&status=resting`) |
| POST   | `/api/orders`     | Crear una orden (cuerpo JSON)                   |
| GET    | `/api/bot/status` | Estado del bot, último precio, historial        |
| POST   | `/api/bot/start`  | Iniciar la estrategia                           |
| POST   | `/api/bot/stop`   | Detener la estrategia                           |

### ⚠️ Aviso

Este software es educativo y está configurado para el entorno **demo**. Si lo
apuntas a la API de producción estarás operando con dinero real bajo tu
propia responsabilidad.
