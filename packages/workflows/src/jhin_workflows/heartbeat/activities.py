"""Activities for the heartbeat workflow."""

from temporalio import activity


@activity.defn
async def record_beat(note: str) -> str:
    activity.logger.info("heartbeat.recorded")
    return note
