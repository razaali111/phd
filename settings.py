"""
Phoenix Gateway price/stock spider (local runner).

A Scrapy port of the scrape-phoenix-gateway edge function's sweep — for running
from your OWN (residential) connection, because Phoenix's gateway denies
cloud/datacenter-origin requests (the same block that stops the edge function).

Phoenix is a GraphQL API (gateway.phoenixgateway.co.uk/graphql), not HTML:
  1. POST login mutation                     → identity JWT cookie (Scrapy keeps it)
  2. per search term: getAllProductsBySearchTerm(queryString, id)  → names+PIPs, NO prices
  3. per unique PIP:   getProductByPipId(pipId, deliveryPointId)   → real price + stock
  → yields one PhoenixPrice per PIP (→ live_prices_cache, supplier 'phoenix').

`id` / `deliveryPointId` = the branch's Phoenix account number (e.g. 21916),
set as PHOENIX_DELIVERY_POINT_ID. Terms default to a 3-letter prefix sweep of
the catalogue (override with -a terms_file=terms.txt or a shorter -a max_terms).

Run:
  scrapy crawl phoenix -a max_terms=10
"""
import json
import re
from datetime import datetime, timedelta, timezone

import scrapy

from phoenix.items import PhoenixPrice

GRAPHQL_URL = "https://gateway.phoenixgateway.co.uk/graphql"
PORTAL_ORIGIN = "https://phoenixgateway.co.uk"

GQL_HEADERS = {
    "Accept": "*/*",
    "Content-Type": "application/json",
    "Content-Language": "en-GB",
    "Origin": PORTAL_ORIGIN,
    "Referer": f"{PORTAL_ORIGIN}/",
    "viper-currency": "GBP",
    "viper-website": "phoenixgateway_default",
    "viper-website-key": "phoenixgateway",
}

LOGIN_MUTATION = (
    "mutation login($userName: String!, $password: String!) {"
    " login(username: $userName, password: $password) { firstName userName id __typename } }"
)
SEARCH_QUERY = (
    "query getAllProductsBySearchTerm($queryString: String!, $id: Int!) {"
    " getAllProductsBySearchTerm(queryString: $queryString, id: $id) { outOf productsMap {"
    " productName EAN PIP phoenixInternalCode vpid productPrice netPrice standardPrice"
    " retailPrice reimbursementPrice manufacturer stockStatus singleStockQuantity"
    " singleProductStatus singleBackOrderFlag onPromotion promotionPrice vatRate } } }"
)
PRODUCT_BY_PIP_QUERY = (
    "query getProductByPipId($pipId: Int!, $deliveryPointId: Int!) {"
    " getProductByPipId(pipId: $pipId, deliveryPointId: $deliveryPointId) {"
    " productName EAN PIP phoenixInternalCode vpid productPrice netPrice standardPrice"
    " retailPrice reimbursementPrice manufacturer stockStatus singleStockQuantity"
    " singleProductStatus singleBackOrderFlag onPromotion promotionPrice vatRate } }"
)

# 3-letter prefix sweep of the catalogue (ported from the edge function).
DEFAULT_SWEEP_TERMS = [
    "aba","aca","ace","aci","acl","ada","ade","adr","ale","alf","all","alm","alo","alp","alt","alu","ama","ami","aml","amo","amp","ana","api","apo","ari","asp","ate","ato","atr","aza","azi",
    "bac","bal","bec","ben","bet","bez","bic","bim","bis","bor","bro","bud","bum","bup","bus",
    "cab","cal","can","cap","car","cef","cel","cet","chl","cil","cim","cin","cip","cit","cla","cli","clo","coa","coc","cod","col","cot","cya","cyc","cyp",
    "dab","dal","dap","dar","des","dex","dia","dic","dig","dih","dil","dip","dis","doc","dom","don","dos","dox","dul","dut",
    "edo","emp","ena","eno","ent","epl","ery","esc","eso","est","eta","eth","eto","exe","eze",
    "fel","fen","fer","fex","fin","fle","flu","fol","for","fos","fur","fus",
    "gab","gal","gen","gli","glu","gly","gos","gra","gri",
    "hal","hep","hyd","hyo",
    "iba","ibu","ilo","imi","ind","inf","ins","ipr","irb","iso","isp","itr","iva",
    "ket",
    "lab","lac","lam","lan","lat","lef","ler","let","lev","lid","lin","lio","lir","lis","lit","lof","lop","lor","los","lym",
    "mac","meb","med","mef","meg","mel","mem","mer","mes","met","mia","mic","mid","mir","moc","mod","mon","mor","mox","myc",
    "nab","nad","naf","nap","nar","neb","nef","nic","nif","nit","niz","nor","nys",
    "ola","olm","ome","ond","orl","ose","oxa","oxc","oxy",
    "pan","par","pen","per","phe","pho","pim","pio","pir","piz","pra","pre","pro","pyr",
    "que","qui",
    "rab","ral","ram","ran","ras","reb","rep","ril","ris","riv","riz","rop","ros","rot",
    "sal","sax","sel","ser","sil","sim","sit","sod","sol","sot","spi","suc","sul","sum",
    "tac","tad","tam","tel","tem","ter","the","thi","tia","tim","tin","tio","tiz","tol","top","tor","tra","tri",
    "uli","ure","urs",
    "val","van","var","ven","ver","vil","vin","vit",
    "war","xip","zaf","zal","zid","zol","zon","zop","zuc",
]

