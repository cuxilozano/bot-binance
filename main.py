import json
import os
import time
import threading
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_DOWN

from flask import Flask, request, jsonify
from binance.client import Client

# ------------------ Config ------------------
PAIR = "BTCUSDC"
JSON_FILE = "estado_compra.json"

STEP_SIZE = Decimal("0.000001")
MIN_QTY = Decimal("0")
DECIMAL_PLACES = 6

_LOT_SIZE_CACHE = None

POLL_SECS = 5
TIMEOUT_HORAS = None   # e.g. 72 si quieres forzar salida tras X horas

# Targets
TP1_FACTOR = 1.0045    # +0.45% -> vende 50%
SL_FACTOR  = 0.990     # -1.0%  -> solo si NO tocó TP1

# Break-even fee-aware (BNB activado: ~0.075% por lado)
FEE_SIDE = 0.00075
BE_FACTOR = 1 + 2 * FEE_SIDE   # ~1.0015  (+0.15%)

# Trailing del 50% restante
TRAIL_ACTIVATION = 1.007   # +0.7% desde entry
TRAIL_DIST = 0.0025        # 0.25% de retroceso desde el pico

# Buffer compras para evitar insufficient balance
QUOTE_BUFFER = 0.002       # 0.2%

# (Opcional) token para /unlock (deja vacío si no lo usas)
UNLOCK_TOKEN = os.getenv("UNLOCK_TOKEN", "")

app = Flask(__name__)
client = Client(
    api_key=os.getenv("BINANCE_API_KEY"),
    api_secret=os.getenv("BINANCE_API_SECRET")
)


def _calcular_decimales(step: Decimal) -> int:
    try:
        step_normalizado = step.normalize()
        return max(0, -step_normalizado.as_tuple().exponent)
    except Exception:
        return DECIMAL_PLACES


def cargar_filtros_lot_size(force: bool = False):
    global STEP_SIZE, MIN_QTY, DECIMAL_PLACES, _LOT_SIZE_CACHE
    if _LOT_SIZE_CACHE is not None and not force:
        return _LOT_SIZE_CACHE
    try:
        info = client.get_symbol_info(symbol=PAIR)
        filtros = info.get("filters", []) if info else []
        lot_filter = next((f for f in filtros if f.get("filterType") == "LOT_SIZE"), None)
        if lot_filter:
            step_val = lot_filter.get("stepSize")
            min_qty_val = lot_filter.get("minQty")
            if step_val is not None:
                step = Decimal(str(step_val))
                if step > 0:
                    STEP_SIZE = step
                    DECIMAL_PLACES = _calcular_decimales(STEP_SIZE)
            if min_qty_val is not None:
                min_qty = Decimal(str(min_qty_val))
                if min_qty >= 0:
                    MIN_QTY = min_qty
            _LOT_SIZE_CACHE = {
                "stepSize": STEP_SIZE,
                "minQty": MIN_QTY,
                "decimals": DECIMAL_PLACES,
            }
            return _LOT_SIZE_CACHE
    except Exception as e:
        print(f"⚠️ No se pudo cargar LOT_SIZE para {PAIR}: {e}")
    _LOT_SIZE_CACHE = {
        "stepSize": STEP_SIZE,
        "minQty": MIN_QTY,
        "decimals": DECIMAL_PLACES,
    }
    return _LOT_SIZE_CACHE


cargar_filtros_lot_size()

# ------------------ Utils ------------------
def cargar_estado():
    try:
        with open(JSON_FILE, "r") as f:
            data = json.load(f)
            # sane defaults si faltan
            if "buy_lock" not in data:
                data["buy_lock"] = False
            return data
    except:
        return {"operacion_abierta": False, "buy_lock": False}

