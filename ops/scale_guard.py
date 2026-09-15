def check_and_fix_workers(runpod_api, endpoint_ids: list[str], target_max_workers: dict[str, int], webhook) -> dict:
    results = {}
    corrected_any = False
    for endpoint_id in endpoint_ids:
        config = runpod_api.get_endpoint_config(endpoint_id)
        target = target_max_workers[endpoint_id]
        current = config["max_workers"]
        if current != target:
            runpod_api.set_max_workers(endpoint_id, target)
            results[endpoint_id] = {"corrected": True, "was": current, "now": target}
            corrected_any = True
        else:
            results[endpoint_id] = {"corrected": False, "was": current, "now": current}

    if corrected_any:
        drifted = {k: v for k, v in results.items() if v["corrected"]}
        webhook.send(f"RunPod max_workers drift corrected: {drifted}")

    return results
