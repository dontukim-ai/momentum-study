"""Handwritten API-shaped fixture; not a captured or verified KIS response."""

from urllib.parse import parse_qs, urlparse

from trading_agent.contracts import KST, D


class FixtureExchange:
    """Offline server-shaped fixture; no KIS or exchange connectivity."""
    def __init__(self, now):
        self.now, self.cash, self.positions, self.rows = now, D(100000), {}, {}
        self.calls, self.drop_order_response, self.business_reject = [], False, False

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, headers, body))
        path = urlparse(url).path
        result = {"rt_cd": "0", "_response_headers": {"tr_cont": "D"}}
        if path.endswith("tokenP"):
            return {"access_token": "fixture-token", "expires_in": 86400}
        if path.endswith("inquire-balance"):
            return {**result, "output1": [{"pdno": s, "hldg_qty": str(q), "ord_psbl_qty": str(q)} for s, q in self.positions.items() if q],
                    "output2": [{"prvs_rcdl_excc_amt": str(self.cash), "dnca_tot_amt": "0"}]}
        if path.endswith("inquire-daily-ccld"):
            day = parse_qs(urlparse(url).query)["INQR_STRT_DT"][0]
            return {**result, "output1": [r.copy() for r in self.rows.values() if r["ord_dt"] == day], "output2": {}}
        if path.endswith("inquire-psbl-order"):
            return {**result, "output": {"ord_psbl_cash": str(self.cash), "nrcvb_buy_qty": "1000"}}
        if path.endswith("inquire-price"):
            return {**result, "output": {"stck_prpr": "1000"}}
        if path.endswith("order-cash"):
            if self.business_reject:
                return {"rt_cd": "1"}
            odno = str(len(self.rows) + 1).zfill(8)
            self.rows[odno] = {"ord_dt": self.now.astimezone(KST).strftime("%Y%m%d"), "ord_gno_brno": "12345", "odno": odno,
                "pdno": body["PDNO"], "sll_buy_dvsn_cd": "02" if headers["tr_id"] == "VTTC0012U" else "01",
                "ord_qty": body["ORD_QTY"], "ord_unpr": body["ORD_UNPR"], "tot_ccld_qty": "0",
                "rmn_qty": body["ORD_QTY"], "cncl_cfrm_qty": "0", "rjct_qty": "0", "tot_ccld_amt": "0", "tot_fee": "0"}
            if self.drop_order_response:
                raise TimeoutError("fixture accepted then lost response")
            return {**result, "output": {"KRX_FWDG_ORD_ORGNO": "12345", "ODNO": odno}}
        if path.endswith("order-rvsecncl"):
            return {**result, "output": {"ODNO": "cancel-request-1"}}
        raise AssertionError("Unexpected operation " + path)

    def fill(self, odno, quantity, notional, fee=D(0)):
        row = self.rows[odno]
        row["tot_ccld_qty"] = str(int(row["tot_ccld_qty"]) + quantity)
        row["rmn_qty"] = str(int(row["rmn_qty"]) - quantity)
        row["tot_ccld_amt"] = str(D(row["tot_ccld_amt"]) + notional)
        if "tot_fee" in row:
            row["tot_fee"] = str(D(row["tot_fee"]) + fee)
        sign = 1 if row["sll_buy_dvsn_cd"] == "02" else -1
        self.positions[row["pdno"]] = self.positions.get(row["pdno"], 0) + sign * quantity
        self.cash -= sign * notional + fee