def guardar_estado(data):
    # asegura campo buy_lock siempre presente
    if "buy_lock" not in data:
        data["buy_lock"] = False
    with open(JSON_FILE, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def formatear_cantidad(qty) -> str:
    try:
        cantidad = Decimal(str(qty))
    except Exception:
        return "0"
    try:
        cantidad = cantidad.quantize(STEP_SIZE, rounding=ROUND_DOWN)
    except Exception:
        pass
    return f"{cantidad:.{DECIMAL_PLACES}f}"


def normalizar_cantidad(qty):
    if qty is None:
        return Decimal("0")
    try:
        cantidad = Decimal(str(qty))
    except Exception:
        return Decimal("0")
    step = STEP_SIZE if STEP_SIZE > 0 else Decimal("0.000001")
    try:
        normalizada = (cantidad // step) * step
        normalizada = normalizada.quantize(step, rounding=ROUND_DOWN)
    except Exception:
        return Decimal("0")
    if normalizada <= 0 or normalizada < MIN_QTY:
        return Decimal("0")
    return normalizada

def obtener_precio_actual():
    ticker = client.get_symbol_ticker(symbol=PAIR)
    return float(ticker["price"])

def get_free(asset):
    b = client.get_asset_balance(asset=asset)
    return float(b["free"]) if b else 0.0

def now_iso():
    return datetime.now().isoformat()

def lock_on():
    st = cargar_estado()
    st["buy_lock"] = True
    guardar_estado(st)

def lock_off():
    st = cargar_estado()
    st["buy_lock"] = False
    st["operacion_abierta"] = False
    # limpia resto de campos para el próximo ciclo
    keep = {"operacion_abierta": False, "buy_lock": False}
    guardar_estado(keep)

def reconciliar_estado():
    st = cargar_estado()
    # si no hay operación pero el lock quedó activo, libera
    if not st.get("operacion_abierta") and st.get("buy_lock"):
        print("ℹ️ No hay operación pero buy_lock activo -> liberando.")
        lock_off()
        return
    if not st.get("operacion_abierta"):
        return
    # si hay operación abierta, alinea qty_restante con wallet
    qty_wallet = get_free("BTC")
    qty_rest = float(st.get("qty_restante", 0.0))
    if qty_wallet <= 0 and qty_rest > 0:
        print("ℹ️ No hay BTC pero estado marcaba operación -> cierro estado.")
        lock_off()
    elif qty_rest > 0 and abs(qty_wallet - qty_rest) / max(qty_rest, 1e-8) > 0.02:
        print(f"ℹ️ Ajusto qty_restante {qty_rest:.8f} -> {qty_wallet:.8f}")
        st["qty_restante"] = qty_wallet
        guardar_estado(st)

# ------------------ Trading ------------------
def comprar_100(uid=None):
    st = cargar_estado()
    if st.get("buy_lock"):
        print("🔒 BUY ignorado: buy_lock activo (esperando cierre total).")
        return
    if st.get("operacion_abierta"):
        print("ℹ️ Ya hay operación abierta. Ignoro BUY.")
        return
    if uid and st.get("last_uid") == uid:
        print("ℹ️ Alerta duplicada ignorada por uid.")
        return

    usdc = get_free("USDC")
    if usdc <= 5:
        print(f"⚠️ USDC insuficiente ({usdc}).")
        return

    usdc_to_spend = usdc * (1.0 - QUOTE_BUFFER)
    orden = client.order_market_buy(
        symbol=PAIR,
        quoteOrderQty=round(usdc_to_spend, 2)
    )

    # calcular precio medio con fills
    fills = orden.get("fills", [])
    if fills:
        spent = sum(float(f["price"]) * float(f["qty"]) for f in fills)
        qty_exec = sum(float(f["qty"]) for f in fills)
        avg_px = spent / qty_exec if qty_exec > 0 else obtener_precio_actual()
    else:
        qty_exec = float(orden.get("executedQty", 0))
        avg_px = obtener_precio_actual()

    qty_exec = normalizar_cantidad(qty_exec)
    qty_exec_float = float(qty_exec)

    st = {
        "operacion_abierta": True,
        "precio_compra": avg_px,
        "hora_compra": now_iso(),
        "qty_total": qty_exec_float,
        "qty_restante": qty_exec_float,
        "tp1_done": False,
        "trail_active": False,
        "trail_peak": None,
        "timeout_horas": TIMEOUT_HORAS,
        "last_uid": uid,
        "buy_lock": True
    }
    guardar_estado(st)
    print(f"✅ COMPRA: {qty_exec_float:.8f} BTC a {avg_px:.2f} USDC")
    print("🔒 buy_lock ACTIVADO: se ignoran nuevas BUY hasta cierre total.")

def vender_qty(qty):
    qty = normalizar_cantidad(qty)
    if qty <= 0:
        return None
    qty_str = formatear_cantidad(qty)
    return client.order_market_sell(symbol=PAIR, quantity=qty_str)

def vender_tp1(st):
    qty_total = float(st["qty_total"])
    qty_obj = qty_total * 0.5
    qty_libre = get_free("BTC")
    qty_sell = normalizar_cantidad(min(qty_obj, qty_libre))
    if qty_sell <= 0:
        return
    orden = vender_qty(qty_sell)
    if orden:
        executed_qty = normalizar_cantidad(orden.get("executedQty", 0.0))
        executed_qty_float = float(executed_qty)
        st["qty_restante"] = max(0.0, float(st["qty_restante"]) - executed_qty_float)
        if executed_qty > 0:
            st["tp1_done"] = True
        guardar_estado(st)
        print(f"🎯 TP1 ejecutado: vendidas {formatear_cantidad(executed_qty)} BTC (50%)")

def vender_resto_y_cerrar(st, motivo="Exit"):
    qty_rest = float(st.get("qty_restante", 0.0))
    if qty_rest <= 0:
        qty_rest = get_free("BTC")
    qty_rest = normalizar_cantidad(qty_rest)
    orden = vender_qty(qty_rest)
    desbloquear = True
    if orden:
        executed_qty = normalizar_cantidad(orden.get("executedQty", 0.0))
        executed_qty_float = float(executed_qty)
        st["qty_restante"] = max(0.0, float(st.get("qty_restante", 0.0)) - executed_qty_float)
        guardar_estado(st)
        if st["qty_restante"] > 0:
            desbloquear = False
            print(
                f"⚠️ Venta parcial ({motivo}): ejecutadas {formatear_cantidad(executed_qty)} BTC, "
                f"quedan {formatear_cantidad(st['qty_restante'])} BTC"
            )
        else:
            print(f"✅ VENTA FINAL ({motivo}): {formatear_cantidad(executed_qty)} BTC")
    else:
        print(f"⚠️ Venta fallida ({motivo}), intenté limpiar todo.")
    if desbloquear:
        lock_off()
        print("🔓 buy_lock LIBERADO: se aceptan nuevas BUY.")

# ------------------ Monitor ------------------
def control_venta():
    print("🚀 Iniciando control de ventas (Modo B + Trailing + Lock)…")
    reconciliar_estado()
    while True:
        try:
            st = cargar_estado()
            if st.get("operacion_abierta"):
                px = obtener_precio_actual()
                entry = float(st["precio_compra"])
                tp1 = entry * TP1_FACTOR
                sl  = entry * SL_FACTOR

                # Timeout
                if TIMEOUT_HORAS is not None:
                    t0 = datetime.fromisoformat(st["hora_compra"])
                    if datetime.now() - t0 > timedelta(hours=TIMEOUT_HORAS):
                        vender_resto_y_cerrar(st, "Timeout")
                        time.sleep(POLL_SECS)
                        continue

                if not bool(st.get("tp1_done", False)):
                    # TP1 o SL antes de TP1
                    if px >= tp1:
                        vender_tp1(st)
                    elif px <= sl:
                        print("🛑 StopLoss antes de TP1")
                        vender_resto_y_cerrar(st, "StopLoss")
                else:
                    # TP1 hecho -> BE o Trailing
                    if not st.get("trail_active"):
                        if px >= entry * TRAIL_ACTIVATION:
                            st["trail_active"] = True
                            st["trail_peak"] = px
                            guardar_estado(st)
                            print(f"🔓 Trailing ON @ {px:.2f} (+{(px/entry-1)*100:.2f}%)")
                        elif px <= entry * BE_FACTOR:
                            vender_resto_y_cerrar(st, "BreakEven")
                    else:
                        # trailing activo
                        if px > float(st["trail_peak"] or entry):
                            st["trail_peak"] = px
                            guardar_estado(st)
                        stop_trail = float(st["trail_peak"]) * (1 - TRAIL_DIST)
                        if px <= stop_trail:
                            vender_resto_y_cerrar(st, "TrailingStop")

            time.sleep(POLL_SECS)
        except Exception as e:
            print(f"❌ Error en control_venta: {e}")
            time.sleep(POLL_SECS)

# ------------------ API ------------------
@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(silent=True) or {}
    if data.get("action") == "buy":
        st = cargar_estado()
        if st.get("buy_lock") or st.get("operacion_abierta"):
            print("🔒 BUY ignorado: operación aún no cerrada (lock).")
            return jsonify({"status": "ignored_locked"})
        uid = str(data.get("uid")) if data.get("uid") else None
        comprar_100(uid)
    return jsonify({"status": "ok"})

@app.route("/status", methods=["GET"])
def status():
    st = cargar_estado()
    price = None
    try:
        price = obtener_precio_actual()
    except Exception:
        pass
    return jsonify({
        "alive": True,
        "pair": PAIR,
        "price": price,
        "state": st
    })

@app.route("/health", methods=["GET"])
def health():
    return "ok", 200

# (Opcional) endpoint para liberar lock manualmente con token
@app.route("/unlock", methods=["POST"])
def unlock():
    token = request.args.get("token", "")
    if not UNLOCK_TOKEN or token != UNLOCK_TOKEN:
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    lock_off()
    return jsonify({"ok": True, "msg": "lock off"})

# ------------------ Boot ------------------
if __name__ == "__main__":
    print("🟢 Bot V7 (Modo B + Trailing + BuyLock) iniciado.")
    hilo = threading.Thread(target=control_venta, daemon=True)
    hilo.start()
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
