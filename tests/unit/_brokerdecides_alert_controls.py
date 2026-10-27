"""Test-only observation of a newly scheduled phone task, never production polling."""


async def poll_and_settle_new_task(poll, service):
    before = getattr(service, "_brokerdecides1_alert_task", None)
    result = await poll(service)
    after = getattr(service, "_brokerdecides1_alert_task", None)
    if after is not None and after is not before:
        await after
    return result
