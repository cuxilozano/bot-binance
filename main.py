from flask import Flask, request, jsonify
from binance.client import Client
from binance.enums import *
import os
import time

app = Flask(__name__)

api_key = os.getenv("BINANCE_API_KEY")
api_secret = os.getenv("BINANCE_API_SECRET")
client = Client(api_key, api_secret)

PRECIO_COMPRA = 0  # Se actualizará tras comprar

@app.route('/webhook', methods=['POST'])
def webhook():
    global PRECIO_COMPRA

    data = request.json
    action = data.get("action")

    if action == "buy":
        try:
            balance = float(client.get_asset_balance(asset='USDC')["free"])
            if balance < 10:
                return jsonify({"error": "No hay suficiente USDC"}), 400

            order = client.order_market_buy(
                symbol='BTCUSDC',
                quoteOrderQty=balance
            )
            fill = float(order["fills"][0]["price"])
            PRECIO_COMPRA = fill
            print(f"✅ Compra ejecutada a {fill} USDC")

        except Exception as e:
            return jsonify({"error": str(e)}), 500

    elif action == "sell":
        if PRECIO_COMPRA == 0:
            return jsonify({"message": "No hay BTC comprado"}), 200

        try:
            btc_balance = float(client.get_asset_balance(asset='BTC')["free"])
            current_price = float(client.get_symbol_ticker(symbol="BTCUSDC")["price"])

            if current_price >= PRECIO_COMPRA * 1.005:
                order = client.order_market_sell(
                    symbol='BTCUSDC',
                    quantity=round(btc_balance, 6)
                )
                print(f"✅ Venta ejecutada a {current_price} USDC")
                PRECIO_COMPRA = 0  # Reiniciar para el siguiente ciclo

        except Exception as e:
            return jsonify({"error": str(e)}), 500

    return jsonify({"message": "OK"}), 200

if __name__ == '__main__':
    app.run(debug=False, host='0.0.0.0', port=8080)
