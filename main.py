import os, json, time, threading, math, traceback
from decimal import Decimal, ROUND_DOWN
from datetime import datetime, timedelta, timezone

from flask import Flask, request, jsonify
from binance.client import Client
from binance.enums import *
from binance.exceptions import BinanceAPIException

# =========================
# Configuración / ENV
# =========================
API_KEY = os.getenv("BINANCE_API_KEY", "")
API_SECRET = os.getenv("BINANCE_API_SECRET", "")

SYMBOL = os.getenv("SYMBOL", "BTCUSDC")
BASE_ASSET = os.getenv("BASE_ASSET", "BTC")
QUOTE_ASSET = os.getenv("QUOTE_ASSET", "USDC")

BUY_PCT = float(os.getenv("BUY_PCT", "1.0"))  # 1.0 = 100%
POLL_SECS = int(os.getenv("POLL_SECS", "2"))
COOLDOWN_BARS = int(os.getenv("COOLDOWN_BARS", "0"))  # velas de 1h

# TP1 + trailing fino
TP1_PCT = float(os.getenv("TP1_PCT", "0.006"))             # +0.6%
TP1_SELL_PCT = float(os.getenv("TP1_SELL_PCT", "0.5"))     # 50%
TRAIL2_DIST_PCT = float(os.getenv("TRAIL2_DIST_PCT", "0.0015"))  # 0.15%

# Stop Loss
SL_MODE = os.getenv("SL_MODE", "cap_atr").lower()  # "cap", "atr", "cap_atr"
SL_MAX_PCT = float(os.getenv("SL_MAX_PCT", "0.008"))  # 0.8% máx
ATR_PERIOD = int(os.getenv("ATR_PERIOD", "14"))
ATR_MULT = float(os.getenv("ATR_MULT", "1.0"))

STATE_PATH = os.getenv("STATE_PATH", "estado.json")

FLASK_HOST = os.getenv("FLASK_HOST", "0.0.0.0")
FLASK_PORT = int(os.getenv("FLASK_PORT", "8080"))

BOT_VERSION = "MINADOR P-TP0.6 + TRAIL0.15 (cap_atr)"

# =========================
# Inicialización
# =========================
app = Flask(__name__)
client = Client(API_KEY, API_SECRET)

state_lock = threading.Lock()
sell_lock = threading.Lock()

# Datos de símbolo (pasos y mínimos)
SYMBOL_INFO = client.get_symbol_info(SYMBOL)
LOT_STEP = Decimal("0.00000100")
MIN_QTY = Decimal("0.00001000")
PRICE_TICK = Decimal("0.01")

for f in SYMBOL_INFO["filters"]:
    if f["filterType"] == "LOT_SIZE":
        LOT_STEP = Decimal(f["stepSize"])
        MIN_QTY = Decimal(f["minQty"])
    if f["filterType"] == "PRICE_FILTER":
        PRICE_TICK = Decimal(f["tickSize"])

def now_iso():
    return datetime.now(timezone.utc).isoformat()

