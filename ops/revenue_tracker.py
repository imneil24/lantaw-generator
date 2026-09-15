def check_revenue(current_annual_revenue: float, threshold: float, webhook, warn_pct: float = 0.8) -> bool:
    if current_annual_revenue >= threshold * warn_pct:
        webhook.send(
            f"Annual revenue ${current_annual_revenue:,.2f} is approaching the "
            f"${threshold:,.2f} LTX-2.3 self-host-commercial license threshold. "
            f"Flag for legal review."
        )
        return True
    return False
