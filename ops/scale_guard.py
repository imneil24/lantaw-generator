def check_and_fix_workers(runpod_api, endpoint_ids: list[str], target_max_workers: dict[str, int], webhook,
                           target_min_workers: dict[str, int] | None = None) -> dict:
    target_min_workers = target_min_workers or {}
    results = {}
    corrected_any = False
    for endpoint_id in endpoint_ids:
        config = runpod_api.get_endpoint_config(endpoint_id)
        target = target_max_workers[endpoint_id]
        current = config["max_workers"]
        corrected = False

        if current != target:
            runpod_api.set_max_workers(endpoint_id, target)
            corrected = True

        if endpoint_id in target_min_workers:
            min_target = target_min_workers[endpoint_id]
            if config["min_workers"] != min_target:
                runpod_api.set_min_workers(endpoint_id, min_target)
                corrected = True

        results[endpoint_id] = {"corrected": corrected, "was": current, "now": target if corrected else current}
        corrected_any = corrected_any or corrected

    if corrected_any:
        drifted = {k: v for k, v in results.items() if v["corrected"]}
        webhook.send(f"RunPod worker config drift corrected: {drifted}")

    return results


def check_queue_depth(runpod_api, endpoint_ids: list[str], threshold: int, webhook) -> dict:
    results = {}
    for endpoint_id in endpoint_ids:
        depth = runpod_api.get_queue_depth(endpoint_id)
        over_threshold = depth > threshold
        results[endpoint_id] = {"depth": depth, "over_threshold": over_threshold}
        if over_threshold:
            webhook.send(f"RunPod queue depth alert: {endpoint_id} has {depth} queued jobs (threshold {threshold})")

    return results
