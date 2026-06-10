import csv
import io
import xml.etree.ElementTree as ET
import requests
import time
import os
import random
from flask import Flask, render_template, request, redirect, Response, abort

# disable warnings until you install a certificate
from requests.packages.urllib3.exceptions import InsecureRequestWarning
requests.packages.urllib3.disable_warnings(InsecureRequestWarning)

BASE_API_URL = "https://localhost:5055/v1/api"
FLEX_WEB_SERVICE_URL = "https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService"
ACCOUNT_ID = os.environ['IBKR_ACCOUNT_ID']
FLEX_TOKEN = os.environ.get("IBKR_FLEX_TOKEN", "")
FLEX_QUERY_ID = os.environ.get("IBKR_FLEX_QUERY_ID", "")

os.environ['PYTHONHTTPSVERIFY'] = '0'

app = Flask(__name__)

FLEX_RETRY_ERROR_CODES = {
    "1001", "1004", "1005", "1006", "1007", "1008", "1009",
    "1019", "1021",
}


def flex_configured():
    return bool(FLEX_TOKEN and FLEX_QUERY_ID)


def _flex_xml_text(root, tag):
    element = root.find(tag)
    return element.text if element is not None else ""


def fetch_flex_activity_statement(token, query_id, max_attempts=12, poll_interval=5):
    headers = {"User-Agent": "ibkr-web-api-demo/1.0"}
    send_response = requests.get(
        f"{FLEX_WEB_SERVICE_URL}/SendRequest",
        params={"t": token, "q": query_id, "v": 3},
        headers=headers,
        timeout=60,
    )
    send_response.raise_for_status()

    send_root = ET.fromstring(send_response.text)
    if _flex_xml_text(send_root, "Status") != "Success":
        error_code = _flex_xml_text(send_root, "ErrorCode")
        error_message = _flex_xml_text(send_root, "ErrorMessage")
        raise RuntimeError(
            f"Flex SendRequest failed ({error_code}): {error_message or 'Unknown error'}"
        )

    reference_code = _flex_xml_text(send_root, "ReferenceCode")
    if not reference_code:
        raise RuntimeError("Flex SendRequest did not return a reference code.")

    for attempt in range(max_attempts):
        if attempt:
            time.sleep(poll_interval)

        statement_response = requests.get(
            f"{FLEX_WEB_SERVICE_URL}/GetStatement",
            params={"t": token, "q": reference_code, "v": 3},
            headers=headers,
            timeout=120,
        )
        statement_response.raise_for_status()
        content = statement_response.content

        try:
            statement_root = ET.fromstring(content)
        except ET.ParseError:
            return content

        error_code = _flex_xml_text(statement_root, "ErrorCode")
        if error_code:
            if error_code in FLEX_RETRY_ERROR_CODES and attempt < max_attempts - 1:
                continue
            error_message = _flex_xml_text(statement_root, "ErrorMessage")
            raise RuntimeError(
                f"Flex GetStatement failed ({error_code}): {error_message or 'Unknown error'}"
            )

        return content

    raise RuntimeError("Flex statement is still generating. Try again in a minute.")


def flex_download_filename(content):
    stripped = content.lstrip()
    if stripped.startswith(b"<?xml") or stripped.startswith(b"<"):
        return "activity_statement.xml", "application/xml"
    return "activity_statement.csv", "text/csv"

@app.template_filter('ctime')
def timectime(s):
    return time.ctime(s/1000)


@app.route("/")
def dashboard():
    try:
        r = requests.get(f"{BASE_API_URL}/portfolio/accounts", verify=False)
        accounts = r.json()
    except Exception as e:
        return 'Make sure you authenticate first then visit this page. <a href="https://localhost:5055">Log in</a>'

    account = accounts[0]

    account_id = accounts[0]["id"]
    r = requests.get(f"{BASE_API_URL}/portfolio/{account_id}/summary", verify=False)
    summary = r.json()
    
    return render_template("dashboard.html", account=account, summary=summary)


@app.route("/lookup")
def lookup():
    symbol = request.args.get('symbol', None)
    stocks = []

    if symbol is not None:
        r = requests.get(f"{BASE_API_URL}/iserver/secdef/search?symbol={symbol}&name=true", verify=False)

        response = r.json()
        stocks = response

    return render_template("lookup.html", stocks=stocks)


@app.route("/contract/<contract_id>/<period>")
def contract(contract_id, period='5d', bar='1d'):
    data = {
        "conids": [
            contract_id
        ]
    }
    
    r = requests.post(f"{BASE_API_URL}/trsrv/secdef", data=data, verify=False)
    contract = r.json()['secdef'][0]

    r = requests.get(f"{BASE_API_URL}/iserver/marketdata/history?conid={contract_id}&period={period}&bar={bar}", verify=False)
    price_history = r.json()

    return render_template("contract.html", price_history=price_history, contract=contract)


@app.route("/orders")
def orders():
    r = requests.get(f"{BASE_API_URL}/iserver/account/orders", verify=False)
    orders = r.json()["orders"]
    
    # place order code
    return render_template("orders.html", orders=orders)


@app.route("/order", methods=['POST'])
def place_order():
    print("== placing order ==")

    data = {
        "orders": [
            {
                "conid": int(request.form.get('contract_id')),
                "orderType": "LMT",
                "price": float(request.form.get('price')),
                "quantity": int(request.form.get('quantity')),
                "side": request.form.get('side'),
                "tif": "GTC"
            }
        ]
    }

    r = requests.post(f"{BASE_API_URL}/iserver/account/{ACCOUNT_ID}/orders", json=data, verify=False)

    return redirect("/orders")

