# main_v5.py — Modo B (TP1 0.5%, TP2 1% / BE, SL -1% si no tocó 0.5%)
import json
import os
import time
import threading
from datetime import datetime, timedelta
from flask import Flask, request, jsonify
from binance.client import Client

# ------------------ Config ------------------
PAIR = "BTCUSDC"
JSON_FILE = "estado_compra.json"

# Polling de precio (segundos)
POLL_SECS = 5

# Timeout opcional por seguridad (None para desactivar)
TIMEOUT_HORAS = None  # o 72 si quieres mantenerlo como antes

# Targets (Modo B)
TP1_FACTOR = 1.005     # +0.5%
TP2_FACTOR = 1.010     # +1.0%
SL_FACTOR  = 0.990     # -1.0% (sólo si NO tocó +0.5%)

# Buffer para evitar "insufficient balance" en compras a mercado
QUOTE_BUFFER = 0.002   # 0.2%

app = Flask(__name__)

client = Client(
    api_key=os.getenv("BINANCE_API_KEY"),
    api_secret=os.getenv("BINANCE_API_SECRET")
)

# ------------------ Utilidades ------------------
def cargar_estado():
    try:
        with open(JSON_FILE, "r") as f:
            return json.load(f)
    except:
        return {"operacion_abierta": False}

