import io
from datetime import date, datetime
import pandas as pd
import streamlit as st
from supabase import create_client, Client

# -----------------------------------------------------------------------------
# PAGE CONFIG & RESPONSIVE STYLING (FORCED HIGH-CONTRAST STATUS CARD)
# -----------------------------------------------------------------------------
st.set_page_config(
    page_title="PTSB Bills & DD Tracker",
    page_icon="💶",
    layout="wide",
    initial_sidebar_state="collapsed",
)

st.markdown(
    """
    <style>
    .block-container {
        padding-top: 2.8rem !important;
        padding-bottom: 3rem !important;
        max-width: 1280px;
    }
    /* Force high-contrast dark card style so it never washes out in light mode */
    .status-banner {
        padding: 1.25rem 1.5rem;
        border-radius: 16px;
        margin-bottom: 1rem;
        background: linear-gradient(135deg, #064e3b !important, #0f172a !important);
        border: 1px solid rgba(16, 185, 129, 0.5) !important;
        color: #f8fafc !important;
    }
    .status-banner * {
        color: #f8fafc !important;
    }
    .banner-ok {
        background: linear-gradient(135deg, #064e3b !important, #0f172a !important) !important;
        border-color: #10b981 !important;
    }
    .banner-warn {
        background: linear-gradient(135deg, #78350f !important, #0f172a !important) !important;
        border-color: #f59e0b !important;
    }
    .banner-danger {
        background: linear-gradient(135deg, #881337 !important, #0f172a !important) !important;
        border-color: #f43f5e !important;
    }
    .big-num {
        font-family: 'SF Mono', 'JetBrains Mono', monospace;
        font-size: 2.2rem;
        font-weight: 800;
        line-height: 1.15;
        color: #34d399 !important;
    }
    .sub-label {
        font-size: 0.8rem;
        text-transform: uppercase;
        letter-spacing: 0.06em;
        font-weight: 600;
        color: #a7f3d0 !important;
    }
    </style>
    """,
    unsafe_allow_html=True,
)


# -----------------------------------------------------------------------------
# SECURITY PIN / PASSWORD LOCK
# -----------------------------------------------------------------------------
def check_password() -> bool:
    expected_pw = st.secrets.get("APP_PASSWORD")
    if not expected_pw:
        return True

    if st.session_state.get("password_authenticated", False):
        return True

    st.markdown("### 🔒 PTSB Bills Tracker — Private Access")
    with st.form("login_form"):
        entered_pw = st.text_input("Enter PIN / Password", type="password")
        submitted = st.form_submit_button("Unlock", type="primary", use_container_width=True)
        if submitted:
            if entered_pw == str(expected_pw):
                st.session_state["password_authenticated"] = True
                st.rerun()
            else:
                st.error("❌ Incorrect PIN / Password")
    return False


if not check_password():
    st.stop()


# -----------------------------------------------------------------------------
# SUPABASE CONNECTION
# -----------------------------------------------------------------------------
@st.cache_resource
def get_supabase_client() -> Client:
    url = None
    key = None

    if "SUPABASE_URL" in st.secrets and "SUPABASE_KEY" in st.secrets:
        url = st.secrets["SUPABASE_URL"]
        key = st.secrets["SUPABASE_KEY"]
    elif "supabase" in st.secrets:
        url = st.secrets["supabase"].get("url") or st.secrets["supabase"].get("SUPABASE_URL")
        key = st.secrets["supabase"].get("key") or st.secrets["supabase"].get("SUPABASE_KEY")
    elif "connections" in st.secrets and "supabase" in st.secrets["connections"]:
        url = st.secrets["connections"]["supabase"].get("SUPABASE_URL") or st.secrets["connections"]["supabase"].get("url")
        key = st.secrets["connections"]["supabase"].get("SUPABASE_KEY") or st.secrets["connections"]["supabase"].get("key")

    if not url or not key:
        st.error("⚠️ Supabase credentials not found in `st.secrets`.")
        st.stop()

    return create_client(url, key)


supabase = get_supabase_client()


# -----------------------------------------------------------------------------
# HELPER FUNCTIONS
# -----------------------------------------------------------------------------
def cycle_sort_key(due_day: int) -> int:
    d = int(due_day)
    return (d - 22) if d >= 22 else (d + 10)