def log(msg):
    print(f"[{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)

# =========================
# Persistencia de estado
# =========================
def cargar_estado():
    if not os.path.exists(STATE_PATH):
        return {
            "operacion_abierta": False,
            "buy_lock": False,
            "last_uid": None,
            "hora_compra": None,
            "precio_compra": None,
            "qty_total": 0.0,
            "qty_restante": 0.0,
            "sl_price": None,
            "tp1_price": None,
            "tp1_done": False,
            "tp1_sell_pct": TP1_SELL_PCT,
            "trail2_active": False,
            "trail2_peak": None,
            "trail2_dist_pct": TRAIL2_DIST_PCT,
            "cooldown_until": None
        }
    with open(STATE_PATH, "r", encoding="utf-8") as f:
        return json.load(f)

def guardar_estado(st, origin=""):
    with state_lock:
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(st, f, indent=2)
    if origin:
        log(f"💾 Estado guardado ({origin})")

# =========================
# Utilidades de mercado
# =========================
def round_step(value: Decimal, step: Decimal) -> Decimal:
    if step == 0:
        return value
    # Floor to step
    q = (value / step).quantize(Decimal('1.'), rounding=ROUND_DOWN)
    return q * step

def formatear_cantidad(q):
    return f"{Decimal(str(q)):.8f}".rstrip('0').rstrip('.') if '.' in f"{q}" else f"{q}"

def normalizar_cantidad(q: Decimal) -> Decimal:
    if q <= 0:
        return Decimal("0")
    q2 = round_step(q, LOT_STEP)
    if q2 < MIN_QTY:
        return Decimal("0")
    return q2

def precio_actual():
    t = client.get_symbol_ticker(symbol=SYMBOL)
    return float(t["price"])

def balances():
    acc = client.get_account()
    b = {x["asset"]: float(x["free"]) for x in acc["balances"]}
    return b.get(BASE_ASSET, 0.0), b.get(QUOTE_ASSET, 0.0)

def get_klines_1h(limit=ATR_PERIOD+50):
    return client.get_klines(symbol=SYMBOL, interval=KLINE_INTERVAL_1HOUR, limit=limit)

def calc_atr(period=ATR_PERIOD):
    # ATR clásico (TR de Wilder)
    ks = get_klines_1h(max(period+1, 50))
    highs = [float(k[2]) for k in ks]
    lows  = [float(k[3]) for k in ks]
    closes= [float(k[4]) for k in ks]
    trs = []
    for i in range(1, len(ks)):
        h, l, pc = highs[i], lows[i], closes[i-1]
        tr = max(h-l, abs(h-pc), abs(l-pc))
        trs.append(tr)
    if len(trs) < period:
        return None
    atr = sum(trs[-period:])/period
    return atr

def calc_sl_price(entry_price: float):
    # Calcula SL usando cap (% del precio) y/o ATR*mult según SL_MODE.
    cap_dist = entry_price * SL_MAX_PCT
    atr = calc_atr(ATR_PERIOD)
    atr_dist = atr * ATR_MULT if atr is not None else None

    if SL_MODE == "cap":
        dist = cap_dist
    elif SL_MODE == "atr":
        dist = atr_dist if atr_dist is not None else cap_dist
    else:  # "cap_atr": usa la MAS PEQUEÑA de ambas (SL más ceñido)
        if atr_dist is None:
            dist = cap_dist
        else:
            dist = min(cap_dist, atr_dist)

    sl = entry_price - dist
    return max(0.0, sl)

# =========================
# Órdenes
# =========================
def comprar_100(uid="ext"):
    """Compra con el % de USDC disponible (BUY_PCT)."""
    try:
        px = precio_actual()
        btc, usdc = balances()
        invertir = Decimal(str(usdc)) * Decimal(str(BUY_PCT))
        if invertir <= 0:
            log("ℹ️ No hay USDC para comprar.")
            return None

        qty = (invertir / Decimal(str(px)))
        qty_n = normalizar_cantidad(qty)
        if qty_n <= 0:
            log("ℹ️ Qty no vendible por LOT/MIN.")
            return None

        orden = client.order_market_buy(symbol=SYMBOL, quantity=float(qty_n))
        avg_px = None
        qty_exec = Decimal("0")
        if orden and orden.get("fills"):
            costo = Decimal("0")
            for f in orden["fills"]:
                qty_exec += Decimal(f["qty"])
                costo += Decimal(f["price"]) * Decimal(f["qty"])
            if qty_exec > 0:
                avg_px = float(costo / qty_exec)
        else:
            avg_px = px

        st = cargar_estado()
        st.update({
            "operacion_abierta": True,
            "buy_lock": True,
            "last_uid": uid,
            "hora_compra": now_iso(),
            "precio_compra": avg_px,
            "qty_total": float(qty_exec),
            "qty_restante": float(qty_exec),

            "sl_price": calc_sl_price(avg_px),

            "tp1_price": avg_px * (1 + TP1_PCT),
            "tp1_done": False,
            "tp1_sell_pct": TP1_SELL_PCT,

            "trail2_active": False,
            "trail2_peak": None,
            "trail2_dist_pct": TRAIL2_DIST_PCT,

            "cooldown_until": None
        })
        guardar_estado(st, origin="buy_ok")
        log(f"✅ COMPRA: {formatear_cantidad(qty_exec)} {BASE_ASSET} @ {avg_px:.2f} | SL @{st['sl_price']:.2f} | TP1 @{st['tp1_price']:.2f}")
        return st
    except BinanceAPIException as e:
        log(f"❌ Binance BUY: {e}")
        return None
    except Exception as e:
        log(f"❌ Error comprar_100: {e}\n{traceback.format_exc()}")
        return None

def vender_qty(qty: Decimal):
    if qty <= 0:
        return None
    qty_n = normalizar_cantidad(qty)
    if qty_n < MIN_QTY:
        return None
    try:
        orden = client.order_market_sell(symbol=SYMBOL, quantity=float(qty_n))
        return orden
    except BinanceAPIException as e:
        log(f"❌ Binance SELL: {e}")
        return None
    except Exception as e:
        log(f"❌ Error vender_qty: {e}\n{traceback.format_exc()}")
        return None

def cerrar_todo(st, motivo=""):
    """Vende todo el remanente y cierra estado + cooldown."""
    if not sell_lock.acquire(blocking=False):
        log("⏳ Venta en curso, salto cerrar_todo duplicado.")
        return False
    try:
        qty_rest = Decimal(str(st.get("qty_restante", 0.0)))
        if qty_rest >= MIN_QTY:
            orden = vender_qty(qty_rest)
            if orden and orden.get("executedQty"):
                log(f"✅ CIERRE TOTAL ({motivo}) qty={orden['executedQty']}")
        else:
            log("ℹ️ Resto < minQty, nada que vender.")

        st2 = cargar_estado()
        st2.update({
            "operacion_abierta": False,
            "buy_lock": False,
            "last_uid": st.get("last_uid"),
            "precio_compra": None,
            "qty_total": 0.0,
            "qty_restante": 0.0,
            "sl_price": None,
            "tp1_price": None,
            "tp1_done": False,
            "trail2_active": False,
            "trail2_peak": None
        })

        # Cooldown
        if COOLDOWN_BARS > 0:
            cooldown_until = (datetime.now(timezone.utc) + timedelta(hours=COOLDOWN_BARS)).isoformat()
        else:
            cooldown_until = None
        st2["cooldown_until"] = cooldown_until

        guardar_estado(st2, origin=f"close_{motivo}")
        return True
    finally:
        sell_lock.release()

def vender_parcial(st, fraccion: float, motivo="TP1"):
    """Vende fracción de qty_restante respetando LOT/MIN."""
    if not sell_lock.acquire(blocking=False):
        log("⏳ Venta en curso, parcial duplicada ignorada.")
        return False
    try:
        fraccion = max(0.0, min(1.0, float(fraccion)))
        qty_rest = Decimal(str(st.get("qty_restante", 0.0)))
        if qty_rest <= 0:
            log("ℹ️ No hay qty_restante.")
            return False

        qty_obj = qty_rest * Decimal(str(fraccion))
        qty_obj_n = normalizar_cantidad(qty_obj)
        if qty_obj_n < MIN_QTY:
            # intenta vender mínimo para no bloquear trailing
            qty_obj_n = normalizar_cantidad(qty_rest * Decimal("0.25"))
            if qty_obj_n < MIN_QTY:
                log("ℹ️ Parcial no vendible (minQty).")
                return False

        orden = vender_qty(qty_obj_n)
        if not orden:
            log("⚠️ Parcial fallida.")
            return False

        executed = Decimal(str(orden.get("executedQty", "0")))
        st["qty_restante"] = float(max(Decimal("0"), qty_rest - executed))
        guardar_estado(st, origin=f"parcial_{motivo}")

        if Decimal(str(st["qty_restante"])) < MIN_QTY:
            log("ℹ️ Resto < minQty tras parcial. Cierro estado.")
            cerrar_todo(st, motivo="DustAfterPartial")
        else:
            log(f"✅ Parcial {motivo}: vendidas {formatear_cantidad(executed)} {BASE_ASSET} | Queda {formatear_cantidad(st['qty_restante'])}")
        return True
    finally:
        sell_lock.release()

# =========================
# Control de salida (hilo)
# =========================
def control_loop():
    log(f"🟢 Bot {BOT_VERSION} iniciado.")
    while True:
        try:
            st = cargar_estado()

            # Cooldown (solo impide nuevas compras)
            if not st.get("operacion_abierta") and st.get("cooldown_until"):
                cu = st["cooldown_until"]
                if cu and datetime.now(timezone.utc) < datetime.fromisoformat(cu):
                    # En cooldown, nada que hacer
                    time.sleep(POLL_SECS)
                    continue
                else:
                    st["cooldown_until"] = None
                    guardar_estado(st, origin="cooldown_done")

            if not st.get("operacion_abierta"):
                time.sleep(POLL_SECS)
                continue

            px = precio_actual()
            entry = float(st["precio_compra"])
            sl = float(st["sl_price"]) if st["sl_price"] else None

            # 1) Stop Loss
            if sl and px <= sl:
                log(f"🛑 SL ejecutado: px={px:.2f} ≤ sl={sl:.2f}")
                cerrar_todo(st, motivo="StopLoss")
                time.sleep(POLL_SECS)
                continue

            # 2) TP1 +0.6% — venta parcial
            if not st.get("tp1_done"):
                tp1 = float(st["tp1_price"])
                if px >= tp1:
                    ok = vender_parcial(st, st.get("tp1_sell_pct", TP1_SELL_PCT), motivo="TP1_+0.6%")
                    st = cargar_estado()
                    if ok and st.get("operacion_abierta"):
                        # Activa trailing fino del resto
                        st["trail2_active"] = True
                        st["trail2_peak"] = px
                        st["tp1_done"] = True
                        guardar_estado(st, origin="tp1_done_trail2_on")
                        log(f"🔓 Trail2 ON (-{TRAIL2_DIST_PCT*100:.2f}%) desde pico {px:.2f}")
                    time.sleep(POLL_SECS)
                    continue

            # 3) Trailing fino del resto (-0.15% desde el máximo)
            if st.get("tp1_done") and st.get("trail2_active"):
                peak = st.get("trail2_peak")
                if peak is None or px > float(peak):
                    st["trail2_peak"] = px
                    guardar_estado(st, origin="trail2_peak_upd")
                stop_trail2 = float(st["trail2_peak"]) * (1 - float(st.get("trail2_dist_pct", TRAIL2_DIST_PCT)))
                if px <= stop_trail2:
                    log(f"⛓️ Trail2 STOP: px {px:.2f} ≤ {stop_trail2:.2f}")
                    cerrar_todo(st, motivo="Trailing2Stop")
                    time.sleep(POLL_SECS)
                    continue

        except Exception as e:
            log(f"❌ control_loop: {e}\n{traceback.format_exc()}")
        time.sleep(POLL_SECS)

# =========================
# Flask webhook
# =========================
@app.route("/health", methods=["GET"])
def health():
    return jsonify(ok=True, version=BOT_VERSION)

@app.route("/webhook", methods=["POST"])
def webhook():
    """
    Espera JSON tipo:
    {
      "secret": "opcional",
      "uid": "id_de_alerta_tradingview",
      "action": "buy" | "ping"
    }
    """
    data = request.get_json(force=True, silent=True) or {}
    uid = str(data.get("uid", "tv"))
    action = str(data.get("action", "")).lower()

    if action == "ping":
        return jsonify(ok=True, msg="pong", uid=uid)

    st = cargar_estado()

    # Chequea cooldown
    if st.get("cooldown_until"):
        cu = st["cooldown_until"]
        if cu and datetime.now(timezone.utc) < datetime.fromisoformat(cu):
            return jsonify(ok=False, msg="cooldown activo", until=cu), 200
        else:
            st["cooldown_until"] = None
            guardar_estado(st, origin="cooldown_clear")

    if action == "buy":
        if st.get("operacion_abierta"):
            return jsonify(ok=False, msg="operación ya abierta (buy_lock)"), 200
        res = comprar_100(uid=uid)
        if res:
            return jsonify(ok=True, msg="buy ejecutada", state=res), 200
        else:
            return jsonify(ok=False, msg="buy fallida"), 200

    return jsonify(ok=False, msg="acción no reconocida"), 400

# =========================
# Arranque
# =========================
if __name__ == "__main__":
    t = threading.Thread(target=control_loop, daemon=True)
    t.start()
    log(f"🟢 {BOT_VERSION} (TP1 +{TP1_PCT*100:.2f}% / Parcial {TP1_SELL_PCT*100:.0f}% / Trail restante {TRAIL2_DIST_PCT*100:.2f}% / SL {SL_MODE}≤{SL_MAX_PCT*100:.2f}% o ATRx{ATR_MULT})")
    app.run(host=FLASK_HOST, port=FLASK_PORT)
