import io
from datetime import date, datetime
import pandas as pd
import streamlit as st
from supabase import create_client, Client

# -----------------------------------------------------------------------------
# PAGE CONFIG & MOBILE-FRIENDLY STYLING
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
        padding-top: 1.2rem;
        padding-bottom: 3rem;
        max-width: 1180px;
    }
    .status-banner {
        padding: 1.15rem 1.35rem;
        border-radius: 14px;
        margin-bottom: 1rem;
        border: 1px solid rgba(255,255,255,0.12);
    }
    .banner-ok {
        background: linear-gradient(135deg, rgba(6, 78, 59, 0.75), rgba(15, 23, 42, 0.9));
        border-color: rgba(16, 185, 129, 0.45);
    }
    .banner-warn {
        background: linear-gradient(135deg, rgba(120, 53, 15, 0.75), rgba(15, 23, 42, 0.9));
        border-color: rgba(245, 158, 11, 0.45);
    }
    .banner-danger {
        background: linear-gradient(135deg, rgba(136, 19, 55, 0.8), rgba(15, 23, 42, 0.9));
        border-color: rgba(244, 63, 94, 0.55);
    }
    .big-num {
        font-family: 'SF Mono', 'JetBrains Mono', monospace;
        font-size: 2.1rem;
        font-weight: 800;
        line-height: 1.15;
    }
    .sub-label {
        font-size: 0.78rem;
        text-transform: uppercase;
        letter-spacing: 0.06em;
        opacity: 0.8;
    }
    </style>
    """,
    unsafe_allow_html=True,
)


# -----------------------------------------------------------------------------
# SECURITY PIN / PASSWORD LOCK (Protects Public Streamlit URLs)
# -----------------------------------------------------------------------------
def check_password() -> bool:
    """Blocks access to the app until the user enters APP_PASSWORD from st.secrets."""
    expected_pw = st.secrets.get("APP_PASSWORD")
    if not expected_pw:
        # If no APP_PASSWORD is set in secrets yet, allow access but show a reminder
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
# SUPABASE CONNECTION (Supports both flat secrets and [connections.supabase])
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
        st.error(
            "⚠️ Supabase credentials not found in `st.secrets`. "
            "Please add `SUPABASE_URL` and `SUPABASE_KEY` to your Streamlit secrets."
        )
        st.stop()

    return create_client(url, key)


supabase = get_supabase_client()


# -----------------------------------------------------------------------------
# HELPER FUNCTIONS: PAYDAY CYCLE ORDERING (22nd -> 31st, then 1st -> 21st)
# -----------------------------------------------------------------------------
def cycle_sort_key(due_day: int) -> int:
    """
    Payday is the 22nd of the month.
    Days 22..31 map to 0..9.
    Days 1..21 map to 10..30.
    This guarantees that a bill due on the 24th is ordered right after the 23rd
    and before the 25th..31st and 1st..21st.
    """
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

# Separate & sort Regular bills strictly by Payday Cycle (22nd -> 21st)
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
# CORE RECONCILIATION MATH
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

# Formula from Row 28: Calculated Balance = Payday Balance - Bills Paid + Adjustments
calculated_balance = round(payday_balance - bills_paid_total + adjustments_total, 2)

# Formula from Row 29: DD Account Buffer = Calculated Balance - Bills Still to be Paid
dd_account_buffer = round(calculated_balance - bills_unpaid_total, 2)

# Discrepancy between Actual PTSB Balance and Calculated Balance
balance_discrepancy = round(actual_balance - calculated_balance, 2)
is_account_balanced = abs(balance_discrepancy) < 0.005

paid_reg_count = sum(1 for b in regular_bills if b.get("is_paid"))
unpaid_reg_count = len(regular_bills) - paid_reg_count

# -----------------------------------------------------------------------------
# HEADER & QUICK ACTIONS BAR
# -----------------------------------------------------------------------------
hdr_left, hdr_right = st.columns([2.2, 1.8])
with hdr_left:
    st.markdown(f"### 💶 PTSB Direct Debit & Bills Tracker — **{meta.get('salary_month', 'Sep')} Cycle**")
    st.caption("Payday Cycle: **22nd → 21st** • Dedicated Bills Account Liquidity & Reconciliation")

with hdr_right:
    q1, q2 = st.columns(2)
    with q1:
        if st.button("✅ Mark Due Up to Today Paid", use_container_width=True):
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
                st.toast("All bills due up to today are already marked Paid!")

    with q2:
        with st.popover("🔄 Start New Payday Cycle", use_container_width=True):
            st.markdown("##### Start New 22nd Payday Cycle")
            st.caption(
                "Resets all bills to **Not Paid**, clears one-month amount overrides & adjustments, "
                "and updates your starting Payday Balance."
            )
            months_list = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
            curr_m = meta.get("salary_month", "Sep")
            next_idx = (months_list.index(curr_m) + 1) % 12 if curr_m in months_list else 0
            new_m = st.selectbox("New Salary Month", months_list, index=next_idx)
            new_payday_bal = st.number_input(
                "New Balance as at Pay Day (€)", value=float(payday_balance), step=10.0, format="%.2f"
            )
            if st.button("🚀 Reset Cycle & Start Month", type="primary", use_container_width=True):
                supabase.table("ptsb_bills").update(
                    {"is_paid": False, "override_amount": None}
                ).neq("id", 0).execute()
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
# HERO BANNER: DD ACCOUNT BUFFER & RECONCILIATION STATUS
# -----------------------------------------------------------------------------
top_left, top_right = st.columns([1.15, 1.85])

with top_left:
    if dd_account_buffer >= 20:
        b_class = "banner-ok"
        b_title = "🟢 FUNDS SUFFICIENT — BUFFER HEALTHY"
        b_msg = (
            f"Your PTSB account covers all **{ fmt_eur(bills_unpaid_total) }** of remaining unpaid bills "
            f"with **{ fmt_eur(dd_account_buffer) }** to spare."
        )
    elif dd_account_buffer >= 0:
        b_class = "banner-warn"
        b_title = "🟡 TIGHT BUFFER — LOW MARGIN"
        b_msg = (
            f"Remaining bills (**{ fmt_eur(bills_unpaid_total) }**) are covered, "
            f"but your buffer is tight at **{ fmt_eur(dd_account_buffer) }**."
        )
    else:
        b_class = "banner-danger"
        b_title = "🔴 SHORTFALL ALERT — TOP UP PTSB ACCOUNT"
        b_msg = (
            f"You need to transfer at least **{ fmt_eur(abs(dd_account_buffer)) }** into PTSB "
            f"to cover all remaining unpaid bills (**{ fmt_eur(bills_unpaid_total) }**)."
        )

    st.markdown(
        f"""
        <div class="status-banner {b_class}">
            <div class="sub-label">{b_title}</div>
            <div class="big-num" style="margin: 0.35rem 0;">{fmt_eur(dd_account_buffer, plus=True)}</div>
            <div style="font-size: 0.88rem; opacity: 0.92;">{b_msg}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    pct_done = (bills_paid_total / total_cycle_commitment) if total_cycle_commitment > 0 else 0.0
    st.progress(
        min(1.0, max(0.0, pct_done)),
        text=f"Cycle Progress: {paid_reg_count}/{len(regular_bills)} Regular Bills Paid ({fmt_eur(bills_paid_total)} of {fmt_eur(total_cycle_commitment)})",
    )

