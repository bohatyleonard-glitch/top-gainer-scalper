import time
import math
import logging
import ccxt
import pandas as pd
import numpy as np

# --- 1. NASTAVENIE LOGOVANIA DO SÚBORU A TERMINÁLU ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler("bot_execution.log"),
        logging.StreamHandler()
    ]
)

# --- 2. KONFIGURÁCIA OBCHODNÝCH PARAMETROV ---
API_KEY = "VAS_API_KLUC"
API_SECRET = "VAS_API_SECRET"

# Ak používate Apex Pro (alebo Bybit), nastavte zodpovedajúce rozhranie cez CCXT
EXCHANGE_ID = 'bybit'  # Pre Apex / Bybit futures
SYMBOL = 'SUI/USDC:USDC'  # Páry: SUI/USDC:USDC alebo SOL/USDC:USDC
TIMEFRAME = '15m'

LEVERAGE = 1           # Nastavenie páky (1x = spotový profil bez rizika likvidácie)
TRADE_CAPITAL_USDC = 500  # Suma v USDC vyčlenená na 1 obchod (alebo % z vášho 7000 USDC účtu)

# Parametre stratégie z vášho Pine Scriptu
FAST_LEN = 5
SLOW_LEN = 20
SIGNAL_LEN = 9
EMA_PRICE_LEN = 9

# Pravidlá pre výstup (50/50 split & Break-Even)
TARGET_FIRST_TP_PCT = 0.0065  # +0.65% na odpredaj prvej polovice (50%)
USE_BREAKEVEN = True
TRAIL_STOP_PCT = 0.015       # 1.5% Trailing Stop vzdialenosť z indikátora
USE_HA_EXIT = True           # Ukončiť pri otočení Heikin-Ashi sviečky do červenej


