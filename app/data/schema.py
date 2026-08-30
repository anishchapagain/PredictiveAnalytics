"""Shared column/dtype constants for Business_Report.csv (and its future DB replacement).

Keeping this in one place means the CSV repository, the eventual Postgres repository, and
any ad hoc analysis script all agree on types -- see CLAUDE.md for the full schema notes
and the verified (full-file) cardinality of each column.
"""

# transstatus has 11 distinct values in the full file (see CLAUDE.md) -- far more than the
# simple Payment/Cancel/Block most docs assume. Only "Payment" represents money that actually
# moved and needs funding; PAYOUTPROCESSING/INITIALIZED are in-flight, and
# FAILED/PAYOUTFAIL/RETURNED/EXPIRED/Cancel/Block/Compliance/OFAC never completed.
STATUS_COMPLETED = "Payment"
ALL_KNOWN_STATUSES = (
    "Payment",
    "Cancel",
    "PAYOUTPROCESSING",
    "FAILED",
    "INITIALIZED",
    "RETURNED",
    "PAYOUTFAIL",
    "Block",
    "Compliance",
    "OFAC",
    "EXPIRED",
)

DATE_COLUMNS = ["TRN_Date", "Paid_Date"]

DTYPES = {
    "Control_No": "string",
    "Agent_Name": "string",
    "Transaction_Method": "string",
    "Payment_Type": "category",
    "Sending_Country": "category",
    "Sending_Country_Currency": "category",
    "Receiver_Country": "category",
    "Payout_Currency": "category",
    "Transaction_Amount_USD": "float64",
    "transstatus": "category",
    "Turn_Around_Time_Hours": "float64",
}

REQUIRED_COLUMNS = [
    "TRN_Date",
    "Sending_Country",
    "Receiver_Country",
    "Agent_Name",
    "Transaction_Amount_USD",
    "transstatus",
]