def ordinal_day(n: int) -> str:
    n = int(n)
    if 11 <= (n % 100) <= 13:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def fmt_eur(val: float, plus: bool = False) -> str:
    v = float(val or 0.0)
    abs_s = f"{abs(v):,.2f}"
    if v < -0.004:
        return f"-€{abs_s}"
    if v > 0.004 and plus:
        return f"+€{abs_s}"
    return f"€{abs_s}"


def effective_amount(row: dict) -> float:
    ov = row.get("override_amount")
    if ov is not None and pd.notna(ov):
        return float(ov)
    return float(row.get("default_amount") or 0.0)


# -----------------------------------------------------------------------------
# DATA ACCESS LAYER
# -----------------------------------------------------------------------------
def load_all_data():
    meta_res = supabase.table("ptsb_cycle_meta").select("*").eq("id", 1).execute()
    if not meta_res.data:
        default_meta = {
            "id": 1,
            "salary_month": "Sep",
            "payday_balance": 3578.46,
            "actual_balance": 591.00,
            "balanced_date": str(date.today()),
        }
        supabase.table("ptsb_cycle_meta").insert(default_meta).execute()
        meta = default_meta
    else:
        meta = meta_res.data[0]

    bills_res = supabase.table("ptsb_bills").select("*").execute()
    bills = bills_res.data or []

    adj_res = supabase.table("ptsb_adjustments").select("*").order("id", desc=True).execute()
    adjustments = adj_res.data or []

    return meta, bills, adjustments


meta, all_bills, adjustments = load_all_data()

regular_bills = [b for b in all_bills if b.get("category") == "regular"]
regular_bills.sort(
    key=lambda b: (
        cycle_sort_key(int(b.get("due_day") or 22)),
        int(b.get("sort_tiebreaker") or 999),
        int(b.get("id") or 0),
    )
)

adhoc_bills = [b for b in all_bills if b.get("category") == "adhoc"]
adhoc_bills.sort(key=lambda b: int(b.get("id") or 0))

# -----------------------------------------------------------------------------
# RECONCILIATION MATH
# -----------------------------------------------------------------------------
paid_regular_sum = sum(effective_amount(b) for b in regular_bills if b.get("is_paid"))
unpaid_regular_sum = sum(effective_amount(b) for b in regular_bills if not b.get("is_paid"))

active_adhoc_bills = [b for b in adhoc_bills if b.get("active_in_cycle")]
paid_adhoc_sum = sum(effective_amount(b) for b in active_adhoc_bills if b.get("is_paid"))
unpaid_adhoc_sum = sum(effective_amount(b) for b in active_adhoc_bills if not b.get("is_paid"))

bills_paid_total = round(paid_regular_sum + paid_adhoc_sum, 2)
bills_unpaid_total = round(unpaid_regular_sum + unpaid_adhoc_sum, 2)
total_cycle_commitment = round(bills_paid_total + bills_unpaid_total, 2)

adjustments_total = round(sum(float(a.get("amount") or 0.0) for a in adjustments), 2)

payday_balance = round(float(meta.get("payday_balance") or 0.0), 2)
actual_balance = round(float(meta.get("actual_balance") or 0.0), 2)

calculated_balance = round(payday_balance - bills_paid_total + adjustments_total, 2)
dd_account_buffer = round(calculated_balance - bills_unpaid_total, 2)
balance_discrepancy = round(actual_balance - calculated_balance, 2)
is_account_balanced = abs(balance_discrepancy) < 0.005

paid_reg_count = sum(1 for b in regular_bills if b.get("is_paid"))

# -----------------------------------------------------------------------------
# HEADER & ACTIONS
# -----------------------------------------------------------------------------
hdr_left, hdr_right = st.columns([2.2, 1.8], gap="medium")
with hdr_left:
    st.markdown(f"### 💶 PTSB Bills Tracker — **{meta.get('salary_month', 'Sep')} Cycle**")
    st.caption("Payday Cycle: **22nd → 21st** • Dedicated Bills Account Liquidity")

