"""Rebuild levered returns and the 80/20 JV waterfall from the annual cash flows.

Run from the repo root:  python analysis/model.py
Writes data/annual_cash_flows.csv, data/partnership_waterfall.csv,
data/checks.csv and data/metrics.json.

Conventions
- Annual periods, cash at year end; the 2030 sale closes in year 5.
- Levered cash flow = NOI - debt service - structural reserve
  (this reproduces the cash-on-cash column in annual_summary.csv).
- Waterfall (as in assumptions.csv): 8% pref on contributed capital, paid
  80/20 from current-year cash; any shortfall accrues (simple, not compounded).
  Cash left after the year's pref is split 50/50. Sale proceeds pay accrued
  pref, then return capital 80/20, then split 50/50.
"""
import csv, json, os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
D = lambda f: os.path.join(ROOT, "data", f)


def irr(cfs, lo=-0.99, hi=10.0):
    npv = lambda r: sum(c / (1 + r) ** t for t, c in enumerate(cfs))
    for _ in range(300):
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if npv(mid) > 0 else (lo, mid)
    return mid


def main():
    m = json.load(open(D("metrics.json")))
    years = [2026, 2027, 2028, 2029, 2030]
    # NOI: 2028 and 2029 at full precision from metrics.json; other years from annual_summary.csv
    summ = {int(r["year"]): r for r in csv.DictReader(open(D("annual_summary.csv")))}
    noi = {y: float(summ[y]["noi_m"]) * 1e6 for y in years}
    noi[2028], noi[2029] = m["noi_2028"], m["noi_2029"]

    value, loan, equity = m["property_value_npv"], m["loan"], m["equity"]
    ds = m["monthly_ds"] * 12
    reserve = {y: 36250 * 1.03 ** i for i, y in enumerate(years)}
    sale, balloon = m["reversion"], m["balloon"]
    net_sale = sale - balloon

    rows, lev, unlev = [], [-equity], [-value]
    for y in years:
        cf = noi[y] - ds - reserve[y]
        sale_cf = net_sale if y == 2030 else 0.0
        lev.append(cf + sale_cf)
        unlev.append(noi[y] - reserve[y] + (sale if y == 2030 else 0.0))
        rows.append(dict(year=y, noi=round(noi[y], 2), debt_service=round(ds, 2),
                         reserve=round(reserve[y], 2), levered_cash_flow=round(cf, 2),
                         net_sale_proceeds=round(sale_cf, 2), equity_cash_flow=round(cf + sale_cf, 2),
                         dscr=round(noi[y] / ds, 2), cash_on_cash_pct=round(cf / equity * 100, 1)))

    # ---- JV waterfall ----
    share = {"Money Partner": 0.8, "Developer": 0.2}
    cap = {p: equity * s for p, s in share.items()}
    pref_due = {p: cap[p] * 0.08 for p in share}
    accrued = {p: 0.0 for p in share}
    dist = {p: [-cap[p]] for p in share}
    wf_rows = []
    for r in rows:
        y, cash = r["year"], r["levered_cash_flow"]
        paid = {p: 0.0 for p in share}
        # 1) current pref plus any accrued shortfall, pro rata 80/20
        owed = {p: pref_due[p] + accrued[p] for p in share}
        tot_owed = sum(owed.values())
        pay = min(cash, tot_owed)
        for p in share:
            paid[p] += pay * owed[p] / tot_owed
            accrued[p] = owed[p] - pay * owed[p] / tot_owed
        cash -= pay
        pref_paid = pay
        # 2) sale: return of capital 80/20
        roc = 0.0
        if y == 2030:
            cash += r["net_sale_proceeds"]
            # accrued pref first (already counted above for operating cash)
            tot_acc = sum(accrued.values())
            pay2 = min(cash, tot_acc)
            for p in share:
                if tot_acc:
                    paid[p] += pay2 * accrued[p] / tot_acc
                    accrued[p] -= pay2 * accrued[p] / tot_acc
            cash -= pay2; pref_paid += pay2
            roc = min(cash, equity)
            for p in share:
                paid[p] += roc * share[p]
            cash -= roc
        # 3) residual 50/50
        for p in share:
            paid[p] += cash * 0.5
        for p in share:
            dist[p].append(paid[p])
        wf_rows.append(dict(year=y, cash_available=round(r["equity_cash_flow"], 2), pref_paid=round(pref_paid, 2),
                            return_of_capital=round(roc, 2), split_50_50=round(cash, 2),
                            to_money_partner=round(paid["Money Partner"], 2), to_developer=round(paid["Developer"], 2),
                            unpaid_pref_carried=round(sum(accrued.values()), 2)))

    lev_irr, unlev_irr = irr(lev), irr(unlev)
    out = {}
    partners = []
    for p in share:
        tot = sum(dist[p][1:])
        out[p] = dict(irr=irr(dist[p]), mult=tot / cap[p], total=tot)
        partners.append(dict(partner=p, equity_share=share[p], capital=round(cap[p], 2),
                             annual_pref_8pct=round(pref_due[p], 2), total_distributed=round(tot, 2),
                             profit=round(tot - cap[p], 2), irr=round(out[p]["irr"], 4),
                             equity_multiple=round(out[p]["mult"], 4)))

    tot_dist = sum(o["total"] for o in out.values())
    tot_equity_cf = sum(lev[1:])
    gap_year = max(abs(w["to_money_partner"] + w["to_developer"] - w["cash_available"]) for w in wf_rows)
    gap_coc = max(abs(r["cash_on_cash_pct"] - float(summ[r["year"]]["cash_on_cash_pct"])) for r in rows)
    # (check, left label, left, right label, right, tolerance)
    checks = [
        ("Sources = uses", "Loan + equity", loan + equity, "Purchase price (NPV value)", value, 1.0),
        ("Loan = 70% LTV", "Loan", loan, "0.70 x value", 0.70 * value, 1.0),
        ("Distributions = distributable cash", "LP + GP distributions", tot_dist,
         "Sum of equity cash flows 2026-2030", tot_equity_cf, 1.0),
        ("Distributions = cash, every year", "Largest yearly gap ($)", gap_year, "Target", 0.0, 1.0),
        ("Net sale", "Sale price - balloon", net_sale, "Equity proceeds at sale", lev[-1] - rows[-1]["levered_cash_flow"], 1.0),
        ("Cash-on-cash ties to annual_summary.csv", "Largest gap (pts)", gap_coc, "Target", 0.0, 0.1),
        ("No unpaid pref after sale", "Accrued pref ($)", sum(accrued.values()), "Target", 0.0, 1.0),
    ]
    with open(D("checks.csv"), "w", newline="") as f:
        w = csv.writer(f); w.writerow(["check", "left_label", "left", "right_label", "right", "difference", "pass"])
        for name, ll, l, rl, r, tol in checks:
            w.writerow([name, ll, round(l, 2), rl, round(r, 2), round(l - r, 2), "PASS" if abs(l - r) <= tol else "FAIL"])

    def dump(path, data):
        with open(D(path), "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(data[0])); w.writeheader(); w.writerows(data)
    dump("annual_cash_flows.csv", rows)
    dump("partnership_waterfall.csv", partners)
    dump("waterfall_by_year.csv", wf_rows)

    m.update(levered_irr=round(lev_irr, 4), unlevered_irr=round(unlev_irr, 4),
             levered_equity_multiple=round(tot_equity_cf / equity, 4), total_equity_distributions=round(tot_equity_cf, 2),
             net_sale_proceeds=round(net_sale, 2),
             lp_irr=round(out["Money Partner"]["irr"], 4), gp_irr=round(out["Developer"]["irr"], 4),
             lp_equity_multiple=round(out["Money Partner"]["mult"], 4), gp_equity_multiple=round(out["Developer"]["mult"], 4),
             lp_total_distributed=round(out["Money Partner"]["total"], 2), gp_total_distributed=round(out["Developer"]["total"], 2),
             timing="annual periods, year-end cash flows")
    json.dump(m, open(D("metrics.json"), "w"), indent=2)
    print(json.dumps({k: m[k] for k in ["levered_irr", "unlevered_irr", "levered_equity_multiple", "total_equity_distributions",
                                        "lp_irr", "gp_irr", "lp_equity_multiple", "gp_equity_multiple",
                                        "lp_total_distributed", "gp_total_distributed"]}, indent=1))
    for row in csv.reader(open(D("checks.csv"))): print(row)
    for wr in wf_rows: print(wr)


if __name__ == "__main__":
    main()
