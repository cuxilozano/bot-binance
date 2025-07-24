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

# PARÁMETROS AJUSTADOS
TIMEOUT_HORAS = 72
TRAILING_ACTIVACION = 1.005   # +0.5%
TRAILING_DISTANCIA = 0.995    # -0.5% desde el máximo
CAIDA_PARA_SEGUNDA_ENTRADA = 0.9925  # -0.75%

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
        return

    usdc_balance = float(client.get_asset_balance(asset="USDC")["free"])
    precio = obtener_precio_actual()
    cantidad_total = usdc_balance / precio / 2  # Solo 50% en la primera entrada

    info = client.get_symbol_info(PAIR)
    step_size = 0.000001
    for f in info["filters"]:
        if f["filterType"] == "LOT_SIZE":
            step_size = float(f["stepSize"])

    cantidad = round_step_size(cantidad_total, step_size)

    orden = client.order_market_buy(symbol=PAIR, quantity=cantidad)
    guardar_estado({
        "operacion_abierta": True,
        "precio_compra": precio,
        "hora_compra": datetime.now().isoformat(),
        "segunda_compra": False,
        "precio_max": precio
    })
    print(f"✅ COMPRA 50%: {cantidad} BTC a {precio}")


def comprar_segundo_tramo():
    estado = cargar_estado()
    if not estado["operacion_abierta"] or estado["segunda_compra"]:
        return

    usdc_balance = float(client.get_asset_balance(asset="USDC")["free"])
    precio = obtener_precio_actual()
    cantidad_total = usdc_balance / precio

    info = client.get_symbol_info(PAIR)
    step_size = 0.000001
    for f in info["filters"]:
        if f["filterType"] == "LOT_SIZE":
            step_size = float(f["stepSize"])

    cantidad = round_step_size(cantidad_total, step_size)
    orden = client.order_market_buy(symbol=PAIR, quantity=cantidad)

    # Calcular nuevo precio medio
    nuevo_precio_medio = (estado["precio_compra"] + precio) / 2
    guardar_estado({
        "operacion_abierta": True,
        "precio_compra": nuevo_precio_medio,
        "hora_compra": estado["hora_compra"],
        "segunda_compra": True,
        "precio_max": nuevo_precio_medio
    })
    print(f"🟦 SEGUNDA COMPRA ejecutada: {cantidad} BTC a {precio}")


def vender():
    estado = cargar_estado()
    if not estado["operacion_abierta"]:
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
    if data.get("action") == "buy":
        comprar()
    return {"status": "ok"}


def control_venta():
    print("🚀 BTC SENTRY activo y vigilando...")
    while True:
        try:
            estado = cargar_estado()
            if estado["operacion_abierta"]:
                precio_actual = obtener_precio_actual()
                precio_compra = estado["precio_compra"]
                precio_max = estado.get("precio_max", precio_compra)
                hora_compra = datetime.fromisoformat(estado["hora_compra"])
                tiempo_transcurrido = datetime.now() - hora_compra

                # Posible segunda compra si no se ha hecho
                if not estado.get("segunda_compra") and precio_actual <= precio_compra * CAIDA_PARA_SEGUNDA_ENTRADA:
                    comprar_segundo_tramo()

                # Trailing Stop dinámico
                if precio_actual > precio_max:
                    estado["precio_max"] = precio_actual
                    guardar_estado(estado)

                if precio_max >= precio_compra * TRAILING_ACTIVACION:
                    if precio_actual <= precio_max * TRAILING_DISTANCIA:
                        print("🔁 TRAILING STOP activado")
                        vender()
                        continue

                # Timeout de seguridad
                if tiempo_transcurrido > timedelta(hours=TIMEOUT_HORAS):
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
