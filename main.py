import json
import os
import time
import threading
import sys
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_DOWN

from flask import Flask, request, jsonify
from binance.client import Client
from binance.exceptions import BinanceAPIException

# =================== Metadatos ===================
BOT_VERSION = "V8.1.0"

# =================== Config ===================
PAIR = "BTCUSDC"
JSON_FILE = os.getenv("STATE_FILE", "estado_compra.json")  # ej. /data/estado_compra.json con Volume

# Loop
POLL_SECS = float(os.getenv("POLL_SECS", "5"))
_timeout_env = os.getenv("TIMEOUT_HORAS", "").strip()
TIMEOUT_HORAS = float(_timeout_env) if _timeout_env else None

# Timeframe & cooldown
TF_INTERVAL = os.getenv("TF_INTERVAL", "1h")   # 1m,5m,15m,30m,1h,2h,4h
COOLDOWN_BARS = int(os.getenv("COOLDOWN_BARS", "2"))

# ATR
ATR_PERIOD = int(os.getenv("ATR_PERIOD", "14"))
ATR_MULT = float(os.getenv("ATR_MULT", "1.0"))
# SL mode: 'cap' => máximo 1.5% (por defecto), 'floor' => mínimo 1.5%
SL_MODE = os.getenv("SL_MODE", "cap")

# Targets solicitados
TP_PCT = float(os.getenv("TP_PCT", "0.02"))        # +2% TP fijo (cierra 100%)
SL_MAX_PCT = float(os.getenv("SL_MAX_PCT", "0.015"))  # 1.5% (cap o floor según SL_MODE)
TRAIL_ON_PCT = float(os.getenv("TRAIL_ON_PCT", "0.019"))   # +1.9% activación de trailing
TRAIL_DIST_PCT = float(os.getenv("TRAIL_DIST_PCT", "0.01")) # 1% de retroceso

# LOT_SIZE (fallbacks; se sobreescriben con los reales al arrancar)
STEP_SIZE = Decimal("0.000001")
MIN_QTY = Decimal("0")
DECIMAL_PLACES = 6
_LOT_SIZE_CACHE = None

# Compras
QUOTE_BUFFER = float(os.getenv("QUOTE_BUFFER", "0.002"))  # 0.2% para evitar insufficient balance

# --- Fees / ventas seguras ---
BNB_MIN = float(os.getenv("BNB_MIN", "0.01"))  # reserva mínima de BNB para comisiones
SELL_SAFETY_PCT = Decimal(os.getenv("SELL_SAFETY_PCT", "0.002"))  # 0.2% si NO hay BNB

# --- Auto-attach mínimos para evitar polvo que bloquee recompras ---
ATTACH_MIN_MULT = Decimal(os.getenv("ATTACH_MIN_MULT", "1.2"))  # ≥ 1.2 * minQty
ATTACH_MIN_USD  = Decimal(os.getenv("ATTACH_MIN_USD",  "5"))    # valor mínimo en USDC

# Token opcional para /unlock
UNLOCK_TOKEN = os.getenv("UNLOCK_TOKEN", "")

# =================== App & Client ===================
app = Flask(__name__)
client = Client(
    api_key=os.getenv("BINANCE_API_KEY"),
    api_secret=os.getenv("BINANCE_API_SECRET")
)

def log(msg): print(msg, flush=True)

STATE_LOCK = threading.Lock()
_en_venta = threading.Lock()  # debounce ventas
_last_watch_log = 0.0
_last_attach_log = 0.0

# =================== Helpers: timeframe ===================
def _tf_to_binance(tf: str) -> str:
    m = {"1m":"1m","5m":"5m","15m":"15m","30m":"30m","1h":"1h","2h":"2h","4h":"4h"}
    return m.get(tf, "1h")

def _tf_seconds(tf: str) -> int:
    m = {"1m":60,"5m":300,"15m":900,"30m":1800,"1h":3600,"2h":7200,"4h":14400}
    return m.get(tf, 3600)

