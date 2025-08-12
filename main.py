import json
import os
from datetime import datetime
from flask import Flask, request
from binance.client import Client

app = Flask(__name__)

client = Client(
    api_key=os.getenv("BINANCE_API_KEY"),
    api_secret=os.getenv("BINANCE_API_SECRET")
)

PAIR = "BTCUSDC"
JSON_FILE = "estado_compra.json"
MIN_TRADE_USD = 5.0  # umbral para considerar 'restos'

def cargar_estado():
    try:
        with open(JSON_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {"operacion_abierta": False}

def guardar_estado(data):
    with open(JSON_FILE, "w") as f:
        json.dump(data, f)

def obtener_precio_actual():
    ticker = client.get_symbol_ticker(symbol=PAIR)
    return float(ticker["price"])

def round_step_size(quantity, step_size):
    # protección ante negativos y precisión
    if step_size <= 0:
        step_size = 0.000001
    q = max(0.0, quantity - (quantity % step_size))
    return round(q, 6)

def limpiar_restos_actualizar_estado():
    """
    Devuelve (btc_balance, usdc_balance, precio, valor_btc).
    Si detecta restos (< MIN_TRADE_USD) y estado marcado abierto, lo cierra en el JSON.
    """
    estado = cargar_estado()
    precio = obtener_precio_actual()
    btc_balance = float(client.get_asset_balance(asset="BTC")["free"] or 0.0)
    usdc_balance = float(client.get_asset_balance(asset="USDC")["free"] or 0.0)
    valor_btc = btc_balance * precio

    # Si hay operación marcada abierta pero el valor de BTC es menor al umbral, limpiamos estado
    if estado.get("operacion_abierta") and valor_btc < MIN_TRADE_USD:
        print(f"ℹ️ Restos de BTC detectados ({valor_btc:.2f} USDC). Marcamos operacion_abierta=False en JSON.")
        guardar_estado({"operacion_abierta": False})

    return btc_balance, usdc_balance, precio, valor_btc

def comprar():
    # Comprobaciones de restos y limpieza de estado
    btc_balance, usdc_balance, precio, valor_btc = limpiar_restos_actualizar_estado()
    estado = cargar_estado()

    if estado.get("operacion_abierta") and valor_btc >= MIN_TRADE_USD:
        print(f"⚠️ Ya hay una operación abierta con valor {valor_btc:.2f} USDC. Ignorando señal de compra.")
        return

    if usdc_balance < MIN_TRADE_USD:
        print(f"ℹ️ USDC disponible ({usdc_balance:.2f}) menor a {MIN_TRADE_USD}. No compramos para evitar órdenes inútiles.")
        return

    cantidad = usdc_balance / precio

    info = client.get_symbol_info(PAIR)
    step_size = 0.000001
    for f in info.get("filters", []):
        if f.get("filterType") == "LOT_SIZE":
            step_size = float(f.get("stepSize", step_size))
            break
    cantidad = round_step_size(cantidad, step_size)

    if cantidad <= 0:
        print("⚠️ La cantidad calculada es 0 tras redondeo. Abortando compra.")
        return

    orden = client.order_market_buy(symbol=PAIR, quantity=cantidad)
    guardar_estado({
        "operacion_abierta": True,
        "precio_compra": precio,
        "hora_compra": datetime.now().isoformat()
    })
    print(f"✅ COMPRA: {cantidad} BTC a {precio} | USDC gastados ~ {usdc_balance:.2f}")

def vender():
    # Comprobaciones de restos y limpieza de estado
    btc_balance, usdc_balance, precio, valor_btc = limpiar_restos_actualizar_estado()
    estado = cargar_estado()

    if not estado.get("operacion_abierta"):
        print("ℹ️ JSON indica que no hay operación abierta.")
    if valor_btc < MIN_TRADE_USD:
        print(f"ℹ️ Valor BTC ({valor_btc:.2f} USDC) menor a {MIN_TRADE_USD}. Ignoramos venta para no liquidar restos.")
        # Aseguramos estado cerrado
        guardar_estado({"operacion_abierta": False})
        return

    info = client.get_symbol_info(PAIR)
    step_size = 0.000001
    for f in info.get("filters", []):
        if f.get("filterType") == "LOT_SIZE":
            step_size = float(f.get("stepSize", step_size))
            break
    cantidad = round_step_size(btc_balance, step_size)

    if cantidad <= 0:
        print("⚠️ La cantidad calculada es 0 tras redondeo. Abortando venta.")
        guardar_estado({"operacion_abierta": False})
        return

    orden = client.order_market_sell(symbol=PAIR, quantity=cantidad)
    guardar_estado({"operacion_abierta": False})
    print(f"✅ VENTA: {cantidad} BTC vendidas a {precio} | Valor ~ {valor_btc:.2f} USDC")

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(silent=True) or {}
    action = data.get("action")
    if action == "buy":
        comprar()
    elif action == "sell":
        vender()
    else:
        print(f"ℹ️ Webhook recibido sin 'action' válida: {data}")
    return {"status": "ok"}

@app.route("/status", methods=["GET"])
def status():
    # Devolvemos balances y estado resumidos (sin claves)
    precio = obtener_precio_actual()
    btc = float(client.get_asset_balance(asset="BTC")["free"] or 0.0)
    usdc = float(client.get_asset_balance(asset="USDC")["free"] or 0.0)
    estado = cargar_estado()
    return {
        "status": "alive",
        "precio": precio,
        "btc_balance": round(btc, 8),
        "usdc_balance": round(usdc, 2),
        "valor_btc_usdc": round(btc * precio, 2),
        "operacion_abierta": estado.get("operacion_abierta", False)
    }

if __name__ == "__main__":
    print("🟢 Bot pasivo con SuperTrend (limpieza de restos automática) iniciado correctamente.")
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