def guardar_estado(data):
    with open(JSON_FILE, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def obtener_precio_actual():
    ticker = client.get_symbol_ticker(symbol=PAIR)
    return float(ticker["price"])

def get_step_size(symbol=PAIR):
    info = client.get_symbol_info(symbol)
    step = 0.000001
    for f in info["filters"]:
        if f["filterType"] == "LOT_SIZE":
            step = float(f["stepSize"])
    return step

def round_step_size(quantity, step_size):
    # redondeo hacia abajo al múltiplo del step
    return float(int(quantity / step_size) * step_size)

def get_free(asset):
    b = client.get_asset_balance(asset=asset)
    return float(b["free"]) if b else 0.0

def now_iso():
    return datetime.now().isoformat()

# ------------------ Trading ------------------
def comprar_100():
    estado = cargar_estado()
    if estado.get("operacion_abierta"):
        print("ℹ️ Ya hay operación abierta. Ignoro BUY.")
        return

    usdc = get_free("USDC")
    if usdc <= 5:
        print(f"⚠️ USDC insuficiente ({usdc}).")
        return

    px = obtener_precio_actual()
    step = get_step_size(PAIR)
    usdc_to_spend = usdc * (1.0 - QUOTE_BUFFER)
    qty_raw = usdc_to_spend / px
    qty = round_step_size(qty_raw, step)
    if qty <= 0:
        print("⚠️ Cantidad calculada <= 0 tras redondeo.")
        return

    orden = client.order_market_buy(symbol=PAIR, quantity=qty)

    # calcular precio medio con fills si existen
    fills = orden.get("fills", [])
    if fills:
        spent = sum(float(f["price"]) * float(f["qty"]) for f in fills)
        qty_exec = sum(float(f["qty"]) for f in fills)
        avg_px = spent / qty_exec if qty_exec > 0 else px
    else:
        # fallback si no vienen fills
        qty_exec = qty
        avg_px = px

    estado = {
        "operacion_abierta": True,
        "precio_compra": avg_px,
        "hora_compra": now_iso(),
        "qty_total": qty_exec,
        "qty_restante": qty_exec,
        "tp1_done": False,         # aún no vendimos el 50%
        "timeout_horas": TIMEOUT_HORAS
    }
    guardar_estado(estado)
    print(f"✅ COMPRA: {qty_exec:.8f} BTC a {avg_px:.2f} USDC")

def vender_qty(qty):
    step = get_step_size(PAIR)
    qty = round_step_size(qty, step)
    if qty <= 0:
        return None
    return client.order_market_sell(symbol=PAIR, quantity=qty)

def cerrar_operacion_total():
    """Vende TODO el BTC libre y marca operación cerrada."""
    btc = get_free("BTC")
    if btc > 0:
        vender_qty(btc)
        print(f"✅ VENTA TOTAL: {btc:.8f} BTC")
    guardar_estado({"operacion_abierta": False})

def vender_tp1(estado):
    """Vende el 50% del total inicial (no del remanente)."""
    qty_total = float(estado["qty_total"])
    qty_obj = qty_total * 0.5
    orden = vender_qty(qty_obj)
    if orden:
        estado["qty_restante"] = max(0.0, float(estado["qty_restante"]) - qty_obj)
        estado["tp1_done"] = True
        guardar_estado(estado)
        print(f"🎯 TP1 ejecutado: vendidas ~{qty_obj:.8f} BTC (50%)")

def vender_resto_y_cerrar(estado, motivo="TP2/BE/SL/Timeout"):
    """Vende todo lo restante y cierra."""
    qty_rest = float(estado.get("qty_restante", 0.0))
    if qty_rest <= 0:
        # por si hay restos en billetera distintos al qty_rest
        qty_rest = get_free("BTC")
    orden = vender_qty(qty_rest)
    if orden:
        print(f"✅ VENTA FINAL ({motivo}): {qty_rest:.8f} BTC")
    else:
        print(f"⚠️ No se pudo vender (quizá qty <= step). Limpio restos.")
    cerrar_operacion_total()

# ------------------ Monitor ------------------
def control_venta():
    print("🚀 Iniciando control de ventas (Modo B)…")
    while True:
        try:
            estado = cargar_estado()
            if estado.get("operacion_abierta"):
                px = obtener_precio_actual()
                entry = float(estado["precio_compra"])
                tp1 = entry * TP1_FACTOR
                tp2 = entry * TP2_FACTOR
                sl  = entry * SL_FACTOR
                tp1_done = bool(estado.get("tp1_done", False))

                # Timeout opcional
                if TIMEOUT_HORAS is not None:
                    t0 = datetime.fromisoformat(estado["hora_compra"])
                    if datetime.now() - t0 > timedelta(hours=TIMEOUT_HORAS):
                        print("⏰ TIMEOUT alcanzado -> cerrar operación")
                        vender_resto_y_cerrar(estado, "Timeout")
                        time.sleep(POLL_SECS)
                        continue

                # Lógica Modo B
                if not tp1_done:
                    # Si alcanza TP1 (+0.5%): vender 50%
                    if px >= tp1:
                        vender_tp1(estado)
                    # SL -1% sólo si NO tocó TP1
                    elif px <= sl:
                        print("🛑 SL antes de TP1 -> cerrar todo")
                        vender_resto_y_cerrar(estado, "SL")
                else:
                    # Tras TP1: TP2 (+1%) o BE (si vuelve al precio de entrada)
                    if px >= tp2:
                        vender_resto_y_cerrar(estado, "TP2")
                    elif px <= entry:
                        vender_resto_y_cerrar(estado, "BreakEven")

            time.sleep(POLL_SECS)
        except Exception as e:
            print(f"❌ Error en control_venta: {e}")
            time.sleep(POLL_SECS)

# ------------------ API ------------------
@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(silent=True) or {}
    if data.get("action") == "buy":
        comprar_100()
    return jsonify({"status": "ok"})

@app.route("/status", methods=["GET"])
def status():
    estado = cargar_estado()
    price = None
    try:
        price = obtener_precio_actual()
    except Exception:
        pass
    return jsonify({
        "alive": True,
        "pair": PAIR,
        "price": price,
        "state": estado
    })

@app.route("/health", methods=["GET"])
def health():
    return "ok", 200

# ------------------ Boot ------------------
if __name__ == "__main__":
    print("🟢 Bot V5 (Modo B) iniciado.")
    hilo = threading.Thread(target=control_venta, daemon=True)
    hilo.start()
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)