# =================== Helpers: LOT_SIZE ===================
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
            _LOT_SIZE_CACHE = {"stepSize": STEP_SIZE, "minQty": MIN_QTY, "decimals": DECIMAL_PLACES}
            return _LOT_SIZE_CACHE
    except Exception as e:
        log(f"⚠️ No se pudo cargar LOT_SIZE para {PAIR}: {e}")
    _LOT_SIZE_CACHE = {"stepSize": STEP_SIZE, "minQty": MIN_QTY, "decimals": DECIMAL_PLACES}
    return _LOT_SIZE_CACHE

# =================== Utils: estado & cantidades ===================
def cargar_estado():
    with STATE_LOCK:
        try:
            with open(JSON_FILE, "r") as f:
                data = json.load(f)
        except:
            data = {"operacion_abierta": False, "buy_lock": False}
        if "buy_lock" not in data:
            data["buy_lock"] = False
        return data

def guardar_estado(data, origin=None):
    if "buy_lock" not in data:
        data["buy_lock"] = False
    os.makedirs(os.path.dirname(JSON_FILE) or ".", exist_ok=True)
    with STATE_LOCK:
        with open(JSON_FILE, "w") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    if origin:
        log(f"💾 Estado guardado ({origin}).")

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
    if qty is None: return Decimal("0")
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
    return float(client.get_symbol_ticker(symbol=PAIR)["price"])

def get_free(asset):
    b = client.get_asset_balance(asset=asset)
    return float(b["free"]) if b else 0.0

def now_iso(): return datetime.now().isoformat()

def lock_off():
    guardar_estado({"operacion_abierta": False, "buy_lock": False}, origin="lock_off")

def reconciliar_estado():
    st = cargar_estado()
    if not st.get("operacion_abierta") and st.get("buy_lock"):
        log("ℹ️ No hay operación pero buy_lock activo -> liberando.")
        lock_off(); return
    if not st.get("operacion_abierta"): return
    qty_wallet = get_free("BTC")
    qty_rest = float(st.get("qty_restante", 0.0))
    if qty_wallet <= 0 and qty_rest > 0:
        log("ℹ️ No hay BTC pero estado marcaba operación -> cierro estado."); lock_off()
    elif qty_rest > 0 and abs(qty_wallet - qty_rest) / max(qty_rest, 1e-8) > 0.02:
        log(f"ℹ️ Ajusto qty_restante {qty_rest:.8f} -> {qty_wallet:.8f}")
        st["qty_restante"] = qty_wallet; guardar_estado(st, origin="reconciliar_estado")

# =================== Datos: ATR ===================
def _fetch_klines(limit: int = 100):
    interval = _tf_to_binance(TF_INTERVAL)
    return client.get_klines(symbol=PAIR, interval=interval, limit=limit)

def _calc_atr(period: int = ATR_PERIOD):
    kl = _fetch_klines(limit=max(period + 2, 20))
    if not kl or len(kl) < period + 1:
        px = obtener_precio_actual()
        return 0.01 * px  # fallback conservador
    highs = [float(k[2]) for k in kl]
    lows = [float(k[3]) for k in kl]
    closes = [float(k[4]) for k in kl]
    trs = []
    for i in range(1, len(kl)):
        hi, lo, prev_close = highs[i], lows[i], closes[i-1]
        tr = max(hi - lo, abs(hi - prev_close), abs(lo - prev_close))
        trs.append(tr)
    if len(trs) < period:
        period = len(trs)
    atr = sum(trs[-period:]) / period
    return atr

# =================== Trading core ===================
def _calcular_niveles(entry):
    atr = _calc_atr(ATR_PERIOD) * ATR_MULT
    sl_atr_price = entry - atr
    sl_cap_price = entry * (1 - SL_MAX_PCT)

    if SL_MODE == "floor":
        # nunca más cerca que 1.5% (mínimo 1.5%)
        sl_price = min(sl_atr_price, sl_cap_price)
    else:
        # por defecto: no más lejos que 1.5% (cap 1.5%)
        sl_price = max(sl_atr_price, sl_cap_price)

    tp_price = entry * (1 + TP_PCT)
    trail_on_price = entry * (1 + TRAIL_ON_PCT)

    atr_pct = atr / entry
    log(f"SL calc -> ATR={atr:.2f} ({atr_pct:.2%}), cap1.5%={sl_cap_price:.2f}, SL final={sl_price:.2f}")

    return {"atr_abs": atr, "sl_price": sl_price, "tp_price": tp_price, "trail_on_price": trail_on_price}