with top_right:
    with st.container(border=True):
        r_col1, r_col2, r_col3 = st.columns([1.2, 1.2, 1.1])

        with r_col1:
            new_pay_bal = st.number_input(
                "1. Balance as at Pay Day (€)",
                value=float(payday_balance),
                step=10.0,
                format="%.2f",
                key="inp_payday_bal",
            )
            if round(new_pay_bal, 2) != payday_balance:
                supabase.table("ptsb_cycle_meta").update(
                    {"payday_balance": round(new_pay_bal, 2), "updated_at": datetime.utcnow().isoformat()}
                ).eq("id", 1).execute()
                st.rerun()

        with r_col2:
            new_act_bal = st.number_input(
                "2. Actual Account Balance Now (€)",
                value=float(actual_balance),
                step=5.0,
                format="%.2f",
                key="inp_actual_bal",
            )
            if round(new_act_bal, 2) != actual_balance:
                supabase.table("ptsb_cycle_meta").update(
                    {
                        "actual_balance": round(new_act_bal, 2),
                        "balanced_date": str(date.today()),
                        "updated_at": datetime.utcnow().isoformat(),
                    }
                ).eq("id", 1).execute()
                st.rerun()

        with r_col3:
            st.markdown("<div class='sub-label' style='margin-bottom:4px;'>Account Balanced?</div>", unsafe_allow_html=True)
            if is_account_balanced:
                st.success(f"**Yes** ({fmt_eur(actual_balance)})")
                st.caption(f"Balanced Date: `{meta.get('balanced_date', '')}`")
            else:
                st.error(f"**No, check it!** ({fmt_eur(balance_discrepancy, plus=True)})")
                b_fix1, b_fix2 = st.columns(2)
                with b_fix1:
                    if st.button("Log Diff", help="Add discrepancy to Adjustments", use_container_width=True):
                        supabase.table("ptsb_adjustments").insert(
                            {
                                "description": f"Reconciliation adjustment ({date.today().strftime('%d %b')})",
                                "amount": balance_discrepancy,
                            }
                        ).execute()
                        supabase.table("ptsb_cycle_meta").update({"balanced_date": str(date.today())}).eq("id", 1).execute()
                        st.rerun()
                with b_fix2:
                    if st.button("Match Calc", help="Set Actual = Calculated Balance", use_container_width=True):
                        supabase.table("ptsb_cycle_meta").update(
                            {"actual_balance": calculated_balance, "balanced_date": str(date.today())}
                        ).eq("id", 1).execute()
                        st.rerun()

        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Bills Paid", fmt_eur(-bills_paid_total))
        m2.metric("Bills Still to Pay", fmt_eur(bills_unpaid_total))
        m3.metric("Adjustments", fmt_eur(adjustments_total, plus=True))
        m4.metric("Calculated Balance", fmt_eur(calculated_balance))