with hdr_right:
    q1, q2 = st.columns(2)
    with q1:
        if st.button("✅ Mark Due Up to Today", use_container_width=True):
            today_pos = cycle_sort_key(date.today().day)
            ids_to_mark = [
                b["id"]
                for b in regular_bills
                if not b.get("is_paid") and cycle_sort_key(int(b.get("due_day") or 22)) <= today_pos
            ]
            if ids_to_mark:
                supabase.table("ptsb_bills").update({"is_paid": True}).in_("id", ids_to_mark).execute()
                st.rerun()
            else:
                st.toast("All bills due up to today are already paid!")

    with q2:
        with st.popover("🔄 New Cycle", use_container_width=True):
            st.markdown("##### Start New 22nd Payday Cycle")
            months_list = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
            curr_m = meta.get("salary_month", "Sep")
            next_idx = (months_list.index(curr_m) + 1) % 12 if curr_m in months_list else 0
            new_m = st.selectbox("New Salary Month", months_list, index=next_idx)
            new_payday_bal = st.number_input("New Payday Balance (€)", value=float(payday_balance), step=10.0, format="%.2f")
            if st.button("🚀 Reset & Start New Cycle", type="primary", use_container_width=True):
                supabase.table("ptsb_bills").update({"is_paid": False, "override_amount": None}).neq("id", 0).execute()
                supabase.table("ptsb_adjustments").delete().neq("id", 0).execute()
                supabase.table("ptsb_cycle_meta").update(
                    {
                        "salary_month": new_m,
                        "payday_balance": round(float(new_payday_bal), 2),
                        "actual_balance": round(float(new_payday_bal), 2),
                        "balanced_date": str(date.today()),
                        "updated_at": datetime.utcnow().isoformat(),
                    }
                ).eq("id", 1).execute()
                st.rerun()

# -----------------------------------------------------------------------------
# HERO BANNER & RECONCILIATION PANEL
# -----------------------------------------------------------------------------
top_left, top_right = st.columns([1.1, 1.9], gap="large")

