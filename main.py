import json
import os
import time
import threading
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

def cargar_estado():
    try:
        with open(JSON_FILE, "r") as f:
            return json.load(f)
    except:
        return {"operacion_abierta": False}

def guardar_estado(data):
    with open(JSON_FILE, "w") as f:
        json.dump(data, f)

def obtener_precio_actual():
    ticker = client.get_symbol_ticker(symbol=PAIR)
    return float(ticker["price"])

def round_step_size(quantity, step_size):
    return round(quantity - (quantity % step_size), 6)

def comprar():
    estado = cargar_estado()
    if estado["operacion_abierta"]:
        print("⚠️ Ya hay una operación abierta. Ignorando señal de compra.")
        return

    usdc_balance = float(client.get_asset_balance(asset="USDC")["free"])
    precio = obtener_precio_actual()
    cantidad = usdc_balance / precio

    info = client.get_symbol_info(PAIR)
    step_size = 0.000001
    for f in info["filters"]:
        if f["filterType"] == "LOT_SIZE":
            step_size = float(f["stepSize"])
    cantidad = round_step_size(cantidad, step_size)

    orden = client.order_market_buy(symbol=PAIR, quantity=cantidad)
    guardar_estado({
        "operacion_abierta": True,
        "precio_compra": precio,
        "hora_compra": datetime.now().isoformat()
    })
    print(f"✅ COMPRA: {cantidad} BTC a {precio}")

def vender():
    estado = cargar_estado()
    if not estado["operacion_abierta"]:
        print("⚠️ No hay operación abierta. Ignorando señal de venta.")
        return

    btc_balance = float(client.get_asset_balance(asset="BTC")["free"])

    info = client.get_symbol_info(PAIR)
    step_size = 0.000001
    for f in info["filters"]:
        if f["filterType"] == "LOT_SIZE":
            step_size = float(f["stepSize"])
    cantidad = round_step_size(btc_balance, step_size)

    orden = client.order_market_sell(symbol=PAIR, quantity=cantidad)
    guardar_estado({"operacion_abierta": False})
    print(f"✅ VENTA: {cantidad} BTC vendidas")

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json()
    action = data.get("action")
    if action == "buy":
        comprar()
    elif action == "sell":
        vender()
    return {"status": "ok"}

@app.route("/status", methods=["GET"])
def status():
    return {"status": "alive"}

if __name__ == "__main__":
    print("🟢 Bot pasivo con SuperTrend iniciado correctamente.")
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