FORM_RE = re.compile(
    r"\b(tablets?|capsules?|caps?|sachets?|ampoules?|vials?|cream|ointment|gel|lotion|"
    r"solution|suspension|syrup|drops?|inhaler|spray|patches?|suppositor(?:y|ies)|"
    r"pessar(?:y|ies)|injection|powder|liquid)\b", re.I)
STRENGTH_RE = re.compile(r"\b(\d+(?:\.\d+)?\s?(?:mg|mcg|g|ml|%|iu|units?))\b", re.I)
PACK_RE = re.compile(r"\b(\d+)\s*(?:x\s*\d+\s*(?:ml|g|mg)?)?\s*$", re.I)


def digits(s):
    return re.sub(r"\D", "", s or "")


def to_pence(v):
    if v is None or v == "":
        return None
    try:
        n = float(re.sub(r"[£,\s]", "", str(v)))
    except ValueError:
        return None
    return round(n * 100) if n > 0 else None


def map_stock(p):
    txt = str(p.get("stockStatus") or p.get("singleProductStatus") or "").lower()
    if re.search(r"in.?stock|available|^a$|green", txt): return "in_stock"
    if re.search(r"out.?of.?stock|unavailable|^n$|red", txt): return "out_of_stock"
    if re.search(r"limited|low|amber|back.?order", txt): return "limited"
    if p.get("singleBackOrderFlag") is True: return "limited"
    try:
        qty = float(p.get("singleStockQuantity"))
        return "in_stock" if qty > 0 else "out_of_stock"
    except (TypeError, ValueError):
        return "unknown"