class AllGreenBot:
    def __init__(self, symbol, timeframe):
        self.symbol = symbol
        self.timeframe = timeframe
        self.exchange = self._init_exchange()
        
        # Stavové premenné obchodu
        self.in_position = False
        self.entry_price = 0.0
        self.total_amount = 0.0
        self.remaining_amount = 0.0
        self.half_sold = False
        self.highest_price_since_buy = 0.0

    def _init_exchange(self):
        """Inicializuje pripojenie k burze cez CCXT"""
        try:
            exchange_class = getattr(ccxt, EXCHANGE_ID)
            exchange = exchange_class({
                'apiKey': API_KEY,
                'secret': API_SECRET,
                'enableRateLimit': True,
                'options': {'defaultType': 'future'}
            })
            exchange.load_markets()
            
            # Nastavenie páky
            try:
                exchange.set_leverage(LEVERAGE, self.symbol)
            except Exception as e:
                logging.warning(f"Nastavenie páky zlyhalo (možno už je nastavená): {e}")

            logging.info(f"Pripojenie k burze {EXCHANGE_ID} bolo úspešné.")
            return exchange
        except Exception as e:
            logging.critical(f"Kritická chyba pri inicializácii burzy: {e}")
            raise e

    def fetch_data(self, limit=100):
        """Stiahne 15M sviečky z burzy"""
        ohlcv = self.exchange.fetch_ohlcv(self.symbol, self.timeframe, limit=limit)
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        return df

    def calculate_indicators(self, df):
        """Prepočíta Heikin-Ashi, Objemový Oscilátor a EMA9 presne ako v Pine Scripte"""
        # 1. Výpočet Heikin-Ashi sviečok
        ha_close = (df['open'] + df['high'] + df['low'] + df['close']) / 4.0
        
        ha_open = np.zeros(len(df))
        ha_open[0] = (df['open'].iloc[0] + df['close'].iloc[0]) / 2.0
        for i in range(1, len(df)):
            ha_open[i] = (ha_open[i-1] + ha_close.iloc[i-1]) / 2.0
            
        ha_high = df[['high', 'open', 'close']].max(axis=1)
        ha_low = df[['low', 'open', 'close']].min(axis=1)

        df['ha_open'] = ha_open
        df['ha_close'] = ha_close

        # 2. Smer objemu na základe HA sviečok
        volume_direction = np.where(df['ha_close'] > df['ha_open'], df['volume'], -df['volume'])

        # 3. EMA a Oscilátor
        fast_ma = pd.Series(volume_direction).ewm(span=FAST_LEN, adjust=False).mean()
        slow_ma = pd.Series(volume_direction).ewm(span=SLOW_LEN, adjust=False).mean()
        oscillator = fast_ma - slow_ma
        signal_line = oscillator.ewm(span=SIGNAL_LEN, adjust=False).mean()

        df['oscillator'] = oscillator
        df['signal_line'] = signal_line
        df['ema9'] = df['close'].ewm(span=EMA_PRICE_LEN, adjust=False).mean()

        return df

    def check_all_green_signal(self, df):
        """Vyhodnotí podmienku All Green (posledná uzavretá sviečka)"""
        last = df.iloc[-2]     # Posledná plne uzavretá sviečka
        prev = df.iloc[-3]     # Predchádzajúca sviečka

        cond_volume_bullish = (last['oscillator'] > 0) and (last['oscillator'] > last['signal_line'])
        cond_volume_crossover = (prev['oscillator'] <= prev['signal_line']) and (last['oscillator'] > last['signal_line']) and (last['oscillator'] > 0)
        
        ema_crossover = (prev['close'] <= prev['ema9']) and (last['close'] > last['ema9'])

        all_green = cond_volume_bullish and (cond_volume_crossover or ema_crossover)
        return all_green

    def format_amount(self, amount):
        """Zaokrúhli množstvo tokenu podľa pravidiel trhu (Precision/LotSize)"""
        market = self.exchange.market(self.symbol)
        precision = market['precision']['amount']
        if precision is not None:
            return round(amount, int(precision))
        return amount

    def open_buy_position(self, current_price):
        """Vypočíta veľkosť a odošle MARKET BUY objednávku na burzu"""
        raw_amount = (TRADE_CAPITAL_USDC * LEVERAGE) / current_price
        amount = self.format_amount(raw_amount)

        logging.info(f"[NÁKUP] Odoielam MARKET BUY pre {self.symbol} | Objem: {amount} za cenu ~{current_price} USDC")
        
        try:
            # Reálna exekúcia na burze
            order = self.exchange.create_market_buy_order(self.symbol, amount)
            
            self.in_position = True
            self.entry_price = current_price  # V produkcii: float(order['price'])
            self.total_amount = amount
            self.remaining_amount = amount
            self.half_sold = False
            self.highest_price_since_buy = current_price
            
            logging.info(f"✅ POZÍCIA OTVORENÁ. Vstupná cena: {self.entry_price} USDC | Celkový objem: {self.total_amount}")
        except Exception as e:
            logging.error(f"Chyba pri otváraní nákupnej pozície: {e}")

    def sell_partial_position(self, current_price, pct=0.5):
        """Odpredá zadané % pozície (napr. 50% pri +0.65% zisku)"""
        raw_sell_amount = self.total_amount * pct
        sell_amount = self.format_amount(raw_sell_amount)

        logging.info(f"[ČIASTOČNÝ PREDAJ] Odpredávam 50% pozície ({sell_amount} {self.symbol}) pri zisku +{TARGET_FIRST_TP_PCT*100}%")

        try:
            order = self.exchange.create_market_sell_order(self.symbol, sell_amount)
            self.remaining_amount -= sell_amount
            self.half_sold = True
            
            logging.info(f"🎯 50% ODPREDANÉ SO ZISKOM! Zostáva v trhu: {self.remaining_amount} {self.symbol}")
            logging.info(f"🔒 BREAK-EVEN AKTÍVNY: Stop-Loss posunutý na vstupnú cenu ({self.entry_price} USDC).")
        except Exception as e:
            logging.error(f"Chyba pri čiastočnom odpredaji: {e}")

    def close_full_position(self, current_price, reason="EXIT"):
        """Uzatvorí zvyšok pozície"""
        amount_to_sell = self.format_amount(self.remaining_amount)
        logging.info(f"[{reason}] Uzatváram zvyšok pozície ({amount_to_sell} {self.symbol}) za cenu {current_price}")

        try:
            order = self.exchange.create_market_sell_order(self.symbol, amount_to_sell)
            
            self.in_position = False
            self.entry_price = 0.0
            self.total_amount = 0.0
            self.remaining_amount = 0.0
            self.half_sold = False
            
            logging.info(f"🏁 OBCHOD UKONČENÝ | Dôvod: {reason}\n" + "-"*60)
        except Exception as e:
            logging.error(f"Chyba pri uzatváraní pozície: {e}")

    def manage_position(self, df, current_price):
        """Manažment otvorenej pozície (50/50 split, Break-Even, Trailing, HA exit)"""
        if current_price > self.highest_price_since_buy:
            self.highest_price_since_buy = current_price

        profit_pct = (current_price - self.entry_price) / self.entry_price
        logging.info(f"Trh: {current_price} USDC | Zisk pozície: {profit_pct*100:.2f}%")

        # 1. KROK: Odpredaj 50% pri dosiahnutí +0.65%
        if not self.half_sold and profit_pct >= TARGET_FIRST_TP_PCT:
            self.sell_partial_position(current_price, pct=0.5)

        # 2. KROK: Ak je odpredaná prvá polovica, riadime zvyšok pozície
        if self.half_sold:
            # Ochrana Break-Even (ak cena klesne späť na vstup)
            if USE_BREAKEVEN and current_price <= self.entry_price:
                self.close_full_position(current_price, reason="BREAK-EVEN STOP")
                return

            # Exit pri otočení Heikin-Ashi sviečky do červenej
            last_ha_close = df['ha_close'].iloc[-1]
            last_ha_open = df['ha_open'].iloc[-1]
            if USE_HA_EXIT and (last_ha_close < last_ha_open):
                self.close_full_position(current_price, reason="HEIKIN-ASHI ČERVENÁ SVIEČKA")
                return

    def run(self):
        """Hlavná slučka bota"""
        logging.info(f"=== SPÚŠŤAM ALL GREEN BOTA PRE {self.symbol} ON {self.timeframe} ===")

        while True:
            try:
                # Sťahovanie dát a výpočet indikátorov
                df = self.fetch_data(limit=100)
                df = self.calculate_indicators(df)
                
                ticker = self.exchange.fetch_ticker(self.symbol)
                current_price = ticker['last']

                # Ak nie sme v obchode, testujeme podmienku na VSTUP
                if not self.in_position:
                    is_all_green = self.check_all_green_signal(df)
                    if is_all_green:
                        logging.info("🚀 DETEKOVANÝ ALL GREEN SIGNÁL!")
                        self.open_buy_position(current_price)
                    else:
                        logging.info(f"Čakám na signál... Aktuálna cena {self.symbol}: {current_price} USDC")
                
                # Ak sme v obchode, riadime pozíciu
                else:
                    self.manage_position(df, current_price)

                # Čakanie pred ďalšou kontrolou (napr. každých 30 sekúnd)
                time.sleep(30)

            except Exception as e:
                logging.error(f"Neočakávaná chyba v hlavnej slučke: {e}")
                time.sleep(10)


# --- 3. SPUSTENIE BOTA ---
if __name__ == "__main__":
    bot = AllGreenBot(symbol=SYMBOL, timeframe=TIMEFRAME)
    bot.run()