def comprar_100(uid=None):
    st = cargar_estado()
    cd_until = st.get("cooldown_until")
    if cd_until and datetime.now() < datetime.fromisoformat(cd_until):
        log(f"⏳ BUY ignorado por cooldown hasta {cd_until}."); return
    if st.get("buy_lock") or st.get("operacion_abierta"):
        log("🔒 BUY ignorado: operación aún no cerrada."); return
    if uid and st.get("last_uid") == uid:
        log("ℹ️ Alerta duplicada ignorada por uid."); return

    cargar_filtros_lot_size()
    if normalizar_cantidad(get_free("BTC")) >= MIN_QTY:
        auto_attach_from_wallet()
        return

    usdc = get_free("USDC")
    if usdc <= 5:
        log(f"⚠️ USDC insuficiente ({usdc})."); return

    usdc_to_spend = usdc * (1.0 - QUOTE_BUFFER)
    orden = client.order_market_buy(
        symbol=PAIR,
        quoteOrderQty=round(usdc_to_spend, 2),
        newClientOrderId=f"MINADOR_BUY_{int(time.time())}"
    )

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

    lv = _calcular_niveles(avg_px)

    st = {
        "operacion_abierta": True,
        "buy_lock": True,
        "last_uid": uid,
        "hora_compra": now_iso(),
        "precio_compra": avg_px,
        "qty_total": qty_exec_float,
        "qty_restante": qty_exec_float,
        "sl_price": lv["sl_price"],
        "tp_price": lv["tp_price"],
        "trail_on_price": lv["trail_on_price"],
        "trail_active": False,
        "trail_peak": None,
        "cooldown_until": None
    }
    guardar_estado(st, origin="comprar_100")
    log(f"✅ COMPRA: {qty_exec_float:.8f} BTC @ {avg_px:.2f} | TP {lv['tp_price']:.2f} | SL {lv['sl_price']:.2f} | TrailON {lv['trail_on_price']:.2f}")

# ---- helpers de venta ----
def qty_vendible_segura(deseada: Decimal | None = None) -> Decimal:
    """
    Máximo que puedo vender ahora mismo:
    - Usa BTC libre real.
    - Si no hay BNB suficiente, deja % de colchón para fee en BTC.
    - Deja siempre 1 step de seguridad.
    - Ajusta a step/minQty.
    - Fuerza que el leftover quede < MIN_QTY (para no bloquear recompras).
    """
    free = Decimal(str(get_free("BTC")))
    if free <= 0:
        return Decimal("0")

    free_bnb = Decimal(str(get_free("BNB")))
    cushion = STEP_SIZE if free_bnb >= Decimal(str(BNB_MIN)) else max((free * SELL_SAFETY_PCT), STEP_SIZE)

    target = free - cushion
    if deseada is not None:
        target = min(target, Decimal(str(deseada)))

    q = normalizar_cantidad(target)
    if q <= 0:
        return Decimal("0")

    leftover = free - q
    if leftover >= MIN_QTY:
        q2 = free - (MIN_QTY - STEP_SIZE)  # deja ~minQty - step
        if deseada is not None:
            q2 = min(q2, Decimal(str(deseada)))
        q = normalizar_cantidad(q2)

    return q if q >= MIN_QTY else Decimal("0")

