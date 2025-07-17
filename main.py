import json
import os
import time
import threading
from datetime import datetime, timedelta
from flask import Flask, request
from binance.client import Client

app = Flask(__name__)

client = Client(
    api_key=os.getenv("BINANCE_API_KEY"),
    api_secret=os.getenv("BINANCE_API_SECRET")
)

# ⚙️ PARÁMETROS DEL BOT
TIMEOUT_HORAS = 72
TAKE_PROFIT = 1.0075      # +0.75%
STOP_LOSS = 0.985         # -1.5%

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
    return float(f"{quantity - (quantity % step_size):.6f}")

def comprar():
    estado = cargar_estado()
    if estado["operacion_abierta"]:
        print("⚠️ Compra ignorada. Ya hay una operación abierta.")
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

    if cantidad <= 0:
        print("❌ Cantidad de compra no válida. No se ejecuta la orden.")
        return

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
        print("⚠️ Venta ignorada. No hay operación abierta.")
        return

    btc_balance = float(client.get_asset_balance(asset="BTC")["free"])

    info = client.get_symbol_info(PAIR)
    step_size = 0.000001
    for f in info["filters"]:
        if f["filterType"] == "LOT_SIZE":
            step_size = float(f["stepSize"])

    cantidad = round_step_size(btc_balance, step_size)

    if cantidad <= 0:
        print("❌ Cantidad de venta no válida.")
        return

    orden = client.order_market_sell(symbol=PAIR, quantity=cantidad)
    guardar_estado({"operacion_abierta": False})
    print(f"✅ VENTA: {cantidad} BTC vendidas")

@app.route("/webhook", methods=["POST"])
def webhook():
    estado = cargar_estado()
    if estado["operacion_abierta"]:
        print("⚠️ Señal ignorada: ya hay una compra abierta.")
        return {"status": "ignored"}

    data = request.get_json()
    if data.get("action") == "buy":
        comprar()
    return {"status": "ok"}

def control_venta():
    print("🟢 Iniciando control de ventas...")
    while True:
        try:
            estado = cargar_estado()
            if estado["operacion_abierta"]:
                precio_actual = obtener_precio_actual()
                precio_compra = estado["precio_compra"]
                hora_compra = datetime.fromisoformat(estado["hora_compra"])
                tiempo_transcurrido = datetime.now() - hora_compra

                print(f"[BOT] Precio actual: {precio_actual:.2f}, Objetivo: {precio_compra * TAKE_PROFIT:.2f}, Stop: {precio_compra * STOP_LOSS:.2f}, Tiempo: {tiempo_transcurrido}")

                if precio_actual >= precio_compra * TAKE_PROFIT:
                    print("🎯 TAKE PROFIT alcanzado")
                    vender()
                elif precio_actual <= precio_compra * STOP_LOSS:
                    print("🛑 STOP LOSS alcanzado")
                    vender()
                elif tiempo_transcurrido > timedelta(hours=TIMEOUT_HORAS):
                    print("⏰ TIMEOUT alcanzado")
                    vender()

            time.sleep(60)
        except Exception as e:
            print(f"❌ Error en control_venta: {e}")
            time.sleep(60)

if __name__ == "__main__":
    threading.Thread(target=control_venta).start()
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)


