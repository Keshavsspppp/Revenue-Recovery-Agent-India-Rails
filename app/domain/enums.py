"""The shared contract. docs/01-DOMAIN-MODEL.md is authoritative; do not add synonyms.

All enums subclass str so they serialise straight into the ledger JSON.
"""

from __future__ import annotations

from enum import Enum


class Rail(str, Enum):
    UPI_AUTOPAY = "UPI_AUTOPAY"
    ENACH = "ENACH"
    CARD_EMANDATE = "CARD_EMANDATE"
    PAYMENT_LINK = "PAYMENT_LINK"  # not a mandate; one-time, customer-initiated


class CauseClass(str, Enum):
    TRANSIENT_INFRA = "TRANSIENT_INFRA"        # bank/rail timeout — retry, say nothing
    INSUFFICIENT_FUNDS = "INSUFFICIENT_FUNDS"  # timing problem, not a message problem
    LIMIT_EXCEEDED = "LIMIT_EXCEEDED"          # over AFA/mandate cap — split or re-register
    AUTH_ARTEFACT = "AUTH_ARTEFACT"            # OTP/registration artefact — customer action
    MANDATE_INVALID = "MANDATE_INVALID"        # structurally wrong — repair on another rail
    MANDATE_REVOKED = "MANDATE_REVOKED"        # customer cancelled — re-consent or stop
    ACCOUNT_TERMINAL = "ACCOUNT_TERMINAL"      # closed/frozen — terminal for this rail
    UNKNOWN = "UNKNOWN"


class ActionType(str, Enum):
    SEND_PREDEBIT_NOTICE = "SEND_PREDEBIT_NOTICE"
    RETRY_DEBIT = "RETRY_DEBIT"
    SEND_MESSAGE = "SEND_MESSAGE"
    SEND_PAYMENT_LINK = "SEND_PAYMENT_LINK"
    REREGISTER_MANDATE = "REREGISTER_MANDATE"
    SPLIT_DEBIT = "SPLIT_DEBIT"
    REQUEST_PTP = "REQUEST_PTP"
    VOICE_CONFIRM_PTP = "VOICE_CONFIRM_PTP"
    OFFER_ACCOMMODATION = "OFFER_ACCOMMODATION"
    ESCALATE_HUMAN = "ESCALATE_HUMAN"
    WAIT = "WAIT"
    CLOSE = "CLOSE"


#: Actions that reach the customer. POL-QH-*, POL-FREQ-*, POL-CONSENT-* apply to these.
#: SEND_PREDEBIT_NOTICE is deliberately absent: it is a regulatory notification, not a
#: recovery contact, and is exempt from the 08:00-19:00 window. See docs/05 POL-QH-001.
CONTACT_ACTIONS: frozenset[ActionType] = frozenset({
    ActionType.SEND_MESSAGE,
    ActionType.SEND_PAYMENT_LINK,
    ActionType.REQUEST_PTP,
    ActionType.VOICE_CONFIRM_PTP,
})

#: Actions that move money on a rail.
DEBIT_ACTIONS: frozenset[ActionType] = frozenset({
    ActionType.RETRY_DEBIT,
    ActionType.SPLIT_DEBIT,
})


class TerminalState(str, Enum):
    RECOVERED = "RECOVERED"                    # settlement confirmed, not an API 200
    TERMINAL_RAIL = "TERMINAL_RAIL"            # no viable rail remains
    OPTED_OUT = "OPTED_OUT"                    # absolute stop on outreach
    DISPUTED = "DISPUTED"                      # human only, automation stops
    HARDSHIP = "HARDSHIP"                      # exited into accommodation
    FATIGUE_EXHAUSTED = "FATIGUE_EXHAUSTED"    # contact budget spent for the window
    EV_BELOW_THRESHOLD = "EV_BELOW_THRESHOLD"  # best remaining action has negative EV
    CYCLE_ENDED = "CYCLE_ENDED"                # horizon reached, unrecovered


class Stage(str, Enum):
    DETECT = "DETECT"
    ASSIGN = "ASSIGN"      # arm assignment — must be the second event for an account
    DIAGNOSE = "DIAGNOSE"
    ELIGIBLE = "ELIGIBLE"
    PROPOSE = "PROPOSE"
    GATE = "GATE"
    EXECUTE = "EXECUTE"
    OBSERVE = "OBSERVE"
    CLOSE = "CLOSE"


class Arm(str, Enum):
    TREATMENT = "treatment"
    HOLDOUT = "holdout"


class Channel(str, Enum):
    SMS = "SMS"
    WHATSAPP = "WHATSAPP"
    EMAIL = "EMAIL"
    VOICE = "VOICE"
    PUSH = "PUSH"


class Verdict(str, Enum):
    ALLOW = "ALLOW"
    DENY = "DENY"


class MerchantCategory(str, Enum):
    SUBSCRIPTION = "SUBSCRIPTION"          # OTT, SaaS — small ticket, high volume
    LENDING_EMI = "LENDING_EMI"            # NBFC EMI — large ticket
    INSURANCE = "INSURANCE"                # high AFA-free cap
    MUTUAL_FUND = "MUTUAL_FUND"            # high AFA-free cap
    CREDIT_CARD_BILL = "CREDIT_CARD_BILL"  # high AFA-free cap
    UTILITY = "UTILITY"


class MandateStatus(str, Enum):
    ACTIVE = "ACTIVE"
    INVALID = "INVALID"
    REVOKED = "REVOKED"
    PENDING_AFA = "PENDING_AFA"


class PTPStatus(str, Enum):
    OPEN = "OPEN"
    KEPT = "KEPT"
    BROKEN = "BROKEN"
    PARTIAL = "PARTIAL"
    LAPSED = "LAPSED"