class PhoenixSpider(scrapy.Spider):
    name = "phoenix"

    def __init__(self, terms_file=None, max_terms=None, *a, **k):
        super().__init__(*a, **k)
        self.terms_file = terms_file
        self.max_terms = int(max_terms) if max_terms else None
        self.seen = set()               # dedupe PIPs across terms
        self.dpid = None

    def _cfg(self, key):
        import os
        return self.settings.get(key) or os.getenv(key, "")

    def _terms(self):
        terms = list(DEFAULT_SWEEP_TERMS)
        if self.terms_file:
            try:
                with open(self.terms_file, encoding="utf-8") as fh:
                    fileterms = [ln.strip() for ln in fh if ln.strip()]
                if fileterms:
                    terms = fileterms
            except FileNotFoundError:
                self.logger.warning("terms file %s not found — using the default sweep list", self.terms_file)
        return terms[: self.max_terms] if self.max_terms else terms

    def _gql(self, op, query, variables, callback, cb_kwargs=None):
        return scrapy.Request(
            GRAPHQL_URL, method="POST",
            body=json.dumps({"operationName": op, "variables": variables, "query": query}),
            headers=GQL_HEADERS, callback=callback, cb_kwargs=cb_kwargs or {}, dont_filter=True,
        )

    # 1. login
    def start_requests(self):
        user, pw = self._cfg("PHOENIX_USER"), self._cfg("PHOENIX_PASS")
        self.dpid = int(digits(str(self._cfg("PHOENIX_DELIVERY_POINT_ID"))) or 0)
        if not user or not pw:
            self.logger.error("Set PHOENIX_USER and PHOENIX_PASS."); return
        if not self.dpid:
            self.logger.error("Set PHOENIX_DELIVERY_POINT_ID (the branch's Phoenix account, e.g. 21916)."); return
        yield self._gql("login", LOGIN_MUTATION, {"userName": user, "password": pw}, self.after_login)

    def after_login(self, response):
        try:
            data = json.loads(response.text)
        except json.JSONDecodeError:
            self.logger.error("login: non-JSON response"); return
        if data.get("errors"):
            self.logger.error("login error: %s", data["errors"][0].get("message")); return
        if not (data.get("data") or {}).get("login", {}).get("id"):
            self.logger.error("login rejected — check PHOENIX_USER / PHOENIX_PASS."); return
        # The identity JWT cookie is now in Scrapy's jar; every GraphQL POST carries it.
        self.logger.info("Logged in. deliveryPointId=%s. Sweeping…", self.dpid)
        for term in self._terms():
            yield self._gql("getAllProductsBySearchTerm", SEARCH_QUERY,
                            {"queryString": term, "id": self.dpid}, self.parse_search)

    # 2. search term → per-PIP hydration
    def parse_search(self, response):
        try:
            data = json.loads(response.text)
        except json.JSONDecodeError:
            return
        products = ((data.get("data") or {}).get("getAllProductsBySearchTerm") or {}).get("productsMap") or []
        for p in products:
            pip = digits(str(p.get("PIP") or ""))
            if not pip or pip in self.seen:
                continue
            self.seen.add(pip)
            yield self._gql("getProductByPipId", PRODUCT_BY_PIP_QUERY,
                            {"pipId": int(pip), "deliveryPointId": self.dpid},
                            self.parse_product, cb_kwargs={"base": p})

    # 3. hydrated product → item
    def parse_product(self, response, base):
        try:
            data = json.loads(response.text)
        except json.JSONDecodeError:
            data = {}
        hp = (data.get("data") or {}).get("getProductByPipId") or {}
        item = self._to_item({**base, **hp})
        if item:
            yield item

    def _to_item(self, p):
        pip = digits(str(p.get("PIP") or ""))
        if not pip:
            return None
        net = to_pence(p.get("netPrice")); std = to_pence(p.get("standardPrice"))
        price = net if net is not None else (to_pence(p.get("productPrice")) or std)
        if price is None:
            return None                 # only cache priced rows (matches the live path)
        ean = digits(str(p.get("EAN") or ""))
        gtin = ean if 8 <= len(ean) <= 14 else None
        desc = (str(p.get("productName")).strip() or None) if p.get("productName") else None
        strength = form = pack = None
        if desc:
            m = STRENGTH_RE.search(desc); strength = m.group(1) if m else None
            m = FORM_RE.search(desc); form = m.group(1) if m else None
            m = PACK_RE.search(desc); pack = m.group(1) if m else None
        try:
            qty = int(float(p.get("singleStockQuantity")))
        except (TypeError, ValueError):
            qty = None
        vat_raw = p.get("vatRate")
        try:
            vat = float(re.sub(r"[%\s]", "", str(vat_raw))) if vat_raw not in (None, "") else None
        except ValueError:
            vat = None
        now = datetime.now(timezone.utc)
        ttl = int(self.settings.get("PHOENIX_TTL_MINUTES", 1440))
        branch_id = self._cfg("PHOENIX_BRANCH_ID") or None

        item = PhoenixPrice()
        item["pip_code"] = pip.zfill(7)
        item["supplier_name"] = "phoenix"
        item["price_pence"] = price
        item["stock_status"] = map_stock(p)
        item["supplier_code"] = str(p["phoenixInternalCode"]) if p.get("phoenixInternalCode") is not None else None
        item["barcode"] = gtin
        item["gtin"] = gtin
        item["stock_quantity"] = qty
        item["fetched_at"] = now.isoformat()
        item["expires_at"] = (now + timedelta(minutes=ttl)).isoformat()
        item["branch_id"] = branch_id
        item["net_price_pence"] = net
        item["standard_price_pence"] = std
        item["retail_price_pence"] = to_pence(p.get("retailPrice"))
        item["reimbursement_price_pence"] = to_pence(p.get("reimbursementPrice"))
        item["manufacturer"] = (str(p.get("manufacturer")).strip() or None) if p.get("manufacturer") else None
        item["vat_rate"] = vat
        item["on_promotion"] = p.get("onPromotion") if isinstance(p.get("onPromotion"), bool) else None
        item["promotion_price_pence"] = to_pence(p.get("promotionPrice"))
        item["description"] = desc
        item["pack_size"] = pack
        item["form"] = form
        item["strength"] = strength
        item["vmp_id"] = str(p["vpid"]).strip() if p.get("vpid") not in (None, "") else None
        return item
