"""Rails: the seam between the measured simulation and a live provider."""

from app.rails.base import GateViolation, RailAdapter, Settlement, require_gate

__all__ = ["GateViolation", "RailAdapter", "Settlement", "require_gate",
           "RazorpayTestAdapter", "RazorpayUnavailable"]


def __getattr__(name: str):
    # Imported lazily so the package costs nothing when the live rails are not in use,
    # which is the default and how every measured number here was produced.
    if name in ("RazorpayTestAdapter", "RazorpayUnavailable"):
        from app.rails import razorpay
        return getattr(razorpay, name)
    raise AttributeError(name)
