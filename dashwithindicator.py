import os
import time
import json
import hmac
import hashlib
import requests
from requests.adapters import HTTPAdapter
from requests.packages.urllib3.util.retry import Retry
from http.server import BaseHTTPRequestHandler, HTTPServer
import threading

# Configuration (Securely fetch API credentials from environment variables)
API_ID = os.environ.get("API_ID")  # Fetch API_ID from environment variables
API_SECRET = os.environ.get("API_SECRET")  # Fetch API_SECRET from environment variables

BASE_URL = "https://payeer.com/api/trade/"
PAIR = "DASH_EUR"  # Updated to DASH_EUR
TRAILING_STOP_PERCENTAGE = 2  # Trailing stop percentage (e.g., 2%)
MAX_RETRIES = 5  # Maximum retries for API calls
RETRY_BACKOFF_FACTOR = 2  # Exponential backoff factor
HEALTH_CHECK_PORT = 8000  # Port for health checks
MIN_INVESTMENT = 0.4  # Minimum investment in EUR
BUY_AMOUNT = 0.4  # Default buy amount (minimum investment)

# Active orders tracking
active_orders = []  # List to track active buy orders

# Helper Functions
def generate_signature(method, req_body):
    """Generate HMAC-SHA256 signature."""
    req_body_str = json.dumps(req_body)
    message = method + req_body_str
    return hmac.new(API_SECRET.encode(), message.encode(), hashlib.sha256).hexdigest()

