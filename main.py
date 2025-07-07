from flask import Flask, request, jsonify
from binance.client import Client
from binance.enums import *
import os
import time
import threading

app = Flask(__name__)

api_key = os.getenv("BINANCE_API_KEY")
api_secret = os.getenv("BINANCE_API_SECRET")
client = Client(api_key, api_secret)

precio_compra = 0  # Se actualiza cuando se compra


def comprar_todo():
    global precio_compra
    usdc_balance = float(client.get_asset_balance(asset='USDC')["free"])
    if usdc_balance < 10:
        print("❌ No hay suficiente USDC.")
        return

    order = client.order_market_buy(
        symbol='BTCUSDC',
        quoteOrderQty=usdc_balance
    )
    precio_compra = float(order["fills"][0]["price"])
    print(f"✅ COMPRA: {order['executedQty']} BTC a {precio_compra} USDC")


def vender_si_1porciento():
    global precio_compra
    while True:
        try:
            if precio_compra == 0:
                time.sleep(60)
                continue

            precio_actual = float(client.get_symbol_ticker(symbol="BTCUSDC")["price"])
            objetivo = precio_compra * 1.005

            if precio_actual >= objetivo:
                btc_balance = float(client.get_asset_balance(asset='BTC')["free"])
                if btc_balance >= 0.00001:
                    cantidad_btc = round(btc_balance, 6)
                    try:
                        client.order_market_sell(
                            symbol="BTCUSDC",
                            quantity=cantidad_btc
                        )
                        print(f"🔴 VENTA: {cantidad_btc} BTC a {precio_actual} USDC")
                        precio_compra = 0  # Reiniciar para próxima compra
                    except Exception as e:
                        print(f"❌ ERROR ejecutando la venta: {e}")
                else:
                    print("⚠️ No hay suficiente BTC para vender (mínimo 0.00001 BTC)")
            else:
                print(f"⏳ Revisando: actual={precio_actual}, objetivo={objetivo}")

        except Exception as e:
            print(f"❌ ERROR en venta automática: {e}")

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
    threading.Thread(target=vender_si_1porciento, daemon=True).start()
    app.run(host='0.0.0.0', port=8080)