st.divider()

# -----------------------------------------------------------------------------
# MAIN TABS / SECTIONS
# -----------------------------------------------------------------------------
main_col, side_col = st.columns([2.05, 1.15], gap="large")

with main_col:
    # -------------------------------------------------------------------------
    # SECTION 1: REGULAR MONTHLY BILLS (Auto-sorted 22nd -> 21st)
    # -------------------------------------------------------------------------
    sec_hdr1, sec_hdr2, sec_hdr3 = st.columns([1.8, 1.1, 0.9])
    with sec_hdr1:
        st.subheader(f"📅 Regular Monthly Bills ({paid_reg_count}/{len(regular_bills)} Paid)")
    with sec_hdr2:
        bill_filter = st.segmented_control(
            "Filter",
            options=["All", "Unpaid", "Paid"],
            default="All",
            label_visibility="collapsed",
        )
    with sec_hdr3:
        with st.popover("➕ Add Regular Bill", use_container_width=True):
            st.markdown("##### Add New Regular Bill")
            st.caption(
                "Automatically slots into your list in **22nd → 21st** payday order. "
                "(e.g., a bill on the **24th** is placed right after the 23rd)."
            )
            with st.form("add_regular_bill_form", clear_on_submit=True):
                new_payee = st.text_input("Payee Name", placeholder="e.g. Broadband / Gym")
                c_a, c_b = st.columns(2)
                new_type = c_a.selectbox("Payment Type", ["DD", "SO", "Card"])
                new_due_day = c_b.number_input("Due Day of Month (1–31)", min_value=1, max_value=31, value=24, step=1)
                new_amt = st.number_input("Monthly Amount (€)", min_value=0.0, value=25.00, step=1.0, format="%.2f")
                new_paid_now = st.checkbox("Already Paid in current cycle?", value=False)
                if st.form_submit_button("Add Regular Bill", type="primary", use_container_width=True):
                    if new_payee.strip():
                        same_day_ties = [
                            int(b.get("sort_tiebreaker") or 10)
                            for b in regular_bills
                            if int(b.get("due_day") or 0) == int(new_due_day)
                        ]
                        next_tie = (max(same_day_ties) + 1) if same_day_ties else 10
                        supabase.table("ptsb_bills").insert(
                            {
                                "category": "regular",
                                "payee": new_payee.strip(),
                                "payment_type": new_type,
                                "due_day": int(new_due_day),
                                "default_amount": round(float(new_amt), 2),
                                "override_amount": None,
                                "is_paid": bool(new_paid_now),
                                "active_in_cycle": True,
                                "sort_tiebreaker": next_tie,
                            }
                        ).execute()
                        st.rerun()

    # Display Regular Bills List
    filtered_regular = [
        b
        for b in regular_bills
        if bill_filter == "All"
        or (bill_filter == "Unpaid" and not b.get("is_paid"))
        or (bill_filter == "Paid" and b.get("is_paid"))
    ]

    with st.container(border=True):
        if not filtered_regular:
            st.info("No bills match the current filter.")
        for idx, bill in enumerate(filtered_regular):
            bid = bill["id"]
            is_paid = bool(bill.get("is_paid"))
            eff_amt = effective_amount(bill)
            def_amt = float(bill.get("default_amount") or 0.0)
            has_override = bill.get("override_amount") is not None and pd.notna(bill.get("override_amount"))
            due_str = ordinal_day(int(bill.get("due_day") or 22))
            ptype = bill.get("payment_type", "DD")

            c_chk, c_info, c_amt, c_edit = st.columns([0.45, 2.55, 1.15, 0.65], vertical_alignment="center")

            with c_chk:
                new_paid_state = st.checkbox(
                    "Paid",
                    value=is_paid,
                    key=f"reg_paid_{bid}",
                    label_visibility="collapsed",
                )
                if new_paid_state != is_paid:
                    supabase.table("ptsb_bills").update({"is_paid": new_paid_state}).eq("id", bid).execute()
                    st.rerun()

            with c_info:
                strike = "~~" if is_paid else ""
                badge_override = " `⚡ Cycle Override`" if has_override else ""
                st.markdown(
                    f"{strike}**{bill['payee']}**{strike} &nbsp;`{ptype}` &nbsp;•&nbsp; Due **{due_str}**{badge_override}"
                )

            with c_amt:
                status_pill = "🟢 Paid" if is_paid else "🔴 Not Paid"
                st.markdown(
                    f"<div style='text-align:right; font-family:monospace; font-weight:700; font-size:1.03rem;'>"
                    f"{fmt_eur(eff_amt)} <span style='font-size:0.75rem; font-weight:500;'>{status_pill}</span></div>",
                    unsafe_allow_html=True,
                )

            with c_edit:
                with st.popover("✏️", use_container_width=True):
                    st.markdown(f"**Edit {bill['payee']}**")
                    e_payee = st.text_input("Payee", value=bill["payee"], key=f"ep_{bid}")
                    ec1, ec2 = st.columns(2)
                    e_type = ec1.selectbox(
                        "Type",
                        ["DD", "SO", "Card"],
                        index=["DD", "SO", "Card"].index(ptype) if ptype in ["DD", "SO", "Card"] else 0,
                        key=f"et_{bid}",
                    )
                    e_due = ec2.number_input(
                        "Due Day (1–31)",
                        min_value=1,
                        max_value=31,
                        value=int(bill.get("due_day") or 22),
                        key=f"ed_{bid}",
                    )
                    st.markdown("---")
                    e_cycle_amt = st.number_input(
                        "Amount for THIS Cycle Only (€)",
                        value=float(eff_amt),
                        step=1.0,
                        format="%.2f",
                        help="Change this if the bill is higher/lower just for this month.",
                        key=f"eca_{bid}",
                    )
                    e_def_amt = st.number_input(
                        "Permanent Default Monthly Amount (€)",
                        value=float(def_amt),
                        step=1.0,
                        format="%.2f",
                        help="Permanent amount used when you start a new cycle.",
                        key=f"eda_{bid}",
                    )

                    b_save, b_del = st.columns(2)
                    with b_save:
                        if st.button("💾 Save", key=f"esave_{bid}", type="primary", use_container_width=True):
                            new_def = round(float(e_def_amt), 2)
                            new_cyc = round(float(e_cycle_amt), 2)
                            new_ov = new_cyc if abs(new_cyc - new_def) > 0.005 else None
                            supabase.table("ptsb_bills").update(
                                {
                                    "payee": e_payee.strip() or bill["payee"],
                                    "payment_type": e_type,
                                    "due_day": int(e_due),
                                    "default_amount": new_def,
                                    "override_amount": new_ov,
                                }
                            ).eq("id", bid).execute()
                            st.rerun()
                    with b_del:
                        if st.button("🗑️ Delete", key=f"edel_{bid}", use_container_width=True):
                            supabase.table("ptsb_bills").delete().eq("id", bid).execute()
                            st.rerun()

            if idx < len(filtered_regular) - 1:
                st.markdown("<hr style='margin: 0.35rem 0; opacity: 0.15;'>", unsafe_allow_html=True)

    # -------------------------------------------------------------------------
    # SECTION 2: SEPARATE AD-HOC / UTILITY BILLS LIST (SSE, Energia, etc.)
    # -------------------------------------------------------------------------
    st.markdown("### ⚡ Ad-Hoc / Bi-Monthly Bills (Separate List)")
    st.caption("Toggle **Active this Month** when an ad-hoc utility bill (like SSE or Energia) falls inside the current salary cycle.")

    with st.container(border=True):
        for idx, abill in enumerate(adhoc_bills):
            aid = abill["id"]
            is_active = bool(abill.get("active_in_cycle"))
            is_paid = bool(abill.get("is_paid"))
            eff_amt = effective_amount(abill)

            ac1, ac2, ac3, ac4, ac5 = st.columns([0.9, 1.9, 1.0, 0.9, 0.55], vertical_alignment="center")
            with ac1:
                new_act = st.toggle("Active", value=is_active, key=f"adhoc_act_{aid}")
                if new_act != is_active:
                    upd = {"active_in_cycle": new_act}
                    if not new_act:
                        upd["is_paid"] = False
                    supabase.table("ptsb_bills").update(upd).eq("id", aid).execute()
                    st.rerun()

            with ac2:
                date_lbl = abill.get("adhoc_date_label") or ordinal_day(int(abill.get("due_day") or 1))
                st.markdown(f"**{abill['payee']}** (`{abill.get('payment_type', 'DD')}`) • Ref: `{date_lbl}`")

            with ac3:
                st.markdown(f"<div style='font-family:monospace; font-weight:700;'>{fmt_eur(eff_amt)}</div>", unsafe_allow_html=True)

            with ac4:
                new_apaid = st.checkbox(
                    "Paid",
                    value=is_paid,
                    disabled=not is_active,
                    key=f"adhoc_paid_{aid}",
                )
                if new_apaid != is_paid:
                    supabase.table("ptsb_bills").update({"is_paid": new_apaid}).eq("id", aid).execute()
                    st.rerun()

            with ac5:
                with st.popover("✏️", use_container_width=True):
                    ea_payee = st.text_input("Payee", value=abill["payee"], key=f"eap_{aid}")
                    ea_date = st.text_input("Ref Date / Note", value=abill.get("adhoc_date_label") or "", key=f"ead_{aid}")
                    ea_amt = st.number_input("Amount (€)", value=float(eff_amt), step=1.0, format="%.2f", key=f"eaa_{aid}")
                    if st.button("💾 Save", key=f"eas_{aid}", type="primary", use_container_width=True):
                        supabase.table("ptsb_bills").update(
                            {
                                "payee": ea_payee.strip() or abill["payee"],
                                "adhoc_date_label": ea_date.strip(),
                                "default_amount": round(float(ea_amt), 2),
                                "override_amount": None,
                            }
                        ).eq("id", aid).execute()
                        st.rerun()
                    if st.button("🗑️ Delete", key=f"eadel_{aid}", use_container_width=True):
                        supabase.table("ptsb_bills").delete().eq("id", aid).execute()
                        st.rerun()

        with st.expander("➕ Add New Ad-Hoc Bill"):
            with st.form("add_adhoc_form", clear_on_submit=True):
                ah_col1, ah_col2, ah_col3 = st.columns(3)
                ah_payee = ah_col1.text_input("Payee Name", placeholder="e.g. Bord Gáis / Car Service")
                ah_date = ah_col2.text_input("Due Date / Ref", placeholder="e.g. 25/03")
                ah_amt = ah_col3.number_input("Amount (€)", value=0.0, step=5.0, format="%.2f")
                if st.form_submit_button("Add Ad-Hoc Bill", use_container_width=True):
                    if ah_payee.strip():
                        supabase.table("ptsb_bills").insert(
                            {
                                "category": "adhoc",
                                "payee": ah_payee.strip(),
                                "payment_type": "DD",
                                "due_day": 15,
                                "adhoc_date_label": ah_date.strip(),
                                "default_amount": round(float(ah_amt), 2),
                                "is_paid": False,
                                "active_in_cycle": True,
                            }
                        ).execute()
                        st.rerun()