def make_request(method, endpoint, data=None):
    """Make a POST request to the Payeer API with retry logic."""
    url = BASE_URL + endpoint
    ts = int(time.time() * 1000)
    req_body = {"ts": ts}
    if data:
        req_body.update(data)
    headers = {
        "Content-Type": "application/json",
        "API-ID": API_ID,
        "API-SIGN": generate_signature(endpoint, req_body),
    }
    # Configure retry strategy
    retry_strategy = Retry(
        total=MAX_RETRIES,
        backoff_factor=RETRY_BACKOFF_FACTOR,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["POST"],
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session = requests.Session()
    session.mount("https://", adapter)
    try:
        response = session.post(url, headers=headers, data=json.dumps(req_body))
        response.raise_for_status()
        result = response.json()
        if result.get("success"):
            return result
        else:
            print(f"Error: {result}")
            return None
    except requests.exceptions.RequestException as e:
        print(f"Request failed: {e}")
        return None

def get_balance():
    """Fetch account balance."""
    response = make_request("POST", "account")
    if response:
        return response.get("balances", {})
    return {}

def place_order(pair, action, amount, price=None, order_type="limit"):
    """Place a new order."""
    data = {
        "pair": pair,
        "type": order_type,
        "action": action,
        "amount": str(amount),
    }
    if price:
        data["price"] = str(price)
    response = make_request("POST", "order_create", data)
    if response:
        return response.get("order_id")
    return None

def get_order_status(order_id):
    """Get the status of an order."""
    data = {"order_id": order_id}
    response = make_request("POST", "order_status", data)
    if response:
        return response.get("order", {})
    return {}

def cancel_order(order_id):
    """Cancel an order."""
    data = {"order_id": order_id}
    response = make_request("POST", "order_cancel", data)
    if response and response.get("success"):
        print(f"Order {order_id} canceled successfully.")
    else:
        print(f"Failed to cancel order {order_id}.")

def get_ticker(pair):
    """Get ticker information for a pair."""
    data = {"pair": pair}
    response = make_request("POST", "ticker", data)
    if response:
        return response.get("pairs", {}).get(pair, {})
    return {}

def get_pair_limits(pair):
    """Fetch minimum amount and value for a specific pair."""
    response = make_request("POST", "info", {"pair": pair})
    if response and response.get("success"):
        pair_info = response["pairs"][pair]
        return {
            "min_amount": float(pair_info["min_amount"]),
            "min_value": float(pair_info["min_value"]),
        }
    return None

# Health Check Server
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        """Respond to health check requests."""
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK")

def start_health_check_server(port):
    """Start a lightweight HTTP server for health checks."""
    server_address = ("", port)
    httpd = HTTPServer(server_address, HealthCheckHandler)
    print(f"Health check server started on port {port}")
    httpd.serve_forever()

# Main Bot Logic
def trading_bot():
    global BUY_AMOUNT  # Declare BUY_AMOUNT as global to modify it
    try:
        while True:  # Outer loop to ensure continuous operation
            print("Fetching balance...")
            balance = get_balance()
            print(f"Balance: {balance}")

            # Fetch ticker data
            ticker = get_ticker(PAIR)
            last_price = float(ticker.get("last", 0))
            print(f"Last price for {PAIR}: {last_price}")

            # Fetch pair limits
            pair_limits = get_pair_limits(PAIR)
            if not pair_limits:
                print(f"Failed to fetch limits for {PAIR}. Retrying in 60 seconds...")
                time.sleep(60)
                continue

            min_amount = pair_limits["min_amount"]
            min_value = pair_limits["min_value"]

            # Adjust BUY_AMOUNT to meet both min_amount and min_value
            BUY_AMOUNT = max(min_amount, MIN_INVESTMENT / last_price)
            print(f"Adjusted BUY_AMOUNT to {BUY_AMOUNT} to meet minimum requirements.")

            # Verify available balance for the buy order
            quote_currency = PAIR.split("_")[1]  # Extract quote currency (e.g., EUR)
            available_balance = float(balance.get(quote_currency, {}).get("available", 0))
            required_balance = BUY_AMOUNT * last_price

            # Wait until sufficient balance is available
            while available_balance < required_balance:
                print(
                    f"Insufficient balance in {quote_currency}. Available: {available_balance}, Required: {required_balance}"
                )
                print("Waiting for sufficient balance...")
                time.sleep(60)  # Wait for 1 minute before checking again
                balance = get_balance()
                available_balance = float(balance.get(quote_currency, {}).get("available", 0))

            # Place a single buy order
            print(f"Placing buy order at {last_price}...")
            buy_order_id = place_order(PAIR, "buy", BUY_AMOUNT, last_price)
            if buy_order_id:
                print(f"Buy order placed successfully. Order ID: {buy_order_id}")
                active_orders.append({
                    "order_id": buy_order_id,
                    "buy_price": last_price,
                    "amount": BUY_AMOUNT,
                    "trailing_stop": None,
                    "highest_price": last_price
                })
            else:
                print(f"Failed to place buy order at {last_price}.")
                continue

            # Monitor all active orders
            while active_orders:
                for order in active_orders[:]:  # Iterate over a copy of the list
                    order_id = order["order_id"]
                    buy_price = order["buy_price"]
                    trailing_stop = order["trailing_stop"]
                    highest_price = order["highest_price"]

                    # Check the status of the buy order
                    buy_order = get_order_status(order_id)
                    if buy_order.get("status") == "success":
                        print(f"Buy order {order_id} filled. Monitoring trailing stop...")

                        # Get the current market price
                        ticker = get_ticker(PAIR)
                        current_price = float(ticker.get("last", 0))
                        print(f"Current price: {current_price}")

                        # Update the trailing stop
                        if current_price > highest_price:
                            highest_price = current_price
                            trailing_stop = highest_price * (1 - TRAILING_STOP_PERCENTAGE / 100)
                            order["highest_price"] = highest_price
                            order["trailing_stop"] = trailing_stop
                            print(f"Updated trailing stop for order {order_id} to: {trailing_stop}")

                        # Check if the price has dropped below the trailing stop
                        if trailing_stop and current_price <= trailing_stop:
                            print(f"Trailing stop triggered for order {order_id}. Selling at {current_price}...")
                            sell_order_id = place_order(PAIR, "sell", order["amount"], current_price)
                            if sell_order_id:
                                print(f"Sell order placed successfully. Order ID: {sell_order_id}")
                            else:
                                print("Failed to place sell order.")
                            active_orders.remove(order)  # Remove the order from active tracking
                            break  # Exit the loop to place a new buy order

                    elif buy_order.get("status") == "canceled":
                        print(f"Buy order {order_id} was canceled.")
                        active_orders.remove(order)  # Remove the canceled order
                        break  # Exit the loop to place a new buy order

                time.sleep(10)  # Poll every 10 seconds
    except Exception as e:
        print(f"An error occurred: {e}")
        time.sleep(10)  # Wait before retrying

if __name__ == "__main__":
    # Start health check server in a separate thread
    health_check_thread = threading.Thread(target=start_health_check_server, args=(HEALTH_CHECK_PORT,))
    health_check_thread.daemon = True
    health_check_thread.start()

    # Run the trading bot
    trading_bot()
