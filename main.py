
import os
import json
import time
from flask import Flask, request, jsonify
from binance.client import Client
from dotenv import load_dotenv
from decimal import Decimal, ROUND_DOWN
from threading import Thread

load_dotenv()

app = Flask(__name__)

API_KEY = os.getenv("BINANCE_API_KEY")
API_SECRET = os.getenv("BINANCE_API_SECRET")
client = Client(API_KEY, API_SECRET)

SYMBOL = "BTCUSDC"
STATE_FILE = "state.json"

def get_balances():
    usdc = float(client.get_asset_balance(asset='USDC')['free'])
    btc = float(client.get_asset_balance(asset='BTC')['free'])
    return usdc, btc

def get_price():
    return float(client.get_symbol_ticker(symbol=SYMBOL)['price'])

def load_state():
    if not os.path.exists(STATE_FILE):
        return {"precio_compra": 0, "btc_comprado": 0, "usdc_invertido": 0}
    with open(STATE_FILE, "r") as f:
        return json.load(f)

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)

def comprar_con_todo():
    usdc, btc = get_balances()
    if usdc < 10:
        print("No hay suficiente USDC para operar.")
        return "Insuficiente USDC", 400

    price = get_price()
    cantidad_btc = (Decimal(str(usdc - 1)) / Decimal(str(price)))
    step_size = Decimal('0.00001')
    cantidad_btc = cantidad_btc.quantize(step_size, rounding=ROUND_DOWN)

    if cantidad_btc > 0:
        print(f"✅ Ejecutando compra: {cantidad_btc} BTC a {price} USDC")
        client.order_market_buy(symbol=SYMBOL, quantity=float(cantidad_btc))
        save_state({
            "precio_compra": price,
            "btc_comprado": float(cantidad_btc),
            "usdc_invertido": usdc
        })
        return "Compra ejecutada", 200
    else:
        print("❌ La cantidad calculada de BTC no es válida.")
        return "Cantidad de BTC inválida", 400

def vender_todo():
    state = load_state()
    btc_comprado = float(state.get("btc_comprado", 0))

    if btc_comprado > 0:
        price = get_price()
        cantidad_btc = Decimal(str(btc_comprado)).quantize(Decimal("0.00001"), rounding=ROUND_DOWN)
        print(f"🚀 Ejecutando venta manual: {cantidad_btc} BTC a {price} USDC")
        client.order_market_sell(symbol=SYMBOL, quantity=float(cantidad_btc))
        save_state({"precio_compra": 0, "btc_comprado": 0, "usdc_invertido": 0})
        return "Venta ejecutada", 200
    else:
        print("❌ No hay BTC para vender.")
        return "Nada que vender", 400

@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.json
    if not data or 'action' not in data:
        return jsonify({'error': 'Solicitud no válida'}), 400

    if data['action'] == 'buy':
        usdc, btc = get_balances()
        price = get_price()
        valor_btc = btc * price

        if valor_btc < 50:
            return comprar_con_todo()
        else:
            return "Ya tienes suficiente BTC, sin compra.", 200

    if data['action'] == 'sell':
        return vender_todo()

    return jsonify({'error': 'Acción desconocida'}), 400

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080)