with side_col:
    # -------------------------------------------------------------------------
    # SECTION 3: ITEMISED ADJUSTMENTS BOX
    # -------------------------------------------------------------------------
    st.subheader(f"🧾 Itemised Adjustments ({fmt_eur(adjustments_total, plus=True)})")
    st.caption("Track non-bill card spends, refunds, or manual adjustments (`+` for money in, `-` for money out).")

    with st.container(border=True):
        with st.form("add_adjustment_form", clear_on_submit=True):
            adj_desc = st.text_input("Description", placeholder="e.g. Once-off card spend or refund")
            adj_amt = st.number_input("Amount (€) — use minus (-) for debit", value=0.00, step=1.00, format="%.2f")
            if st.form_submit_button("➕ Add Adjustment", type="primary", use_container_width=True):
                if adj_desc.strip() and abs(adj_amt) > 0.001:
                    supabase.table("ptsb_adjustments").insert(
                        {"description": adj_desc.strip(), "amount": round(float(adj_amt), 2)}
                    ).execute()
                    st.rerun()

        if adjustments:
            st.markdown("---")
            for adj in adjustments:
                aj_col1, aj_col2, aj_col3 = st.columns([2.1, 1.1, 0.45], vertical_alignment="center")
                aj_col1.markdown(f"<span style='font-size:0.9rem;'>{adj['description']}</span>", unsafe_allow_html=True)
                amt_v = float(adj["amount"])
                color = "#34d399" if amt_v >= 0 else "#fb7185"
                aj_col2.markdown(
                    f"<div style='text-align:right; font-family:monospace; font-weight:700; color:{color};'>"
                    f"{fmt_eur(amt_v, plus=True)}</div>",
                    unsafe_allow_html=True,
                )
                if aj_col3.button("✖", key=f"del_adj_{adj['id']}", help="Remove adjustment"):
                    supabase.table("ptsb_adjustments").delete().eq("id", adj["id"]).execute()
                    st.rerun()
        else:
            st.caption("No adjustments logged for this cycle.")

    # -------------------------------------------------------------------------
    # SECTION 4: BREAKDOWN BY PAYMENT TYPE & EXCEL EXPORT
    # -------------------------------------------------------------------------
    st.subheader("📊 Breakdown by Payment Type")
    with st.container(border=True):
        for pt_code, pt_label in [("SO", "Standing Orders (SO)"), ("DD", "Direct Debits (DD)"), ("Card", "Recurring Card")]:
            pt_bills = [b for b in regular_bills if b.get("payment_type") == pt_code]
            pt_paid = sum(effective_amount(b) for b in pt_bills if b.get("is_paid"))
            pt_unpaid = sum(effective_amount(b) for b in pt_bills if not b.get("is_paid"))
            st.markdown(
                f"**{pt_label}** — Total: `{fmt_eur(pt_paid + pt_unpaid)}`  \n"
                f"<span style='font-size:0.82rem; opacity:0.8;'>Paid: {fmt_eur(pt_paid)} • Still to Pay: {fmt_eur(pt_unpaid)}</span>",
                unsafe_allow_html=True,
            )
            if pt_code != "Card":
                st.markdown("<hr style='margin:0.4rem 0; opacity:0.15;'>", unsafe_allow_html=True)

    # Excel Snapshot Export
    excel_buf = io.BytesIO()
    with pd.ExcelWriter(excel_buf, engine="openpyxl") as writer:
        summary_df = pd.DataFrame(
            [
                {"Metric": "Salary Month", "Value": meta.get("salary_month", "Sep")},
                {"Metric": "Balance as at Pay Day (€)", "Value": payday_balance},
                {"Metric": "Bills Paid (€)", "Value": bills_paid_total},
                {"Metric": "Bills Still to be Paid (€)", "Value": bills_unpaid_total},
                {"Metric": "Adjustments (€)", "Value": adjustments_total},
                {"Metric": "Calculated Balance (€)", "Value": calculated_balance},
                {"Metric": "DD Account Buffer (€)", "Value": dd_account_buffer},
                {"Metric": "Actual Account Balance (€)", "Value": actual_balance},
                {"Metric": "Account Balanced?", "Value": "Yes" if is_account_balanced else "No, check it!"},
            ]
        )
        summary_df.to_excel(writer, sheet_name="Summary", index=False)

        bills_export_df = pd.DataFrame(
            [
                {
                    "Category": b["category"],
                    "Payee": b["payee"],
                    "Type": b["payment_type"],
                    "Due Day": ordinal_day(int(b["due_day"])) if b["category"] == "regular" else b.get("adhoc_date_label", ""),
                    "Default Amount (€)": b["default_amount"],
                    "Cycle Override (€)": b["override_amount"],
                    "Effective Amount (€)": effective_amount(b),
                    "Status": "Paid" if b["is_paid"] else "Not Paid",
                }
                for b in (regular_bills + adhoc_bills)
            ]
        )
        bills_export_df.to_excel(writer, sheet_name="Bills", index=False)

    st.download_button(
        label="📥 Download Current Cycle as Excel (.xlsx)",
        data=excel_buf.getvalue(),
        file_name=f"PTSB_Monthly_Finances_{meta.get('salary_month', 'Cycle')}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        use_container_width=True,
    )