def vender_qty(qty):
    """
    Market sell robusto:
    - Ajusta a free BTC real + colchón (fees/redondeos).
    - Si no hay BNB, deja % de seguridad en BTC.
    - Reintenta ante:
        * -2010 insufficient balance -> recalcula vendible y reduce 0.1% + 1 step.
        * -1013 LOT_SIZE -> recarga filtros y re-normaliza.
    """
    q = qty_vendible_segura(Decimal(str(qty)))
    if q <= 0:
        return None

    def _try(q_):
        return client.order_market_sell(
            symbol=PAIR,
            quantity=formatear_cantidad(q_),
            newClientOrderId=f"MINADOR_SELL_{int(time.time())}"
        )

    intentos = 0
    while intentos < 3:
        intentos += 1
        try:
            return _try(q)
        except BinanceAPIException as e:
            msg = str(e)
            if e.code == -2010 and "insufficient" in msg.lower():
                nuevo = qty_vendible_segura()
                if nuevo <= 0 or nuevo >= q:
                    nuevo = normalizar_cantidad(q * Decimal("0.999") - STEP_SIZE)
                if nuevo >= MIN_QTY:
                    log(f"↻ Reintento (-2010) con qty={formatear_cantidad(nuevo)}")
                    q = nuevo
                    continue
                log("⚠️ -2010 y resto < minQty. Nada que vender.")
                return None
            if e.code == -1013 and "LOT_SIZE" in msg:
                cargar_filtros_lot_size(force=True)
                nuevo = qty_vendible_segura(q)
                if nuevo >= MIN_QTY:
                    log(f"↻ Reintento (-1013 LOT_SIZE) con qty={formatear_cantidad(nuevo)}")
                    q = nuevo
                    continue
                log("⚠️ -1013 y qty < minQty. Nada que vender.")
                return None
            log(f"❌ BinanceAPIException en vender_qty: {e}")
            raise
    log("⚠️ Venta abortada tras 3 reintentos.")
    return None

def _aplicar_cooldown(st):
    secs = _tf_seconds(TF_INTERVAL) * COOLDOWN_BARS
    until = datetime.now() + timedelta(seconds=secs)
    st["cooldown_until"] = until.isoformat()
    guardar_estado(st, origin="cooldown_set")
    log(f"⏳ Cooldown activado {COOLDOWN_BARS} velas ({TF_INTERVAL}) hasta {st['cooldown_until']}.")

def _cerrar_todo(st, motivo="Exit"):
    if not _en_venta.acquire(blocking=False):
        log("⏳ Venta ya en curso. Ignoro señal duplicada.")
        return
    try:
        qty_estado = Decimal(str(st.get("qty_restante", 0.0)))
        qty_wallet = Decimal(str(get_free("BTC")))
        qty_target = max(qty_estado, qty_wallet)

        qty_sell = qty_vendible_segura(qty_target)
        if qty_sell <= 0:
            log(f"ℹ️ Nada vendible ({motivo}). Posible dust < minQty. Libero lock + cooldown.")
            lock_off()
            st2 = cargar_estado(); _aplicar_cooldown(st2)
            return

        orden = vender_qty(qty_sell)
        desbloquear = True
        if orden:
            executed_qty = normalizar_cantidad(orden.get("executedQty", 0.0))
            st["qty_restante"] = max(0.0, float(st.get("qty_restante", 0.0)) - float(executed_qty))
            guardar_estado(st, origin=f"close_{motivo}")
            if st["qty_restante"] > 0:
                desbloquear = False
                log(f"⚠️ Venta parcial ({motivo}): quedan {formatear_cantidad(st['qty_restante'])} BTC")
            else:
                log(f"✅ CIERRE TOTAL ({motivo}): {formatear_cantidad(executed_qty)} BTC")
        else:
            log(f"⚠️ Venta fallida ({motivo}).")
        if not desbloquear and float(st.get("qty_restante", 0.0)) < float(MIN_QTY):
            log("ℹ️ Resto < minQty (dust). Libero lock."); desbloquear = True
        if desbloquear:
            lock_off()
            st2 = cargar_estado(); _aplicar_cooldown(st2)
    finally:
        _en_venta.release()

