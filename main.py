from flask import Flask, request, jsonify
from binance.client import Client
from binance.enums import *
from binance.helpers import round_step_size
import os
import time
import threading
from datetime import datetime, timedelta

app = Flask(__name__)

api_key = os.getenv("BINANCE_API_KEY")
api_secret = os.getenv("BINANCE_API_SECRET")
client = Client(api_key, api_secret)

precio_compra = 0
hora_compra = None
TIMEOUT_HORAS = 120  # 5 días
TAKE_PROFIT = 1.005  # +0.5%
STOP_LOSS = 0.998    # -0.2%


def comprar_todo():
    global precio_compra, hora_compra
    usdc_balance = float(client.get_asset_balance(asset='USDC')["free"])
    if usdc_balance < 10:
        print("❌ No hay suficiente USDC.")
        return

    order = client.order_market_buy(
        symbol='BTCUSDC',
        quoteOrderQty=usdc_balance
    )
    precio_compra = float(order["fills"][0]["price"])
    hora_compra = datetime.utcnow()
    print(f"✅ COMPRA: {order['executedQty']} BTC a {precio_compra} USDC a las {hora_compra}")


def vender_todo_btc(precio_actual):
    global precio_compra, hora_compra
    btc_balance = float(client.get_asset_balance(asset='BTC')["free"])
    if btc_balance > 0.0001:
        cantidad = round_step_size(btc_balance, 0.000001)
        client.order_market_sell(
            symbol="BTCUSDC",
            quantity=cantidad
        )
        print(f"🔴 VENTA: {cantidad} BTC a {precio_actual} USDC")
    else:
        print("⚠️ No hay suficiente BTC para vender.")
    precio_compra = 0
    hora_compra = None


def control_venta():
    global precio_compra, hora_compra
    while True:
        try:
            if precio_compra == 0 or hora_compra is None:
                time.sleep(60)
                continue

            precio_actual = float(client.get_symbol_ticker(symbol="BTCUSDC")["price"])
            objetivo = precio_compra * TAKE_PROFIT
            stop = precio_compra * STOP_LOSS
            ahora = datetime.utcnow()
            tiempo_pasado = (ahora - hora_compra).total_seconds() / 3600

            if precio_actual >= objetivo:
                vender_todo_btc(precio_actual)
            elif tiempo_pasado >= TIMEOUT_HORAS:
                if precio_actual <= stop:
                    print("🛑 Timeout alcanzado: venta por pérdida máxima -0.2%")
                else:
                    print("🕒 Timeout alcanzado: venta al precio actual")
                vender_todo_btc(precio_actual)
            else:
                print(f"⏳ Revisando: actual={precio_actual:.2f}, objetivo={objetivo:.2f}, tiempo={tiempo_pasado:.1f}h")

        except Exception as e:
            print(f"❌ ERROR en control de venta: {e}")

        time.sleep(60)


@app.route('/webhook', methods=['POST'])
def webhook():
    global precio_compra
    data = request.json
    if not data or data.get("action") != "buy":
        return jsonify({"status": "Sin acción"}), 200

    comprar_todo()
    return jsonify({"status": "Compra ejecutada"}), 200


if __name__ == '__main__':
    threading.Thread(target=control_venta, daemon=True).start()
    app.run(host='0.0.0.0', port=8080)
