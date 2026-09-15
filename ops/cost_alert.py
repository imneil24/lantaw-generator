def check_spend(runpod_api, webhook, limit_per_hour: float, threshold_pct: float) -> bool:
    current_spend = runpod_api.get_current_hourly_spend()
    threshold = limit_per_hour * threshold_pct
    if current_spend >= threshold:
        webhook.send(
            f"RunPod hourly spend ${current_spend:.2f} has reached "
            f"{threshold_pct * 100:.0f}% of the ${limit_per_hour:.2f}/hr account limit."
        )
        return True
    return False