# =================== Auto-attach (blindado) ===================
def auto_attach_from_wallet():
    global _last_attach_log
    cargar_filtros_lot_size()
    qty_wallet_raw = Decimal(str(get_free("BTC")))
    qty_wallet = normalizar_cantidad(qty_wallet_raw)

    attach_min_qty = max(MIN_QTY * ATTACH_MIN_MULT, STEP_SIZE * 3)
    px = Decimal(str(obtener_precio_actual()))
    usd_val = qty_wallet * px

    if qty_wallet < attach_min_qty or usd_val < ATTACH_MIN_USD:
        # throttle de log: 60s
        if time.time() - _last_attach_log > 60:
            log(
                f"🔎 Auto-attach: nada que adjuntar (wallet={qty_wallet_raw:.8f} < minQty {attach_min_qty} o ${ATTACH_MIN_USD})"
            )
            _last_attach_log = time.time()
        return

    st = cargar_estado()
    if st.get("operacion_abierta"): return
    try:
        trades = client.get_my_trades(symbol=PAIR, limit=50)
        total_qty = 0.0; spent = 0.0
        for tr in reversed(trades):
            if tr.get("isBuyer"):
                q = float(tr["qty"]); p = float(tr["price"])
                total_qty += q; spent += q * p
                if total_qty >= float(qty_wallet) * 0.98:
                    break
        entry = spent / total_qty if total_qty > 0 else float(px)
        lv = _calcular_niveles(entry)
        st.update({
            "operacion_abierta": True, "buy_lock": True, "last_uid": None,
            "hora_compra": now_iso(), "precio_compra": entry,
            "qty_total": float(qty_wallet), "qty_restante": float(qty_wallet),
            "sl_price": lv["sl_price"], "tp_price": lv["tp_price"],
            "trail_on_price": lv["trail_on_price"], "trail_active": False, "trail_peak": None,
            "cooldown_until": None
        })
        guardar_estado(st, origin="auto_attach")
        log(f"🔗 Auto-attach: {formatear_cantidad(qty_wallet)} BTC | entry≈{entry:.2f} | TP {lv['tp_price']:.2f} | SL {lv['sl_price']:.2f}")
    except Exception as e:
        log(f"⚠️ Auto-attach falló: {e}")

# =================== Monitor ===================
def control_venta():
    global _last_watch_log
    log("🚀 Iniciando control (TP2%, SL ATR≤1.5% [cap/floor], Trailing 1.9%/1%, Cooldown 2 velas)…")
    reconciliar_estado()
    while True:
        try:
            st = cargar_estado()

            # auto-attach si hay BTC pero estado vacío y NO en cooldown
            cd_until = st.get("cooldown_until")
            if not st.get("operacion_abierta"):
                if not cd_until or datetime.now() >= datetime.fromisoformat(cd_until):
                    auto_attach_from_wallet()

            st = cargar_estado()
            if st.get("operacion_abierta"):
                px = obtener_precio_actual()
                entry = float(st["precio_compra"])
                tp = float(st["tp_price"])
                sl = float(st["sl_price"])
                trail_on = float(st["trail_on_price"])

                now = time.time()
                if now - _last_watch_log > 30:
                    log(f"👀 WATCH | px={px:.2f} entry={entry:.2f} TP@{tp:.2f} SL@{sl:.2f} TrailON@{trail_on:.2f} trail_active={st.get('trail_active')} peak={st.get('trail_peak')}")
                    _last_watch_log = now

                if TIMEOUT_HORAS is not None:
                    t0 = datetime.fromisoformat(st["hora_compra"])
                    if datetime.now() - t0 > timedelta(hours=TIMEOUT_HORAS):
                        log("⏰ Timeout forzado")
                        _cerrar_todo(st, "Timeout")
                        time.sleep(POLL_SECS); continue

                # 1) Stop Loss
                if px <= sl:
                    log("🛑 SL por ATR")
                    _cerrar_todo(st, "StopLoss"); time.sleep(POLL_SECS); continue

                # 2) Take Profit +2%
                if px >= tp:
                    log("🎯 TP +2% alcanzado")
                    _cerrar_todo(st, "TakeProfit"); time.sleep(POLL_SECS); continue

                # 3) Trailing
                if not st.get("trail_active"):
                    if px >= trail_on:
                        st["trail_active"] = True
                        st["trail_peak"] = px
                        guardar_estado(st, origin="trail_on")
                        log(f"🔓 Trailing ON @ {px:.2f}")
                else:
                    if px > float(st["trail_peak"] or entry):
                        st["trail_peak"] = px
                        guardar_estado(st, origin="trail_peak")
                    stop_trail = float(st["trail_peak"]) * (1 - TRAIL_DIST_PCT)
                    if px <= stop_trail:
                        log("⛓️ TrailingStop ejecutado")
                        _cerrar_todo(st, "TrailingStop"); time.sleep(POLL_SECS); continue

            time.sleep(POLL_SECS)
        except Exception as e:
            log(f"❌ Error en control_venta: {e}")
            time.sleep(POLL_SECS)