@app.route("/orders/<order_id>/cancel")
def cancel_order(order_id):
    cancel_url = f"{BASE_API_URL}/iserver/account/{ACCOUNT_ID}/order/{order_id}" 
    r = requests.delete(cancel_url, verify=False)

    return r.json()


@app.route("/portfolio")
def portfolio():
    r = requests.get(f"{BASE_API_URL}/portfolio/{ACCOUNT_ID}/positions/0", verify=False)

    if r.content:
        positions = r.json()
    else:
        positions = []

    # return my positions, how much cash i have in this account
    return render_template(
        "portfolio.html",
        positions=positions,
        flex_configured=flex_configured(),
    )


@app.route("/portfolio/csv")
def portfolio_csv():
    r = requests.get(f"{BASE_API_URL}/portfolio/{ACCOUNT_ID}/positions/0", verify=False)

    if r.content:
        positions = r.json()
    else:
        positions = []

    output = io.StringIO()
    writer = csv.writer(output)

    headers = [
        "Contract ID", "Name", "Contract Description",
        "Quantity", "Average Cost", "Market Price", "Market Value",
        "Unrealized P/L"
    ]
    writer.writerow(headers)

    for item in positions:
        writer.writerow([
            item.get("conid", ""),
            item.get("name", ""),
            item.get("contractDesc", ""),
            item.get("position", ""),
            item.get("avgCost", ""),
            item.get("mktPrice", ""),
            item.get("mktValue", ""),
            item.get("unrealizedPnl", ""),
        ])

    output.seek(0)
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=portfolio.csv"},
    )


@app.route("/activity-statement/download")
def activity_statement_download():
    if not flex_configured():
        abort(
            400,
            "Set IBKR_FLEX_TOKEN and IBKR_FLEX_QUERY_ID in your environment. "
            "Create an Activity Flex Query in IBKR Client Portal under Reporting > Flex Queries.",
        )

    try:
        content = fetch_flex_activity_statement(FLEX_TOKEN, FLEX_QUERY_ID)
    except requests.RequestException as exc:
        abort(502, f"Could not reach IBKR Flex Web Service: {exc}")
    except RuntimeError as exc:
        abort(502, str(exc))

    filename, mimetype = flex_download_filename(content)
    return Response(
        content,
        mimetype=mimetype,
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@app.route("/watchlists")
def watchlists():
    r = requests.get(f"{BASE_API_URL}/iserver/watchlists", verify=False)

    watchlist_data = r.json()["data"]
    watchlists = []
    if "user_lists" in watchlist_data:
        watchlists = watchlist_data["user_lists"]
        
    return render_template("watchlists.html", watchlists=watchlists)


@app.route("/watchlists/<int:id>")
def watchlist_detail(id):
    r = requests.get(f"{BASE_API_URL}/iserver/watchlist?id={id}", verify=False)

    watchlist = r.json()

    return render_template("watchlist.html", watchlist=watchlist)


@app.route("/watchlists/<int:id>/delete")
def watchlist_delete(id):
    r = requests.delete(f"{BASE_API_URL}/iserver/watchlist?id={id}", verify=False)

    return redirect("/watchlists")

@app.route("/watchlists/create", methods=['POST'])
def create_watchlist():
    data = request.get_json()
    name = data['name']

    rows = []
    symbols = data['symbols'].split(",")
    for symbol in symbols:
        symbol = symbol.strip()
        if symbol:
            r = requests.get(f"{BASE_API_URL}/iserver/secdef/search?symbol={symbol}&name=true&secType=STK", verify=False)
            contract_id = r.json()[0]['conid']
            rows.append({"C": contract_id})

    data = {
        "id": int(time.time()),
        "name": name,
        "rows": rows
    }

    r = requests.post(f"{BASE_API_URL}/iserver/watchlist", json=data, verify=False)
    
    return redirect("/watchlists")

@app.route("/scanner")
def scanner():
    r = requests.get(f"{BASE_API_URL}/iserver/scanner/params", verify=False)
    params = r.json()

    scanner_map = {}
    filter_map = {}

    for item in params['instrument_list']:
        scanner_map[item['type']] = {
            "display_name": item['display_name'],
            "filters": item['filters'],
            "sorts": []
        }

    for item in params['filter_list']:
        filter_map[item['group']] = {
            "display_name": item['display_name'],
            "type": item['type'],
            "code": item['code']
        }

    for item in params['scan_type_list']:
        for instrument in item['instruments']:
            scanner_map[instrument]['sorts'].append({
                "name": item['display_name'],
                "code": item['code']
            })

    for item in params['location_tree']:
        scanner_map[item['type']]['locations'] = item['locations']


    submitted = request.args.get("submitted", "")
    selected_instrument = request.args.get("instrument", "")
    location = request.args.get("location", "")
    sort = request.args.get("sort", "")
    scan_results = []
    filter_code = request.args.get("filter", "")
    filter_value = request.args.get("filter_value", "")

    if submitted:
        data = {
            "instrument": selected_instrument,
            "location": location,
            "type": sort,
            "filter": [
                {
                    "code": filter_code,
                    "value": filter_value
                }
            ]
        }
            
        r = requests.post(f"{BASE_API_URL}/iserver/scanner/run", json=data, verify=False)
        scan_results = r.json()

    return render_template("scanner.html", params=params, scanner_map=scanner_map, filter_map=filter_map, scan_results=scan_results)