with top_left:
    if dd_account_buffer >= 20:
        b_class = "banner-ok"
        b_title = "🟢 BUFFER HEALTHY"
        b_msg = f"Covers remaining unpaid bills ({fmt_eur(bills_unpaid_total)}) with {fmt_eur(dd_account_buffer)} to spare."
    elif dd_account_buffer >= 0:
        b_class = "banner-warn"
        b_title = "🟡 TIGHT BUFFER"
        b_msg = f"Remaining bills are covered, but margin is tight ({fmt_eur(dd_account_buffer)})."
    else:
        b_class = "banner-danger"
        b_title = "🔴 SHORTFALL ALERT"
        b_msg = f"Transfer at least {fmt_eur(abs(dd_account_buffer))} into PTSB."

    st.markdown(
        f"""
        <div class="status-banner {b_class}">
            <div class="sub-label">{b_title}</div>
            <div class="big-num" style="margin: 0.3rem 0;">{fmt_eur(dd_account_buffer, plus=True)}</div>
            <div style="font-size: 0.85rem; line-height: 1.4;">{b_msg}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    pct_done = (bills_paid_total / total_cycle_commitment) if total_cycle_commitment > 0 else 0.0
    st.progress(
        min(1.0, max(0.0, pct_done)),
        text=f"Progress: {paid_reg_count}/{len(regular_bills)} Paid ({fmt_eur(bills_paid_total)} / {fmt_eur(total_cycle_commitment)})",
    )

with top_right:
    with st.container(border=True):
        r_col1, r_col2, r_col3 = st.columns([1.2, 1.2, 1.1], gap="small")

        with r_col1:
            new_pay_bal = st.number_input("1. Payday Balance (€)", value=float(payday_balance), step=10.0, format="%.2f", key="inp_payday_bal")
            if round(new_pay_bal, 2) != payday_balance:
                supabase.table("ptsb_cycle_meta").update({"payday_balance": round(new_pay_bal, 2)}).eq("id", 1).execute()
                st.rerun()

        with r_col2:
            new_act_bal = st.number_input("2. Actual Balance (€)", value=float(actual_balance), step=5.0, format="%.2f", key="inp_actual_bal")
            if round(new_act_bal, 2) != actual_balance:
                supabase.table("ptsb_cycle_meta").update({"actual_balance": round(new_act_bal, 2), "balanced_date": str(date.today())}).eq("id", 1).execute()
                st.rerun()

        with r_col3:
            st.markdown("<div class='sub-label' style='margin-bottom:6px;'>Balanced?</div>", unsafe_allow_html=True)
            if is_account_balanced:
                st.success(f"**Yes** ({fmt_eur(actual_balance)})")
            else:
                st.error(f"**No!** ({fmt_eur(balance_discrepancy, plus=True)})")
                fix1, fix2 = st.columns(2)
                with fix1:
                    if st.button("Log Diff", help="Log discrepancy as adjustment", use_container_width=True):
                        supabase.table("ptsb_adjustments").insert({"description": f"Reconciliation diff", "amount": balance_discrepancy}).execute()
                        supabase.table("ptsb_cycle_meta").update({"balanced_date": str(date.today())}).eq("id", 1).execute()
                        st.rerun()
                with fix2:
                    if st.button("Match", help="Set Actual = Calculated", use_container_width=True):
                        supabase.table("ptsb_cycle_meta").update({"actual_balance": calculated_balance, "balanced_date": str(date.today())}).eq("id", 1).execute()
                        st.rerun()

        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Paid", fmt_eur(-bills_paid_total))
        m2.metric("Unpaid", fmt_eur(bills_unpaid_total))
        m3.metric("Adjustments", fmt_eur(adjustments_total, plus=True))
        m4.metric("Calculated", fmt_eur(calculated_balance))

st.divider()

# -----------------------------------------------------------------------------
# MAIN LAYOUT: BILLS & SIDEBAR (Adjustments / Export)
# -----------------------------------------------------------------------------
main_col, side_col = st.columns([2.0, 1.1], gap="large")

with main_col:
    # Regular Bills
    sec_1, sec_2, sec_3 = st.columns([1.7, 1.1, 0.9])
    with sec_1:
        st.subheader(f"📅 Regular Bills ({paid_reg_count}/{len(regular_bills)})")
    with sec_2:
        bill_filter = st.segmented_control("Filter", ["All", "Unpaid", "Paid"], default="All", label_visibility="collapsed")
    with sec_3:
        with st.popover("➕ Add Bill", use_container_width=True):
            st.markdown("##### Add Regular Bill")
            with st.form("add_reg_form", clear_on_submit=True):
                new_payee = st.text_input("Payee Name")
                ca, cb = st.columns(2)
                new_type = ca.selectbox("Type", ["DD", "SO", "Card"])
                new_due = cb.number_input("Due Day (1-31)", 1, 31, 24)
                new_amt = st.number_input("Amount (€)", 0.0, value=25.0, step=1.0)
                new_paid = st.checkbox("Already Paid?")
                if st.form_submit_button("Add Bill", type="primary", use_container_width=True):
                    if new_payee.strip():
                        supabase.table("ptsb_bills").insert({
                            "category": "regular", "payee": new_payee.strip(), "payment_type": new_type,
                            "due_day": int(new_due), "default_amount": round(float(new_amt), 2),
                            "is_paid": bool(new_paid), "active_in_cycle": True, "sort_tiebreaker": 10
                        }).execute()
                        st.rerun()

    filtered_regular = [
        b for b in regular_bills
        if bill_filter == "All" or (bill_filter == "Unpaid" and not b.get("is_paid")) or (bill_filter == "Paid" and b.get("is_paid"))
    ]

    with st.container(border=True):
        if not filtered_regular:
            st.info("No bills match this filter.")
        for idx, bill in enumerate(filtered_regular):
            bid = bill["id"]
            is_paid = bool(bill.get("is_paid"))
            eff_amt = effective_amount(bill)
            def_amt = float(bill.get("default_amount") or 0.0)
            has_ov = bill.get("override_amount") is not None and pd.notna(bill.get("override_amount"))
            due_str = ordinal_day(int(bill.get("due_day") or 22))
            ptype = bill.get("payment_type", "DD")

            c_chk, c_info, c_amt, c_edit = st.columns([0.4, 2.6, 1.2, 0.6], vertical_alignment="center")

            with c_chk:
                np_state = st.checkbox("Paid", value=is_paid, key=f"r_pd_{bid}", label_visibility="collapsed")
                if np_state != is_paid:
                    supabase.table("ptsb_bills").update({"is_paid": np_state}).eq("id", bid).execute()
                    st.rerun()

            with c_info:
                ov_badge = " ⚡" if has_ov else ""
                st.markdown(f"**{bill['payee']}** `{ptype}` • Due **{due_str}**{ov_badge}")

            with c_amt:
                st.markdown(f"<div style='text-align:right; font-family:monospace; font-weight:700;'>{fmt_eur(eff_amt)}</div>", unsafe_allow_html=True)

            with c_edit:
                with st.popover("✏️", use_container_width=True):
                    ep = st.text_input("Payee", value=bill["payee"], key=f"ep_{bid}")
                    et1, et2 = st.columns(2)
                    et = et1.selectbox("Type", ["DD", "SO", "Card"], index=["DD", "SO", "Card"].index(ptype) if ptype in ["DD", "SO", "Card"] else 0, key=f"et_{bid}")
                    ed = et2.number_input("Due Day", 1, 31, int(bill.get("due_day") or 22), key=f"ed_{bid}")
                    eca = st.number_input("Amount THIS Cycle (€)", value=float(eff_amt), step=1.0, format="%.2f", key=f"eca_{bid}")
                    eda = st.number_input("Permanent Default (€)", value=float(def_amt), step=1.0, format="%.2f", key=f"eda_{bid}")
                    
                    bs, bd = st.columns(2)
                    with bs:
                        if st.button("Save", key=f"es_{bid}", type="primary", use_container_width=True):
                            nd = round(float(eda), 2)
                            nc = round(float(eca), 2)
                            nov = nc if abs(nc - nd) > 0.005 else None
                            supabase.table("ptsb_bills").update({
                                "payee": ep.strip(), "payment_type": et, "due_day": int(ed),
                                "default_amount": nd, "override_amount": nov
                            }).eq("id", bid).execute()
                            st.rerun()
                    with bd:
                        if st.button("Delete", key=f"edl_{bid}", use_container_width=True):
                            supabase.table("ptsb_bills").delete().eq("id", bid).execute()
                            st.rerun()

            if idx < len(filtered_regular) - 1:
                st.markdown("<hr style='margin: 0.3rem 0; opacity: 0.15;'>", unsafe_allow_html=True)

    # Ad-Hoc Bills
    st.markdown("### ⚡ Ad-Hoc / Utilities")
    with st.container(border=True):
        for abill in adhoc_bills:
            aid = abill["id"]
            ia = bool(abill.get("active_in_cycle"))
            iap = bool(abill.get("is_paid"))
            ea = effective_amount(abill)

            ac1, ac2, ac3, ac4, ac5 = st.columns([0.8, 2.1, 1.0, 0.9, 0.5], vertical_alignment="center")
            with ac1:
                na = st.toggle("Active", value=ia, key=f"ah_act_{aid}")
                if na != ia:
                    supabase.table("ptsb_bills").update({"active_in_cycle": na, "is_paid": False if not na else iap}).eq("id", aid).execute()
                    st.rerun()
            with ac2:
                st.markdown(f"**{abill['payee']}** • Ref: `{abill.get('adhoc_date_label')}`")
            with ac3:
                st.markdown(f"<div style='font-family:monospace; font-weight:700;'>{fmt_eur(ea)}</div>", unsafe_allow_html=True)
            with ac4:
                nap = st.checkbox("Paid", value=iap, disabled=not ia, key=f"ah_pd_{aid}")
                if nap != iap:
                    supabase.table("ptsb_bills").update({"is_paid": nap}).eq("id", aid).execute()
                    st.rerun()
            with ac5:
                with st.popover("✏️", use_container_width=True):
                    ap = st.text_input("Payee", value=abill["payee"], key=f"ahp_{aid}")
                    ad = st.text_input("Ref Date", value=abill.get("adhoc_date_label") or "", key=f"ahd_{aid}")
                    aa = st.number_input("Amount (€)", value=float(ea), step=1.0, format="%.2f", key=f"aha_{aid}")
                    if st.button("Save", key=f"ahs_{aid}", type="primary", use_container_width=True):
                        supabase.table("ptsb_bills").update({"payee": ap.strip(), "adhoc_date_label": ad.strip(), "default_amount": round(float(aa), 2), "override_amount": None}).eq("id", aid).execute()
                        st.rerun()
                    if st.button("Delete", key=f"ahdel_{aid}", use_container_width=True):
                        supabase.table("ptsb_bills").delete().eq("id", aid).execute()
                        st.rerun()

        with st.expander("➕ Add Ad-Hoc Bill"):
            with st.form("add_ah", clear_on_submit=True):
                ah1, ah2, ah3 = st.columns(3)
                ah_p = ah1.text_input("Payee")
                ah_d = ah2.text_input("Ref Date (e.g. 25/03)")
                ah_a = ah3.number_input("Amount (€)", 0.0, step=5.0)
                if st.form_submit_button("Add", use_container_width=True):
                    if ah_p.strip():
                        supabase.table("ptsb_bills").insert({
                            "category": "adhoc", "payee": ah_p.strip(), "payment_type": "DD",
                            "due_day": 15, "adhoc_date_label": ah_d.strip(), "default_amount": round(float(ah_a), 2),
                            "is_paid": False, "active_in_cycle": True
                        }).execute()
                        st.rerun()


with side_col:
    st.subheader(f"🧾 Adjustments ({fmt_eur(adjustments_total, plus=True)})")
    with st.container(border=True):
        with st.form("add_adj", clear_on_submit=True):
            ad_desc = st.text_input("Note", placeholder="e.g. Card spend / refund")
            ad_amt = st.number_input("Amount (€) (- for debit)", value=0.0, step=1.0, format="%.2f")
            if st.form_submit_button("➕ Add Adjustment", type="primary", use_container_width=True):
                if ad_desc.strip() and abs(ad_amt) > 0.001:
                    supabase.table("ptsb_adjustments").insert({"description": ad_desc.strip(), "amount": round(float(ad_amt), 2)}).execute()
                    st.rerun()

        if adjustments:
            st.markdown("---")
            for adj in adjustments:
                aj1, aj2, aj3 = st.columns([2.0, 1.1, 0.4])
                aj1.markdown(f"<span style='font-size:0.85rem;'>{adj['description']}</span>", unsafe_allow_html=True)
                av = float(adj["amount"])
                col = "#34d399" if av >= 0 else "#fb7185"
                aj2.markdown(f"<div style='text-align:right; font-family:monospace; font-weight:700; color:{col};'>{fmt_eur(av, plus=True)}</div>", unsafe_allow_html=True)
                if aj3.button("✖", key=f"da_{adj['id']}"):
                    supabase.table("ptsb_adjustments").delete().eq("id", adj["id"]).execute()
                    st.rerun()
        else:
            st.caption("No adjustments logged.")

    st.subheader("📊 By Payment Type")
    with st.container(border=True):
        for pt_code, pt_label in [("SO", "Standing Orders"), ("DD", "Direct Debits"), ("Card", "Recurring Card")]:
            pt_bills = [b for b in regular_bills if b.get("payment_type") == pt_code]
            pt_paid = sum(effective_amount(b) for b in pt_bills if b.get("is_paid"))
            pt_unpaid = sum(effective_amount(b) for b in pt_bills if not b.get("is_paid"))
            st.markdown(f"**{pt_label}**: {fmt_eur(pt_paid + pt_unpaid)} <span style='font-size:0.78rem; opacity:0.8;'>(Paid: {fmt_eur(pt_paid)})</span>", unsafe_allow_html=True)

    # Excel export
    excel_buf = io.BytesIO()
    with pd.ExcelWriter(excel_buf, engine="openpyxl") as writer:
        pd.DataFrame([
            {"Metric": "Salary Month", "Value": meta.get("salary_month", "Sep")},
            {"Metric": "Payday Balance (€)", "Value": payday_balance},
            {"Metric": "Bills Paid (€)", "Value": bills_paid_total},
            {"Metric": "Bills Unpaid (€)", "Value": bills_unpaid_total},
            {"Metric": "Adjustments (€)", "Value": adjustments_total},
            {"Metric": "Calculated Balance (€)", "Value": calculated_balance},
            {"Metric": "DD Account Buffer (€)", "Value": dd_account_buffer},
            {"Metric": "Actual Balance (€)", "Value": actual_balance},
        ]).to_excel(writer, sheet_name="Summary", index=False)
        
        pd.DataFrame([
            {"Payee": b["payee"], "Type": b["payment_type"], "Due": b["due_day"], "Amount": effective_amount(b), "Status": "Paid" if b["is_paid"] else "Unpaid"}
            for b in (regular_bills + adhoc_bills)
        ]).to_excel(writer, sheet_name="Bills", index=False)

    st.download_button(
        label="📥 Download Excel Snapshot",
        data=excel_buf.getvalue(),
        file_name=f"PTSB_Bills_{meta.get('salary_month', 'Cycle')}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        use_container_width=True,
    )