# =================== API ===================
@app.route("/", methods=["GET"])
def root(): return "ok", 200

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(silent=True) or {}
    if data.get("action") == "buy":
        st = cargar_estado()
        cd_until = st.get("cooldown_until")
        if cd_until and datetime.now() < datetime.fromisoformat(cd_until):
            log(f"⏳ BUY ignorado por cooldown hasta {cd_until}.")
            return jsonify({"status": "cooldown_active", "until": cd_until})
        if st.get("buy_lock") or st.get("operacion_abierta"):
            log("🔒 BUY ignorado: operación aún no cerrada.")
            return jsonify({"status": "ignored_locked"})
        cargar_filtros_lot_size()
        if normalizar_cantidad(get_free("BTC")) >= MIN_QTY:
            auto_attach_from_wallet()
            return jsonify({"status": "attached_existing_position"})
        uid = str(data.get("uid")) if data.get("uid") else None
        comprar_100(uid)
    return jsonify({"status": "ok"})

@app.route("/status", methods=["GET"])
def status():
    st = cargar_estado()
    price = None
    try: price = obtener_precio_actual()
    except Exception: pass
    return jsonify({
        "alive": True, "version": BOT_VERSION, "pair": PAIR, "price": price, "state": st,
        "lot_size": {"stepSize": str(STEP_SIZE), "minQty": str(MIN_QTY), "decimals": DECIMAL_PLACES},
        "config": {
            "TF_INTERVAL": TF_INTERVAL, "COOLDOWN_BARS": COOLDOWN_BARS,
            "ATR_PERIOD": ATR_PERIOD, "ATR_MULT": ATR_MULT, "SL_MODE": SL_MODE,
            "TP_PCT": TP_PCT, "SL_MAX_PCT": SL_MAX_PCT,
            "TRAIL_ON_PCT": TRAIL_ON_PCT, "TRAIL_DIST_PCT": TRAIL_DIST_PCT,
            "POLL_SECS": POLL_SECS, "TIMEOUT_HORAS": TIMEOUT_HORAS
        }
    })

@app.route("/health", methods=["GET"])
def health(): return "ok", 200

@app.route("/unlock", methods=["POST"])
def unlock():
    token = request.args.get("token", "")
    if not UNLOCK_TOKEN or token != UNLOCK_TOKEN:
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    lock_off()
    return jsonify({"ok": True, "msg": "lock off"})

# =================== Boot ===================
if __name__ == "__main__":
    try:
        log(f"🟢 Bot {BOT_VERSION} (TP2% / SL ATR[{SL_MODE}]≤1.5% / Trail 1.9%–1% / Cooldown 2 velas) iniciando…")
        log(f"ENV PORT={os.environ.get('PORT')}  PAIR={PAIR}  STATE_FILE={JSON_FILE}  TF={TF_INTERVAL}  POLL={POLL_SECS}s")
        # 1) LOT_SIZE ANTES del hilo
        try:
            cfg = cargar_filtros_lot_size(force=True)
            log(f"ℹ️ LOT_SIZE: step={cfg['stepSize']}, minQty={cfg['minQty']}, decimals={cfg['decimals']}")
        except Exception as e:
            log(f"⚠️ No se pudo cargar LOT_SIZE en arranque: {e}")
        # 2) Hilo de control
        hilo = threading.Thread(target=control_venta, daemon=True)
        hilo.start()
        # 3) Flask
        port = int(os.environ.get("PORT", 8080))
        app.run(host="0.0.0.0", port=port, debug=False)
    except Exception as e:
        log(f"❌ Error fatal en arranque: {e}")
        import traceback; traceback.print_exc()
        sys.exit(1)
